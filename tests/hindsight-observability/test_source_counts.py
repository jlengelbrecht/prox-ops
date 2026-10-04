"""Protected source aggregation emits only fixed counts and UTC bounds."""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('source_counts', ROOT / 'scripts/hindsight-source-counts.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
START, END = '2026-10-03T09:00:00Z', '2026-10-03T09:30:00Z'
UUID = '12345678-1234-1234-1234-123456789abc'
CONTAINER = 'containerd://' + 'a' * 64


def line(message, stamp='2026-10-03T09:10:00Z', logger='hindsight_api.worker.poller'):
    return json.dumps({'timestamp': stamp, 'logger': logger, 'message': message,
                       'exception': 'synthetic-secret'}) + '\n'


def receipt_for(result, source):
    observed = {key: source[key] for key in MODULE.IDENTITY}
    return {**result, **observed, 'capture_before': observed,
            'capture_after': observed, 'producer_exit_code': 0}


class SourceCountTests(unittest.TestCase):
    def test_fixed_aggregate_and_half_open_window(self):
        source = [line(f'Task {UUID} timed out: synthetic-secret'),
                  line(f'Task {UUID} failed: HTTP 429 synthetic-secret'),
                  line(f'Task {UUID} deferred until synthetic-secret'),
                  line('Codex LLM quota exhausted for synthetic-secret', logger='hindsight_api.engine.providers.codex_llm'),
                  line('Codex refresh_token is permanently invalid; synthetic-secret', logger='hindsight_api.engine.providers.codex_llm'),
                  line(f'Task {UUID} failed: synthetic-secret', stamp=END), '{bad', 'x' * 70000]
        result = MODULE.counts(source, START, END)
        self.assertEqual(result['worker_timeout'], 1)
        self.assertEqual(result['worker_failed'], 1)
        self.assertEqual(result['worker_deferred'], 1)
        self.assertEqual(result['provider_429_evidence'], 1)
        self.assertEqual(result['auth_refresh_error'], 1)
        self.assertEqual(result['malformed_lines'], 2)
        self.assertEqual(result['source_lines'], len(source))
        self.assertNotIn('synthetic-secret', json.dumps(result))
        unrelated = MODULE.counts([line(f'Task {UUID} failed: HTTP 429 from unrelated upstream')], START, END)
        self.assertEqual(unrelated['worker_failed'], 1)
        self.assertEqual(unrelated['provider_429_evidence'], 0)
        for near in ('untrusted_worker_proxy', 'hindsight_api.worker.poller.extra'):
            self.assertEqual(MODULE.counts([line(f'Task {UUID} failed: synthetic-secret', logger=near)],
                                           START, END)['worker_failed'], 0)
        for near in ('proxy.codex_llm', 'hindsight_api.engine.providers.codex_llm.extra'):
            self.assertEqual(MODULE.counts([line('Codex LLM quota exhausted for synthetic-secret', logger=near)],
                                           START, END)['provider_429_evidence'], 0)

    def test_aggregate_only_trial_math(self):
        flags = dict.fromkeys(('source_coverage', 'scrape_coverage', 'lifecycle_complete',
                               'provider_evidence_complete', 'outcome_evidence_complete',
                               'db_wait_coverage'), True)
        base = dict(start_utc='2026-10-03T08:00:00Z', end_utc='2026-10-03T08:30:00Z',
                    provider_429=0, auth_refresh_error=1, worker_timeout=0,
                    readiness_failure=0, llm_p95={'retain': 10},
                    recall_p95={'devb0x': 2}, reflect_p95={'devb0x': 4},
                    llm_observations={'retain': 100}, recall_observations={'devb0x': 100},
                    reflect_observations={'devb0x': 100},
                    completions={'devb0x': 10}, pending={'devb0x': 100},
                    completion_error={'devb0x': 0.1}, completion_measure={'devb0x': 'prometheus_estimate'},
                    db_wait_five_minutes=False, **flags)
        second = {**base, 'start_utc': '2026-10-03T08:30:00Z',
                  'end_utc': START, 'pending': {'devb0x': 5}}
        trial = {**second, 'start_utc': START, 'end_utc': END,
                 'provider_429': 1, 'auth_refresh_error': 2,
                 'llm_p95': {'retain': 16}, 'completions': {'devb0x': 4},
                 'pending': {'devb0x': 10}}
        banks = {'devb0x'}
        with self.assertRaisesRegex(ValueError, '^incomplete comparison$'):
            MODULE.trial_regressions(base, second, trial, banks)
        self.assertEqual(set(MODULE._candidate_regressions(base, second, trial, banks)),
                         {'provider_429', 'auth_refresh_error', 'llm_p95:retain', 'throughput:devb0x'})
        invalid = [
            {**trial, 'provider_429': None}, {**trial, 'provider_429': -1},
            {**trial, 'provider_429': 1.5}, {**trial, 'provider_429': True},
            {**trial, 'llm_p95': {'retain': float('nan')}},
            {**trial, 'llm_p95': {'other': 16}},
            {**trial, 'recall_p95': {}}, {**trial, 'completions': {}},
            {**trial, 'source_coverage': False},
            {**trial, 'llm_observations': {'retain': 1}},
            {**trial, 'completion_error': {'devb0x': -1}},
            {**trial, 'completion_error': {'devb0x': 0}},
            {**trial, 'completion_measure': {'devb0x': 'unknown'}},
            {**trial, 'db_wait_five_minutes': 0},
            {**trial, 'start_utc': '2026-10-03T09:01:00Z'},
        ]
        for sample in invalid:
            with self.subTest(sample=sample), self.assertRaisesRegex(ValueError, '^incomplete comparison$'):
                MODULE._candidate_regressions(base, second, sample, banks)
        with self.assertRaisesRegex(ValueError, '^incomplete comparison$'):
            MODULE._candidate_regressions(base, second, trial, {'devb0x', 'obsidian'})
        quiet = {**second, 'completions': {'devb0x': 0}}
        with self.assertRaisesRegex(ValueError, '^incomplete comparison$'):
            MODULE._candidate_regressions(base, quiet, trial, banks)
        self.assertIn('throughput:devb0x', MODULE._candidate_regressions(
            base, second, {**trial, 'completions': {'devb0x': 0}}, banks))
        self.assertIn('throughput:devb0x', MODULE._candidate_regressions(
            base, second, {**trial, 'completions': {'devb0x': 1.0344827586206897}}, banks))
        self.assertEqual(MODULE._candidate_regressions(base, second, {**trial,
            'completions': {'devb0x': 10}, 'pending': {'devb0x': 10},
            'provider_429': 0, 'auth_refresh_error': 1,
            'llm_p95': {'retain': 10}}, banks), [])
        with self.assertRaisesRegex(ValueError, '^incomplete comparison$'):
            MODULE._candidate_regressions(base, second, {**trial,
                'completions': {'devb0x': 4.999999999},
                'completion_error': {'devb0x': 0.1}}, banks)

    def test_coverage_requires_producer_and_complete_instances(self):
        source = dict(pod_uid=UUID, container_id=CONTAINER, log_source='current',
                      interval_start=START, interval_end=END, lifecycle_start=START,
                      lifecycle_end=END, first_timestamp=None, last_timestamp=None,
                      source_lines=0, producer_exit_code=0, lifecycle_verified=True,
                      rotation_verified=True, retention_verified=True)
        manifest = dict(complete=True, inventory_complete=True, lifecycle_complete=True, sources=[source])
        MODULE.validate_coverage(manifest, START, END)
        for invalid in [dict(manifest, inventory_complete=False), dict(manifest, sources=[]),
                        dict(manifest, complete=False),
                        dict(manifest, sources=[dict(source, interval_end='2026-10-03T09:20:00Z')]),
                        dict(manifest, sources=[dict(source, first_timestamp=START)]),
                        dict(manifest, sources=[dict(source, first_timestamp='2026-10-03T09:29:00Z',
                                                           last_timestamp='2026-10-03T09:29:00Z', source_lines=1,
                                                           retention_verified=False)]),
                        dict(manifest, sources=[dict(source, first_timestamp='2026-10-03T08:59:00Z',
                                                           last_timestamp=START, source_lines=1)])]:
            with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
                MODULE.validate_coverage(invalid, START, END)
        receipt = receipt_for(MODULE.counts([], START, END), source)
        self.assertEqual(MODULE.aggregate_receipts(manifest, [receipt], START, END)['verified_sources'], 1)
        later = {**source, 'interval_end': '2026-10-03T09:40:00Z',
                 'lifecycle_end': '2026-10-03T09:40:00Z',
                 'first_timestamp': '2026-10-03T09:35:00Z',
                 'last_timestamp': '2026-10-03T09:35:00Z', 'source_lines': 1}
        self.assertEqual(MODULE.counts([line('ordinary', stamp='2026-10-03T09:35:00Z')],
                                       START, END)['in_window_lines'], 0)
        MODULE.validate_coverage({**manifest, 'sources': [later]}, START, END)
        second = {**source, 'pod_uid': 'other', 'interval_start': '2026-10-03T09:15:00Z'}
        with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
            MODULE.aggregate_receipts({**manifest, 'sources': [source, second]}, [receipt], START, END)

    def test_cli_refuses_unbound_stream(self):
        source = dict(pod_uid=UUID, container_id=CONTAINER, log_source='current',
                      interval_start=START, interval_end=END, lifecycle_start=START,
                      lifecycle_end=END, first_timestamp=None, last_timestamp=None,
                      source_lines=0, producer_exit_code=0, lifecycle_verified=True,
                      rotation_verified=True, retention_verified=True)
        with tempfile.TemporaryDirectory() as temp:
            coverage = pathlib.Path(temp) / 'coverage.json'
            args = [sys.executable, str(ROOT / 'scripts/hindsight-source-counts.py'),
                    '--start-utc', START, '--end-utc', END, '--coverage-file', str(coverage),
                    '--pod-uid', UUID,
                    '--container-id', CONTAINER, '--log-source', 'current']
            coverage.write_text(json.dumps(dict(complete=True, inventory_complete=True,
                lifecycle_complete=True, sources=[source])))
            run = subprocess.run(args, input=line(f'Task {UUID} failed: synthetic-secret'),
                                 text=True, capture_output=True)
            self.assertNotEqual(run.returncode, 0)
            self.assertEqual(run.stdout, '')
            self.assertEqual(run.stderr.strip(), 'source coverage unknown')

    def test_cli_fails_closed_on_bounded_invalid_source(self):
        source = dict(pod_uid=UUID, container_id=CONTAINER, log_source='current',
                      interval_start=START, interval_end=END, lifecycle_start=START,
                      lifecycle_end=END, first_timestamp=None, last_timestamp=None,
                      source_lines=1, producer_exit_code=0, lifecycle_verified=True,
                      rotation_verified=True, retention_verified=True)
        with tempfile.TemporaryDirectory() as temp:
            coverage = pathlib.Path(temp) / 'coverage.json'
            coverage.write_text(json.dumps(dict(complete=True, inventory_complete=True,
                lifecycle_complete=True, sources=[source])))
            argv = ['source-counts', '--start-utc', START, '--end-utc', END,
                    '--coverage-file', str(coverage), '--capture-pod', 'hindsight-api-abcde',
                    '--pod-uid', UUID, '--container-id', CONTAINER, '--log-source', 'current']
            for payload in (b'x' * 200000 + b'\n', b'\xff\n'):
                result = MODULE.counts(MODULE.bounded_lines(io.BytesIO(payload)), START, END)
                with mock.patch.object(sys, 'argv', argv), \
                     mock.patch.object(MODULE, 'capture_source', return_value=result), \
                     mock.patch('sys.stdout', new_callable=io.StringIO) as out, \
                     mock.patch('sys.stderr', new_callable=io.StringIO) as err:
                    self.assertEqual(MODULE.main(), 1)
                self.assertEqual(out.getvalue(), '')
                self.assertEqual(err.getvalue().strip(), 'source coverage unknown')

    def test_each_physical_instance_is_covered_and_receipts_are_possible(self):
        other_uid = 'abcdef12-1234-1234-1234-123456789abc'
        first = dict(pod_uid=UUID, container_id=CONTAINER, log_source='current',
                     interval_start=START, interval_end=END, lifecycle_start=START,
                     lifecycle_end=END, first_timestamp='2026-10-03T09:10:00+00:00',
                     last_timestamp='2026-10-03T09:10:00+00:00', source_lines=1,
                     producer_exit_code=0, lifecycle_verified=True,
                     rotation_verified=True, retention_verified=True)
        second = {**first, 'pod_uid': other_uid,
                  'container_id': 'containerd://' + 'b' * 64,
                  'first_timestamp': '2026-10-03T09:20:00+00:00',
                  'last_timestamp': '2026-10-03T09:20:00+00:00'}
        manifest = dict(complete=True, inventory_complete=True, lifecycle_complete=True,
                        sources=[first, second])
        receipts = []
        for source, event in [(first, 'failed: synthetic-secret'),
                              (second, 'timed out: synthetic-secret')]:
            stamp = source['first_timestamp']
            receipts.append(receipt_for(MODULE.counts([line(f'Task {UUID} {event}', stamp=stamp)],
                                                     START, END), source))
        total = MODULE.aggregate_receipts(manifest, receipts, START, END)
        self.assertEqual((total['verified_sources'], total['source_events'],
                          total['worker_failed'], total['worker_timeout']), (2, 2, 1, 1))
        truncated = {**second, 'interval_end': '2026-10-03T09:15:00Z',
                     'first_timestamp': '2026-10-03T09:10:00+00:00',
                     'last_timestamp': '2026-10-03T09:10:00+00:00'}
        with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
            MODULE.validate_coverage({**manifest, 'sources': [first, truncated]}, START, END)
        duplicate = {**second, 'pod_uid': UUID, 'container_id': CONTAINER,
                     'log_source': 'previous'}
        with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
            MODULE.validate_coverage({**manifest, 'sources': [first, duplicate]}, START, END)
        for mutation in ({'worker_failed': 99}, {'source_events': 2},
                         {'in_window_lines': 2}, {'provider_429_evidence': 2},
                         {'malformed_lines': False}, {'unknown_key': 1},
                         {'capture_after': {**receipts[0]['capture_after'], 'container_id': second['container_id']}},
                         {'producer_exit_code': 1}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
                MODULE.aggregate_receipts(manifest, [{**receipts[0], **mutation}, receipts[1]], START, END)

    def test_retry_in_and_out_of_window(self):
        result = MODULE.counts([line(f'Task {UUID} scheduled for retry at 2026-10-03T09:15:00Z'),
                                line(f'Task {UUID} scheduled for retry at 2026-10-03T09:40:00Z', stamp=END)],
                               START, END)
        self.assertEqual(result['worker_retry'], 1)
        self.assertEqual(result['source_events'], 1)

    def test_invalid_bounds(self):
        for start, end in ((END, START), ('2026-10-03T09:00:00', END)):
            with self.assertRaises(ValueError):
                MODULE.counts([], start, end)

    def test_binary_reader_bounds_and_invalid_encoding(self):
        class Checked(io.BytesIO):
            def readline(self, size=-1):
                self.assert_size(size)
                return super().readline(size)
            def assert_size(self, size):
                self_size = MODULE.MAX_LINE + 1
                if size != self_size:
                    raise AssertionError('unbounded read')
        payload = (b'x' * 200000 + b'\n' + b'\xff\n' +
                   line(f'Task {UUID} failed: synthetic-secret').encode())
        lines = list(MODULE.bounded_lines(Checked(payload)))
        self.assertEqual(lines[:2], [None, None])
        self.assertEqual(MODULE.counts(lines, START, END)['malformed_lines'], 2)

    def test_capture_binds_identity_and_producer_status(self):
        key = (UUID, CONTAINER, 'current')
        observed = dict(zip(MODULE.IDENTITY, key))
        event = line(f'Task {UUID} failed: synthetic-secret').encode()
        class FakeProcess:
            def __init__(self, status=0):
                self.stdout = io.BytesIO(event)
                self.status = status
            def wait(self, timeout=None):
                return self.status
            def kill(self):
                pass
        with mock.patch.object(MODULE, 'observed_source', side_effect=[observed, observed]), \
             mock.patch.object(MODULE.subprocess, 'Popen', return_value=FakeProcess()):
            receipt = MODULE.capture_source('hindsight-api-abcde', key, START, END)
        self.assertEqual((receipt['worker_failed'], receipt['source_lines']), (1, 1))
        self.assertEqual(receipt['capture_before'], receipt['capture_after'])
        for before, after, status in [
            (observed, {**observed, 'container_id': 'containerd://' + 'b' * 64}, 0),
            ({**observed, 'container_id': 'containerd://' + 'b' * 64}, observed, 0),
            (observed, observed, 1),
        ]:
            with self.subTest(status=status, before=before, after=after), \
                 mock.patch.object(MODULE, 'observed_source', side_effect=[before, after]), \
                 mock.patch.object(MODULE.subprocess, 'Popen', return_value=FakeProcess(status)):
                with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
                    MODULE.capture_source('hindsight-api-abcde', key, START, END)

    def test_capture_deadline_covers_stalled_and_continuous_stdout(self):
        key = (UUID, CONTAINER, 'current')
        observed = dict(zip(MODULE.IDENTITY, key))
        for script in ('import time; time.sleep(3)',
                       'import sys, time; end=time.monotonic()+3\nwhile time.monotonic()<end: sys.stdout.write("x\\n"); sys.stdout.flush()'):
            with self.subTest(script=script):
                process = subprocess.Popen([sys.executable, '-c', script], stdout=subprocess.PIPE)
                started = time.monotonic()
                with mock.patch.object(MODULE, 'observed_source', return_value=observed), \
                     mock.patch.object(MODULE.subprocess, 'Popen', return_value=process), \
                     mock.patch.object(MODULE, 'CAPTURE_TIMEOUT', 0.15, create=True):
                    with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
                        MODULE.capture_source('hindsight-api-abcde', key, START, END)
                self.assertLess(time.monotonic() - started, 2)
                self.assertIsNotNone(process.poll())
                self.assertTrue(process.stdout.closed)

    def test_observed_current_and_previous_are_distinct_physical_containers(self):
        prior = 'containerd://' + 'b' * 64
        pod = {'metadata': {'uid': UUID}, 'status': {'containerStatuses': [
            {'name': 'api', 'containerID': CONTAINER,
             'lastState': {'terminated': {'containerID': prior}}}]}}
        def response(data, code=0):
            return subprocess.CompletedProcess([], code, json.dumps(data).encode(), b'')
        with mock.patch.object(MODULE.subprocess, 'run', return_value=response(pod)):
            self.assertEqual(MODULE.observed_source('hindsight-api-abcde', 'current')['container_id'], CONTAINER)
            self.assertEqual(MODULE.observed_source('hindsight-api-abcde', 'previous')['container_id'], prior)
        with mock.patch.object(MODULE.subprocess, 'run', return_value=response({**pod,
                'status': {'containerStatuses': [{'name': 'api', 'containerID': CONTAINER}]}})):
            with self.assertRaisesRegex(ValueError, '^source coverage unknown$'):
                MODULE.observed_source('hindsight-api-abcde', 'previous')


if __name__ == '__main__':
    unittest.main()
