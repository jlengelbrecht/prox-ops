"""Process runtime of the read-only collector for the Hindsight runnable-backlog aggregate.

The contract core, collector.py beside this file, owns the poll script, the child environment, the result classes,
sample validation and rendering. This module adds only what the core leaves out: one deadline-bounded psql child
per poll, a sequential schedule with backoff, an HTTP endpoint that serves pre-rendered bytes, and the stop signal.
Importing it starts no thread, process, socket or signal handler; the container runs python3 -I -S runtime.py.
It is written to the Python 3.9 standard library of the pinned CloudNativePG image.
"""
import hashlib
import http.server
import importlib.util
import os
import random
import selectors
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time

CORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'collector.py')
# -I leaves the script directory off sys.path, so the core is loaded from its fixed sibling path only.
_spec = importlib.util.spec_from_file_location('hindsight_backlog_collector', CORE_PATH)
core = sys.modules[_spec.name] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)

PORT = 9810
DEADLINE = 20.0
KILL_GRACE = 1.0
MAX_STDOUT = 1024 * 1024
MAX_STDERR = 4096
CHUNK = 65536
INTERVAL = 60.0
JITTER = 0.1
MAX_BACKOFF = 300.0
# Poll starts are at most MAX_BACKOFF plus one bounded poll apart, so an older tick means the loop is stuck.
STALL = 400.0
HTTP_SLOTS = 4
HTTP_TIMEOUT = 5.0
WAKE = 0.25
METRICS_TYPE = 'text/plain; version=0.0.4; charset=utf-8'


def run_psql(env, argv=core.PSQL_ARGV, script=core.POLL_SCRIPT, deadline=DEADLINE, clock=time.monotonic):
    """Run one child to completion within the deadline; it is always reaped before this returns."""
    try:
        child = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=env, close_fds=True, start_new_session=True)
    except (OSError, ValueError):
        return core.RunResult(None, b'', b'', False, False)
    end = clock() + deadline
    out, err = bytearray(), bytearray()
    try:
        timed_out, oversized = _exchange(child, script, out, err, end, clock)
        if not (timed_out or oversized):
            try:
                child.wait(max(0.0, end - clock()))
            except subprocess.TimeoutExpired:
                timed_out = True
    finally:
        if child.returncode is None:
            _terminate(child)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
    if timed_out or oversized:
        return core.RunResult(None, b'', b'', timed_out, oversized)
    return core.RunResult(child.returncode, bytes(out), bytes(err), False, False)


