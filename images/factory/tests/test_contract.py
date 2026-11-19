#!/usr/bin/env python3
"""Offline packaging and serial supervisor checks."""
import importlib.util
import io
import json
import os
import ctypes
import base64
from contextlib import contextmanager
from pathlib import Path
import re
import select
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_LAUNCH = [str(ROOT / "test-local.sh")]

# Only the dedicated fake-owner process becomes a subreaper. The runner can
# create unrelated siblings at any point without making them fake descendants.


def ownership_operation(method):
    """Keep close() behind every operation that signals captured pidfds."""
    def guarded(self, *args, **kwargs):
        with self.operation_lock:
            return method(self, *args, **kwargs)
    return guarded


class FakeOwnership:
    """Hold pidfds acquired while each fake descendant's parent is verified."""

    def __init__(self, process, watch=True, exclusive=False):
        self.process = process
        self.root_fd = os.pidfd_open(process.pid)
        try:
            self.exclusive = exclusive
            self.drained = False
            self.completion_failed = False
            direct = self.children(os.getpid()) if exclusive else set()
            if exclusive and direct != {process.pid}:
                raise AssertionError(f"fake owner has other direct children: {direct}, expected {process.pid}")
            self.fds = {}
            self.seen_pids = {process.pid}
            # A signaler may have copied one of these descriptors. Keep every
            # captured pidfd open until the watcher and cleanup have finished.
            self.retired_fds = []
            self.lock = threading.Lock()
            self.operation_lock = threading.RLock()
            self.done = threading.Event()
            self.thread = None
            if watch:
                self.thread = threading.Thread(target=self._watch, daemon=True)
                self.thread.start()
        except BaseException:
            os.close(self.root_fd)
            raise

    @staticmethod
    def children(parent):
        result = set()
        try:
            for task in Path(f"/proc/{parent}/task").iterdir():
                try:
                    result.update(int(value) for value in (task / "children").read_text().split())
                except (FileNotFoundError, ProcessLookupError):
                    continue
        except (FileNotFoundError, ProcessLookupError):
            pass
        return result

    @staticmethod
    def parent_of(pid):
        try:
            fields = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b") ", 1)[1].split()
            return int(fields[1])
        except (FileNotFoundError, ProcessLookupError, IndexError, ValueError):
            return None

    def capture(self):
        pending = [self.process.pid]
        seen = set()
        while pending:
            parent = pending.pop()
            with self.lock:
                parent_fd = self.root_fd if parent == self.process.pid else self.fds.get(parent)
            if parent_fd is None or select.select([parent_fd], [], [], 0)[0]:
                continue
            for pid in self.children(parent):
                if pid in seen:
                    continue
                seen.add(pid)
                with self.lock:
                    held = self.fds.get(pid)
                if held:
                    if not select.select([held], [], [], 0)[0]:
                        pending.append(pid)
                        continue
                    with self.lock:
                        if self.fds.get(pid) == held:
                            self.retired_fds.append(self.fds.pop(pid))
                try:
                    fd = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    # Keep both pidfds open while checking ancestry. A numeric
                    # parent PID can be reused after the selected parent exits;
                    # the child's stat alone would then admit an outside task.
                    if (self.parent_of(pid) != parent or
                            select.select([parent_fd], [], [], 0)[0]):
                        continue
                    with self.lock:
                        if pid not in self.fds:
                            self.fds[pid] = fd
                            self.seen_pids.add(pid)
                            fd = None
                finally:
                    if fd is not None:
                        os.close(fd)
                pending.append(pid)

    def _watch(self):
        while not self.done.is_set():
            self.capture()
            self.done.wait(0.01)

    @ownership_operation
    def close_watch(self):
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                raise AssertionError("fake ownership watcher did not stop")
        self.capture()

    @ownership_operation
    def confirm_completion(self):
        """Reap exited adopted children; only ECHILD proves the fake tree ended."""
        if self.completion_failed:
            return False
        if self.drained:
            return True
        if not self.exclusive or self.process.returncode is None:
            raise AssertionError("fake root must exit before completion proof")
        self.capture()
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                self.drained = True
                return True
            if pid == 0:
                self.completion_failed = True
                return False
            self.seen_pids.add(pid)

    @staticmethod
    def send(fd, signum):
        try:
            signal.pidfd_send_signal(fd, signum)
        except ProcessLookupError:
            pass

    @ownership_operation
    def terminate(self):
        # These descriptors remain bound to the captured tasks even if the
        # supervisor was reaped and their numeric PIDs have been reused.
        if self.exclusive:
            self.terminate_exclusive()
            return
        with self.lock:
            fds = list(self.fds.values())
        for fd in fds:
            self.send(fd, signal.SIGKILL)
        self.send(self.root_fd, signal.SIGTERM)
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if select.select([self.root_fd], [], [], 0)[0]:
                break
            time.sleep(0.01)
        if not select.select([self.root_fd], [], [], 0)[0]:
            self.send(self.root_fd, signal.SIGKILL)
        for fd in fds:
            self.send(fd, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if all(select.select([fd], [], [], 0)[0] for fd in fds):
                return
            time.sleep(0.01)
        raise AssertionError("captured fake descendant did not exit")

    @ownership_operation
    def terminate_exclusive(self):
        """Keep discovery live through TERM; waitpid proves the tree drained."""
        self.send(self.root_fd, signal.SIGTERM)
        started = time.monotonic()
        deadline = started + 3
        root_reaped = False
        while time.monotonic() < deadline:
            self.capture()
            # After an ancestor exits, its children become our direct children
            # because this dedicated owner is a subreaper. The exclusive-child
            # invariant makes newly adopted children attributable to this fake.
            for pid in self.children(os.getpid()) - {self.process.pid}:
                with self.lock:
                    held = self.fds.get(pid)
                if held is not None:
                    if not select.select([held], [], [], 0)[0]:
                        continue
                    with self.lock:
                        if self.fds.get(pid) == held:
                            self.retired_fds.append(self.fds.pop(pid))
                try:
                    fd = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                if self.parent_of(pid) != os.getpid():
                    os.close(fd)
                    continue
                with self.lock:
                    if pid in self.fds:
                        os.close(fd)
                    else:
                        self.fds[pid] = fd
                        self.seen_pids.add(pid)
            with self.lock:
                fds = list(self.fds.values())
            for fd in fds:
                self.send(fd, signal.SIGKILL)
            if not select.select([self.root_fd], [], [], 0)[0] and time.monotonic() - started >= 1:
                self.send(self.root_fd, signal.SIGKILL)
            if not root_reaped and select.select([self.root_fd], [], [], 0)[0]:
                self.process.wait(timeout=0.5)
                root_reaped = True
            if root_reaped:
                while True:
                    try:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        self.drained = True
                        return
                    if pid == 0:
                        break
            time.sleep(0.01)
        self.send(self.root_fd, signal.SIGKILL)
        raise AssertionError("fake child table did not drain")

    @ownership_operation
    def force_reap(self):
        """Best effort after a cleanup error; a failed barrier remains failure."""
        self.send(self.root_fd, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if select.select([self.root_fd], [], [], 0)[0] and self.process.returncode is None:
                self.process.wait(timeout=0.5)
            for pid in self.children(os.getpid()) - {self.process.pid}:
                try:
                    fd = os.pidfd_open(pid)
                except ProcessLookupError:
                    continue
                try:
                    if self.parent_of(pid) == os.getpid():
                        self.seen_pids.add(pid)
                        self.send(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            if self.process.returncode is not None:
                while True:
                    try:
                        pid, _ = os.waitpid(-1, os.WNOHANG)
                    except ChildProcessError:
                        self.drained = True
                        return
                    if pid == 0:
                        break
            time.sleep(0.01)
        raise AssertionError("fake child table did not drain after cleanup error")

    @ownership_operation
    def close(self):
        if self.exclusive and not self.drained:
            raise AssertionError("fake ownership cannot close before kernel child drain")
        self.done.set()
        if self.thread is not None:
            self.thread.join(timeout=1)
            if self.thread.is_alive():
                raise AssertionError("fake ownership watcher did not stop")
        with self.lock:
            fds = list(self.fds.values()) + self.retired_fds
            self.fds.clear()
            self.retired_fds = []
        for fd in fds:
            os.close(fd)
        os.close(self.root_fd)


def cleanup_fake(process):
    owner = process.fake_owner
    try:
        try:
            owner.terminate()
            try:
                if isinstance(process, RemoteFakeProcess) and process.completion_failed:
                    # A forced tree must never become caller-facing success.
                    owner.request("communicate", timeout=4, timeout_arg=3)
                else:
                    process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                owner.send(owner.root_fd, signal.SIGKILL)
                if isinstance(process, RemoteFakeProcess) and process.completion_failed:
                    owner.request("communicate", timeout=4, timeout_arg=3)
                else:
                    process.communicate(timeout=3)
        except BaseException:
            if owner.exclusive:
                owner.force_reap()
            raise
    finally:
        try:
            owner.close_watch()
        finally:
            try:
                owner.close()
            finally:
                if isinstance(owner, RemoteFakeOwner):
                    owner.finish()


def _launch_in_owner(argv, kwargs):
    """Keep the new child before exec until this process owns its pidfd."""
    inject_owner_failure = kwargs.pop("_owner_fail", False)
    reader, writer = os.pipe()
    code = ('import os,sys\n'
            'fd=int(sys.argv[1])\n'
            'grant=os.read(fd,1)\n'
            'os.close(fd)\n'
            'if grant != b"G": sys.exit(125)\n'
            'os.execv(sys.argv[2],sys.argv[2:])\n')
    process = None
    owner = None
    try:
        process = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-c", code, str(reader), *argv],
            pass_fds=(reader,), **kwargs)
        closing, reader = reader, None
        os.close(closing)
        if inject_owner_failure:
            with patch.object(FakeOwnership, "__init__", side_effect=OSError("injected pidfd failure")):
                owner = FakeOwnership(process, exclusive=True)
        else:
            owner = FakeOwnership(process, exclusive=True)
        process.fake_owner = owner
        os.write(writer, b"G")
        closing, writer = writer, None
        os.close(closing)
        return process
    except BaseException:
        if owner is None and process is not None:
            # Without a grant the interpreter cannot create fake descendants.
            if writer is not None:
                closing, writer = writer, None
                try:
                    os.close(closing)
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()  # Still our unreaped, ungranted direct child.
                process.wait(timeout=3)
        elif owner is not None:
            cleanup_fake(process)
        raise
    finally:
        for fd in (reader, writer):
            if fd is not None:
                os.close(fd)


def _wire_value(value):
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, tuple):
        return [_wire_value(item) for item in value]
    if isinstance(value, list):
        return [_wire_value(item) for item in value]
    if isinstance(value, dict):
        if any(isinstance(key, bytes) for key in value) or set(value) in ({"bytes"}, {"__afk_pairs__"}):
            return {"__afk_pairs__": [[_wire_value(key), _wire_value(item)]
                                      for key, item in value.items()]}
        return {key: _wire_value(item) for key, item in value.items()}
    if isinstance(value, os.PathLike):
        return _wire_value(os.fspath(value))
    return value


def _unwire_value(value):
    if isinstance(value, dict):
        if set(value) == {"bytes"}:
            return base64.b64decode(value["bytes"], validate=True)
        if set(value) == {"__afk_pairs__"}:
            return {_unwire_value(key): _unwire_value(item)
                    for key, item in value["__afk_pairs__"]}
        return {key: _unwire_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unwire_value(item) for item in value]
    return value


def _wire_message(channel, payload):
    body = json.dumps(_wire_value(payload), ensure_ascii=True).encode("ascii")
    if len(body) > 16 * 1024 * 1024:
        raise AssertionError("fake owner control message too large")
    packet = struct.pack("!I", len(body)) + body
    deadline = time.monotonic() + 5
    while packet:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("fake owner control write timed out")
        poller = select.poll()
        poller.register(channel.write_fd, select.POLLOUT | select.POLLHUP | select.POLLERR)
        if not poller.poll(max(1, int(remaining * 1000))):
            raise TimeoutError("fake owner control write timed out")
        written = os.write(channel.write_fd, packet[:4096])
        packet = packet[written:]


def _read_message(channel, deadline):
    def exact(length):
        chunks = []
        while length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("fake owner control timed out")
            poller = select.poll()
            poller.register(channel.read_fd, select.POLLIN | select.POLLHUP | select.POLLERR)
            if not poller.poll(max(1, int(remaining * 1000))):
                raise TimeoutError("fake owner control timed out")
            chunk = os.read(channel.read_fd, length)
            if not chunk:
                raise EOFError("fake owner control EOF")
            chunks.append(chunk)
            length -= len(chunk)
        return b"".join(chunks)
    size = struct.unpack("!I", exact(4))[0]
    if size > 16 * 1024 * 1024:
        raise ValueError("malformed fake owner response length")
    return _unwire_value(json.loads(exact(size).decode("ascii")))


class OwnerChannel:
    def __init__(self, read_fd, write_fd):
        self.read_fd = read_fd
        self.write_fd = write_fd

    def close(self):
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class RemoteFakeOwner:
    """Bounded channel to the process whose only child tree is this fake."""

    exclusive = True

    def __init__(self, channel, process):
        self.channel = channel
        self.process = process
        self.drained = False
        self.closed = False
        self.root_fd = None  # The real pidfd stays in the owner process.
        self.operation_lock = threading.RLock()
        self.fake_process = None
        self.seen_pids = set()
        self.last_child_table = None
        self.abandon_error = None

    @ownership_operation
    def request(self, op, timeout=8, **fields):
        if self.closed:
            raise AssertionError("fake owner channel already closed")
        if timeout < 0 or timeout > 15:
            raise ValueError("fake owner request bound exceeded")
        try:
            _wire_message(self.channel, dict(op=op, **fields))
            response = _read_message(self.channel, time.monotonic() + timeout + 1)
        except BaseException as error:
            self.abandon(interrupt=isinstance(error, (TimeoutError, KeyboardInterrupt, InterruptedError)))
            raise
        if not isinstance(response, dict) or response.get("op") != op or type(response.get("ok")) is not bool:
            self.abandon()
            raise ValueError("malformed fake owner response")
        if not response["ok"]:
            kind = response.get("type")
            message = response.get("error", "fake owner error")
            if kind == "TimeoutExpired":
                raise subprocess.TimeoutExpired(self.process.pid, fields.get("timeout"))
            if kind == "OSError":
                raise OSError(message)
            raise AssertionError(message)
        return response.get("result")

    def abandon(self, interrupt=False):
        if self.closed:
            return
        self.closed = True
        # Interrupt an active wait/capture in the dedicated owner before
        # closing control. Its signal handler enters the bounded drain path.
        if interrupt:
            try:
                fd = os.pidfd_open(self.process.pid)
                try:
                    signal.pidfd_send_signal(fd, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                finally:
                    os.close(fd)
            except OSError as error:
                self.abandon_error = f"owner interruption unverified: {error}"
        self.channel.close()
        try:
            self.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.abandon_error = "fake owner did not retire after control failure"
        else:
            if self.process.returncode != 0:
                self.abandon_error = f"fake owner drain unverified: exit {self.process.returncode}"

    def terminate(self):
        result = self.request("terminate", timeout=5)
        self.fake_process.returncode = result["returncode"]
        self.seen_pids.update(result["seen_pids"])
        self.last_child_table = result["child_table"]
        self.drained = True

    def force_reap(self):
        result = self.request("force_reap", timeout=4)
        self.fake_process.returncode = result["returncode"]
        self.seen_pids.update(result["seen_pids"])
        self.last_child_table = result["child_table"]
        self.drained = True

    def close_watch(self):
        self.request("close_watch", timeout=3)

    def close(self):
        self.request("close", timeout=3)

    def finish(self):
        if self.closed:
            return
        self.closed = True
        self.channel.close()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired as error:
            raise AssertionError("fake owner process did not exit") from error
        if self.process.returncode != 0:
            raise AssertionError(f"fake owner exited {self.process.returncode}")

    def send_root(self, signum):
        self.request("send_root", signum=signum)

    def capture(self):
        return self.request("capture")

    def child_table(self):
        return self.request("child_table")

    def signal_descendants(self, signum):
        return self.request("signal_descendants", signum=signum)


class RemoteFakeProcess:
    def __init__(self, owner, pid, argv, text):
        self.fake_owner = owner
        self.pid = pid
        self.args = argv
        self.text_mode = text
        self.returncode = None
        self.output = None
        self.error = None
        self.completion_failed = False
        self.completion_proved = False
        self.output_collected = False

    def _status(self, result):
        self.returncode = result["returncode"]
        return self.returncode

    def poll(self):
        if self.fake_owner.closed:
            return self.returncode
        return self._status(self.fake_owner.request("poll"))

    def wait(self, timeout=None):
        if self.completion_failed:
            raise AssertionError("fake tree did not complete")
        if self.fake_owner.closed:
            if not self.completion_proved:
                raise AssertionError("fake tree completion unavailable after owner failure")
            return self.returncode
        bound = 8 if timeout is None else timeout
        result = self.fake_owner.request("wait", timeout=bound + 1, timeout_arg=bound)
        if not isinstance(result, dict) or type(result.get("completed")) is not bool or type(result.get("returncode")) is not int:
            self.fake_owner.abandon()
            raise ValueError("malformed fake completion response")
        self._status(result)
        if not result["completed"]:
            self.completion_failed = True
            raise AssertionError("fake tree did not complete")
        self.completion_proved = True
        return self.returncode

    def send_signal(self, signum):
        if self.fake_owner.closed and self.returncode is not None:
            return
        self.fake_owner.send_root(signum)

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.send_signal(signal.SIGKILL)

    def communicate(self, timeout=None):
        if self.completion_failed:
            raise AssertionError("fake tree did not complete")
        if self.fake_owner.closed:
            if not self.completion_proved or not self.output_collected:
                raise AssertionError("fake output unavailable after owner failure")
            return self.output, self.error
        bound = 8 if timeout is None else timeout
        result = self.fake_owner.request("communicate", timeout=bound + 1, timeout_arg=bound)
        if not isinstance(result, dict) or type(result.get("completed")) is not bool or type(result.get("returncode")) is not int:
            self.fake_owner.abandon()
            raise ValueError("malformed fake completion response")
        self._status(result)
        if not result["completed"]:
            self.completion_failed = True
            raise AssertionError("fake tree did not complete")
        if "output" not in result or "error" not in result:
            self.fake_owner.abandon()
            raise ValueError("malformed fake output response")
        self.completion_proved = True
        self.output, self.error = result["output"], result["error"]
        self.output_collected = True
        return self.output, self.error


def _fake_owner_main(read_fd, write_fd):
    channel = OwnerChannel(read_fd, write_fd)
    fake = None
    owner = None
    clean = False
    draining = False
    try:
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise OSError("test pidfd ownership unavailable")
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "test subreaper unavailable")
        def interrupted(_signum, _frame):
            if draining:
                return
            raise InterruptedError("fake owner interrupted")
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        launch = _read_message(channel, time.monotonic() + 5)
        if launch.get("op") != "launch":
            raise ValueError("malformed fake owner launch")
        fake = _launch_in_owner(launch["argv"], launch["kwargs"])
        owner = fake.fake_owner
        _wire_message(channel, dict(op="launch", ok=True, result={"pid": fake.pid}))
        while True:
            request = _read_message(channel, time.monotonic() + 120)
            op = request.get("op")
            try:
                if op == "fault_eof":
                    return
                if op == "fault_malformed":
                    os.write(channel.write_fd, struct.pack("!I", 1) + b"{")
                    return
                if op == "poll":
                    result = {"returncode": fake.poll()}
                elif op == "wait":
                    root_status = fake.wait(timeout=request["timeout_arg"])
                    completed = owner.confirm_completion()
                    if not completed:
                        owner.force_reap()
                    result = {"returncode": root_status, "completed": completed}
                elif op == "communicate":
                    output, error = fake.communicate(timeout=request["timeout_arg"])
                    completed = owner.confirm_completion()
                    if not completed:
                        owner.force_reap()
                    result = {"returncode": fake.returncode, "completed": completed}
                    if completed:
                        result.update(output=output, error=error)
                elif op == "send_root":
                    owner.send(owner.root_fd, request["signum"])
                    result = None
                elif op == "capture":
                    owner.capture()
                    with owner.lock:
                        result = {str(pid): [os.fstat(held).st_dev, os.fstat(held).st_ino]
                                  for pid, held in owner.fds.items()}
                elif op == "child_table":
                    result = sorted(FakeOwnership.children(os.getpid()))
                elif op == "signal_descendants":
                    owner.capture()
                    with owner.lock:
                        pairs = list(owner.fds.items())
                    for _, held in pairs:
                        owner.send(held, request["signum"])
                    result = [pid for pid, _ in pairs]
                elif op == "terminate":
                    owner.terminate()
                    result = {"returncode": fake.returncode, "seen_pids": sorted(owner.seen_pids),
                              "child_table": sorted(FakeOwnership.children(os.getpid()))}
                elif op == "force_reap":
                    owner.force_reap()
                    result = {"returncode": fake.returncode, "seen_pids": sorted(owner.seen_pids),
                              "child_table": sorted(FakeOwnership.children(os.getpid()))}
                elif op == "close_watch":
                    owner.close_watch()
                    result = None
                elif op == "close":
                    owner.close()
                    clean = True
                    result = None
                else:
                    raise ValueError("unknown fake owner operation")
                _wire_message(channel, dict(op=op, ok=True, result=result))
                if op == "close":
                    return
            except BaseException as error:
                if isinstance(error, (InterruptedError, KeyboardInterrupt)):
                    raise
                _wire_message(channel, dict(op=op, ok=False, type=type(error).__name__, error=str(error)))
    except BaseException as error:
        try:
            _wire_message(channel, dict(op="launch", ok=False, type=type(error).__name__, error=str(error)))
        except (OSError, EOFError):
            pass
        if not isinstance(error, (InterruptedError, KeyboardInterrupt)):
            raise
    finally:
        draining = True
        if owner is not None and not clean:
            if not owner.drained:
                owner.force_reap()
            owner.close_watch()
            owner.close()
        channel.close()


def launch_owned_fake(argv, **kwargs):
    """Start a childless dedicated owner, then launch the fake inside it."""
    request_read, request_write = os.pipe()
    reply_read, reply_write = os.pipe()
    parent = OwnerChannel(reply_read, request_write)
    controller = None
    try:
        controller = subprocess.Popen(
            [sys.executable, "-I", __file__, "--fake-owner", str(request_read), str(reply_write)],
            pass_fds=(request_read, reply_write), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(request_read)
        request_read = None
        os.close(reply_write)
        reply_write = None
        _wire_message(parent, dict(op="launch", argv=argv, kwargs=kwargs))
        response = _read_message(parent, time.monotonic() + 5)
        if not isinstance(response, dict) or response.get("op") != "launch" or not response.get("ok"):
            message = response.get("error", "malformed fake owner launch response") if isinstance(response, dict) else "malformed fake owner launch response"
            raise OSError(message)
        owner = RemoteFakeOwner(parent, controller)
        fake = RemoteFakeProcess(owner, response["result"]["pid"], argv, kwargs.get("text", False))
        owner.fake_process = fake
        return fake
    except BaseException:
        parent.close()
        if controller is not None:
            controller.wait(timeout=5)
        raise
    finally:
        for fd in (request_read, reply_write):
            if fd is not None:
                os.close(fd)


@contextmanager
def managed_fake(process):
    try:
        yield process
    finally:
        cleanup_fake(process)


class ContractTests(unittest.TestCase):
    def test_pins_and_isolation(self):
        lock = (ROOT / "versions.lock").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        fixture = (ROOT / "test-local.sh").read_text()
        self.assertIn("f62d4efa4768a9ca985248f5ad931cc2e5c53f41", lock)
        self.assertIn("sha256:c67e92c8099ba22853ab4cfda7cfb494d253d6012ca82dee98ba8976772af018", dockerfile)
        self.assertIn("fdb65eb961d3d4c1959601a51bc9bc5663b0403369ad50a422af16947ac3f3ba", (ROOT / "checksums.sha256").read_text())
        for expected in ("--network none", "--read-only", "--cap-drop ALL", "--log-driver none",
                         "DOCKER_HOST=unix:///run/docker.sock", "source_git -c tar.umask=0022 archive --format=tar",
                         "GIT_NO_REPLACE_OBJECTS=1", "GIT_NO_LAZY_FETCH=1"):
            self.assertIn(expected, fixture)
        self.assertNotIn("AFK_FACTORY_OFFLINE_SYNTH", dockerfile)
        self.assertIn("--refuse-public", dockerfile)
        self.assertIn("rm -f /etc/machine-id", dockerfile)
        self.assertIn("ORCA_INSTANCE_ID=$container_name", fixture)
        self.assertNotIn("COPY . ", dockerfile)

    def test_docker_owned_cleanup_paths(self):
        fixture = (ROOT / "test-local.sh").read_text()
        cleanup = fixture.split("cleanup() {\n", 1)[1].split("\ndock() {", 1)[0]
        cleanup = "cleanup() {\n" + cleanup
        detached = fixture.split("container_id=$(dock run -d ", 1)[1].split("\nready=0", 1)[0]
        detached = "container_id=$(dock run -d " + detached
        stop = fixture.split("dock stop --time 15", 1)[1].split("\n", 1)[0]
        stop = "dock stop --time 15" + stop
        for scenario, expected in (("success", ["stop", "rm-owned", "image-rm"]),
                                   ("remove-fails", ["stop", "rm-owned", "image-rm"]),
                                   ("partial", ["rm-owned", "image-rm"]),
                                   ("foreign", ["image-rm"])):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                script = f'''set -euo pipefail
container_name=fixture-test; container_id=''; image_tag=fixture:image; image_built=1
scratch={directory}/scratch; client_cfg={directory}/config; stage={directory}/stage
mkdir -p "$scratch" "$client_cfg" "$stage"
run_flags=()
scenario={scenario}
dock() {{
  printf '%s\\n' "$*" >> {directory}/calls
  case "$1 $2" in
    "run -d")
      if [[ "$scenario" == partial || "$scenario" == foreign ]]; then return 17; fi
      printf 'owned-id\\n';;
    "container inspect")
      if [[ "$scenario" == foreign ]]; then printf 'someone-else\\n';
      else printf '%s\\n' "$container_name"; fi;;
    "image inspect") printf '%s\\n' "$container_name";;
    "rm -f") printf 'rm-owned\\n' >> {directory}/events
      if [[ "$scenario" == remove-fails ]]; then return 9; fi;;
    "image rm") printf 'image-rm\\n' >> {directory}/events;;
    "stop --time") printf 'stop\\n' >> {directory}/events;;
  esac
}}
{cleanup}
trap 'cleanup "$?"' EXIT
{detached}
{stop if scenario in ("success", "remove-fails") else ""}
'''
                result = subprocess.run(["bash", "-c", script], text=True,
                                        capture_output=True)
                self.assertEqual(result.returncode,
                                 17 if scenario in ("partial", "foreign") else
                                 1 if scenario == "remove-fails" else 0,
                                 result.stderr)
                events = (Path(directory) / "events").read_text().splitlines()
                self.assertEqual(events, expected)
                calls = (Path(directory) / "calls").read_text()
                self.assertNotIn("run --rm", calls)
                self.assertNotIn("rm -f owned-id", calls)

    def test_git_source_pin_ignores_replacement_and_caller_settings(self):
        fixture = (ROOT / "test-local.sh").read_text()
        source_git = "source_git() {\n" + fixture.split("source_git() {\n", 1)[1].split("\n[[", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            def git(*args):
                return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
            git("init", "-q")
            (repo / "payload").write_text("pinned\n")
            git("add", "payload")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned")
            pinned = git("rev-parse", "HEAD")
            tree = git("rev-parse", "HEAD^{tree}")
            (repo / "payload").write_text("replacement\n")
            git("add", "payload")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "replacement")
            git("replace", pinned, git("rev-parse", "HEAD"))
            trace = Path(directory) / "trace"
            env = dict(os.environ, GIT_TRACE=str(trace), GIT_CONFIG_COUNT="1",
                       GIT_CONFIG_KEY_0="core.logAllRefUpdates", GIT_CONFIG_VALUE_0="true")
            result = subprocess.run(["bash", "-c", source_git + '\nsource_git rev-parse --verify "$1^{tree}"',
                                     "bash", pinned], env=dict(env, source_repo=str(repo)),
                                    text=True, capture_output=True, check=True)
            self.assertEqual(result.stdout.strip(), tree)
            self.assertFalse(trace.exists())

    def test_private_stage_ignores_existing_symlink_hardlink_and_shared_parent(self):
        fixture = (ROOT / "test-local.sh").read_text()
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("chmod 644 /opt/factory/poll-supervisor.py", dockerfile)
        prepare = 'stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)' + fixture.split(
            'stage=$(mktemp -d /tmp/factory-fixture.XXXXXXXX)', 1)[1].split('scratch=$(mktemp -d)', 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory) / "fixture"
            here.mkdir()
            for name in ("Dockerfile", "safe-extract.py", "poll-supervisor.sh", "poll-supervisor.py"):
                (here / name).write_bytes((ROOT / name).read_bytes())
            archive = Path(directory) / "archive"
            archive.write_bytes(b"synthetic archive")
            (here / "checksums.sha256").write_text(
                "74a06ea6113b7f98d70ba2286ddaa21a9a17e25ed2bb8736a823dbc46177eb44  inputs/orca-server-linux-x64.tar.gz\n")
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for name, data, mode in (("bin/agent-flow-kit", "#!/bin/sh\nexit 0\n", 0o755),
                                     ("lib/module", "payload\n", 0o644),
                                     ("adapters/runtime-scope.sh", "#!/bin/sh\n", 0o755)):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(data)
                path.chmod(mode)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned"], check=True)
            script = ('set -euo pipefail\numask 077\n'
                      'here="$1"; source_repo="$2"; archive="$3"; commit=HEAD; '
                      'asset=orca-server-linux-x64.tar.gz; url=unused; tree=synthetic\n'
                      'source_git() { git -C "$source_repo" "$@"; }\n' + prepare +
                      'test "$(stat -c %a "$stage")" = 700\n'
                      # This is the actual producer with umask 077. A 0600
                      # helper would be unreadable to image UID 10001 after
                      # root-owned Docker COPY, even if the wrapper is 0755.
                      'test "$(stat -c %a "$stage/poll-supervisor.py")" = 644\n'
                      # Model Docker COPY's mode preservation into a public
                      # /opt/factory tree and check the other-UID access bits.
                      'install -d -m 0755 "$stage/image/opt/factory"\n'
                      'cp -p -- "$stage/poll-supervisor.py" "$stage/image/opt/factory/poll-supervisor.py"\n'
                      'test "$(stat -c %a "$stage/image/opt/factory")" = 755\n'
                      'test "$(stat -c %a "$stage/image/opt/factory/poll-supervisor.py")" = 644\n'
                      'test -x "$stage/inputs/afk/bin/agent-flow-kit"\n')
            external = Path(directory) / "external"
            (external / "afk").mkdir(parents=True)
            sentinel = external / "afk/sentinel"
            sentinel.write_text("keep me")
            for scenario in ("symlink", "hardlink", "shared"):
                with self.subTest(scenario=scenario):
                    inputs = here / "inputs"
                    if scenario == "symlink":
                        inputs.symlink_to(external, target_is_directory=True)
                    else:
                        inputs.mkdir(mode=0o777)
                        inputs.chmod(0o777)
                        (inputs / "afk").mkdir()
                        (inputs / "afk/sentinel").write_text("keep me")
                        if scenario == "hardlink":
                            (inputs / "orca-server-linux-x64.tar.gz").hardlink_to(archive)
                    result = subprocess.run(["bash", "-c", script, "bash", str(here), str(repo), str(archive)],
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(sentinel.read_text(), "keep me")
                    self.assertEqual(archive.read_bytes(), b"synthetic archive")
                    if scenario != "symlink":
                        self.assertEqual((inputs / "afk/sentinel").read_text(), "keep me")
                        self.assertEqual(inputs.stat().st_mode & 0o777, 0o777)
                        if scenario == "hardlink":
                            self.assertEqual((inputs / "orca-server-linux-x64.tar.gz").read_bytes(), b"synthetic archive")
                        for child in inputs.iterdir():
                            if child.is_dir():
                                for nested in child.iterdir():
                                    nested.unlink()
                                child.rmdir()
                            else:
                                child.unlink()
                        inputs.rmdir()
                    else:
                        inputs.unlink()

    def test_safe_extraction_rejects_escape(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for name, link in (("../escape", None), ("root/link", "../../escape")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "input.tgz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    if link:
                        member.type = tarfile.SYMTYPE
                        member.linkname = link
                    else:
                        member.size = 1
                    tar.addfile(member, None if link else io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    module.extract(archive, Path(directory) / "out")

    def test_safe_extraction_preserves_server_layout(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "input.tgz"
            with tarfile.open(archive, "w:gz") as tar:
                root = tarfile.TarInfo("./orca-server")
                root.type = tarfile.DIRTYPE
                tar.addfile(root)
                member = tarfile.TarInfo("./orca-server/bin/orca-ide")
                member.size = 1
                member.mode = 0o755
                tar.addfile(member, io.BytesIO(b"x"))
            destination = Path(directory) / "out"
            module.extract(archive, destination)
            self.assertEqual((destination / "bin/orca-ide").read_bytes(), b"x")
            self.assertTrue((destination / "bin/orca-ide").stat().st_mode & 0o111)

    def supervisor(self, directory, body, cadence="1"):
        fake = Path(directory) / "fake-afk"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
        copy = Path(directory) / "poll-supervisor.py"
        copy.write_text((ROOT / "poll-supervisor.py").read_text().replace(
            "/opt/afk/bin/agent-flow-kit", str(fake)))
        env = dict(os.environ, AFK_POLL_CADENCE_SECONDS=cadence,
                   TEST_LOG=str(Path(directory) / "events"))
        return launch_owned_fake(["/usr/bin/python3", "-I", str(copy)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 text=True)

    def test_cadence_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            for invalid in ("0", "-2", "abc", "1.5", "86401", "99999999999999999999999999"):
                process = self.supervisor(directory, "exit 0\n", invalid)
                with managed_fake(process):
                    _, error = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 2)
                    self.assertIn("positive integer", error)

    def test_long_pass_serial_and_failure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.supervisor(directory,
                'printf "start %s\\n" "$(date +%s%N)" >> "$TEST_LOG"\n'
                'sleep 2\nprintf "end %s\\n" "$(date +%s%N)" >> "$TEST_LOG"\nexit 7\n')
            with managed_fake(process):
                # The leader must drain the first pass before the next starts;
                # process startup and group scanning have no fixed wall time.
                deadline = time.monotonic() + 8
                events = []
                while len(events) < 3 and time.monotonic() < deadline:
                    log = Path(directory) / "events"
                    if log.exists():
                        events = log.read_text().splitlines()
                    if len(events) < 3:
                        time.sleep(0.02)
                self.assertEqual([line.split()[0] for line in events[:3]], ["start", "end", "start"])
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertIn("pass_exit=7", error)
                self.assertIn("stopped", error)

    def test_poll_output_is_suppressed_but_status_is_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "fake-afk"
            fake.write_text('#!/usr/bin/env bash\n'
                            'printf "SYNTHETIC_STDOUT_MARKER\\n"\n'
                            'printf "SYNTHETIC_STDERR_MARKER\\n" >&2\n'
                            'printf "done\\n" > "$TEST_LOG"\n'
                            'exit 7\n')
            fake.chmod(0o755)
            wrapper = Path(directory) / "poll-supervisor.py"
            wrapper.write_text((ROOT / "poll-supervisor.py").read_text().replace(
                "/opt/afk/bin/agent-flow-kit", str(fake)))
            env = dict(os.environ, AFK_POLL_CADENCE_SECONDS="30",
                       TEST_LOG=str(Path(directory) / "events"))
            process = launch_owned_fake(["/usr/bin/python3", "-I", str(wrapper)], env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True)
            with managed_fake(process):
                deadline = time.monotonic() + 3
                while not (Path(directory) / "events").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((Path(directory) / "events").exists())
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(output, "")
                self.assertNotIn("SYNTHETIC_STDOUT_MARKER", error)
                self.assertNotIn("SYNTHETIC_STDERR_MARKER", error)
                self.assertRegex(error, r"poll-supervisor: pass_exit=7 at=\d{4}-\d{2}-\d{2}T")
                self.assertIn("poll-supervisor: stopped", error)

    def test_term_kills_owned_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.supervisor(directory,
                'sleep 30 &\nprintf "%s\\n" "$!" > "$TEST_LOG"\nwait\n')
            pid = None
            with managed_fake(process):
                deadline = time.monotonic() + 3
                while not (Path(directory) / "events").exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                pid = int((Path(directory) / "events").read_text())
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=5)
                self.assertIsNotNone(process.returncode)
                # A reaped process may briefly remain a zombie in a container.
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)


class PostPrFixTests(unittest.TestCase):
    def fixture(self):
        return (ROOT / "test-local.sh").read_text()

    def test_high_descriptor_launch_and_owned_drain(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_modified_supervisor(
                directory,
                'trap "exit 0" TERM\nprintf "ready\\n" > "$TEST_LOG"\n'
                'sleep 30 & wait\n',
                'log("helper_started")',
                'high_fds = [os.open(os.devnull, os.O_RDONLY) for _ in range(1030)]\n'
                '    log("helper_started")')
            with managed_fake(process):
                self.wait_for(Path(directory) / "events")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 0, error)
                self.assertNotIn("launch_failed", error)
                self.assertNotIn("cancellation_children_unreaped", error)
                self.assertEqual(FakeOwnership.children(os.getpid()),
                                 {process.fake_owner.process.pid})

    def test_nonascii_proc_name_is_selected_as_owned_child(self):
        spec = importlib.util.spec_from_file_location("poll_supervisor", ROOT / "poll-supervisor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        code = ('import ctypes,signal,time\n'
                'ctypes.CDLL(None).prctl(15, b"\\xff", 0, 0, 0)\n'
                'signal.signal(signal.SIGTERM, lambda *_: exit(0))\n'
                'print("ready", flush=True)\n'
                'while True: time.sleep(1)\n')
        child = subprocess.Popen(["/usr/bin/python3", "-I", "-c", code],
                                 stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(module.Supervisor.parent_of(child.pid), os.getpid())
            self.assertEqual(FakeOwnership.children(os.getpid()), {child.pid})
            attempted, successful = module.Supervisor().signal_owned(signal.SIGTERM)
            self.assertEqual((attempted, successful), (1, 1))
            self.assertEqual(child.wait(timeout=2), 0)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()

    def test_non_utf8_fake_descendant_drains_before_output_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            child_code = ('import ctypes,sys,time\n'
                          'ctypes.CDLL(None).prctl(15, b"\\xff", 0, 0, 0)\n'
                          'open(sys.argv[1], "w").write("ready")\n'
                          'while True: time.sleep(1)\n')
            root_code = ('import os,signal,subprocess,sys,time\n'
                         'signal.signal(signal.SIGTERM, lambda *_: os._exit(0))\n'
                         'child = subprocess.Popen(["/usr/bin/python3", "-I", "-c", '
                         'sys.argv[2], sys.argv[3]])\n'
                         'deadline = time.monotonic() + 2\n'
                         'while not os.path.exists(sys.argv[3]) and time.monotonic() < deadline: '
                         'time.sleep(0.01)\n'
                         'open(sys.argv[1], "w").write(str(child.pid))\n'
                         'while True: time.sleep(1)\n')
            root = launch_owned_fake(
                ["/usr/bin/python3", "-I", "-c", root_code,
                 str(temp / "child_pid"), child_code, str(temp / "child_ready")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owner = root.fake_owner
            root_probe = os.pidfd_open(root.pid)
            child_probe = None
            try:
                self.wait_for(temp / "child_pid")
                child_pid = int((temp / "child_pid").read_text())
                child_probe = os.pidfd_open(child_pid)
                self.wait_for(temp / "child_ready")
                self.assertIn(b"\xff", Path(f"/proc/{child_pid}/stat").read_bytes())
                self.assertEqual(FakeOwnership.parent_of(child_pid), root.pid)
                captured = owner.capture()
                self.assertIn(str(child_pid), captured)
                self.assertEqual(captured[str(child_pid)],
                                 [os.fstat(child_probe).st_dev, os.fstat(child_probe).st_ino])
                original_communicate = root.communicate

                def collect_after_drain(*args, **kwargs):
                    self.assertTrue(owner.drained, "output collected before kernel child drain")
                    self.assertEqual(owner.child_table(), [])
                    self.assertTrue(select.select([child_probe], [], [], 0)[0])
                    return original_communicate(*args, **kwargs)

                with patch.object(root, "communicate", side_effect=collect_after_drain):
                    cleanup_fake(root)
                self.assertEqual(root.returncode, 0)
                self.assertEqual(root.poll(), 0)
                self.assertTrue(owner.closed)
                self.assertEqual(owner.process.returncode, 0)
                self.assertTrue(select.select([root_probe], [], [], 0)[0])
            finally:
                if not owner.closed:
                    if child_probe is not None:
                        FakeOwnership.send(child_probe, signal.SIGKILL)
                    FakeOwnership.send(root_probe, signal.SIGKILL)
                    cleanup_fake(root)
                if child_probe is not None:
                    os.close(child_probe)
                os.close(root_probe)

    def test_term_once_for_retained_identity_and_new_child(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            script = temp / "timer.py"
            script.write_text(
                'import os,signal,subprocess,time\n'
                'deadline = None\n'
                'def term(*_):\n'
                '    global deadline\n'
                '    with open(os.environ["TEST_TERMS"], "a") as out: out.write("T")\n'
                '    subprocess.Popen(["/usr/bin/sleep", "30"])\n'
                '    deadline = time.monotonic() + 0.35\n'
                'signal.signal(signal.SIGTERM, term)\n'
                'open(os.environ["TEST_LOG"], "w").write("ready")\n'
                'while deadline is None or time.monotonic() < deadline: time.sleep(0.02)\n')
            process = self.launch_supervisor(directory,
                'exec /usr/bin/python3 -I "$TEST_SCRIPT"\n', cadence="86400",
                extras=dict(TEST_SCRIPT=str(script), TEST_TERMS=str(temp / "terms")))
            with managed_fake(process):
                self.wait_for(temp / "events")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual((temp / "terms").read_text(), "T")
                self.assertNotIn("cancellation_escalated", error)
                self.assertEqual(FakeOwnership.children(os.getpid()),
                                 {process.fake_owner.process.pid})

    def test_inherited_pipe_is_drained_before_output_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            script = ('import os,subprocess,time\n'
                      'subprocess.Popen(["/usr/bin/sleep", "30"])\n'
                      'open(os.environ["TEST_LOG"], "w").write("ready")\n'
                      'while not os.path.exists(os.environ["TEST_RELEASE"]): time.sleep(0.01)\n')
            process = launch_owned_fake(["/usr/bin/python3", "-I", "-c", script],
                                        env=dict(os.environ, TEST_LOG=str(Path(directory) / "ready"),
                                                 TEST_RELEASE=str(Path(directory) / "release")),
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owner = process.fake_owner
            with managed_fake(process):
                root_fd = os.pidfd_open(process.pid)
                try:
                    self.wait_for(Path(directory) / "ready")
                    self.assertIsNone(process.poll())  # The producer still awaits release.
                    (Path(directory) / "release").write_text("go")
                    self.assertTrue(select.select([root_fd], [], [], 3)[0],
                                    "fake root did not exit after release")
                    self.assertEqual(process.poll(), 0)  # Root exit is intermediate.
                    with self.assertRaisesRegex(AssertionError, "fake tree did not complete"):
                        process.wait(timeout=2)
                    self.assertEqual(process.returncode, 0)
                    self.assertTrue(process.completion_failed)
                finally:
                    os.close(root_fd)
            self.assertTrue(owner.drained)
            self.assertEqual(FakeOwnership.children(os.getpid()), set())
            self.assertTrue(owner.closed)
            self.assertEqual(owner.process.returncode, 0)

    def test_missing_readiness_retires_fake_from_first_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            script = 'import signal,time\nsignal.signal(signal.SIGTERM, lambda *_: exit(0))\nwhile True: time.sleep(1)\n'
            process = launch_owned_fake(["/usr/bin/python3", "-I", "-c", script],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owner = process.fake_owner
            with self.assertRaises(AssertionError):
                with managed_fake(process):
                    self.wait_for(Path(directory) / "missing", timeout=0.2)
            self.assertTrue(owner.drained)
            self.assertIsNotNone(process.returncode)
            self.assertEqual(FakeOwnership.children(os.getpid()), set())
            self.assertTrue(owner.closed)
            self.assertEqual(owner.process.returncode, 0)

    def test_production_signal_scan_does_not_follow_recycled_descendant_pair(self):
        spec = importlib.util.spec_from_file_location("poll_supervisor", ROOT / "poll-supervisor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        supervisor = module.Supervisor()
        parent = os.getpid()
        phase = ["original"]
        opened = {}
        delivered = []
        entered = threading.Event()
        release = threading.Event()
        result = []

        def children(pid):
            if pid == parent:
                return [2000] if phase[0] != "adopted" else [3000]
            if pid == 2000:
                entered.set()
                if not release.wait(2):
                    raise AssertionError("descendant discovery stalled")
                return [3000]  # The old queue would reopen this numeric PID.
            return []

        def parent_of(pid):
            if pid == 2000:
                return parent
            if pid == 3000:
                return parent if phase[0] == "adopted" else 2000
            return None

        def open_pidfd(pid):
            fd = os.open(os.devnull, os.O_RDONLY)
            opened[fd] = pid
            return fd

        def send(fd, signum):
            os.fstat(fd)  # The selected descriptor must still be live.
            delivered.append((opened[fd], signum))
            if opened[fd] == 2000 and phase[0] == "foreign":
                raise ProcessLookupError  # Original parent has exited.

        def pidfd_ready(fd, _timeout):
            os.fstat(fd)
            return opened[fd] == 2000 and phase[0] == "foreign"

        def first_scan():
            try:
                result.append(supervisor.signal_owned(signal.SIGTERM))
            except BaseException as error:
                result.append(error)

        with patch.object(module.Supervisor, "children_of", side_effect=children), \
             patch.object(module.Supervisor, "parent_of", side_effect=parent_of), \
             patch.object(module.os, "pidfd_open", side_effect=open_pidfd), \
             patch.object(module, "readable", side_effect=pidfd_ready), \
             patch.object(module.signal, "pidfd_send_signal", side_effect=send):
            worker = threading.Thread(target=first_scan)
            worker.start()
            try:
                self.assertTrue(entered.wait(2), "descendant scan did not start")
                supervisor.stop(signal.SIGTERM, None)
                phase[0] = "foreign"  # Reused numeric pair has foreign ancestry.
            finally:
                release.set()
                worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [(0, 0)])
            self.assertTrue(supervisor.stopping)
            self.assertEqual(delivered, [])
            self.assertNotIn(3000, opened.values())

            # Once the owned descendant is adopted, a fresh direct-child
            # scan may safely select its current pidfd and signal it.
            phase[0] = "adopted"
            self.assertEqual(supervisor.signal_owned(signal.SIGKILL), (1, 1))
            self.assertEqual(delivered[-1], (3000, signal.SIGKILL))

    def test_production_signal_scan_rechecks_parent_after_child_validation(self):
        spec = importlib.util.spec_from_file_location("poll_supervisor", ROOT / "poll-supervisor.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        supervisor = module.Supervisor()
        parent = os.getpid()
        exited = [False]
        opened = {}
        delivered = []

        def children(pid):
            return {parent: [2000], 2000: [3000]}.get(pid, [])

        def parent_of(pid):
            if pid == 3000:
                exited[0] = True  # Original parent exits during stat read.
                return 2000  # A reused numeric pair would appear to match.
            return parent

        def open_pidfd(pid):
            fd = os.open(os.devnull, os.O_RDONLY)
            opened[fd] = pid
            return fd

        def pidfd_ready(fd, _timeout):
            os.fstat(fd)
            return opened[fd] == 2000 and exited[0]

        def send(fd, _signum):
            os.fstat(fd)
            delivered.append(opened[fd])
            if opened[fd] == 2000 and exited[0]:
                raise ProcessLookupError

        with patch.object(module.Supervisor, "children_of", side_effect=children), \
             patch.object(module.Supervisor, "parent_of", side_effect=parent_of), \
             patch.object(module.os, "pidfd_open", side_effect=open_pidfd), \
             patch.object(module, "readable", side_effect=pidfd_ready), \
             patch.object(module.signal, "pidfd_send_signal", side_effect=send):
            self.assertEqual(supervisor.signal_owned(signal.SIGTERM), (0, 0))
        self.assertTrue(exited[0])
        self.assertEqual(delivered, [])

    def test_planned_symlink_graph(self):
        spec = importlib.util.spec_from_file_location("safe_extract", ROOT / "safe-extract.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cases = (("escape", [("a", "."), ("link", "a/../outside")], False),
                 ("cycle", [("a", "b"), ("b", "a")], False),
                 ("valid", [("a", "dir"), ("link", "a/file")], True))
        for label, links, valid in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "in.tgz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo("root/dir/file")
                    member.size = 1
                    tar.addfile(member, io.BytesIO(b"x"))
                    for name, target in links:
                        member = tarfile.TarInfo("root/" + name)
                        member.type = tarfile.SYMTYPE
                        member.linkname = target
                        tar.addfile(member)
                out = Path(directory) / "out"
                if valid:
                    module.extract(archive, out)
                    self.assertEqual((out / "link").read_bytes(), b"x")
                else:
                    with self.assertRaises(ValueError):
                        module.extract(archive, out)
                    self.assertFalse(out.exists())

    def test_curl_disables_synthetic_config_first(self):
        fixture = self.fixture()
        command = "/usr/bin/env -i" + fixture.split("if ! /usr/bin/env -i", 1)[1].split("; then", 1)[0]
        self.assertIn("/usr/bin/curl --disable", command)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            source = temp / "source"
            source.write_bytes(b"synthetic archive")
            marker = temp / "config-read"
            (temp / ".curlrc").write_text(f'trace = "{marker}"\n')
            stage = temp / "stage"
            (stage / "inputs").mkdir(parents=True)
            script = 'stage="$1"; asset=archive; url="$2"; ' + command
            result = subprocess.run(["/bin/bash", "-c", script, "bash", str(stage), source.as_uri()],
                                    env=dict(os.environ, HOME=str(temp), CURL_HOME=str(temp),
                                             XDG_CONFIG_HOME=str(temp)), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((stage / "inputs/archive.part").read_bytes(), b"synthetic archive")
            self.assertFalse(marker.exists())

    def test_stop_requires_running_then_clean_exit(self):
        stop = "stop_state() {\n" + self.fixture().split("stop_state() {\n", 1)[1].split(
            "# EXIT trap removes", 1)[0]
        running = dict(Running=True, Status="running")
        clean = dict(Running=False, Status="exited", ExitCode=0,
                     OOMKilled=False, Dead=False, Error="")
        cases = (("clean", running, clean, True),
                 ("sigterm", running, dict(clean, ExitCode=143), True),
                 ("already-stopped", clean, clean, False),
                 ("sigkill", running, dict(clean, ExitCode=137), False),
                 ("unexpected", running, dict(clean, ExitCode=1), False),
                 ("oom", running, dict(clean, OOMKilled=True), False),
                 ("error", running, dict(clean, Error="synthetic"), False))
        for label, before, after, accepted in cases:
            with self.subTest(label=label):
                script = ("set -euo pipefail\ncontainer_id=synthetic; stopped=0\n"
                          f"before='{json.dumps(before)}'; after='{json.dumps(after)}'\n"
                          'dock() { if [[ "$1" == stop ]]; then stopped=1; '
                          'elif (( stopped )); then printf "%s\\n" "$after"; '
                          'else printf "%s\\n" "$before"; fi; }\n' + stop)
                result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                self.assertEqual("orca_observed_nonforced_stop=true" in result.stdout, accepted)

    def test_fixture_failure_report_is_exclusive_and_only_fixed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            repo = temp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            for label, source, expected_phase, expected_reason, expected_status in (
                ("missing", temp / "missing", "source_checkout", "invalid_source_checkout", 2),
                ("unpinned", repo, "source_pin", "source_pin_failed", 1),
            ):
                with self.subTest(label=label):
                    report = temp / f"{label}.json"
                    result = subprocess.run(
                        FIXTURE_LAUNCH + ["--report", str(report),
                         "--afk-repo", str(source)], text=True, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, expected_status)
                    self.assertEqual((result.stdout, result.stderr), ("", ""))
                    self.assertEqual(json.loads(report.read_text()), {
                        "schema": "factory-fixture-report/1", "status": "FAIL",
                        "phase": expected_phase, "reason": expected_reason,
                        "observed_exit_status": expected_status})
                    self.assertEqual(report.stat().st_mode & 0o777, 0o600)
                    original = report.read_bytes()
                    repeated = subprocess.run(
                        FIXTURE_LAUNCH + ["--report", str(report),
                         "--afk-repo", str(source)], capture_output=True, timeout=5)
                    self.assertEqual(repeated.returncode, 2)
                    self.assertEqual(report.read_bytes(), original)
            link = temp / "linked.json"
            link.symlink_to(report)
            refused = subprocess.run(
                FIXTURE_LAUNCH + ["--report", str(link),
                 "--afk-repo", str(repo)], capture_output=True, timeout=5)
            self.assertEqual(refused.returncode, 2)
            self.assertTrue(link.is_symlink())

    def test_fixture_clean_entry_and_real_export_reject_hostile_environment(self):
        fixture = self.fixture()
        self.assertTrue(os.access(FIXTURE_LAUNCH[0], os.X_OK))
        self.assertTrue(fixture.startswith("#!/usr/bin/env -S -i PATH=/usr/bin:/bin "))
        source_git = "source_git() {\n" + fixture.split("source_git() {\n", 1)[1].split("\n[[", 1)[0]
        export = "source_git -c tar.umask=0022 archive --format=tar " + fixture.split(
            "source_git -c tar.umask=0022 archive --format=tar ", 1)[1].split("\n# Docker COPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            hook = temp / "hook"
            bash_marker, tar_marker, path_marker = (temp / name for name in
                                                     ("bash-ran", "tar-ran", "path-ran"))
            hook.write_text(f'printf ran > "{bash_marker}"\n')
            tar_hook = temp / "tar-hook"
            tar_hook.write_text(f'#!/bin/sh\nprintf ran > "{tar_marker}"\n')
            tar_hook.chmod(0o755)
            fakebin = temp / "fakebin"
            fakebin.mkdir()
            for name in ("git", "tar", "curl", "docker", "env"):
                fake = fakebin / name
                fake.write_text(f'#!/bin/sh\nprintf ran > "{path_marker}"\nexit 89\n')
                fake.chmod(0o755)
            hostile = dict(os.environ, BASH_ENV=str(hook),
                           TAR_OPTIONS=f"--checkpoint=1 --checkpoint-action=exec={tar_hook}",
                           PATH=str(fakebin), GIT_TRACE=str(temp / "git-trace"),
                           GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.hooksPath",
                           GIT_CONFIG_VALUE_0=str(fakebin),
                           **{"BASH_FUNC_tar%%": f'() {{ printf ran > "{path_marker}"; }}'})
            repo = temp / "repo"
            repo.mkdir()
            subprocess.run(["/usr/bin/git", "init", "-q", str(repo)], check=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
            for name, data, mode in (("bin/agent-flow-kit", b"#!/bin/sh\nexit 0\n", 0o755),
                                     ("lib/module", b"payload\n", 0o644),
                                     ("adapters/runtime-scope.sh", b"#!/bin/sh\n", 0o755)):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                path.chmod(mode)
            git_env = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}
            subprocess.run(["/usr/bin/git", "-C", str(repo), "add", "."], check=True, env=git_env)
            subprocess.run(["/usr/bin/git", "-C", str(repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned"],
                           check=True, env=git_env)
            report = temp / "report.json"
            result = subprocess.run(FIXTURE_LAUNCH + ["--afk-repo", str(repo), "--report", str(report)],
                                    env=hostile, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(report.read_text())["status"], "FAIL")
            self.assertFalse(any(p.exists() for p in (bash_marker, tar_marker, path_marker)))
            stage = temp / "stage"
            (stage / "inputs/afk").mkdir(parents=True)
            script = ('set -euo pipefail\nsource_repo="$1"; stage="$2"; commit=HEAD\n'
                      + source_git + '\n' + export)
            # Run the actual production export/tar statement under TAR_OPTIONS.
            # It clears both subprocess environments even when the parent is dirty.
            result = subprocess.run(["/bin/bash", "-c", script, "bash", str(repo), str(stage)],
                                    env=dict(hostile, BASH_ENV=""), capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((stage / "inputs/afk/bin/agent-flow-kit").read_bytes(), b"#!/bin/sh\nexit 0\n")
            self.assertEqual((stage / "inputs/afk/lib/module").read_bytes(), b"payload\n")
            self.assertTrue(os.access(stage / "inputs/afk/bin/agent-flow-kit", os.X_OK))
            self.assertFalse(any(p.exists() for p in (bash_marker, tar_marker, path_marker)))
            failed = subprocess.run(["/bin/bash", "-c", script.replace("commit=HEAD", "commit=missing"),
                                     "bash", str(repo), str(stage)], env=dict(hostile, BASH_ENV=""),
                                    capture_output=True, timeout=5)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(any(p.exists() for p in (bash_marker, tar_marker, path_marker)))

    def test_fixture_report_records_cleanup_failure_after_successful_checks(self):
        fixture = self.fixture()
        finish = "finish() {\n" + fixture.split("finish() {\n", 1)[1].split("\ntrap finish EXIT", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "cleanup.json"
            script = ("set -euo pipefail\n"
                      'report_path="$1"; fixture_cleanup_active=1; cleanup_failed=0\n'
                      "readiness_diagnostic=''; readiness_exit_status=''\n"
                      'phase=supervisor_stop; reason=supervisor_stop_failed_or_unverified\n'
                      'cleanup() { cleanup_failed=1; printf "opaque secret\\n" >&2; return 1; }\n'
                      "exec >/dev/null 2>&1\n" + finish + "\ntrap finish EXIT\ntrue\n")
            result = subprocess.run(["bash", "-c", script, "bash", str(report)],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 1)
            self.assertEqual((result.stdout, result.stderr), ("", ""))
            self.assertEqual(json.loads(report.read_text()), {
                "schema": "factory-fixture-report/1", "status": "FAIL",
                "phase": "cleanup", "reason": "owned_cleanup_failed", "observed_exit_status": 1})
            first = Path(directory) / "first-failure.json"
            script = ("set -euo pipefail\n"
                      'report_path="$1"; fixture_cleanup_active=1; cleanup_failed=0\n'
                      "readiness_diagnostic='container_exited_before_marker'; readiness_exit_status='1'\n"
                      'phase=supervisor_readiness; reason=supervisor_readiness_failed\n'
                      'cleanup() { cleanup_failed=1; return 1; }\n'
                      "exec >/dev/null 2>&1\n" + finish + "\ntrap finish EXIT\nexit 2\n")
            result = subprocess.run(["bash", "-c", script, "bash", str(first)],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(first.read_text()), {
                "schema": "factory-fixture-report/1", "status": "FAIL",
                "phase": "supervisor_readiness", "reason": "supervisor_readiness_failed",
                "observed_exit_status": 2,
                "readiness_diagnostic": "container_exited_before_marker",
                "observed_container_exit_status": 1})

    def test_report_publication_requires_successful_close_and_exclusive_rename(self):
        writer = self.fixture().split('import ctypes, json, os, sys, tempfile\n', 1)[1].split('\nPY', 1)[0]
        writer = 'import ctypes, json, os, sys, tempfile\n' + writer
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            report = temp / "result.json"
            # A full JSON write followed by a failed close must never expose PASS.
            close_fault = ('import os, sys\nfrom unittest.mock import patch\n'
                           'real_fdopen = os.fdopen\n'
                           'class FailedClose:\n'
                           '    def __init__(self, file): self.file = file\n'
                           '    def __enter__(self): return self.file.__enter__()\n'
                           '    def __exit__(self, *args):\n'
                           '        self.file.__exit__(*args)\n'
                           '        raise OSError("synthetic close failure")\n'
                           'def fdopen(*args, **kwargs): return FailedClose(real_fdopen(*args, **kwargs))\n'
                           'with patch.object(os, "fdopen", side_effect=fdopen):\n'
                           '    exec(sys.stdin.read())\n')
            failed = subprocess.run(["/usr/bin/python3", "-I", "-c", close_fault,
                                     str(report), "complete", "ok", "0", "", ""],
                                    input=writer, text=True, capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(report.exists())
            self.assertEqual(list(temp.iterdir()), [])
            passed = subprocess.run(["/usr/bin/python3", "-I", "-c", writer,
                                     str(report), "complete", "ok", "0", "", ""],
                                    capture_output=True)
            self.assertEqual(passed.returncode, 0, passed.stderr)
            self.assertEqual(json.loads(report.read_text()), {
                "schema": "factory-fixture-report/1", "status": "PASS", "phase": "complete",
                "reason": "ok", "observed_exit_status": 0})
            self.assertEqual(report.stat().st_mode & 0o777, 0o600)
            original = report.read_bytes()
            refused = subprocess.run(["/usr/bin/python3", "-I", "-c", writer,
                                      str(report), "complete", "ok", "0", "", ""], capture_output=True)
            self.assertNotEqual(refused.returncode, 0)
            self.assertEqual(report.read_bytes(), original)
            self.assertEqual(list(temp.iterdir()), [report])
            diagnostic = temp / "diagnostic.json"
            recorded = subprocess.run(["/usr/bin/python3", "-I", "-c", writer,
                                       str(diagnostic), "supervisor_readiness",
                                       "supervisor_readiness_failed", "1",
                                       "container_exited_before_marker_event_launch_failed", "126"],
                                      capture_output=True)
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            self.assertEqual(json.loads(diagnostic.read_text()), {
                "schema": "factory-fixture-report/1", "status": "FAIL",
                "phase": "supervisor_readiness", "reason": "supervisor_readiness_failed",
                "observed_exit_status": 1,
                "readiness_diagnostic": "container_exited_before_marker_event_launch_failed",
                "observed_container_exit_status": 126})

    def test_local_git_umask_and_optimized_refusals(self):
        fixture = self.fixture()
        source_git = "source_git() {\n" + fixture.split("source_git() {\n", 1)[1].split("\n[[", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            executable = repo / "bin" / "agent-flow-kit"
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n")
            executable.chmod(0o755)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Fixture",
                            "-c", "user.email=fixture@example.invalid", "commit", "-qm", "pinned"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "tar.umask", "0077"], check=True)
            cmd = source_git + '\nsource_git -c tar.umask=0022 archive --format=tar HEAD bin'
            result = subprocess.run(["bash", "-c", cmd], env=dict(os.environ, source_repo=str(repo)),
                                    capture_output=True, check=True)
            with tarfile.open(fileobj=io.BytesIO(result.stdout)) as tar:
                self.assertEqual(tar.getmember("bin/agent-flow-kit").mode & 0o777, 0o755)
            optimized = dict(os.environ, PYTHONOPTIMIZE="1")
            inventory = fixture.split("/usr/bin/python3 -I -c 'import sys\n", 1)[1].split("' || {", 1)[0]
            result = subprocess.run(["/usr/bin/python3", "-I", "-O", "-c", "import sys\n" + inventory],
                                    input=b"", capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            readability = fixture.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
            payload = repo / "payload"
            payload.write_text("readable")
            payload.chmod(0o600)
            result = subprocess.run(["/usr/bin/python3", "-I", "-O", "-c", readability, str(repo)],
                                    capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            payload.chmod(0o644)
            result = subprocess.run(["/usr/bin/python3", "-I", "-O", "-c", readability, str(repo)],
                                    capture_output=True, env=optimized)
            self.assertEqual(result.returncode, 0, result.stderr)
            comparison = fixture.split("/usr/bin/python3 -I -c 'import hashlib", 1)[1].split("' \"$stage", 1)[0]
            result = subprocess.run(["/usr/bin/python3", "-I", "-O", "-c", "import hashlib" + comparison,
                                     str(repo), str(repo)], input=b"100644 blob abc\tbin/missing\0",
                                    capture_output=True, env=optimized)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotRegex(fixture, r"\bassert\s")

    def test_cleanup_on_later_mktemp_failure_and_unique_names(self):
        fixture = self.fixture()
        early = "release_temps() {\n" + fixture.split(
            "release_temps() {\n", 1)[1].split('\nfinish() {', 1)[0]
        names = "nonce=$(/usr/bin/python3 -I -c" + fixture.split("nonce=$(/usr/bin/python3 -I -c", 1)[1].split(
            "container_id=''", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            script = f'''set -euo pipefail
TEST_ROOT="{directory}"; TEST_COUNTER="{directory}/count"
mktemp() {{
  local n=0
  [[ ! -f "$TEST_COUNTER" ]] || n=$(cat "$TEST_COUNTER")
  n=$((n+1)); printf '%s' "$n" > "$TEST_COUNTER"
  if (( n == 3 )); then return 9; fi
  mkdir "$TEST_ROOT/alloc$n"; printf '%s\\n' "$TEST_ROOT/alloc$n"
}}
stage=''; scratch=''; client_cfg=''
{early}
trap release_temps EXIT
stage=$(mktemp -d)
scratch=$(mktemp -d)
client_cfg=$(mktemp -d)
'''
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((Path(directory) / "alloc1").exists())
            self.assertFalse((Path(directory) / "alloc2").exists())
            result = subprocess.run(["bash", "-c", names + '\nfirst="$container_name"; '
                                     + names + '\ntest "$first" != "$container_name"'],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def wait_for(self, path, timeout=5):
        deadline = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), str(path))

    def launch_supervisor(self, directory, body, cadence="30", extras=None):
        temp = Path(directory)
        fake = temp / "fake-afk"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
        copy = temp / "poll-supervisor.py"
        source = (ROOT / "poll-supervisor.py").read_text().replace(
            "/opt/afk/bin/agent-flow-kit", str(fake))
        copy.write_text(source)
        env = dict(os.environ, AFK_POLL_CADENCE_SECONDS=cadence,
                   TEST_LOG=str(temp / "events"))
        if extras:
            env.update(extras)
        return launch_owned_fake(["/usr/bin/python3", "-I", str(copy)], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def run_modified_supervisor(self, directory, body, old, new, grace=None, observe=False):
        temp = Path(directory)
        fake = temp / "fake-afk"
        fake.write_text("#!/usr/bin/env bash\n" + body)
        fake.chmod(0o755)
        helper = temp / "poll-supervisor.py"
        source = (ROOT / "poll-supervisor.py").read_text().replace(
            "/opt/afk/bin/agent-flow-kit", str(fake))
        self.assertIn(old, source)
        source = source.replace(old, new, 1)
        if observe:
            source = source.replace('self.stopping = True\n',
                'self.stopping = True\n'
                '        with open(os.environ["TEST_ORDER"], "a") as trace:\n'
                '            trace.write("handled_stop\\n")\n', 1)
            source = source.replace('self.startup = os.read(startup_reader, 1)\n',
                'self.startup = os.read(startup_reader, 1)\n'
                '            if not self.startup:\n'
                '                with open(os.environ["TEST_ORDER"], "a") as trace:\n'
                '                    trace.write("exec_proof\\n")\n', 1)
        if grace is not None:
            source = source.replace("GRACE = 3.0", f"GRACE = {grace}")
        helper.write_text(source)
        return launch_owned_fake(["/usr/bin/python3", "-I", str(helper)],
            env=dict(os.environ, AFK_POLL_CADENCE_SECONDS="1",
                     TEST_LOG=str(temp / "events"), TEST_SYNC=str(temp / "preexec"),
                     TEST_RELEASE=str(temp / "release"), TEST_ORDER=str(temp / "order")),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    managed_fake = staticmethod(managed_fake)

    def test_cancel_before_release_denies_cli_and_reaps(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_modified_supervisor(directory, 'touch "$TEST_LOG"\n',
                'os.close(closing)\n            try:\n                if not signal.sigpending()',
                'os.close(closing)\n            os.kill(os.getpid(), signal.SIGTERM)\n            try:\n                if not signal.sigpending()')
            with self.managed_fake(process):
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(output, "")
                self.assertFalse((Path(directory) / "events").exists())
                self.assertIn("pass_interrupted", error)

    def test_final_preexec_stop_is_handled_after_exec_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original = 'os.execv(CLI, [CLI, "paperclip-poll"])'
            paused = ('open(os.environ["TEST_SYNC"], "w").close()\n'
                      '                    while not os.path.exists(os.environ["TEST_RELEASE"]):\n'
                      '                        time.sleep(0.01)\n'
                      '                    os.execv(CLI, [CLI, "paperclip-poll"])')
            process = self.run_modified_supervisor(directory, 'touch "$TEST_LOG"\nexit 0\n', original, paused,
                                                   observe=True)
            with self.managed_fake(process):
                self.wait_for(temp / "preexec")
                process.send_signal(signal.SIGTERM)
                time.sleep(0.05)
                self.assertFalse((temp / "order").exists())
                (temp / "release").touch()
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual((temp / "order").read_text().splitlines(), ["exec_proof", "handled_stop"])
                self.assertRegex(error, r"pass_exit=(0|143)")

    def test_preexec_timeout_retires_child_before_handled_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            original = 'os.execv(CLI, [CLI, "paperclip-poll"])'
            paused = ('open(os.environ["TEST_SYNC"], "w").close()\n'
                      '                    time.sleep(30)\n'
                      '                    os.execv(CLI, [CLI, "paperclip-poll"])')
            process = self.run_modified_supervisor(directory, 'touch "$TEST_LOG"\n', original, paused)
            with self.managed_fake(process):
                self.wait_for(temp / "preexec")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 1, error)
                self.assertFalse((temp / "events").exists())
                self.assertIn("launch_failed=OSError", error)

    def test_killed_before_exec_never_reports_a_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_modified_supervisor(directory, 'touch "$TEST_LOG"\n',
                'os.execv(CLI, [CLI, "paperclip-poll"])',
                'os.kill(os.getpid(), signal.SIGKILL)\n'
                '                    os.execv(CLI, [CLI, "paperclip-poll"])')
            with self.managed_fake(process):
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 1, error)
                self.assertFalse((Path(directory) / "events").exists())
                self.assertIn("pass_interrupted", error)
                self.assertNotIn("pass_exit=", error)

    def test_fake_cleanup_holds_identity_across_discovery_and_reuse(self):
        root = subprocess.Popen(["/usr/bin/sleep", "30"])
        foreign = subprocess.Popen(["/usr/bin/sleep", "30"])
        owner = FakeOwnership(root, watch=False)
        foreign_fd = os.pidfd_open(foreign.pid)
        try:
            owner.children = lambda parent: {foreign.pid} if parent == root.pid else set()
            owner.parent_of = lambda _pid: -1  # A candidate failed ancestry verification.
            owner.capture()
            self.assertNotIn(foreign.pid, owner.fds)
            # Simulate a captured child PID recycled onto an unrelated task:
            # cleanup uses its original held descriptor, never reopens the PID.
            owner.fds[foreign.pid] = os.dup(owner.root_fd)
            with patch.object(os, "pidfd_open", side_effect=AssertionError("reopened stale PID")):
                owner.capture()
                owner.terminate()
            self.assertIsNone(foreign.poll())
        finally:
            owner.send(owner.root_fd, signal.SIGKILL)
            owner.send(foreign_fd, signal.SIGKILL)
            root.wait(timeout=3)
            foreign.wait(timeout=3)
            os.close(foreign_fd)
            owner.close()

    def test_fake_capture_rejects_recycled_parent_after_exit(self):
        for exit_at in ("enumeration", "parentage"):
            with self.subTest(exit_at=exit_at):
                root = subprocess.Popen(["/usr/bin/sleep", "30"])
                outside = subprocess.Popen(["/usr/bin/sleep", "30"])
                owner = FakeOwnership(root, watch=False)
                outside_fd = os.pidfd_open(outside.pid)
                real_send = owner.send
                outside_signal = []
                parentage_read = []

                def exit_root():
                    real_send(owner.root_fd, signal.SIGKILL)
                    root.wait(timeout=3)

                def children(parent):
                    self.assertEqual(parent, root.pid)
                    if exit_at == "enumeration":
                        exit_root()
                    return {outside.pid}

                def parent_of(pid):
                    self.assertEqual(pid, outside.pid)
                    parentage_read.append(pid)
                    if exit_at == "parentage":
                        exit_root()
                    # Model a recycled numeric parent PID. The retained
                    # parent pidfd still reports the original task's exit.
                    return root.pid

                def guarded_send(fd, signum):
                    if os.fstat(fd).st_ino == os.fstat(outside_fd).st_ino:
                        outside_signal.append(signum)
                        return
                    real_send(fd, signum)

                try:
                    with patch.object(owner, "children", side_effect=children), \
                         patch.object(owner, "parent_of", side_effect=parent_of), \
                         patch.object(owner, "send", side_effect=guarded_send):
                        owner.capture()
                        self.assertEqual(parentage_read, [outside.pid])
                        self.assertNotIn(outside.pid, owner.fds)
                        owner.terminate()
                    self.assertEqual(outside_signal, [])
                    self.assertIsNone(outside.poll())
                finally:
                    real_send(owner.root_fd, signal.SIGKILL)
                    signal.pidfd_send_signal(outside_fd, signal.SIGKILL)
                    root.wait(timeout=3)
                    outside.wait(timeout=3)
                    os.close(outside_fd)
                    owner.close()

    def test_fake_cleanup_retains_pidfd_during_concurrent_capture_and_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            children_file = Path(directory) / "children"
            code = ('import os, signal, subprocess, sys, time\n'
                    'children = [subprocess.Popen(["/usr/bin/sleep", "30"]) for _ in range(2)]\n'
                    'with open(sys.argv[1] + ".tmp", "w") as out:\n'
                    '    out.write(" ".join(str(p.pid) for p in children))\n'
                    'os.replace(sys.argv[1] + ".tmp", sys.argv[1])\n'
                    'signal.signal(signal.SIGTERM, lambda *_: None)\n'
                    'while True: time.sleep(1)\n')
            root = launch_owned_fake(["/usr/bin/python3", "-I", "-c", code,
                                      str(children_file)])
            owner = root.fake_owner
            sibling = subprocess.Popen(["/usr/bin/sleep", "30"])
            sibling_fd = os.pidfd_open(sibling.pid)
            probes = []
            try:
                self.wait_for(children_file)
                child_pids = [int(pid) for pid in children_file.read_text().split()]
                self.assertEqual(len(child_pids), 2)
                self.assertEqual(len(set(child_pids)), 2)
                probes = [os.pidfd_open(pid) for pid in child_pids]
                captured = owner.capture()
                self.assertTrue(all(str(pid) in captured for pid in child_pids))
                for pid, fd in zip(child_pids, probes):
                    self.assertEqual(captured[str(pid)],
                                     [os.fstat(fd).st_dev, os.fstat(fd).st_ino])
                errors = []
                def signal_children():
                    try:
                        owner.signal_descendants(signal.SIGKILL)
                    except BaseException as error:
                        errors.append(error)
                worker = threading.Thread(target=signal_children)
                worker.start()
                owner.capture()
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                cleanup_fake(root)
                self.assertTrue(owner.drained)
                self.assertEqual(owner.last_child_table, [])
                self.assertTrue(all(select.select([fd], [], [], 0)[0] for fd in probes))
                self.assertFalse(select.select([sibling_fd], [], [], 0)[0])
                self.assertIsNone(sibling.poll())
                self.assertNotIn(sibling.pid, owner.seen_pids)
            finally:
                if not owner.closed:
                    cleanup_fake(root)
                signal.pidfd_send_signal(sibling_fd, signal.SIGKILL)
                sibling.wait(timeout=3)
                os.close(sibling_fd)
                for fd in probes:
                    os.close(fd)

    def test_fake_owner_control_faults_retire_only_its_tree(self):
        for fault in ("fault_eof", "fault_malformed", "caller_interrupt", "caller_wait_interrupt", "owner_interrupt"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                child_file = Path(directory) / "child"
                code = ('import os,subprocess,sys,time\n'
                        'child=subprocess.Popen(["/usr/bin/sleep","30"], start_new_session=True)\n'
                        'open(sys.argv[1],"w").write(str(child.pid))\n'
                        'while True: time.sleep(1)\n')
                root = launch_owned_fake(["/usr/bin/python3", "-I", "-c", code,
                                          str(child_file)])
                owner = root.fake_owner
                sibling = subprocess.Popen(["/usr/bin/sleep", "30"])
                sibling_fd = os.pidfd_open(sibling.pid)
                root_fd = os.pidfd_open(root.pid)
                child_fd = None
                try:
                    self.wait_for(child_file)
                    child_pid = int(child_file.read_text())
                    child_fd = os.pidfd_open(child_pid)
                    self.assertIn(str(child_pid), owner.capture())
                    if fault == "caller_interrupt":
                        with patch.object(sys.modules[__name__], "_read_message",
                                          side_effect=KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                owner.capture()
                    elif fault == "caller_wait_interrupt":
                        def interrupt_active_wait(channel, deadline):
                            time.sleep(0.1)
                            raise KeyboardInterrupt
                        with patch.object(sys.modules[__name__], "_read_message",
                                          side_effect=interrupt_active_wait):
                            with self.assertRaises(KeyboardInterrupt):
                                root.wait(timeout=5)
                    elif fault == "owner_interrupt":
                        owner_fd = os.pidfd_open(owner.process.pid)
                        try:
                            signal.pidfd_send_signal(owner_fd, signal.SIGTERM)
                        finally:
                            os.close(owner_fd)
                        with self.assertRaises((OSError, EOFError, ValueError, TimeoutError)):
                            owner.capture()
                    else:
                        with self.assertRaises((EOFError, ValueError)):
                            owner.request(fault)
                    self.assertTrue(owner.closed)
                    self.assertIsNotNone(owner.process.returncode)
                    self.assertIsNone(owner.abandon_error)
                    self.assertTrue(select.select([root_fd], [], [], 0)[0])
                    self.assertTrue(select.select([child_fd], [], [], 0)[0])
                    self.assertFalse(select.select([sibling_fd], [], [], 0)[0])
                    self.assertIsNone(sibling.poll())
                finally:
                    if not owner.closed:
                        cleanup_fake(root)
                    signal.pidfd_send_signal(sibling_fd, signal.SIGKILL)
                    sibling.wait(timeout=3)
                    for fd in (child_fd, root_fd, sibling_fd):
                        if fd is not None:
                            os.close(fd)

    def test_closed_stream_descendant_blocks_completed_output_and_spares_late_sibling(self):
        for fault in (False, True):
            with self.subTest(control_fault=fault), tempfile.TemporaryDirectory() as directory:
                child_file = Path(directory) / "child"
                code = ('import os,subprocess,sys\n'
                        'child=subprocess.Popen(["/usr/bin/sleep", "30"], '
                        'stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, '
                        'stderr=subprocess.DEVNULL, start_new_session=True)\n'
                        'open(sys.argv[1], "w").write(str(child.pid))\n'
                        'print("root_done", flush=True)\n')
                fake = launch_owned_fake(["/usr/bin/python3", "-I", "-c", code,
                                          str(child_file)], stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE)
                owner = fake.fake_owner
                sibling = subprocess.Popen(["/usr/bin/sleep", "30"])
                sibling_fd = os.pidfd_open(sibling.pid)
                child_fd = None
                try:
                    self.wait_for(child_file)
                    child_pid = int(child_file.read_text())
                    child_fd = os.pidfd_open(child_pid)
                    deadline = time.monotonic() + 2
                    while fake.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(fake.poll(), 0)  # The root really exited zero.
                    self.assertIn(child_pid, owner.child_table())
                    self.assertFalse(select.select([child_fd], [], [], 0)[0])
                    if fault:
                        with self.assertRaises(EOFError):
                            owner.request("fault_eof")
                        self.assertTrue(owner.closed)
                        with self.assertRaisesRegex(AssertionError, "completion unavailable"):
                            fake.wait(timeout=3)
                        with self.assertRaisesRegex(AssertionError, "output unavailable"):
                            fake.communicate(timeout=3)
                    else:
                        with self.assertRaisesRegex(AssertionError, "fake tree did not complete"):
                            fake.communicate(timeout=3)
                        self.assertEqual(fake.returncode, 0)
                        self.assertIsNone(fake.output)
                        self.assertIsNone(fake.error)
                        self.assertTrue(fake.completion_failed)
                        with self.assertRaisesRegex(AssertionError, "fake tree did not complete"):
                            fake.communicate(timeout=3)
                        cleanup_fake(fake)
                        self.assertTrue(owner.drained)
                        self.assertEqual(owner.last_child_table, [])
                    self.assertTrue(select.select([child_fd], [], [], 0)[0])
                    self.assertFalse(Path(f"/proc/{child_pid}").exists())
                    self.assertFalse(select.select([sibling_fd], [], [], 0)[0])
                    self.assertIsNone(sibling.poll())
                    self.assertNotIn(sibling.pid, owner.seen_pids)
                    self.assertEqual(owner.process.returncode, 0)
                finally:
                    if not owner.closed:
                        cleanup_fake(fake)
                    signal.pidfd_send_signal(sibling_fd, signal.SIGKILL)
                    sibling.wait(timeout=3)
                    os.close(sibling_fd)
                    if child_fd is not None:
                        os.close(child_fd)

    def test_fake_owner_high_fd_channel_and_byte_metadata(self):
        held = []
        try:
            held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1030)]
            env = dict(os.environb)
            env[b"OWNER_BYTE"] = b"\xff"
            script = ('import os\n'
                      'assert os.environb[b"OWNER_BYTE"] == b"\\xff"\n'
                      'print("byte-ok")\n')
            fake = launch_owned_fake([b"/usr/bin/python3", b"-I", b"-c", script.encode()],
                                     env=env, stdout=subprocess.PIPE)
            self.assertGreater(fake.fake_owner.channel.read_fd, 1024)
            with managed_fake(fake):
                output, _ = fake.communicate(timeout=3)
                self.assertEqual(fake.returncode, 0)
                self.assertEqual(output, b"byte-ok\n")
            self.assertEqual(fake.fake_owner.last_child_table, [])
            for ordinary in ({"bytes": "eA=="}, {"__afk_pairs__": [["a", 1]]}):
                self.assertEqual(_unwire_value(_wire_value(ordinary)), ordinary)
            env = dict(os.environ, OWNER_WIRE_TAG="eA==")
            fake = launch_owned_fake(["/usr/bin/python3", "-I", "-c",
                                      'import os; print(os.environ["OWNER_WIRE_TAG"])'],
                                     env=env, stdout=subprocess.PIPE)
            with managed_fake(fake):
                output, _ = fake.communicate(timeout=3)
                self.assertEqual(output, b"eA==\n")
        finally:
            for fd in held:
                os.close(fd)

    def test_fake_cleanup_retires_captured_child_after_supervisor_exit(self):
        root = subprocess.Popen(["/usr/bin/true"])
        child = subprocess.Popen(["/usr/bin/sleep", "30"])
        owner = FakeOwnership(root, watch=False)
        try:
            owner.fds[child.pid] = os.pidfd_open(child.pid)
            root.wait(timeout=3)
            owner.close_watch()  # Root is gone; the previously held child remains owned.
            owner.terminate()
            self.assertEqual(child.wait(timeout=3), -signal.SIGKILL)
        finally:
            owner.send(owner.root_fd, signal.SIGKILL)
            owner.send(owner.fds[child.pid], signal.SIGKILL)
            root.wait(timeout=3)
            child.wait(timeout=3)
            owner.close()

    def test_fake_cleanup_catches_child_forked_on_term_even_if_watch_close_fails(self):
        for failure in ("none", "watch_close", "terminate"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                temp = Path(directory)
                root_code = ('import os, signal, subprocess, time\n'
                             'def stop(_signum, _frame):\n'
                             '    child = subprocess.Popen(["/usr/bin/sleep", "30"], start_new_session=True)\n'
                             '    open(os.environ["TEST_CHILD"], "w").write(str(child.pid))\n'
                             '    os._exit(0)\n'
                             'signal.signal(signal.SIGTERM, stop)\n'
                             'open(os.environ["TEST_READY"], "w").close()\n'
                             'while True: time.sleep(1)\n')
                root = launch_owned_fake(["/usr/bin/python3", "-I", "-c", root_code],
                                         env=dict(os.environ, TEST_CHILD=str(temp / "child"),
                                                  TEST_READY=str(temp / "ready")))
                owner = root.fake_owner
                sibling = subprocess.Popen(["/usr/bin/sleep", "30"])
                sibling_fd = os.pidfd_open(sibling.pid)
                try:
                    self.wait_for(temp / "ready")
                    if failure == "watch_close":
                        original_close = owner.close_watch
                        def failed_close():
                            original_close()
                            raise AssertionError("synthetic watcher close failure")
                        with patch.object(owner, "close_watch", side_effect=failed_close):
                            with self.assertRaisesRegex(AssertionError, "watcher close failure"):
                                cleanup_fake(root)
                    elif failure == "terminate":
                        def failed_terminate():
                            owner.send_root(signal.SIGTERM)
                            self.wait_for(temp / "child")
                            raise AssertionError("synthetic termination failure")
                        with patch.object(owner, "terminate", side_effect=failed_terminate):
                            with self.assertRaisesRegex(AssertionError, "termination failure"):
                                cleanup_fake(root)
                    else:
                        cleanup_fake(root)
                    self.assertTrue((temp / "child").exists())
                    child_pid = int((temp / "child").read_text())
                    self.assertIn(child_pid, owner.seen_pids)
                    self.assertFalse(Path(f"/proc/{child_pid}").exists())
                    self.assertTrue(owner.drained)
                    self.assertEqual(owner.last_child_table, [])
                    self.assertIsNotNone(root.returncode)
                    self.assertFalse(select.select([sibling_fd], [], [], 0)[0])
                    self.assertIsNone(sibling.poll())
                    self.assertNotIn(sibling.pid, owner.seen_pids)
                finally:
                    if not owner.closed:
                        cleanup_fake(root)
                    signal.pidfd_send_signal(sibling_fd, signal.SIGKILL)
                    sibling.wait(timeout=3)
                    os.close(sibling_fd)

    def test_startup_failure_is_bounded_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            process = self.run_modified_supervisor(directory, 'touch "$TEST_LOG"\n',
                'launched = self.launch()', 'raise OSError("synthetic startup failure")')
            with self.managed_fake(process):
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertFalse((Path(directory) / "events").exists())
                self.assertIn("launch_failed=OSError", error)

    def test_post_grant_failures_retire_forked_tree(self):
        waits_for_fake = ('deadline = time.monotonic() + 1\n'
                          '            while not os.path.exists(os.environ["TEST_LOG"]) and time.monotonic() < deadline:\n'
                          '                time.sleep(0.01)\n'
                          '            raise OSError("injected startup I/O failure")')
        paths = (
            ('grant_close', 'closing, writer = writer, None\n            os.close(closing)',
             'closing, writer = writer, None\n            os.close(closing)\n            ' + waits_for_fake),
            ('poll', 'if not readable(startup_reader, STARTUP_GRACE):',
             'if True:\n                ' + waits_for_fake.replace('\n            ', '\n                ') +
             '\n            if False:'),
            ('read', 'self.startup = os.read(startup_reader, 1)', waits_for_fake),
            ('pipe_close', 'closing, startup_reader = startup_reader, None\n            os.close(closing)', waits_for_fake),
        )
        for name, old, new in paths:
            with self.subTest(path=name), tempfile.TemporaryDirectory() as directory:
                process = self.run_modified_supervisor(directory,
                    'setsid /usr/bin/sleep 30 &\n'
                    'printf "started\\n" > "$TEST_LOG"\n'
                    'wait\n', old, new)
                with managed_fake(process):
                    output, error = process.communicate(timeout=6)
                    self.assertEqual(process.returncode, 1, error)
                    self.assertEqual(output, "")
                    self.assertTrue((Path(directory) / "events").exists(), name)
                    self.assertIn("launch_failed=OSError", error)
                    self.assertNotIn("pass_exit=", error)
                    self.assertEqual(FakeOwnership.children(os.getpid()),
                                     {process.fake_owner.process.pid}, name)

    def test_fake_owner_constructor_failure_retires_ungranted_child(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(OSError, "pidfd failure"):
                launch_owned_fake(["/usr/bin/python3", "-I", "-c",
                                   'open("' + str(Path(directory) / "events") + '", "w").close()'],
                                  _owner_fail=True)
            self.assertFalse((Path(directory) / "events").exists())
            self.assertEqual(FakeOwnership.children(os.getpid()), set())

    def test_direct_fake_early_leader_exit_drains_new_session_child(self):
        with tempfile.TemporaryDirectory() as directory:
            child = Path(directory) / "child"
            script = Path(directory) / "fake"
            script.write_text('#!/usr/bin/env bash\n'
                              'setsid /usr/bin/sleep 30 &\n'
                              'printf "%s\\n" "$!" > "$TEST_LOG"\n'
                              'exit 0\n')
            script.chmod(0o755)
            process = launch_owned_fake([str(script), "paperclip-poll"],
                                        env=dict(os.environ, TEST_LOG=str(child)),
                                        start_new_session=True)
            with managed_fake(process):
                self.wait_for(child)
                self.assertEqual(process.poll(), 0)
                with self.assertRaisesRegex(AssertionError, "fake tree did not complete"):
                    process.wait(timeout=3)
                self.assertEqual(process.returncode, 0)
            self.assertEqual(FakeOwnership.children(os.getpid()), set())

    def test_disappeared_escalation_targets_do_not_claim_kill(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.run_modified_supervisor(directory,
                'touch "$TEST_LOG"\n/usr/bin/sleep 0.2\nexit 0\n',
                'self.signal_owned(signal.SIGKILL)',
                'time.sleep(0.3); self.signal_owned(signal.SIGKILL)', grace=0.0)
            try:
                self.wait_for(temp / "events")
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertIn("cancellation_drained=true", error)
                self.assertNotIn("cancellation_escalated=kill", error)
            finally:
                cleanup_fake(process)

    def test_sleep_has_no_pass_status_and_handled_term_status_is_actual(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'trap "exit 0" TERM\n'
                'printf "ready\\n" >> "$TEST_LOG"\n'
                'while :; do /usr/bin/sleep 0.02; done\n', cadence="86400")
            try:
                self.wait_for(temp / "events")
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(output, "")
                self.assertIn("pass_exit=0", error)
                self.assertEqual(error.count("pass_exit="), 1)
                self.assertNotIn("cancellation_escalated=kill", error)
            finally:
                cleanup_fake(process)

    def test_no_pass_status_from_cadence_sleep(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'printf "poll\\n" >> "$TEST_LOG"\nexit 7\n', cadence="86400")
            try:
                self.wait_for(temp / "events")
                time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual(error.count("pass_exit="), 1)
                self.assertIn("pass_exit=7", error)
            finally:
                cleanup_fake(process)

    def test_fork_handoff_keeps_next_pass_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'printf "start\\n" >> "$TEST_LOG"\n'
                "bash -c 'for i in {1..30}; do /usr/bin/sleep 0.01 & done; "
                'wait; touch "$TEST_DESC_READY"; /usr/bin/sleep 1\' &\n'
                'exit 0\n', cadence="1", extras=dict(TEST_DESC_READY=str(temp / "ready")))
            try:
                self.wait_for(temp / "ready")
                time.sleep(0.2)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                self.assertIn("cancellation_drained=true", error)
            finally:
                cleanup_fake(process)

    def test_fake_cli_readiness_follows_trap_and_reaps_on_stop(self):
        fixture = self.fixture()
        fake = fixture.split("<<'FAKE'\n", 1)[1].split("\nFAKE", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            script = temp / "fake"
            script.write_text("#!/usr/bin/env bash\n" + fake.replace("/scratch/", str(temp) + "/"))
            script.chmod(0o755)
            process = launch_owned_fake([str(script), "paperclip-poll"], start_new_session=True)
            with managed_fake(process):
                self.wait_for(temp / "fake-running")
                deadline = time.monotonic() + 2
                while True:
                    children = process.fake_owner.capture()
                    if len(children) >= 2 or time.monotonic() >= deadline:
                        break
                    time.sleep(0.01)
                self.assertGreaterEqual(len(children), 2)
                self.assertGreaterEqual(len(process.fake_owner.signal_descendants(signal.SIGTERM)), 2)
                self.wait_for(temp / "fake-reaped")
                # Child-first delivery can finish the shell's outer wait
                # before the parent sees TERM. The marker must still exist.
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                (temp / "fake-observed").touch()
                self.assertEqual(process.wait(timeout=5), 0)

    def test_fake_cli_initial_child_command_127_cannot_publish_reap(self):
        fixture = self.fixture()
        fake = fixture.split("<<'FAKE'\n", 1)[1].split("\nFAKE", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            script = temp / "fake"
            script.write_text("#!/usr/bin/env bash\n" + fake.replace("/scratch/", str(temp) + "/"))
            script.chmod(0o755)
            sleep_shim = temp / "sleep"
            sleep_shim.write_text("#!/usr/bin/env bash\n"
                                  "[[ \"$1\" != 300 ]] || exit 127\n"
                                  "exec /bin/sleep \"$@\"\n")
            sleep_shim.chmod(0o755)
            env = dict(os.environ, PATH=f"{temp}:{os.environ['PATH']}")
            process = launch_owned_fake([str(script), "paperclip-poll"], env=env,
                                        start_new_session=True)
            with managed_fake(process):
                self.assertNotEqual(process.wait(timeout=5), 0)
                self.assertTrue((temp / "fake-child-started").is_file())
                self.assertEqual((temp / "fake-child-status").read_text().strip(), "127")
                self.assertFalse((temp / "fake-reaped").exists())
                self.assertFalse((temp / "fake-observed").exists())

    def test_fake_cli_child_can_exit_before_parent_term(self):
        fixture = self.fixture()
        fake = fixture.split("<<'FAKE'\n", 1)[1].split("\nFAKE", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            script = temp / "fake"
            script.write_text("#!/usr/bin/env bash\n" + fake.replace("/scratch/", str(temp) + "/"))
            script.chmod(0o755)
            sleep_shim = temp / "sleep"
            sleep_shim.write_text("#!/usr/bin/env bash\n"
                                  "[[ \"$1\" != 300 ]] || exit 0\n"
                                  "exec /bin/sleep \"$@\"\n")
            sleep_shim.chmod(0o755)
            env = dict(os.environ, PATH=f"{temp}:{os.environ['PATH']}")
            process = launch_owned_fake([str(script), "paperclip-poll"], env=env,
                                        start_new_session=True)
            with managed_fake(process):
                self.wait_for(temp / "fake-reaped")
                self.assertEqual((temp / "fake-child-status").read_text().strip(), "0")
                self.assertIsNone(process.poll())
                process.send_signal(signal.SIGTERM)
                (temp / "fake-observed").touch()
                self.assertEqual(process.wait(timeout=5), 0)

    def test_staged_installed_layout_launches_fake_with_image_defaults(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("COPY poll-supervisor.sh /opt/factory/poll-supervisor.sh", dockerfile)
        self.assertIn("COPY poll-supervisor.py /opt/factory/poll-supervisor.py", dockerfile)
        self.assertIn("WORKDIR /home/factory", dockerfile)
        fixture = self.fixture()
        fake_body = fixture.split("<<'FAKE'\n", 1)[1].split("\nFAKE", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            factory = root / "opt/factory"
            afk = root / "opt/afk/bin"
            home = root / "home/factory"
            scratch = root / "scratch"
            for path in (factory, afk, home, scratch, root / "cache"):
                path.mkdir(parents=True, exist_ok=True)
            helper = factory / "poll-supervisor.py"
            wrapper = factory / "poll-supervisor.sh"
            cli = afk / "agent-flow-kit"
            token = "fixed-test-token"
            cli.write_text("#!/usr/bin/env bash\n# fixture-token:" + token + "\n" +
                           fake_body.replace("/scratch/", str(scratch) + "/"))
            cli.chmod(0o755)
            helper.write_text((ROOT / "poll-supervisor.py").read_text().replace(
                "/opt/afk/bin/agent-flow-kit", str(cli)).replace(
                "/scratch/supervisor-events", str(scratch / "supervisor-events")))
            wrapper.write_text((ROOT / "poll-supervisor.sh").read_text().replace(
                "/opt/factory/poll-supervisor.py", str(helper)))
            wrapper.chmod(0o755)
            events = scratch / "supervisor-events"
            # Exercise the literal shell command passed to Docker, including
            # its argument positions and fixed startup markers.
            match = re.search(r"/bin/bash -c \\\n  '(.*?)' _ \"\$nonce\"\)", fixture, re.S)
            self.assertIsNotNone(match)
            command = (match.group(1).replace("/opt/afk/bin/agent-flow-kit", str(cli))
                       .replace("/opt/factory/poll-supervisor.sh", str(wrapper))
                       .replace("/scratch/supervisor-events", str(events)))
            env = dict(PATH="/usr/bin:/bin", HOME=str(home), XDG_CACHE_HOME=str(root / "cache"),
                       XDG_STATE_HOME=str(home / ".local/state"), TMPDIR=str(scratch),
                       AFK_POLL_CADENCE_SECONDS="86400", FACTORY_FIXTURE_DIAGNOSTICS="1")
            process = launch_owned_fake(["/bin/bash", "-c", command, "_", token], cwd=home, env=env,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            with managed_fake(process):
                self.wait_for(scratch / "fake-running")
                process.send_signal(signal.SIGTERM)
                self.wait_for(scratch / "fake-reaped")
                (scratch / "fake-observed").touch()
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                lines = events.read_text().splitlines()
                self.assertEqual(lines[:3], ["fixture: entry_started", "fixture: bind_verified",
                                             "poll-supervisor: helper_started"])
                self.assertIn("poll-supervisor: stopped", lines)

    def test_installed_smoke_command_discards_unknown_interpreter_stderr(self):
        fixture = self.fixture()
        match = re.search(r"/bin/bash -c \\\n  '(.*?)' _ \"\$nonce\"\)", fixture, re.S)
        self.assertIsNotNone(match)
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            cli = temp / "agent-flow-kit"
            wrapper = temp / "poll-supervisor.sh"
            events = temp / "supervisor-events"
            cli.write_text("#!/usr/bin/env bash\n# fixture-token:known\n")
            wrapper.write_text("#!/usr/bin/env bash\nprintf 'private interpreter failure\\n' >&2\nexit 2\n")
            wrapper.chmod(0o755)
            command = (match.group(1).replace("/opt/afk/bin/agent-flow-kit", str(cli))
                       .replace("/opt/factory/poll-supervisor.sh", str(wrapper))
                       .replace("/scratch/supervisor-events", str(events)))
            result = subprocess.run(["/bin/bash", "-c", command, "_", "known"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr, "")
            self.assertEqual(events.read_text().splitlines(),
                             ["fixture: entry_started", "fixture: bind_verified"])

    def test_readiness_diagnostic_reports_only_state_and_known_events(self):
        fixture = self.fixture()
        code = fixture.split('diagnostic=$(/usr/bin/python3 -I - "$smoke_state" "$stage/supervisor-events" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events"
            events.write_text("arbitrary secret text\npoll-supervisor: launch_failed\n")
            state = json.dumps(dict(Running=False, Status="exited", ExitCode=1,
                                    Error="secret-docker-error"))
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", code, state, str(events)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "container_exited_before_marker_event_launch_failed 1")
            self.assertNotIn("secret", result.stdout)
            events.write_text("untrusted content only\n")
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", code,
                                     json.dumps(dict(Running=True, Status="running")), str(events)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "container_running_marker_absent")
            events.write_text("fixture: entry_started\nfixture: bind_verified\n"
                              "arbitrary interpreter traceback with secret\n")
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", code,
                                     json.dumps(dict(Running=False, Status="exited", ExitCode=2)),
                                     str(events)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "container_exited_before_marker_event_bind_verified 2")
            events.write_text("fixture: entry_started\nfixture: bind_verified\n"
                              "poll-supervisor: helper_started\n"
                              "poll-supervisor: invalid_configuration\n")
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", code,
                                     json.dumps(dict(Running=False, Status="exited", ExitCode=2)),
                                     str(events)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(),
                             "container_exited_before_marker_event_invalid_configuration 2")
            events.write_text("fixture: entry_started\npoll-supervisor: helper_started\n"
                              "poll-supervisor: stop_outcome\n")
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", code,
                                     json.dumps(dict(Running=False, Status="exited", ExitCode=1)),
                                     str(events)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(),
                             "container_exited_before_marker_event_stop_outcome 1")

    def test_supervisor_event_writer_parser_roundtrip(self):
        fixture = self.fixture()
        parser = fixture.split('diagnostic=$(/usr/bin/python3 -I - "$smoke_state" "$stage/supervisor-events" <<\'PY\'\n', 1)[1].split("\nPY", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "supervisor-events"
            helper = Path(directory) / "poll-supervisor.py"
            helper.write_text((ROOT / "poll-supervisor.py").read_text().replace(
                "/scratch/supervisor-events", str(events)))
            spec = importlib.util.spec_from_file_location("fixture_supervisor", helper)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            state = json.dumps(dict(Running=False, Status="exited", ExitCode=1))
            with patch.dict(os.environ, FACTORY_FIXTURE_DIAGNOSTICS="1"):
                module.log("helper_started")
                module.log("launch_failed=OSError")
            self.assertEqual(events.read_text().splitlines(),
                             ["poll-supervisor: helper_started", "poll-supervisor: launch_failed"])
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", parser, state, str(events)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "container_exited_before_marker_event_launch_failed 1")
            with patch.dict(os.environ, FACTORY_FIXTURE_DIAGNOSTICS="1"):
                module.log("stop_outcome=forced_or_unreaped")
            self.assertEqual(events.read_text().splitlines()[-1], "poll-supervisor: stop_outcome")
            result = subprocess.run(["/usr/bin/python3", "-I", "-c", parser, state, str(events)],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), "container_exited_before_marker_event_stop_outcome 1")

    def test_repeated_term_reaps_descendant(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'sleep 30 &\nprintf "%s\\n" "$!" > "$TEST_LOG"\nwait\n')
            try:
                self.wait_for(temp / "events")
                deadline = time.monotonic() + 5
                pid = None
                while pid is None and time.monotonic() < deadline:
                    try:
                        pid = int((temp / "events").read_text().strip())
                    except ValueError:
                        time.sleep(0.01)
                self.assertIsNotNone(pid)
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
                _, error = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                cleanup_fake(process)

    def test_host_python_ignores_synthetic_startup_paths(self):
        fixture = self.fixture()
        # Every host-side Python site in the helper uses the fixed interpreter
        # and isolated mode; the one in-container readiness probe is separate.
        self.assertEqual(fixture.count("/usr/bin/python3 -I"), 9)
        self.assertEqual(len(re.findall(r"(?<![\w/])python3\b", fixture)), 1)
        inventory = fixture.split("/usr/bin/python3 -I -c 'import sys\n", 1)[1].split("' || {", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            startup = root / "sitecustomize.py"
            marker = root / "startup-ran"
            startup.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('ran')\n")
            env = dict(os.environ, PYTHONPATH=str(root))
            for cwd in (root, root / "child"):
                cwd.mkdir(exist_ok=True)
                result = subprocess.run(
                    ["/usr/bin/python3", "-I", "-c", "import sys\n" + inventory],
                    input=b"", cwd=cwd, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())

    def test_term_ignoring_group_has_bounded_forced_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'trap "" TERM\n'
                'bash -c \'trap "" TERM; echo ready > "$TEST_DESC_READY"; exec /usr/bin/sleep 30\' &\n'
                'desc=$!\n'
                'while [[ ! -f "$TEST_DESC_READY" ]]; do /usr/bin/sleep 0.01; done\n'
                'printf "%s\\n" "$desc" > "$TEST_LOG"\n'
                'wait "$desc"\n',
                extras=dict(TEST_DESC_READY=str(temp / "desc-ready")))
            try:
                self.wait_for(temp / "events")
                descendant = int((temp / "events").read_text().strip())
                process.send_signal(signal.SIGTERM)
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                self.assertIn("stop_outcome=forced_or_unreaped", error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
                self.assertEqual((temp / "events").read_text().splitlines(), [str(descendant)])
            finally:
                cleanup_fake(process)

    def test_leader_exits_first_but_ignoring_descendant_is_stopped(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                "trap 'exit 0' TERM\n"
                'bash -c \'trap "" TERM; echo ready > "$TEST_DESC_READY"; exec /usr/bin/sleep 30\' &\n'
                'desc=$!\n'
                'while [[ ! -f "$TEST_DESC_READY" ]]; do /usr/bin/sleep 0.01; done\n'
                'printf "%s\\n" "$desc" > "$TEST_LOG"\n'
                'wait "$desc"\n',
                extras=dict(TEST_DESC_READY=str(temp / "desc-ready")))
            descendant = None
            try:
                self.wait_for(temp / "events")
                descendant = int((temp / "events").read_text().strip())
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
                self.assertEqual((temp / "events").read_text().splitlines(), [str(descendant)])
            finally:
                cleanup_fake(process)

    def test_natural_leader_exit_waits_for_descendant_before_next_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            process = self.launch_supervisor(directory,
                'printf "start\\n" >> "$TEST_LOG"\n'
                "bash -c 'trap \"\" TERM; echo $$ > \"$TEST_DESC_PID\"; exec /usr/bin/sleep 20' &\n"
                'while [[ ! -s "$TEST_DESC_PID" ]]; do /usr/bin/sleep 0.01; done\n'
                'touch "$TEST_CLI_DONE"\nexit 0\n', cadence="1", extras=dict(
                    TEST_DESC_PID=str(temp / "desc-pid"), TEST_CLI_DONE=str(temp / "cli-done")))
            descendant = None
            try:
                self.wait_for(temp / "cli-done")
                descendant = int((temp / "desc-pid").read_text().strip())
                # A second cadence passes while the first pass's child is live.
                time.sleep(1.3)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
                self.assertEqual((temp / "events").read_text().splitlines(), ["start"])
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(descendant)],
                                       text=True, capture_output=True).stdout.strip()
                self.assertTrue(not state or state.startswith("Z"), state)
            finally:
                cleanup_fake(process)

    def test_stalled_wall_clock_cannot_extend_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            fakebin = temp / "bin"
            fakebin.mkdir()
            date = fakebin / "date"
            date.write_text('#!/usr/bin/env bash\n'
                            'if [[ "$1" == +%s ]]; then printf "1000000\\n"; exit; fi\n'
                            'exec /usr/bin/date "$@"\n')
            date.chmod(0o755)
            process = self.launch_supervisor(directory,
                'trap "" TERM\ntouch "$TEST_LOG"\nexec /usr/bin/sleep 30\n',
                extras=dict(PATH=str(fakebin) + ":" + os.environ["PATH"]))
            try:
                self.wait_for(temp / "events")
                start = time.monotonic()
                process.send_signal(signal.SIGTERM)
                output, error = process.communicate(timeout=8)
                self.assertLess(time.monotonic() - start, 7)
                self.assertEqual(process.returncode, 1, error)
                self.assertEqual(output, "")
                self.assertIn("cancellation_escalated=kill", error)
            finally:
                cleanup_fake(process)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--fake-owner":
        _fake_owner_main(int(sys.argv[2]), int(sys.argv[3]))
    else:
        unittest.main()
