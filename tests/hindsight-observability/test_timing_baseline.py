"""The retrospective diagnostic is bounded, noninteractive, and aggregate only."""
import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('timing_baseline', ROOT / 'scripts/hindsight-timing-baseline.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
START, END = '2026-10-03T09:00:00Z', '2026-10-03T09:30:00Z'


def row(bank='devb0x', eligible=1, completed='2026-10-03T09:10:00Z', queue=1, wall=2):
    return json.dumps({'bank_id': bank, 'operation_type': 'retain', 'completed_at': completed,
                       'eligible_count': eligible, 'queue_seconds': queue,
                       'claimed_wall_seconds': wall, 'total_seconds': queue + wall}) + '\n'


class TimingBaselineTests(unittest.TestCase):
    def test_per_bank_coverage_and_quiet_bank(self):
        result = MODULE.summarize([row()], START, END)
        self.assertEqual(result['groups'][0]['coverage'], 1)
        self.assertEqual(result['missing_reference_banks'], ['obsidian'])
        self.assertEqual(result['cohort']['end_utc'], '2026-10-03T09:30:00+00:00')
        self.assertEqual(MODULE.summarize([], START, END)['groups'], [])

    def test_bounds_and_malformed_data_fail_closed(self):
        for start, end in [(END, START), ('2026-10-03T09:00:00', END),
                           (START, '2026-10-03T09:29:00Z'), (START, '2026-10-05T09:30:00Z')]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                MODULE.bounds(start, end)
        samples = ['{bad', row(completed='2026-10-03T09:30:00Z'),
                   row(completed=123), row(queue=float('nan')), row() + 'x' * 5000,
                   row().replace('"queue_seconds": 1', '"queue_seconds": "synthetic-secret"')]
        for sample in samples:
            with self.subTest(sample=sample[:20]), self.assertRaises(ValueError) as failure:
                MODULE.summarize([sample], START, END)
            self.assertNotIn('synthetic-secret', str(failure.exception))

    def test_fractional_seconds_preserve_utc_cohort(self):
        for width in range(1, 7):
            fraction = '123456'[:width]
            timestamp = f'2026-10-03T09:10:00.{fraction}Z'
            with self.subTest(width=width):
                result = MODULE.summarize([row(completed=timestamp)], START, END)
                self.assertEqual(result['sampled_rows'], 1)
                self.assertEqual(result['observed_completion_range'],
                                 [f'2026-10-03T09:10:00.{fraction.ljust(6, "0")}+00:00'] * 2)
                self.assertEqual(MODULE.parse_bound(timestamp).microsecond,
                                 int(fraction.ljust(6, '0')))
        self.assertEqual(MODULE.parse_bound('2026-10-03T09:10:00.1+00:00').microsecond,
                         100000)

    def test_fractional_seconds_reject_malformed_and_non_utc(self):
        for timestamp in ('2026-10-03T09:10:00.Z', '2026-10-03T09:10:00.12xZ',
                          '2026-10-03T09:10:00.12', '2026-10-03T09:10:00.1+01:00',
                          '2026-10-03T09:10:00.1-01:00', '2026-10-03T09:10:00.1234567Z'):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                MODULE.summarize([row(completed=timestamp)], START, END)

    def test_per_bank_limit_and_coverage(self):
        with mock.patch.object(MODULE, 'MAX_SAMPLES_PER_GROUP', 2):
            result = MODULE.summarize([row(eligible=3), row(eligible=3), row('obsidian')], START, END)
            self.assertTrue(result['groups'][0]['truncated'])
            self.assertEqual(result['groups'][0]['coverage'], 0.666667)
            with self.assertRaises(ValueError):
                MODULE.summarize([row(eligible=3)] * 3, START, END)

    def test_sql_is_read_only_per_bank_and_windowed(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('check_postgres_newest.py', workflow)
        self.assertIn('actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5.1.0\n'
                      '        with:\n          persist-credentials: false', workflow)
        self.assertIn("python-version: ['3.10', '3.12']", workflow)
        self.assertIn('python-version: ${{ matrix.python-version }}', workflow)
        sql = MODULE.SQL.lower()
        for fragment in ('partition by bank_id, operation_type', 'completed_at >= timestamptz',
                         'completed_at < timestamptz', "status = 'completed'", "operation_type <> 'batch_retain'",
                         'coalesce(retry_count, 0) = 0', 'completed_at desc, operation_id desc'):
            self.assertIn(fragment, sql)
        for forbidden in ('task_payload', 'error_message', 'insert ', 'update ', 'delete '):
            self.assertNotIn(forbidden, sql)
        self.assertNotIn('operation_id', sql.split('json_build_object', 1)[1].split('from ranked', 1)[0])

    def test_real_subprocess_args_windows_and_late_error(self):
        real_popen = subprocess.Popen
        seen = []
        def fixture(command, **kwargs):
            seen.append((command, kwargs))
            return real_popen([sys.executable, '-c',
                               'import sys; sys.stdout.write(' + repr(row()) + ')'], **kwargs)
        with mock.patch.object(MODULE.subprocess, 'Popen', side_effect=fixture):
            result = MODULE.summarize(MODULE.query_lines(START, END), START, END)
        self.assertEqual(result['sampled_rows'], 1)
        self.assertIn("TIMESTAMPTZ '2026-10-03T09:00:00+00:00'", seen[0][0][-1])
        command = seen[0][0][-1]
        self.assertLess(command.index('SET LOCAL statement_timeout = 15000;'),
                        command.index("SET LOCAL TIME ZONE 'UTC';"))
        self.assertLess(command.index("SET LOCAL TIME ZONE 'UTC';"), command.index('WITH eligible'))
        self.assertIn('-w', seen[0][0])
        self.assertIs(seen[0][1]['stdin'], subprocess.DEVNULL)

    def test_total_deadline_covers_stalled_stdout_and_reaps(self):
        real_popen = subprocess.Popen
        child = []
        def fixture(command, **kwargs):
            process = real_popen([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
            child.append(process)
            return process
        started = time.monotonic()
        with mock.patch.object(MODULE.subprocess, 'Popen', side_effect=fixture):
            with self.assertRaises(ValueError):
                list(MODULE.query_lines(START, END, deadline_seconds=.1))
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNotNone(child[0].poll())

    def test_main_has_fixed_diagnostics_and_no_partial_output(self):
        with mock.patch.object(MODULE, 'query_lines', side_effect=ValueError('secret detail')), \
             mock.patch('sys.argv', ['baseline', '--start-utc', START, '--end-utc', END]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as error, \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(MODULE.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(error.getvalue().strip(), 'baseline unavailable: invalid input or read-only query')

    def test_partial_valid_row_then_nonzero_exit_emits_no_baseline(self):
        real_popen = subprocess.Popen
        def fixture(command, **kwargs):
            script = 'import sys; sys.stdout.write(' + repr(row()) + '); sys.stdout.flush(); sys.exit(7)'
            return real_popen([sys.executable, '-c', script], **kwargs)
        with mock.patch.object(MODULE.subprocess, 'Popen', side_effect=fixture), \
             mock.patch('sys.argv', ['baseline', '--start-utc', START, '--end-utc', END]), \
             mock.patch('sys.stderr', new_callable=io.StringIO) as error, \
             mock.patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(MODULE.main(), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(error.getvalue().strip(), 'baseline unavailable: invalid input or read-only query')


if __name__ == '__main__':
    unittest.main()
