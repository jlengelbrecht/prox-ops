"""The runnable-backlog diagnostic is read-only, bounded, aggregate only and fails closed."""
import contextlib
import copy
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
SPEC = importlib.util.spec_from_file_location('queue_backlog', ROOT / 'scripts/hindsight-queue-backlog.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SECRET = 'synthetic-secret-doc-key'


def group(bank='devb0x', kind='retain', **overrides):
    item = {'bank': bank, 'operation_type': kind, 'pending': 4, 'payload_null_pending': 1,
            'deferred': 1, 'due': 2, 'runnable': 1, 'serialization_blocked': 1, 'processing': 1,
            'assigned_pending': 0, 'assigned_runnable': 0, 'future_created_runnable': 0,
            'runnable_oldest_task_age_seconds': 12.5, 'runnable_oldest_retry_lateness_seconds': None}
    item.update(overrides)
    return item


def document(groups=None, banks=('devb0x', 'obsidian')):
    return {'capacity_exceeded': False, 'observed_at': '2026-10-06T12:00:00.123456+00:00',
            'banks': [{'bank': bank, 'registered': True} for bank in banks],
            'groups': [group()] if groups is None else groups}


def encode(value):
    return (json.dumps(value) + '\n').encode()


class ParseTests(unittest.TestCase):
    def test_valid_result_rolls_up_and_keeps_empty_bank_as_true_zero(self):
        future = group(kind='graph_maintenance', pending=1, payload_null_pending=0, deferred=0, due=1, runnable=1,
                       serialization_blocked=0, future_created_runnable=1, runnable_oldest_task_age_seconds=0)
        result = MODULE.parse_result(encode(document([group(), future])), 256)
        self.assertEqual(result['schema'], 'hindsight-queue-backlog/1')
        devb0x, obsidian = result['banks']
        self.assertEqual([t['operation_type'] for t in devb0x['types']], ['graph_maintenance', 'retain'])
        self.assertEqual(devb0x['types'][0]['runnable_oldest_task_age_seconds'], 0.0)
        totals = devb0x['totals']
        self.assertEqual((totals['runnable'], totals['future_created_runnable'],
                          totals['runnable_oldest_task_age_seconds']), (2, 1, 12.5))
        self.assertEqual((obsidian['types'], obsidian['totals']['runnable_oldest_task_age_seconds']), ([], None))
        self.assertTrue(all(obsidian['totals'][name] == 0 for name in MODULE.COUNTS))
        self.assertEqual(result['totals']['pending'], 5)
        self.assertIn('not a claim guarantee', result['semantics']['runnable'])
        self.assertIn('not continuous eligible wait', result['semantics']['runnable_oldest_task_age_seconds'])
        self.assertIn('not separate claim demand', result['semantics']['serialization_blocked'])
        self.assertIn('public schema only', result['semantics']['scope'])

    def test_unregistered_and_unusual_bank_ids_are_reported_and_safely_encoded(self):
        bank = 'team "a" / ü'
        doc = document([group(bank=bank)], banks=('devb0x',))
        doc['banks'].append({'bank': bank, 'registered': False})
        result = MODULE.parse_result(encode(doc), 256)
        self.assertEqual([(b['bank'], b['registered']) for b in result['banks']], [('devb0x', True), (bank, False)])
        self.assertIn('team \\"a\\" / \\u00fc', json.dumps(result, sort_keys=True))

    def test_malformed_nonfinite_and_inconsistent_results_fail_closed(self):
        broken = [
            b'', b'{bad\n', encode(document())[:-1], encode(document()) * 2,
            encode(document()).replace(b'12.5', b'NaN'), encode(document()).replace(b'12.5', b'Infinity'),
            encode(document()).replace(b'12.5', b'1e999'), encode([document()]),
            b'\xff\xfe\n', b'[' * 100000 + b'\n',
        ]
        group_breaks = [
            {'runnable': 2}, {'pending': 9}, {'runnable': -1, 'serialization_blocked': 3},
            {'processing': True}, {'processing': 1.0}, {'assigned_runnable': 1},
            {'runnable_oldest_task_age_seconds': None}, {'runnable_oldest_task_age_seconds': -0.001},
            {'future_created_runnable': 2}, {'future_created_runnable': 1},
            {'error_message': SECRET}, {'operation_type': SECRET + ' x'}, {'bank': 'unlisted'},
        ]
        bank_breaks = [('devb0x', True), ('bad\nbank', True), ('esc\x1b[31m', True), ('bidi' + chr(0x202e) + 'kab', True),
                       ('', True), ('x' * 257, True), ('lonely', False)]
        mutations = [
            lambda d: d.update(extra=SECRET),
            lambda d: d.update(observed_at='2026-10-06T12:00:00'),
            lambda d: d.update(observed_at='2026-10-06T12:00:00+01:00'),
            lambda d: d.update(capacity_exceeded=True),
            lambda d: d['groups'][0].pop('future_created_runnable'),
            lambda d: d['groups'].append(copy.deepcopy(d['groups'][0])),
            *[lambda d, change=change: d['groups'][0].update(change) for change in group_breaks],
            *[lambda d, bank=bank: d['banks'].append({'bank': bank[0], 'registered': bank[1]})
              for bank in bank_breaks],
        ]
        for mutate in mutations:
            doc = document()
            mutate(doc)
            broken.append(encode(doc))
        for data in broken:
            with self.subTest(data=data[:60]), self.assertRaises(MODULE.InvalidResult) as failure:
                MODULE.parse_result(data, 256)
            self.assertNotIn(SECRET, str(failure.exception))
            self.assertEqual(failure.exception.message, 'backlog unknown: invalid aggregate result')

    def test_group_and_output_bounds_are_explicit_never_truncated(self):
        with self.assertRaises(MODULE.CapacityExceeded):
            MODULE.parse_result(encode({'capacity_exceeded': True}), 256)
        doc = document([group(bank=f'b{i}') for i in range(3)], banks=[f'b{i}' for i in range(3)])
        with self.assertRaises(MODULE.CapacityExceeded):
            MODULE.parse_result(encode(doc), 2)
        self.assertEqual(len(MODULE.parse_result(encode(doc), 3)['banks']), 3)
        with self.assertRaises(MODULE.OutputBoundExceeded):
            MODULE.parse_result(b' ' * MODULE.MAX_OUTPUT + encode(doc), 3)


class QueryTests(unittest.TestCase):
    def fake(self, script, seen=None):
        real_popen = subprocess.Popen
        children = []
        def popen(command, **kwargs):
            if seen is not None:
                seen.append((command, kwargs))
            child = real_popen([sys.executable, '-c', script], **kwargs)
            children.append(child)
            return child
        return mock.patch.object(MODULE.subprocess, 'Popen', side_effect=popen), children

    def test_single_read_only_psql_invocation_with_fixed_session_options(self):
        seen = []
        patch, _ = self.fake('import sys; sys.stdout.write(' + repr(encode(document()).decode()) + ')', seen)
        inherited = {'PGOPTIONS': '-c default_transaction_read_only=off -c search_path=evil,pg_catalog'}
        with patch, mock.patch.dict(MODULE.os.environ, inherited):
            result = MODULE.collect(17)
        self.assertEqual((result['max_groups'], len(seen)), (17, 1))
        command, kwargs = seen[0]
        self.assertEqual(command[:9], ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1', '-c'])
        self.assertEqual(len(command), 10)
        sql = command[-1]
        self.assertTrue(sql.startswith('BEGIN TRANSACTION READ ONLY; SET LOCAL statement_timeout = 10000; '
                                       'SET LOCAL lock_timeout = 1000; SET LOCAL search_path = pg_catalog;'))
        self.assertTrue(sql.endswith('END)::jsonb; COMMIT;'))
        self.assertIn('> 17 OR', sql)
        self.assertNotIn('shell', kwargs)
        self.assertEqual((kwargs['stdin'], kwargs['stderr']), (subprocess.DEVNULL, subprocess.DEVNULL))
        self.assertEqual(kwargs['env']['PGCONNECT_TIMEOUT'], '5')
        self.assertEqual(kwargs['env']['PGOPTIONS'], '-c default_transaction_read_only=on -c statement_timeout=10000 '
                                                     '-c lock_timeout=1000 -c search_path=pg_catalog')
        self.assertFalse(any('password' in part.lower() for part in command[:-1]))

    def test_query_reads_only_the_two_source_relations_and_emits_no_row_content(self):
        sql = MODULE.build_sql(256)
        lower = sql.lower()
        self.assertEqual(lower.count('public.'), 2)
        self.assertIn('from public.async_operations', MODULE.SQL_SOURCES.lower())
        self.assertIn('select bank_id from public.banks', MODULE.SQL_SOURCES.lower())
        self.assertNotIn('public.', MODULE.SQL_BODY.lower())
        for verb in ('insert ', 'update ', 'delete ', 'create ', 'drop ', 'alter ', 'grant ',
                     'truncate', 'for update', 'pg_', 'set role', 'copy '):
            self.assertNotIn(verb, lower)
        output = lower.split('select (case when', 1)[1]
        for column in ('operation_id', 'task_payload', 'serialization_key', "'worker_id'",
                       'error_message', 'mission', 'config', "'name'"):
            self.assertNotIn(column, output)
        self.assertNotIn('worker_id is null', lower)
        for bad in (0, MODULE.HARD_MAX_GROUPS + 1, '5; DROP', True, 2.0):
            with self.subTest(bound=bad), self.assertRaises(MODULE.BacklogUnknown):
                MODULE.build_sql(bad)

    def assert_reaped_failure(self, script, error_type, deadline):
        patch, children = self.fake(script)
        started = time.monotonic()
        with patch, self.assertRaises(MODULE.BacklogUnknown) as failure:
            MODULE.run_query('SELECT 1;', deadline_seconds=deadline)
        self.assertLess(time.monotonic() - started, deadline + 3)
        self.assertIsNotNone(children[0].poll())
        self.assertIs(type(failure.exception), error_type)

    def test_stalled_child_hits_wall_deadline_and_is_reaped(self):
        self.assert_reaped_failure('import time; time.sleep(30)', MODULE.BacklogUnknown, .2)

    def test_child_that_closes_stdout_but_never_exits_is_reaped(self):
        self.assert_reaped_failure('import os, time; os.close(1); time.sleep(30)', MODULE.BacklogUnknown, 1)

    def test_oversized_stdout_is_a_distinct_bounded_failure(self):
        self.assert_reaped_failure('import sys\nwhile True: sys.stdout.write("x" * 65536)',
                                   MODULE.OutputBoundExceeded, 5)

    def test_nonzero_exit_after_valid_output_is_a_failure(self):
        script = 'import sys; sys.stdout.write(' + repr(encode(document()).decode()) + '); sys.stdout.flush(); sys.exit(3)'
        patch, _ = self.fake(script)
        with patch, self.assertRaises(MODULE.BacklogUnknown):
            MODULE.collect()

    def test_missing_psql_is_a_fixed_failure(self):
        with mock.patch.object(MODULE.subprocess, 'Popen', side_effect=FileNotFoundError(SECRET)), \
             self.assertRaises(MODULE.BacklogUnknown) as failure:
            MODULE.run_query('SELECT 1;')
        self.assertNotIn(SECRET, repr(failure.exception))


class MainTests(unittest.TestCase):
    def run_main(self, argv, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch('sys.argv', ['backlog', *argv]))
            error = stack.enter_context(mock.patch('sys.stderr', new_callable=io.StringIO))
            output = stack.enter_context(mock.patch('sys.stdout', new_callable=io.StringIO))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(MODULE, name, value))
            try:
                code = MODULE.main()
            except SystemExit as exit_:
                code = exit_.code
        return code, output.getvalue(), error.getvalue()

    def test_fixed_error_messages_and_no_partial_output(self):
        for error_type in (MODULE.BacklogUnknown, MODULE.InvalidResult, MODULE.CapacityExceeded,
                           MODULE.OutputBoundExceeded, MODULE.CleanupIncomplete):
            with self.subTest(error=error_type.__name__):
                code, output, error = self.run_main([], run_query=mock.Mock(side_effect=error_type(SECRET)))
                self.assertEqual((code, output, error), (1, '', error_type.message + '\n'))

    def test_unkillable_child_reports_cleanup_incomplete_through_real_query_path(self):
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('psql', 1)
        with mock.patch.object(MODULE.subprocess, 'Popen', return_value=process):
            code, output, error = self.run_main([], _drain=mock.Mock(side_effect=OSError(SECRET)), REAP_TIMEOUT=.01)
        self.assertEqual((code, output, error.strip()), (1, '', MODULE.CleanupIncomplete.message))
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        process.stdout.close.assert_called_once()

    def test_bad_child_data_never_echoed(self):
        leaked = encode(document()).replace(b'"devb0x"', b'"' + SECRET.encode() + b'\\u0000"')
        code, output, error = self.run_main([], run_query=mock.Mock(return_value=leaked))
        self.assertEqual((code, output), (1, ''))
        self.assertEqual(error.strip(), 'backlog unknown: invalid aggregate result')

    def test_success_emits_one_json_document(self):
        code, output, error = self.run_main(['--max-groups', '8'], run_query=mock.Mock(return_value=encode(document())))
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(output.count('\n'), 1)
        self.assertEqual(json.loads(output)['max_groups'], 8)

    def test_max_groups_argument_is_bounded(self):
        for value in ('0', '1025', '-3', 'abc', '1e3', "8; DROP TABLE x"):
            with self.subTest(value=value):
                query = mock.Mock()
                code, output, _ = self.run_main(['--max-groups', value], run_query=query)
                self.assertEqual((code, output), (2, ''))
                query.assert_not_called()

    def test_workflow_runs_unit_and_native_checks(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        for path in ('scripts/hindsight-queue-backlog.py', 'tests/hindsight-observability/test_queue_backlog.py',
                     'tests/hindsight-observability/check_postgres_queue_backlog.py'):
            self.assertIn('      - ' + path + '\n', workflow)
        self.assertIn('python -m unittest discover -s tests/hindsight-observability -p test_queue_backlog.py -v',
                      workflow)
        self.assertIn('python tests/hindsight-observability/check_postgres_queue_backlog.py', workflow)
        self.assertIn('python tests/hindsight-observability/check_postgres_newest.py', workflow)


if __name__ == '__main__':
    unittest.main()
