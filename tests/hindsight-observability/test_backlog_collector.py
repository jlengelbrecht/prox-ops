"""Pure contract tests for the inert backlog collector core: stdlib only, written to the Python 3.9 surface.

Run directly (python3 -I -S <this file>) so the in-image proof executes every case; a skip fails the run.
Real TLS, privilege, cancellation and the function itself are proven natively elsewhere, not here.
"""
import ast
import datetime
import hashlib
import importlib.util
import json
import math
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'kubernetes/apps/database/hindsight-backlog/collector/collector.py'


def load():
    spec = importlib.util.spec_from_file_location('backlog_collector', SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load()

# The aggregate contract as app/runnable-backlog.sql renders it, restated here rather than read from the module.
COUNTS = ('pending', 'payload_null_pending', 'deferred', 'due', 'runnable', 'serialization_blocked',
          'processing', 'assigned_pending', 'assigned_runnable', 'future_created_runnable')
AGE, LATENESS = 'runnable_oldest_task_age_seconds', 'runnable_oldest_retry_lateness_seconds'
CLASSES = {'ok', 'capacity_exceeded', 'credentials', 'precheck_cap', 'precheck_temp', 'precheck_privilege',
           'precheck_tls', 'precheck_budget', 'precheck_placement', 'hsb01', 'statement_cancel', 'permission',
           'auth', 'tls', 'connect', 'other_sqlstate', 'deadline', 'oversized', 'malformed', 'stale_or_skewed'}
DATA = {'tasks', AGE, LATENESS, 'bank_runnable', 'bank_active', 'bank_registered', 'runnable', 'active',
        'groups', 'banks'}
OBSERVED = '2026-10-07T18:00:00.000000+00:00'
T0 = datetime.datetime(2026, 10, 7, 18, tzinfo=datetime.timezone.utc).timestamp()
MONO = 1000.0
CANARY_PASSWORD = 'fixture-password-canary-not-a-real-secret'
CANARY_STDERR = 'canary-detail bank-doc-77 payload'


def group(bank, operation_type, **fields):
    item = dict.fromkeys(COUNTS, 0)
    item.update({'bank': bank, 'operation_type': operation_type, AGE: None, LATENESS: None})
    item.update({{'age': AGE, 'late': LATENESS}.get(name, name): value for name, value in fields.items()})
    return item


def body(banks=(), groups=(), observed=OBSERVED, **top):
    doc = {'capacity_exceeded': False, 'observed_at': observed,
           'banks': [{'bank': bank, 'registered': registered} for bank, registered in banks], 'groups': list(groups)}
    doc.update(top)
    return (json.dumps(doc) + '\n').encode()


def one(**fields):
    return body([('b', True)], [group('b', 'retain', **fields)])


MIXED = body([('devb0x', True), ('quiet', True), ('orphan bank!', False)], [
    group('devb0x', 'retain', pending=5, payload_null_pending=1, deferred=1, due=3, runnable=2,
          serialization_blocked=1, processing=2, assigned_pending=1, assigned_runnable=1, age=12.5, late=3.25),
    group('devb0x', 'graph_maintenance', processing=1),
    group('orphan bank!', 'batch_retain', pending=1, due=1, runnable=1, age=4)])


def pseudonym(bank):
    return 'h:' + hashlib.sha256(bank.encode()).hexdigest()[:16]


def outcome(stdout, now=T0):
    try:
        C.parse_sample(stdout, now)
    except C.Unknown as unknown:
        return unknown.result
    return 'ok'


def status(**changes):
    return C.first_boot('test')._replace(**changes)


def held(stdout=MIXED, received=T0, mono=MONO, **changes):
    """The snapshot a scheduler publishes for a sample received at (received, mono)."""
    sample = C.parse_sample(stdout, received)
    fields = {'valid': True, 'last_success': received, 'observed': sample.observed, **changes}
    return C.publish(status(**fields), sample, C.hold_expiry(sample, received, mono))


SAMPLE_LINE = re.compile(r'([a-z_]+)(?:\{((?:[a-z_]+="(?:[^"\\\n]|\\[\\"n])*",?)+)\})? (\S+)')


def exposition(data):
    """Parse strict text exposition: declared families, finite values, unique series."""
    text = data.decode('utf-8')
    if not text.endswith('\n') or re.search(r'[\x00-\x09\x0b-\x1f\x7f]', text):
        raise AssertionError('exposition is not newline-terminated printable text')
    kinds, series = {}, {}
    for line in text.splitlines():
        if line.startswith('# HELP '):
            continue
        if line.startswith('# TYPE '):
            name, kind = line[7:].split(' ')
            if name in kinds or kind not in ('gauge', 'counter', 'summary'):
                raise AssertionError(line)
            kinds[name] = kind
            continue
        parsed = SAMPLE_LINE.fullmatch(line)
        if not parsed or not line.startswith('hindsight_backlog_'):
            raise AssertionError(line)
        name, labels, value = parsed.groups()
        family = name if name in kinds else re.sub(r'_(sum|count)$', '', name)
        key = (name[len('hindsight_backlog_'):], tuple(sorted(re.findall(r'([a-z_]+)="((?:[^"\\]|\\.)*)"',
                                                                         labels or ''))))
        if family not in kinds or key in series or not math.isfinite(float(value)):
            raise AssertionError(line)
        series[key] = float(value)
    return series


def scrape(snapshot, now=MONO):
    return exposition(snapshot.body(now))


def value(series, name, **labels):
    return series.get((name, tuple(sorted(labels.items()))))


def names(series):
    return {name for name, _ in series}


class ContractTests(unittest.TestCase):
    def test_first_boot_is_unknown_with_every_result_class_at_zero(self):
        series = scrape(C.publish(status()))
        self.assertEqual({dict(labels)['result'] for name, labels in series if name == 'polls_total'}, CLASSES)
        self.assertEqual({v for (name, _), v in series.items() if name == 'polls_total'}, {0})
        self.assertEqual(value(series, 'sample_valid'), 0)
        self.assertFalse(names(series) & (DATA | {'last_success_timestamp_seconds', 'observed_timestamp_seconds'}))
        self.assertEqual((value(series, 'capacity_exceeded'), value(series, 'collector_info', version='test')), (0, 1))
        self.assertEqual(value(scrape(C.publish(status(valid=True, last_success=T0))), 'sample_valid'), 0)

    def test_known_empty_sample_is_explicit_zero_not_absence(self):
        series = scrape(held(body([('idle', True)])))
        self.assertEqual([value(series, name) for name in ('sample_valid', 'banks', 'groups', 'runnable', 'active')],
                         [1, 1, 0, 0, 0])
        self.assertEqual((value(series, 'bank_runnable', bank_id='idle'), value(series, 'bank_active', bank_id='idle')),
                         (0, 0))
        self.assertNotIn('tasks', names(series))

    def test_mixed_banks_export_exact_counts_sums_and_registration(self):
        series, orphan = scrape(held()), pseudonym('orphan bank!')
        expected = {'pending': 5, 'payload_null_pending': 1, 'deferred': 1, 'due': 3, 'runnable': 2,
                    'serialization_blocked': 1, 'processing': 2, 'assigned_pending': 1, 'assigned_runnable': 1,
                    'future_created_runnable': 0}
        for state, count in expected.items():
            self.assertEqual(value(series, 'tasks', bank_id='devb0x', operation_type='retain', state=state), count)
        self.assertEqual(value(series, AGE, bank_id='devb0x', operation_type='retain'), 12.5)
        self.assertEqual(value(series, LATENESS, bank_id='devb0x', operation_type='retain'), 3.25)
        self.assertEqual(value(series, AGE, bank_id=orphan, operation_type='batch_retain'), 4)
        self.assertIsNone(value(series, LATENESS, bank_id=orphan, operation_type='batch_retain'))
        self.assertIsNone(value(series, AGE, bank_id='devb0x', operation_type='graph_maintenance'))
        for bank, runnable, active, registered in (('devb0x', 2, 8, 1), ('quiet', 0, 0, 1), (orphan, 1, 1, 0)):
            self.assertEqual(value(series, 'bank_runnable', bank_id=bank), runnable)
            self.assertEqual(value(series, 'bank_active', bank_id=bank), active)
            self.assertEqual(value(series, 'bank_registered', bank_id=bank), registered)
        self.assertEqual([value(series, n) for n in ('runnable', 'active', 'groups', 'banks')], [3, 9, 3, 3])
        self.assertEqual(sum(1 for name, _ in series if name == 'tasks'), 30)

    def test_impossible_arithmetic_discards_the_whole_sample(self):
        # Lateness is a nullable minimum: runnable rows that all lack a retry time leave it null.
        for fields in (dict(pending=1, due=1, runnable=1, age=0), dict(pending=1, due=1, runnable=1, age=2, late=1)):
            self.assertEqual(outcome(one(**fields)), 'ok')
        broken = [dict(pending=2, due=1, processing=1), dict(pending=1, due=1, runnable=2, age=1),
                  dict(pending=2, due=2, runnable=1, age=1), dict(pending=1, due=1, serialization_blocked=1,
                                                                  assigned_pending=2),
                  dict(pending=1, due=1, runnable=1, assigned_runnable=2, age=1),
                  dict(pending=1, due=1, runnable=1, future_created_runnable=2, age=1),
                  dict(pending=1, due=1, runnable=1), dict(pending=1, due=1, serialization_blocked=1, age=3),
                  dict(processing=1, late=2), dict(pending=1, due=1, runnable=1, assigned_runnable=1, age=1), {}]
        for fields in broken:
            with self.subTest(fields=fields):
                self.assertEqual(outcome(one(**fields)), 'malformed')

    def test_counts_and_ages_must_be_bounded_finite_json_numbers(self):
        self.assertEqual(outcome(one(processing=10 ** 12)), 'ok')
        for bad in (True, 1.0, -1, 10 ** 12 + 1, '1', None):
            with self.subTest(count=bad):
                self.assertEqual(outcome(one(processing=bad)), 'malformed')
        template = one(pending=1, due=1, runnable=1, age='AGE')
        for literal in (b'NaN', b'Infinity', b'-Infinity', b'1e400', b'-0.5', b'true', b'"1"', b'1' + b'0' * 400):
            with self.subTest(age=literal):
                self.assertEqual(outcome(template.replace(b'"AGE"', literal)), 'malformed')

    def test_shape_drift_is_malformed(self):
        cases = {
            'extra top key': body(extra=1), 'missing key': body().replace(b', "groups": []', b''),
            'extra bank key': body().replace(b'"banks": []', b'"banks": [{"bank": "a", "registered": true, "x": 1}]'),
            'registered int': body().replace(b'"banks": []', b'"banks": [{"bank": "a", "registered": 1}]'),
            'extra column': body([('a', True)], [dict(group('a', 'retain', processing=1), worker_id='w-1')]),
            'duplicate key': body().replace(b'{"capacity_exceeded": false', b'{"capacity_exceeded": false, '
                                                                            b'"capacity_exceeded": false'),
            'not an object': b'[]\n', 'two lines': body() + body(), 'no newline': body().rstrip(b'\n'),
            'empty': b'', 'bad utf-8': b'\xff\n', 'banks not a list': body().replace(b'"banks": []', b'"banks": {}'),
            'deep nesting': b'[' * 20000 + b']' * 20000 + b'\n',
        }
        for label, stdout in cases.items():
            with self.subTest(label):
                self.assertEqual(outcome(stdout), 'malformed')

    def test_bank_and_group_identity_bounds(self):
        many = [('b%d' % n, True) for n in range(257)]
        self.assertEqual(outcome(body(many[:256])), 'ok')
        self.assertEqual(outcome(body([('a', True)], [group('a', t, processing=1) for t in ('9A-z_', 'r' * 64)])), 'ok')
        cases = {
            'duplicate bank': body([('a', True), ('a', False)]),
            'duplicate group': body([('a', True)], [group('a', 'retain', processing=1)] * 2),
            'group bank unlisted': body([('a', True)], [group('z', 'retain', processing=1)]),
            'empty bank': body([('', True)]), 'long bank': body([('x' * 257, True)]),
            'bank not a string': body().replace(b'"banks": []', b'"banks": [{"bank": 5, "registered": true}]'),
            '257 banks': body(many),
            '257 groups': body(many[:256], [group(b, 'retain', processing=1) for b, _ in many[:256]]
                               + [group('b0', 'consolidation', processing=1)]),
        }
        for bad in ('', 'r' * 65, 'retain\n', 'ret ain', 'a"b', 'retaín', 'a\\b'):
            cases['operation %r' % bad] = body([('a', True)], [group('a', bad, processing=1)])
        for label, stdout in cases.items():
            with self.subTest(label):
                self.assertEqual(outcome(stdout), 'malformed')

    def test_observed_at_must_be_the_exact_utc_rendering(self):
        for observed in ('2026-10-07T18:00:00Z', '2026-10-07T18:00:00.000+00:00', '2026-10-07T18:00:00.000000Z',
                         '2026-10-07T18:00:00.000000+01:00', '2026-10-07 18:00:00.000000+00:00',
                         '٢026-10-07T18:00:00.000000+00:00', '2026-13-07T18:00:00.000000+00:00', 12345):
            with self.subTest(observed=observed):
                self.assertEqual(outcome(body(observed=observed)), 'malformed')

    def test_stale_or_future_samples_are_unknown(self):
        for shift, expected in ((30, 'ok'), (31, 'stale_or_skewed'), (-5, 'ok'), (-6, 'stale_or_skewed')):
            with self.subTest(shift=shift):
                self.assertEqual(outcome(MIXED, T0 + shift), expected)

    def test_capacity_exceeded_is_its_own_unknown_only_in_its_exact_form(self):
        self.assertEqual(outcome(b'{"capacity_exceeded": true}\n'), 'capacity_exceeded')
        for stdout in (b'{"capacity_exceeded": 1}\n', b'{"capacity_exceeded": false}\n',
                       b'{"capacity_exceeded": true, "observed_at": "%s"}\n' % OBSERVED.encode()):
            with self.subTest(stdout=stdout):
                self.assertEqual(outcome(stdout), 'malformed')

    def test_bank_labels_are_verbatim_or_fixed_pseudonyms_and_collisions_fail_closed(self):
        unsafe = ['x' * 129, 'line\nbreak "quoted" \\ back', 'café', 'sp ace']
        series = scrape(held(body([('a.b:c@d-e_1', True)] + [(bank, True) for bank in unsafe])))
        self.assertEqual({dict(labels)['bank_id'] for name, labels in series if name == 'bank_registered'},
                         {'a.b:c@d-e_1'} | {pseudonym(bank) for bank in unsafe})
        self.assertEqual(outcome(body([('sp ace', True), (pseudonym('sp ace'), True)])), 'malformed')

    def test_discarded_samples_carry_only_their_class(self):
        leaky = body([('a', True)], [group('a', 'retain', processing=CANARY_STDERR)])
        for stdout in (leaky, leaky.rstrip(b'\n'), b'{"' + CANARY_STDERR.encode() + b'": \n'):
            with self.subTest(stdout=stdout):
                with self.assertRaises(C.Unknown) as raised:
                    C.parse_sample(stdout, T0)
                self.assertEqual(raised.exception.args, ('malformed',))
                self.assertTrue(raised.exception.__suppress_context__)


class FailureTests(unittest.TestCase):
    def test_each_failure_maps_to_one_fixed_class(self):
        error = 'psql:<stdin>:9: ERROR:  %s\n' + CANARY_STDERR + '\n'
        cases = [(3, error % code, expected) for code, expected in (
            ('HSC01', 'precheck_cap'), ('HSC02', 'precheck_temp'), ('HSC03', 'precheck_privilege'),
            ('HSC04', 'precheck_tls'), ('HSC05', 'precheck_budget'), ('HSC06', 'precheck_placement'),
            ('HSB01', 'hsb01'), ('57014', 'statement_cancel'), ('42501', 'permission'), ('42883', 'permission'),
            ('3F000', 'permission'), ('28P01', 'auth'), ('53300', 'connect'), ('XX000', 'other_sqlstate'))]
        connection = 'psql: error: connection to server at "postgres-rw" (10.0.0.1), port 5432 failed: %s\n'
        cases += [(3, CANARY_STDERR, 'other_sqlstate'),
                  (2, connection % 'FATAL:  password authentication failed for user "x"', 'auth'),
                  (2, 'FATAL:  no pg_hba.conf entry for host "a", user "u", database "d", SSL encryption', 'auth'),
                  (2, connection % 'FATAL:  role "x" is not permitted to log in', 'auth'),
                  (2, connection % 'server certificate for "a" does not match host name "b"', 'tls'),
                  (2, connection % 'SSL error: certificate verify failed', 'tls'),
                  (2, connection % 'Connection refused ' + CANARY_STDERR, 'connect'),
                  (1, CANARY_STDERR, 'connect'), (None, '', 'connect'), (0, '', None)]
        for code, stderr, expected in cases:
            with self.subTest(expected=expected, stderr=stderr):
                self.assertEqual(C.classify(C.RunResult(code, b'', stderr.encode(), False, False)), expected)
        self.assertEqual(C.classify(C.RunResult(None, b'', b'', True, False)), 'deadline')
        self.assertEqual(C.classify(C.RunResult(None, b'', b'', False, True)), 'oversized')
        self.assertEqual(set(C.CLASSES), CLASSES)

    def test_credentials_are_bounded_regular_files_naming_the_reader_role(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = pathlib.Path(tmp.name)
        user, other, password, fifo = (folder / name for name in ('username', 'other', 'password', 'fifo'))
        user.write_text('hindsight_backlog_metrics\n')
        other.write_text('hindsight\n')
        password.write_bytes(b'p' * 1023 + b'\n')
        os.mkfifo(str(fifo))
        self.assertEqual(C.read_credentials((str(user), str(password))), ('hindsight_backlog_metrics', 'p' * 1023))
        cases = [(other, password), (user, folder / 'absent'), (user, folder), (user, fifo)]
        for n, content in enumerate((b'', b'\n', b'p' * 1025, b'pa\x00ss', b'pa\rss', b'pass\n\n', b'pa\x01ss',
                                     b'\xffpass')):
            cases.append((user, folder / ('bad%d' % n)))
            cases[-1][1].write_bytes(content)
        for paths in cases:
            with self.subTest(paths=[path.name for path in paths]):
                with self.assertRaises(C.Unknown) as raised:
                    C.read_credentials(tuple(map(str, paths)))
                self.assertEqual(raised.exception.args, ('credentials',))

    def test_child_gets_only_the_fixed_argv_and_environment(self):
        hostile = {'PGOPTIONS': '-c statement_timeout=0', 'PGHOST': 'evil', 'PGSERVICE': 'prod', 'HOME': '/root',
                   'PGPASSFILE': '/x', 'PGSSLMODE': 'disable', 'PGTZ': 'x'}
        with mock.patch.dict(os.environ, hostile):
            env = C.child_env('hindsight_backlog_metrics', CANARY_PASSWORD)
        self.assertEqual(env, {
            'PATH': '/usr/lib/postgresql/16/bin:/usr/bin:/bin', 'LC_ALL': 'C',
            'PGHOST': 'postgres-rw.database.svc.cluster.local', 'PGPORT': '5432',
            'PGDATABASE': 'hindsight', 'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tls/ca.crt',
            'PGCONNECT_TIMEOUT': '10', 'PGAPPNAME': 'hindsight-backlog-collector',
            'PGTARGETSESSIONATTRS': 'read-write', 'PGUSER': 'hindsight_backlog_metrics',
            'PGPASSWORD': CANARY_PASSWORD})
        env['PGHOST'] = 'evil'
        self.assertEqual(C.child_env('u', 'p')['PGHOST'], 'postgres-rw.database.svc.cluster.local')
        self.assertEqual(list(C.PSQL_ARGV), ['psql', '-X', '-q', '-A', '-t', '-v', 'ON_ERROR_STOP=1', '-v',
                                             'VERBOSITY=sqlstate', '-f', '-'])

    def test_poll_script_is_read_only_prechecked_and_calls_only_the_function(self):
        script = C.POLL_SCRIPT.decode('ascii')
        lines = script.strip().splitlines()
        self.assertEqual((lines[0], lines[-2], lines[-1]),
                         ('BEGIN READ ONLY;', 'SELECT hindsight_metrics.runnable_backlog();', 'COMMIT;'))
        for setting in ("statement_timeout = '5s'", "lock_timeout = '1s'", 'max_parallel_workers_per_gather = 0',
                        "idle_in_transaction_session_timeout = '10s'"):
            self.assertIn('SET LOCAL %s;\n' % setting, script)
        codes = re.findall(r"ERRCODE = '(\w+)'", script)
        self.assertEqual(codes, ['HSC01', 'HSC02', 'HSC03', 'HSC04', 'HSC05', 'HSC06'])
        self.assertEqual(script.count('runnable_backlog()'), 2)
        self.assertLess(script.index('$pre$;'), script.index('SELECT hindsight_metrics.runnable_backlog();'))
        for check in ("'128MB'", "'TEMPORARY'", "'hindsight_backlog_metrics'", 'pg_stat_ssl', "'SET')",
                      "transaction_read_only') <> 'on'", 'pg_is_in_recovery()', 'has_any_column_privilege'):
            self.assertIn(check, script)
        self.assertEqual(re.findall(r'public\.\w+', script), ['public.async_operations', 'public.banks'] * 2)
        self.assertEqual(re.findall(r'\bFROM\s+(\w+)', script), ['pg_settings', 'pg_roles', 'pg_auth_members',
                                                                 'pg_stat_ssl'])
        self.assertEqual(re.findall(r'^SET (?!LOCAL )', script, re.M), [])
        self.assertIsNone(re.search(r'\b(insert|update|delete|merge|create|alter|drop|grant|revoke|reset|copy|'
                                    r'truncate|lock|listen|notify|set_config|pg_sleep|dblink|lo_\w+)\b',
                                    script.lower()))


class SnapshotTests(unittest.TestCase):
    def test_last_good_is_held_only_within_180s_of_observed_at(self):
        # A later failed poll republishes the held sample with sample_valid 0 and the same expiry.
        snapshot = held(received=T0 + 10, valid=False)
        live, gone = scrape(snapshot, MONO + 169.9), scrape(snapshot, MONO + 170.0)
        self.assertEqual((value(live, 'sample_valid'), value(live, 'runnable')), (0, 3))
        self.assertFalse(names(gone) & DATA)
        self.assertEqual(value(gone, 'observed_timestamp_seconds'), T0)
        self.assertEqual(value(gone, 'last_success_timestamp_seconds'), T0 + 10)
        # A stalled poller's valid sample withdraws at expiry and stops claiming validity at the same instant.
        valid = held()
        live, gone = scrape(valid, MONO + 179.9), scrape(valid, MONO + 180.0)
        self.assertEqual((value(live, 'sample_valid'), value(gone, 'sample_valid'),
                          value(gone, 'observed_timestamp_seconds')), (1, 0, T0))
        self.assertFalse(names(gone) & DATA)
        # Expiry is fixed at receipt; a receiver behind the database clock holds at most HOLD + MAX_SKEW.
        sample = C.parse_sample(MIXED, T0)
        for received, expires in ((T0, MONO + 180), (T0 + 30, MONO + 150), (T0 - 5, MONO + 185)):
            with self.subTest(received=received):
                self.assertEqual(C.hold_expiry(sample, received, MONO), expires)

    def test_status_counters_and_timestamps_are_exported_exactly(self):
        results = dict(dict.fromkeys(CLASSES, 0), ok=3, connect=2, capacity_exceeded=1)
        series = scrape(C.publish(status(version='a"b\\c\nd', results=results, capacity=True, last_success=T0 + 0.5,
                                         observed=T0 - 1)))
        self.assertEqual([value(series, 'polls_total', result=name) for name in ('ok', 'connect', 'auth')], [3, 2, 0])
        self.assertEqual((value(series, 'capacity_exceeded'), value(series, 'sample_valid')), (1, 0))
        self.assertEqual((value(series, 'last_success_timestamp_seconds'), value(series, 'observed_timestamp_seconds')),
                         (T0 + 0.5, T0 - 1))
        self.assertEqual(value(series, 'collector_info', version='a\\"b\\\\c\\nd'), 1)
        self.assertFalse(names(series) & DATA)
        with self.assertRaises(ValueError):
            C.publish(status(observed=float('nan')))


class RuntimeSurfaceTests(unittest.TestCase):
    INERT = {'subprocess', 'socket', 'http', 'urllib', 'threading', 'signal', 'selectors', 'asyncio',
             'multiprocessing', 'ctypes', 'ssl'}

    def test_module_is_inert_on_import_and_has_no_process_or_network_surface(self):
        threads = threading.active_count()
        load()
        self.assertEqual(threading.active_count(), threads)
        tree = ast.parse(SOURCE.read_text())
        imported = {alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imported & self.INERT, imported)
        self.assertIsNone(re.search(r'\bos\.(system|popen|exec\w*|spawn\w*|fork\w*|kill\w*)\b|__main__',
                                    SOURCE.read_text()))


if __name__ == '__main__':
    run = unittest.main(exit=False, verbosity=2).result
    sys.exit(0 if run.wasSuccessful() and run.testsRun and not run.skipped else 1)
