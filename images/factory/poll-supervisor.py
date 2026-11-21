#!/usr/bin/env python3
"""Serial Linux poll supervisor with a kernel child-reaping drain barrier."""

import ctypes
import os
import select
import signal
import sys
import time
from datetime import datetime, timezone

CLI = "/opt/afk/bin/agent-flow-kit"
GRACE = 3.0
REAP_GRACE = 2.0
STARTUP_GRACE = 2.0
PR_SET_CHILD_SUBREAPER = 36
STOP_SIGNALS = {signal.SIGTERM, signal.SIGINT}


def readable(fd, timeout):
    """Poll one pipe or pidfd without select's FD_SETSIZE limit."""
    watcher = select.poll()
    watcher.register(fd, select.POLLIN | select.POLLHUP | select.POLLERR | select.POLLNVAL)
    return bool(watcher.poll(max(0, int(timeout * 1000))))


def log(message):
    print("poll-supervisor: " + message, file=sys.stderr, flush=True)
    if os.environ.get("FACTORY_FIXTURE_DIAGNOSTICS") == "1":
        fixed = ("helper_started", "unsupported_linux_child_primitives",
                 "subreaper_unavailable", "pass_interrupted",
                 "cancellation_children_unreaped", "stop_outcome", "stopped")
        code = message.split("=", 1)[0].split(" ", 1)[0]
        if message.startswith("cadence must be "):
            code = "invalid_configuration"
        if code == "launch_failed" or code in fixed:
            try:
                with open("/scratch/supervisor-events", "a", encoding="ascii") as out:
                    out.write("poll-supervisor: " + code + "\n")
            except OSError:
                pass  # Diagnostics never change the supervision result.


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cadence():
    value = os.environ.get("AFK_POLL_CADENCE_SECONDS", "90")
    if not value.isascii() or not value.isdecimal() or value.startswith("0") or not 1 <= len(value) <= 5 or int(value) > 86400:
        raise ValueError("cadence must be a positive integer at most 86400")
    return int(value)


