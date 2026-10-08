"""Runtime tests for the backlog collector: fake children, temp files and a loopback HTTP server only, stdlib only.

No test reaches a database, a provider or any address other than 127.0.0.1. Real psql, TLS, authentication, the
function and server-side cancellation are proven natively before activation, not here.
"""
import ast
import datetime
import hashlib
import http.client
import importlib.util
import io
import json
import os
import pathlib
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'kubernetes/apps/database/hindsight-backlog/collector/runtime.py'


def load():
    spec = importlib.util.spec_from_file_location('backlog_collector_runtime', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


R = load()
C = R.core
READER = 'hindsight_backlog_metrics'
OBSERVED = '2026-10-07T18:00:00.000000+00:00'
T0 = datetime.datetime(2026, 10, 7, 18, tzinfo=datetime.timezone.utc).timestamp()
MONO = 1000.0
CANARY_PASSWORD = 'fixture-password-canary-not-a-real-secret'
CANARY_STDERR = 'canary-detail bank-doc-77 payload'

# A stand-in for psql. Each mode is one behaviour the runner must bound; none touches the network.
FAKE_CHILD = r'''
import hashlib, json, os, signal, sys, time
mode = sys.argv[1]
if mode == 'echo':
    script = sys.stdin.buffer.read()
    print(json.dumps({'argv': sys.argv[2:], 'env': dict(os.environ), 'stdin': hashlib.sha256(script).hexdigest(),
                      'own_group': os.getpgid(0) == os.getpid()}))
elif mode == 'exact':
    sys.stdout.buffer.write(b'x' * int(sys.argv[2]))
elif mode == 'stderr':
    sys.stderr.buffer.write(b'e' * 1000000)
    sys.stderr.flush()
    print('{}')
elif mode == 'refuse':
    os.close(0)
    sys.stderr.write('psql: error: connection to server failed: FATAL:  password authentication failed for user "x"\n')
    sys.exit(2)
else:
    if mode == 'stubborn':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif mode == 'hang':
        # Records what exec handed over; the marker proves SIGTERM itself reached the child, not the later SIGKILL.
        inherited = {'blocked': sorted(map(int, signal.pthread_sigmask(signal.SIG_BLOCK, []))),
                     'default': signal.getsignal(signal.SIGTERM) == signal.SIG_DFL}
        def on_term(signum, frame):
            with open(sys.argv[2] + '.term', 'w') as marker:
                json.dump(inherited, marker)
            sys.exit(0)
        signal.signal(signal.SIGTERM, on_term)
    with open(sys.argv[2], 'w') as pidfile:
        pidfile.write(str(os.getpid()))
    if mode == 'flood':
        sys.stdout.buffer.write(b'x' * (1024 * 1024 + 1))
        sys.stdout.flush()
    time.sleep(60)
'''


def group(bank, **counts):
    item = dict.fromkeys(C.COUNTS, 0)
    item.update({'bank': bank, 'operation_type': 'retain', C.AGE: None, C.LATENESS: None})
    item.update(counts)
    return item


def sample(banks=(('b', True),), groups=()):
    doc = {'capacity_exceeded': False, 'observed_at': OBSERVED,
           'banks': [{'bank': bank, 'registered': registered} for bank, registered in banks], 'groups': list(groups)}
    return C.RunResult(0, (json.dumps(doc) + '\n').encode(), b'', False, False)


OK = sample(groups=[group('b', pending=2, due=2, runnable=2, **{C.AGE: 5.0})])
CANCEL = C.RunResult(3, b'', ('psql:<stdin>:9: ERROR:  57014\n' + CANARY_STDERR + '\n').encode(), False, False)
REFUSED = C.RunResult(2, b'', b'psql: error: connection refused\n', False, False)


class FakeRun:
    """Hands out queued results; an unexpected call has no result to take and fails the test."""
    def __init__(self, *results):
        self.results, self.envs = list(results), []

    def __call__(self, env):
        self.envs.append(env)
        return self.results.pop(0)


def line(text):
    return (text + '\n').encode()


def has_data(body):
    return b'hindsight_backlog_tasks{' in body or b'hindsight_backlog_runnable ' in body


def get(port, path, method='GET'):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, response.getheader('Content-Type'), response.read()
    finally:
        connection.close()


class Fixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = pathlib.Path(tmp.name)
        (self.folder / 'username').write_text(READER + '\n')
        (self.folder / 'password').write_text(CANARY_PASSWORD + '\n')
        (self.folder / 'fake.py').write_text(FAKE_CHILD)
        self.paths = (str(self.folder / 'username'), str(self.folder / 'password'))
        # Settable clocks: tests move them through return_value.
        self.wall, self.mono, self.log = mock.Mock(return_value=T0 + 1), mock.Mock(return_value=MONO), io.StringIO()

    def collector(self, run, jitter=0.0):
        return R.Collector('test', run, self.paths, self.wall, self.mono, lambda: jitter, self.log)

    def argv(self, *args):
        return (sys.executable, '-I', '-S', str(self.folder / 'fake.py')) + args

    def serve(self, collector, deadline=R.HTTP_TIMEOUT):
        server = R.MetricsServer(('127.0.0.1', 0), collector, deadline=deadline)
        thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
        thread.start()
        # Cleanups run last-in first-out: shutdown, close, join.
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]