def _exchange(child, script, out, err, end, clock):
    """Feed the script and drain both outputs together, so neither side can block on a full pipe."""
    pending = memoryview(script)
    with selectors.DefaultSelector() as selector:
        for stream, events in ((child.stdin, selectors.EVENT_WRITE), (child.stdout, selectors.EVENT_READ),
                               (child.stderr, selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events)
        while selector.get_map():
            remaining = end - clock()
            if remaining <= 0:
                return True, False
            for key, _ in selector.select(remaining):
                stream = key.fileobj
                if stream is child.stdin:
                    try:
                        pending = pending[os.write(stream.fileno(), pending[:CHUNK]):]
                    except BlockingIOError:
                        continue
                    except OSError:
                        # The child closed its input early; its exit status and stderr carry the outcome.
                        pending = pending[:0]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                    continue
                try:
                    chunk = os.read(stream.fileno(), CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                elif stream is child.stdout:
                    out += chunk[:MAX_STDOUT + 1 - len(out)]
                    if len(out) > MAX_STDOUT:
                        return False, True
                else:
                    # stderr past its cap is still drained, only dropped.
                    err += chunk[:MAX_STDERR - len(err)]
    return False, False


def _terminate(child):
    """TERM the child's process group, KILL it after the grace period, and reap the child."""
    for signum, grace in ((signal.SIGTERM, KILL_GRACE), (signal.SIGKILL, None)):
        try:
            os.killpg(child.pid, signum)
        except OSError:
            pass
        try:
            child.wait(grace)
            return
        except subprocess.TimeoutExpired:
            pass


class Collector:
    """The sequential poller. It owns all mutable state and publishes it as one immutable snapshot per poll."""

    def __init__(self, version, run=run_psql, credentials=core.CREDENTIALS, wall=time.time, clock=time.monotonic,
                 jitter=random.random, log=None):
        self._run, self._credentials, self._wall, self._jitter = run, credentials, wall, jitter
        self._log = log if log is not None else sys.stderr
        self.stopping = threading.Event()
        self._sample, self._expires = None, 0.0
        self.clock, self.tick, self.backoff = clock, clock(), INTERVAL
        self.status = core.first_boot(version)
        self.snapshot = core.publish(self.status)

    def _outcome(self):
        try:
            env = core.child_env(*core.read_credentials(self._credentials))
        except core.Unknown as unknown:
            return unknown.result, None
        result = self._run(env)
        received, received_mono = self._wall(), self.clock()
        failure = core.classify(result)
        if failure is not None:
            return failure, None
        try:
            sample = core.parse_sample(result.stdout, received)
        except core.Unknown as unknown:
            return unknown.result, None
        return 'ok', (sample, received, core.hold_expiry(sample, received, received_mono))

    def poll(self):
        """One poll: at most one child, one result class, one published snapshot and one fixed log line."""
        started = self.tick = self.clock()
        result, held = self._outcome()
        results = dict(self.status.results)
        results[result] += 1
        if held is not None:
            self._sample, received, self._expires = held
            self.status = self.status._replace(results=results, valid=True, capacity=False, last_success=received,
                                               observed=self._sample.observed)
            self.backoff = INTERVAL
        else:
            self.status = self.status._replace(results=results, valid=False, capacity=result == 'capacity_exceeded')
            self.backoff = min(MAX_BACKOFF, self.backoff * 2)
            if self._sample is not None and self.clock() >= self._expires:
                self._sample = None
        self.snapshot = core.publish(self.status, self._sample, self._expires)
        line = 'poll result=%s duration=%.3f' % (result, self.clock() - started)
        if held is not None:
            line += ' groups=%d banks=%d' % (len(self._sample.groups), len(self._sample.banks))
        self._log.write(line + '\n')
        self._log.flush()
        return result

    def delay(self):
        return min(MAX_BACKOFF, self.backoff * (1.0 + JITTER * self._jitter()))

    def run(self, wait=None):
        """Poll at once, then after each delay until stopped; a poll starts only after the previous child is reaped."""
        wait = wait if wait is not None else self.stopping.wait
        while not self.stopping.is_set():
            self.poll()
            if wait(self.delay()):
                break

    def stop(self):
        self.stopping.set()

    def healthy(self):
        """Whether the poll loop is still ticking; it never reflects database health."""
        return self.clock() - self.tick < STALL


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'hindsight-backlog-collector'
    sys_version = ''
    timeout = HTTP_TIMEOUT

    def do_GET(self):
        collector = self.server.collector
        if self.path == '/metrics':
            self._reply(200, collector.snapshot.body(collector.clock()), METRICS_TYPE)
        elif self.path == '/healthz' and collector.healthy():
            self._reply(200, b'ok\n')
        elif self.path == '/healthz':
            self._reply(503, b'')
        else:
            self._reply(404, b'')

    def send_error(self, code, message=None, explain=None):
        # A fixed status and empty body: the default error page would echo the client's request line.
        self.close_connection = True
        self._reply(code, b'')

    def log_message(self, format, *args):
        # Request lines are client-controlled and never logged.
        pass

    def _reply(self, code, body, kind='text/plain; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MetricsServer(http.server.ThreadingHTTPServer):
    """Serves the latest snapshot's bytes; a request never reaches the poller, the database or a lock."""
    daemon_threads = True

    def __init__(self, address, collector, slots=HTTP_SLOTS, deadline=HTTP_TIMEOUT):
        self.collector, self._deadline = collector, deadline
        self._slots = threading.BoundedSemaphore(slots)
        super().__init__(address, _Handler)

    def server_bind(self):
        # HTTPServer would resolve the host's FQDN here; nothing reads it, so the DNS lookup is skipped.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = _Handler.server_version, self.server_address[1]

    def process_request(self, request, client_address):
        # Handler threads are bounded: a connection beyond the bound is closed unanswered, never queued.
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # The handler timeout bounds each read only; this bounds the whole request, so a client trickling bytes
        # cannot keep its slot. Cutting the socket turns the stalled read into end-of-file.
        timer = threading.Timer(self._deadline, _cut, (request,))
        timer.daemon = True
        timer.start()
        try:
            super().process_request_thread(request, client_address)
        finally:
            timer.cancel()
            self._slots.release()

    def handle_error(self, request, client_address):
        sys.stderr.write('http request failed\n')


def _cut(request):
    try:
        request.shutdown(socket.SHUT_RDWR)
    except OSError:  # the request already ended and closed its socket
        pass


def version(path=CORE_PATH):
    with open(path, 'rb') as source:
        return hashlib.sha256(source.read()).hexdigest()[:12]


def _poll(collector, died):
    try:
        collector.run()
    except BaseException:
        # One fixed line instead of a traceback; the event wakes the main thread, so frozen bytes are never served.
        sys.stderr.write('poll loop failed\n')
        died.set()


def main(collector=None, address=('', PORT)):
    """Serve and poll until SIGTERM or SIGINT; 0 after a clean drain, 1 when the poll loop ended on its own."""
    collector = collector if collector is not None else Collector(version())
    # Handlers, not a blocked mask: exec keeps a mask, leaving psql deaf to TERM, but resets caught handlers. This
    # thread never waits on the stop event, so the handler cannot block on a lock its own thread holds.
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda signum, frame: collector.stop())
    server = MetricsServer(address, collector)
    died = threading.Event()
    poller = threading.Thread(target=_poll, args=(collector, died), name='poll', daemon=True)
    threading.Thread(target=server.serve_forever, args=(0.5,), name='metrics', daemon=True).start()
    poller.start()
    while not (collector.stopping.is_set() or died.wait(WAKE)):
        pass
    # An in-flight child is reaped within its own deadline, which bounds the drain.
    poller.join(DEADLINE + KILL_GRACE + 1.0)
    server.shutdown()
    server.server_close()
    return 1 if died.is_set() or poller.is_alive() else 0


if __name__ == '__main__':
    sys.exit(main())