class Supervisor:
    def __init__(self):
        self.stopping = False
        self.forced = False
        self.failed = False
        self.main_pid = None
        self.main_reaped = False
        self.main_status = None
        self.main_signaled = False
        self.exec_observed = False
        self.startup = None
        self.kill_sent = {}

    def stop(self, _signum, _frame):
        self.stopping = True

    def launch(self):
        # The parent cannot handle a stop between its final grant and the
        # child's exec. Signal delivery in this interval stays pending until
        # CLOEXEC proves exec (or the pre-exec child is retired). The handler
        # itself never waits for a child-held lock.
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        reader = writer = startup_reader = startup_writer = None
        allocated = False
        primary_failure = None
        try:
            if self.stopping:
                return False
            reader, writer = os.pipe()
            startup_reader, startup_writer = os.pipe()  # CLOEXEC proves exec.
            pid = os.fork()
            if pid == 0:
                try:
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    os.close(writer)
                    os.close(startup_reader)
                    os.setsid()
                    allowed = os.read(reader, 1) == b"G"
                    os.close(reader)
                    if not allowed:
                        os.write(startup_writer, b"D")
                        os._exit(125)
                    with open(os.devnull, "wb", buffering=0) as null:
                        os.dup2(null.fileno(), 1)
                        os.dup2(null.fileno(), 2)
                    os.execv(CLI, [CLI, "paperclip-poll"])
                except BaseException:
                    os.write(startup_writer, b"F")
                    os._exit(126)
            allocated = True
            self.main_pid = pid
            self.main_reaped = False
            self.main_status = None
            self.main_signaled = False
            self.exec_observed = False
            closing, reader = reader, None
            os.close(closing)
            closing, startup_writer = startup_writer, None
            os.close(closing)
            try:
                if not signal.sigpending().intersection(STOP_SIGNALS):
                    os.write(writer, b"G")
            except BrokenPipeError:
                pass
            closing, writer = writer, None
            os.close(closing)
            if not readable(startup_reader, STARTUP_GRACE):
                raise OSError("startup deadline exceeded")
            self.startup = os.read(startup_reader, 1)
            self.observe_exec()
            closing, startup_reader = startup_reader, None
            os.close(closing)
            return True
        except BaseException as error:
            primary_failure = error
            if allocated:
                # The grant may already have committed. Keep the mask held
                # while retiring every local child, including adoptees.
                self.stopping = True
                try:
                    self.drain_pass(report=False)
                except BaseException:
                    self.failed = True
            raise
        finally:
            for fd in (reader, writer, startup_reader, startup_writer):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        self.failed = True
            try:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            except BaseException:
                if allocated and primary_failure is None:
                    self.stopping = True
                    try:
                        self.drain_pass(report=False)
                    except BaseException:
                        self.failed = True
                if primary_failure is None:
                    raise
                self.failed = True

    def observe_exec(self):
        """A live child with a changed executable has crossed exec."""
        if self.main_pid is None or self.exec_observed:
            return
        try:
            self.exec_observed = os.readlink(f"/proc/{self.main_pid}/exe") != os.readlink("/proc/self/exe")
        except OSError:
            pass  # A vanished child supplies no exec proof.

    def reap(self):
        """Return true only after waitpid reports no owned children."""
        self.observe_exec()
        while True:
            try:
                # Keep the child waitable while checking retained KILL pidfds.
                child = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except ChildProcessError:
                return True
            except InterruptedError:
                continue
            if child is None:
                return False
            pid = child.si_pid
            records = self.kill_sent.get(pid, [])
            matched = None
            for held in records:
                try:
                    # A reaped old task yields ECHILD, even if this numeric
                    # PID now names a different waitable child.
                    same_child = self.waitable_pidfd(held, pid)
                except OSError:
                    same_child = False
                if same_child:
                    matched = held
                    break
                self.failed = True  # Retain an unproved KILL outcome.
            try:
                _, status = os.waitpid(pid, 0)
            except InterruptedError:
                continue
            if pid == self.main_pid and not self.main_reaped:
                # A later owned child may reuse the leader's numeric PID while
                # this pass waits for the whole descendant tree to drain.
                self.main_reaped = True
                self.main_status = os.waitstatus_to_exitcode(status)
                self.main_signaled = self.main_status < 0
                if self.main_status < 0:
                    self.main_status = 128 - self.main_status
            if matched is not None:
                records.remove(matched)
                os.close(matched)
                if not records:
                    del self.kill_sent[pid]
                if os.waitstatus_to_exitcode(status) == -signal.SIGKILL and not self.forced:
                    self.forced = True
                    log("cancellation_escalated=kill")

    @staticmethod
    def waitable_pidfd(fd, pid):
        """Confirm that a retained pidfd names this waitable child."""
        while True:
            try:
                child = os.waitid(os.P_PIDFD, fd,
                                  os.WEXITED | os.WNOHANG | os.WNOWAIT)
                return child is not None and child.si_pid == pid
            except InterruptedError:
                continue

    @staticmethod
    def children_of(pid):
        try:
            children = set()
            for task in os.scandir(f"/proc/{pid}/task"):
                try:
                    with open(f"{task.path}/children", encoding="ascii") as stream:
                        children.update(int(value) for value in stream.read().split())
                except FileNotFoundError:
                    continue  # A thread exited during target discovery.
            return list(children)
        except (OSError, ValueError):
            return []

    @staticmethod
    def parent_of(pid):
        try:
            with open(f"/proc/{pid}/stat", "rb") as stream:
                fields = stream.read().rsplit(b") ", 1)[1].split()
            return int(fields[1])
        except (OSError, IndexError, ValueError):
            return None

    def signal_owned(self, signum, term_sent=None):
        """Signal descendants child-first with live parent and child pidfds."""
        # Frames retain each selected parent's pidfd through its child scan.
        # If it exited, its numeric PID could have been reused: defer children
        # until adoption makes them direct descendants of a live frame.
        frames = [(os.getpid(), None, iter(self.children_of(os.getpid())))]
        attempted = successful = 0
        def live(fd):
            try:
                return not readable(fd, 0)
            except OSError:
                self.failed = True
                return False

        try:
            while frames:
                parent, parent_fd, children = frames[-1]
                if parent_fd is not None and not live(parent_fd):
                    pid = None
                else:
                    pid = next(children, None)
                if pid is not None:
                    try:
                        fd = os.pidfd_open(pid)
                    except ProcessLookupError:
                        continue
                    except OSError:
                        self.failed = True
                        continue
                    if ((parent_fd is not None and not live(parent_fd)) or
                            self.parent_of(pid) != parent or
                            (parent_fd is not None and not live(parent_fd))):
                        os.close(fd)
                        continue
                    frames.append((pid, fd, iter(self.children_of(pid))))
                    continue
                frames.pop()
                if parent_fd is None:
                    continue
                try:
                    previous = term_sent.get(parent) if term_sent is not None else None
                    if previous is not None:
                        if live(previous):
                            continue  # Same retained task already received TERM.
                        os.close(term_sent.pop(parent))
                    held = None
                    try:
                        if not live(parent_fd):
                            continue  # An exited task cannot establish a forced stop.
                        if term_sent is not None:
                            held = os.dup(parent_fd)
                        attempted += 1
                        signal.pidfd_send_signal(parent_fd, signum)
                        successful += 1
                        if signum == signal.SIGKILL:
                            records = self.kill_sent.setdefault(parent, [])
                            already_recorded = False
                            for fd in records:
                                try:
                                    # A live pidfd with this PID cannot name
                                    # an older, already recycled task.
                                    if (not readable(fd, 0) or
                                            self.waitable_pidfd(fd, parent)):
                                        already_recorded = True
                                        break
                                except ChildProcessError:
                                    pass  # The earlier task was reaped.
                                except OSError:
                                    self.failed = True
                            if not already_recorded:
                                records.append(os.dup(parent_fd))
                        if term_sent is not None:
                            term_sent[parent] = held
                            held = None
                    except ProcessLookupError:
                        pass
                    except OSError:
                        self.failed = True
                    finally:
                        if held is not None:
                            os.close(held)
                finally:
                    os.close(parent_fd)
        finally:
            for _, fd, _ in frames:
                if fd is not None:
                    os.close(fd)
        return attempted, successful

    def drain_pass(self, report=True):
        term_at = kill_at = None
        term_sent = {}
        try:
            while not self.reap():
                now = time.monotonic()
                if self.stopping:
                    if term_at is None:
                        term_at = now
                    if now - term_at < GRACE:
                        identities_uncertain = False
                        for pid, fd in list(term_sent.items()):
                            try:
                                exited = readable(fd, 0)
                            except OSError:
                                self.failed = identities_uncertain = True
                                continue
                            if exited:
                                os.close(term_sent.pop(pid))
                        if not identities_uncertain:
                            self.signal_owned(signal.SIGTERM, term_sent)
                    else:
                        self.signal_owned(signal.SIGKILL)
                        if kill_at is None:
                            kill_at = now
                        if now - kill_at >= REAP_GRACE:
                            log("cancellation_children_unreaped")
                            self.failed = True
                            return
                time.sleep(0.02)
        finally:
            for fd in term_sent.values():
                os.close(fd)
            # A parent may reap a descendant after our KILL delivery. Its
            # status is then unavailable here even after the child table
            # drains, so a remaining retained identity cannot prove a
            # nonforced stop.
            if self.kill_sent:
                self.failed = True
            for records in self.kill_sent.values():
                for fd in records:
                    os.close(fd)
            self.kill_sent.clear()
        if self.stopping:
            log("cancellation_drained=true")
        # EOF alone is ambiguous: SIGKILL before exec closes the pipe too.
        # Normal exits originate only from the exec path or the explicit D/F
        # paths above; a signal exit needs independent live-exec observation.
        if report:
            interrupted = (self.startup or self.main_status is None or
                           (self.main_signaled and not self.exec_observed))
            if interrupted:
                log("pass_interrupted at=" + stamp())
                if self.startup != b"D" or not self.stopping:
                    self.failed = True
            else:
                log("pass_exit=%d at=%s" % (self.main_status, stamp()))
        self.main_pid = None
        self.startup = None

    def run(self):
        interval = cadence()
        while not self.stopping and not self.failed:
            started = time.monotonic()
            try:
                launched = self.launch()
            except Exception as error:
                log("launch_failed=" + error.__class__.__name__)
                self.failed = True
                break
            if not launched:
                break
            self.drain_pass()
            if self.stopping or self.failed:
                break
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                deadline = time.monotonic() + remaining
                while not self.stopping and time.monotonic() < deadline:
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        log("stopped")
        if self.forced or self.failed:
            log("stop_outcome=forced_or_unreaped")
            return 1
        return 0


def main():
    log("helper_started")
    if sys.platform != "linux" or not os.path.isdir("/proc/self/task") or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        log("unsupported_linux_child_primitives")
        return 1
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        log("subreaper_unavailable errno=%d" % ctypes.get_errno())
        return 1
    try:
        supervisor = Supervisor()
        signal.signal(signal.SIGTERM, supervisor.stop)
        signal.signal(signal.SIGINT, supervisor.stop)
        return supervisor.run()
    except ValueError as error:
        log(str(error))
        return 2
    except OSError as error:
        log("supervision_failed=" + error.__class__.__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