class RunnerTests(Fixture):
    def test_child_gets_the_fixed_script_flags_and_a_scrubbed_environment(self):
        hostile = {'PGOPTIONS': '-c statement_timeout=0', 'PGHOST': 'evil', 'PGSERVICE': 'prod', 'HOME': '/root',
                   'PGPASSFILE': '/x', 'PGSSLMODE': 'disable', 'PGSYSCONFDIR': '/x', 'PYTHONPATH': '/x'}
        with mock.patch.dict(os.environ, hostile):
            result = R.run_psql(C.child_env(READER, CANARY_PASSWORD), self.argv('echo', *C.PSQL_ARGV[1:]))
        self.assertEqual((result.returncode, result.timed_out, result.oversized), (0, False, False))
        seen = json.loads(result.stdout)
        self.assertEqual(seen['argv'], ['-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-v', 'VERBOSITY=sqlstate',
                                        '-f', '-'])
        # Python's own C-locale coercion may add LC_CTYPE; every other variable is exactly the fixed set.
        env = {name: value for name, value in seen['env'].items() if name != 'LC_CTYPE'}
        self.assertEqual(env, C.child_env(READER, CANARY_PASSWORD))
        self.assertEqual(seen['stdin'], hashlib.sha256(C.POLL_SCRIPT).hexdigest())
        self.assertTrue(seen['own_group'])

    def test_a_child_that_ignores_sigterm_is_killed_after_the_grace_period_and_reaped(self):
        # A child that obeys SIGTERM is covered through main() in MainTests, under the production signal setup.
        pidfile = self.folder / 'stubborn'
        started = time.monotonic()
        result = R.run_psql({}, self.argv('stubborn', str(pidfile)), deadline=0.5)
        self.assertTrue(0.45 + R.KILL_GRACE <= time.monotonic() - started < 1.5 + R.KILL_GRACE)
        self.assertEqual(result, C.RunResult(None, b'', b'', True, False))
        self.assertEqual(C.classify(result), 'deadline')
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_output_is_bounded_and_oversized_stdout_ends_the_child_early(self):
        pidfile = self.folder / 'pid'
        started = time.monotonic()
        result = R.run_psql({}, self.argv('flood', str(pidfile)), deadline=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result, C.RunResult(None, b'', b'', False, True))
        self.assertEqual(C.classify(result), 'oversized')
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        exact = R.run_psql({}, self.argv('exact', str(R.MAX_STDOUT)), deadline=10)
        self.assertEqual((exact.returncode, len(exact.stdout), exact.oversized), (0, R.MAX_STDOUT, False))
        # stderr is drained in full, so the child never blocks on it, but only 4 KiB is kept.
        result = R.run_psql({}, self.argv('stderr'), deadline=10)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b'{}\n', b'e' * R.MAX_STDERR))

    def test_children_that_refuse_input_or_never_start_are_still_classified(self):
        result = R.run_psql({}, self.argv('refuse'), script=b'x' * (4 * 1024 * 1024), deadline=10)
        self.assertEqual((result.returncode, result.timed_out), (2, False))
        self.assertEqual(C.classify(result), 'auth')
        result = R.run_psql({}, (str(self.folder / 'absent-psql'),))
        self.assertEqual(result, C.RunResult(None, b'', b'', False, False))
        self.assertEqual(C.classify(result), 'connect')


class CollectorTests(Fixture):
    def test_last_good_is_held_after_a_failure_only_until_expiry(self):
        run = FakeRun(OK, CANCEL, REFUSED)
        collector = self.collector(run)
        self.assertEqual(collector.poll(), 'ok')
        self.assertIn(line('hindsight_backlog_sample_valid 1'), collector.snapshot.body(MONO))
        self.assertIn(line('hindsight_backlog_runnable 2'), collector.snapshot.body(MONO))
        self.wall.return_value, self.mono.return_value = T0 + 61, MONO + 60
        self.assertEqual(collector.poll(), 'statement_cancel')
        snapshot = collector.snapshot
        # Received one second after observed_at, so the hold ends 179 s later on the monotonic clock.
        held, gone = snapshot.body(MONO + 178.9), snapshot.body(MONO + 179)
        self.assertIn(line('hindsight_backlog_sample_valid 0'), held)
        self.assertIn(line('hindsight_backlog_runnable 2'), held)
        self.assertIn(line('hindsight_backlog_polls_total{result="statement_cancel"} 1'), held)
        self.assertFalse(has_data(gone))
        self.assertIn(line('hindsight_backlog_sample_valid 0'), gone)
        self.assertIn(b'hindsight_backlog_observed_timestamp_seconds ', gone)
        # A failure after expiry drops the held sample: no clock reading can bring it back.
        self.mono.return_value = MONO + 200
        self.assertEqual(collector.poll(), 'connect')
        self.assertFalse(has_data(collector.snapshot.body(0.0)))
        self.assertEqual(len(run.envs), 3)

    def test_an_empty_known_sample_exports_explicit_zeros(self):
        collector = self.collector(FakeRun(sample(banks=[('idle', True)])))
        self.assertEqual(collector.poll(), 'ok')
        body = collector.snapshot.body(MONO)
        for text in ('hindsight_backlog_sample_valid 1', 'hindsight_backlog_runnable 0', 'hindsight_backlog_active 0',
                     'hindsight_backlog_bank_runnable{bank_id="idle"} 0', 'hindsight_backlog_groups 0'):
            self.assertIn(line(text), body)
        self.assertNotIn(b'hindsight_backlog_tasks{', body)

    def test_capacity_exceeded_is_unknown_with_its_flag_set(self):
        collector = self.collector(FakeRun(C.RunResult(0, b'{"capacity_exceeded": true}\n', b'', False, False)))
        self.assertEqual(collector.poll(), 'capacity_exceeded')
        body = collector.snapshot.body(MONO)
        for text in ('hindsight_backlog_capacity_exceeded 1', 'hindsight_backlog_sample_valid 0'):
            self.assertIn(line(text), body)
        self.assertFalse(has_data(body))

    def test_bad_credentials_never_spawn_a_child(self):
        password, username = self.folder / 'password', self.folder / 'username'
        cases = [(password, None), (password, b''), (password, b'p' * 1025), (password, b'pa\x00ss'),
                 (password, b'pa\rss'), (password, b'pa\nss\n'), (username, b'hindsight\n')]
        for path, content in cases:
            with self.subTest(file=path.name, content=content and content[:8]):
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                run = FakeRun()
                self.assertEqual(self.collector(run).poll(), 'credentials')
                self.assertEqual(run.envs, [])
                path.write_text((CANARY_PASSWORD if path == password else READER) + '\n')

    def test_the_child_environment_is_rebuilt_from_scratch_on_every_poll(self):
        run = FakeRun(OK, OK)
        collector = self.collector(run)
        collector.poll()
        (self.folder / 'password').write_text('rotated\n')
        collector.poll()
        self.assertEqual(run.envs, [C.child_env(READER, CANARY_PASSWORD), C.child_env(READER, 'rotated')])

    def test_logs_and_exposition_carry_no_secret_or_server_text(self):
        collector = self.collector(FakeRun(CANCEL, OK))
        bodies = [collector.snapshot.body(MONO)]
        for _ in range(2):
            collector.poll()
            bodies.append(collector.snapshot.body(MONO))
        self.assertEqual(self.log.getvalue(), 'poll result=statement_cancel duration=0.000\n'
                                              'poll result=ok duration=0.000 groups=1 banks=1\n')
        for leak in (CANARY_PASSWORD, CANARY_STDERR, '57014', '/secrets', str(self.folder)):
            self.assertNotIn(leak, self.log.getvalue())
            for body in bodies:
                self.assertNotIn(leak.encode(), body)

    def test_backoff_doubles_to_five_minutes_and_resets_on_success(self):
        collector = self.collector(FakeRun(REFUSED, CANCEL, REFUSED, REFUSED, REFUSED, OK))
        self.assertEqual(collector.delay(), 60)
        backoffs = []
        for _ in range(6):
            collector.poll()
            backoffs.append(collector.backoff)
        self.assertEqual(backoffs, [120, 240, 300, 300, 300, 60])
        # Jitter adds at most ten percent and never lifts a delay past the cap.
        jittered = self.collector(FakeRun(REFUSED, REFUSED, REFUSED), jitter=0.999999)
        self.assertTrue(60 < jittered.delay() <= 66)
        for _ in range(3):
            jittered.poll()
        self.assertEqual(jittered.delay(), 300)

    def test_polls_run_one_at_a_time_from_start_up_and_stop_ends_scheduling(self):
        events = []

        def run(env):
            events.append('poll')
            if len(events) == 5:
                collector.stop()
            return OK

        def wait(delay):
            events.append(('wait', delay))
            return False
        collector = self.collector(run)
        collector.run(wait)
        self.assertEqual(events, ['poll', ('wait', 60.0)] * 3)
        stopped = self.collector(FakeRun())
        stopped.stop()
        stopped.run(wait)
        self.assertEqual(stopped.status.results, C.first_boot('test').results)


class ServerTests(Fixture):
    def test_scrapes_serve_the_snapshot_and_never_call_the_database(self):
        run = FakeRun(OK)
        collector = self.collector(run)
        port = self.serve(collector)
        # First boot is a fixed unknown state, served before any poll has run.
        _, _, body = get(port, '/metrics')
        for text in ('hindsight_backlog_sample_valid 0', 'hindsight_backlog_polls_total{result="ok"} 0'):
            self.assertIn(line(text), body)
        self.assertFalse(has_data(body) or b'observed_timestamp_seconds' in body)
        self.assertEqual(get(port, '/healthz')[::2], (200, b'ok\n'))
        collector.poll()
        status, kind, body = get(port, '/metrics')
        self.assertEqual((status, kind), (200, R.METRICS_TYPE))
        self.assertEqual(body, collector.snapshot.body(MONO))
        self.assertIn(line('hindsight_backlog_runnable 2'), body)
        # Expiry is re-rendered by the scrape itself, with no poll in between.
        self.mono.return_value = MONO + 179
        _, _, body = get(port, '/metrics')
        self.assertFalse(has_data(body))
        self.assertEqual(len(run.envs), 1)
        # Health reflects only the poll loop's tick, never the database.
        self.mono.return_value = MONO + R.STALL
        self.assertEqual(get(port, '/healthz')[::2], (503, b''))

    def test_other_requests_get_a_fixed_empty_answer_and_are_not_logged(self):
        port = self.serve(self.collector(FakeRun()))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            for method, path, expected in (('GET', '/metrics?' + CANARY_STDERR.replace(' ', '-'), 404),
                                           ('GET', '/' + CANARY_STDERR.replace(' ', '-'), 404),
                                           ('POST', '/metrics', 501), ('HEAD', '/metrics', 501)):
                with self.subTest(method=method, path=path):
                    self.assertEqual(get(port, path, method)[::2], (expected, b''))
            with socket.create_connection(('127.0.0.1', port), timeout=3) as raw:
                raw.sendall(b'GET /' + CANARY_STDERR.encode() + b' HTTP/1.0 extra\r\n\r\n')
                answer = b''.join(iter(lambda: raw.recv(4096), b''))
        self.assertNotIn(b'canary', answer)
        self.assertNotIn('canary', stderr.getvalue())

    def test_slots_are_bounded_and_a_trickling_client_loses_its_slot_at_the_request_deadline(self):
        port = self.serve(self.collector(FakeRun()), deadline=0.5)
        slow = [socket.create_connection(('127.0.0.1', port), timeout=0.02) for _ in range(R.HTTP_SLOTS)]
        for raw in slow:
            self.addCleanup(raw.close)
            raw.sendall(b'GET /metrics HTTP/1.1\r\nX-Slow: ')
        # Every slot is taken, so one more connection is closed unanswered, before any deadline could free a slot.
        with socket.create_connection(('127.0.0.1', port), timeout=0.25) as extra:
            self.assertEqual(extra.recv(4096), b'')
        started = time.monotonic()
        # A byte every few tens of ms keeps each read inside the per-read timeout; only the request deadline ends it.
        while slow and time.monotonic() - started < 3:
            raw = slow.pop(0)
            try:
                raw.sendall(b'a')
                closed = raw.recv(1) == b''
            except socket.timeout:
                closed = False
            except OSError:
                closed = True
            if not closed:
                slow.append(raw)
        self.assertEqual(slow, [])
        self.assertLess(time.monotonic() - started, 2)
        for _ in range(60):
            try:
                self.assertEqual(get(port, '/healthz')[0], 200)
                break
            except ConnectionError:
                time.sleep(0.05)  # a slot frees once its cut handler has finished
        else:
            self.fail('no handler slot was released')


class MainTests(Fixture):
    def drive(self, collector, trigger):
        """Run main() on the main thread, as the container does, while trigger runs alongside it."""
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        handlers = {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
        helper = threading.Thread(target=trigger, args=(port,), daemon=True)
        helper.start()
        try:
            return R.main(collector, ('127.0.0.1', port)), port
        finally:
            helper.join(5)
            for signum, handler in handlers.items():
                signal.signal(signum, handler)

    def test_sigterm_lets_the_in_flight_poll_and_its_real_child_finish_then_closes_the_server(self):
        entered, release, seen, pidfile = threading.Event(), threading.Event(), {}, self.folder / 'pid'

        def run(env):
            entered.set()
            release.wait(5)
            # A real child, spawned by the poll thread after main() took the stop signals, as in production.
            return R.run_psql({}, self.argv('hang', str(pidfile)), deadline=1.0)

        def trigger(port):
            try:
                if entered.wait(3):
                    seen['scrape'] = get(port, '/metrics')[2]
            finally:
                os.kill(os.getpid(), signal.SIGTERM)
                seen['stopping'] = collector.stopping.wait(3)
                seen['polls'] = sum(collector.status.results.values())
                release.set()
        collector = self.collector(run)
        code, port = self.drive(collector, trigger)
        # The scrape was answered while the poll was held, and the stop was taken before that poll ended.
        self.assertIn(line('hindsight_backlog_sample_valid 0'), seen.pop('scrape', b''))
        self.assertEqual(seen, {'stopping': True, 'polls': 0})
        self.assertEqual((code, collector.status.results), (0, dict(C.first_boot('test').results, deadline=1)))
        # The marker exists only if the child's own SIGTERM handler ran: main() left no stop signal blocked.
        self.assertEqual(json.loads((self.folder / 'pid.term').read_text()), {'blocked': [], 'default': True})
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection(('127.0.0.1', port), timeout=1).close()

    def test_a_poll_loop_that_dies_ends_the_process_with_failure_and_one_fixed_line(self):
        run = mock.Mock(side_effect=RuntimeError(CANARY_STDERR))
        with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
            code, _ = self.drive(self.collector(run), lambda port: None)
        self.assertEqual((code, stderr.getvalue()), (1, 'poll loop failed\n'))


class SurfaceTests(unittest.TestCase):
    STDLIB = {'hashlib', 'http', 'importlib', 'os', 'random', 'selectors', 'signal', 'socket', 'socketserver',
              'subprocess', 'sys', 'threading', 'time'}
    NEWER_THAN_3_9 = re.compile(r'^\s*(match|case)\s.*:\s*$|\b(TaskGroup|tomllib|pairwise|bit_count|'
                                r'ExceptionGroup|aiter|anext)\b|datetime\.UTC\b|dataclass\(|\bstrict=|except\s*\*|'
                                r'typing\.Self\b|contextlib\.chdir\b|isinstance\([^)]*\|', re.M)

    def test_import_starts_nothing_and_loads_the_core_by_its_sibling_path(self):
        threads, mask = threading.active_count(), signal.pthread_sigmask(signal.SIG_BLOCK, [])
        handlers = [signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)]
        with mock.patch('subprocess.Popen', side_effect=AssertionError('spawn')), \
                mock.patch('socket.socket', side_effect=AssertionError('socket')):
            module = load()
        self.assertEqual(threading.active_count(), threads)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), mask)
        self.assertEqual([signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)], handlers)
        self.assertEqual(pathlib.Path(module.core.__file__), SOURCE.parent / 'collector.py')
        self.assertIs(sys.modules['hindsight_backlog_collector'], module.core)
        digest = hashlib.sha256((SOURCE.parent / 'collector.py').read_bytes()).hexdigest()
        self.assertEqual(module.version(), digest[:12])

    def test_source_stays_on_the_python_3_9_stdlib_surface(self):
        text = SOURCE.read_text()
        tree = ast.parse(text, feature_version=(3, 9))
        ast.parse(pathlib.Path(__file__).read_text(), feature_version=(3, 9))
        imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, self.STDLIB)
        self.assertIsNone(self.NEWER_THAN_3_9.search(text))
        self.assertIsNone(re.search(r'\bos\.(system|popen|exec\w*|spawn\w*|fork\w*)\b|shell=True', text))
        self.assertEqual(text.count("if __name__ == '__main__':"), 1)
        self.assertTrue(text.rstrip().endswith("if __name__ == '__main__':\n    sys.exit(main())"))
