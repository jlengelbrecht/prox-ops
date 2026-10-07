"""The database TEMP revoke is static, inert SQL, and its native TLS proof refuses anything but an empty fixture."""
import contextlib
import importlib.util
import json
import pathlib
import re
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'backlog_hardening_native', ROOT / 'tests/hindsight-observability/check_postgres_backlog_hardening.py')
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)
SQL = NATIVE.SQL.read_text()


def statements(sql):
    """Non-comment SQL with string literals blanked, so names inside messages and privilege names do not count."""
    code = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    return re.sub(r"'[^']*'", "''", code)


class SqlTests(unittest.TestCase):
    def test_one_guarded_transaction_that_only_revokes_public_temporary(self):
        top = [line for line in SQL.splitlines() if not line.startswith((' ', '--', '$'))
               and line not in ('DECLARE', 'BEGIN', 'END')]
        self.assertEqual(top, [
            'BEGIN;', 'SET LOCAL search_path = pg_catalog, pg_temp;', "SET LOCAL lock_timeout = '5s';",
            "SET LOCAL statement_timeout = '30s';", 'DO $guard$',
            'REVOKE TEMPORARY ON DATABASE hindsight FROM PUBLIC;', 'DO $verify$', 'COMMIT;'])
        code = statements(SQL).lower()
        self.assertEqual(re.findall(r'\b(grant|create|alter|drop|insert|update|delete|truncate|copy|execute|'
                                    r'reset|set role|set session|comment|security)\b', code), [])
        # No psql meta-commands or variables: the Job passes only ON_ERROR_STOP.
        self.assertFalse(any(line.lstrip().startswith('\\') for line in SQL.splitlines()))
        self.assertEqual(re.findall(r'(?<!:):(?![:=])\S', code), [])

    def test_every_native_refusal_names_a_guard_the_sql_raises(self):
        raised = re.findall(r"RAISE EXCEPTION '([^']*)'", SQL)
        cases = {**NATIVE.PRE_APPLY_REFUSALS, **NATIVE.POST_APPLY_REFUSALS,
                 'held session': (None, None, None, NATIVE.SESSION_REFUSAL)}
        for label, (_, _, _, message) in cases.items():
            with self.subTest(label):
                self.assertTrue(any(message in text for text in raised[:-1]), message)

    def test_sql_stays_unmapped_and_apart_from_the_function(self):
        self.assertEqual([path.name for path in NATIVE.SQL.parent.iterdir()], ['db-acl.sql'])
        for path in (ROOT / 'kubernetes').rglob('*.y*ml'):
            self.assertNotIn('db-acl', path.read_text(errors='replace'), path)
        self.assertEqual(re.findall(r'runnable[-_]backlog|hindsight_metrics|temp_file_limit', SQL), [])

    def test_workflow_runs_the_native_proof_against_the_tls_fixture(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        step = ('      - run: python tests/hindsight-observability/check_postgres_backlog_hardening.py\n'
                '        env:\n'
                "          HINDSIGHT_BACKLOG_FIXTURE: '1'\n"
                '          PGHOST: 127.0.0.1\n'
                "          PGPORT: '5433'\n")
        self.assertEqual(workflow.count(step), 1)
        self.assertLess(workflow.index('-p 127.0.0.1:5433:5432'), workflow.index(step))


class FixtureSafetyTests(unittest.TestCase):
    SAFE = {'HINDSIGHT_BACKLOG_FIXTURE': '1', 'PGHOST': '127.0.0.1', 'PGPORT': '5433', 'PGUSER': 'postgres',
            'PATH': '/usr/bin'}

    def main_exit(self, environ, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(NATIVE.os.environ, environ, clear=True))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(NATIVE, name, value))
            exited = stack.enter_context(self.assertRaises(SystemExit))
            NATIVE.main()
        return str(exited.exception.code)

    def test_unsafe_environment_is_refused_before_any_connection(self):
        connect = mock.Mock(side_effect=AssertionError('connected'))
        for change in ({'HINDSIGHT_BACKLOG_FIXTURE': '0'}, {'PGHOSTADDR': '10.20.0.5'}, {'PGHOST': '10.20.0.5'},
                       {'PGDATABASE': 'hindsight'}, {'PGUSER': 'app'}, {'PGUSER': 'hindsight_backlog_undeclared'},
                       {'PGUSER': 'hindsight'}):
            with self.subTest(change=change), mock.patch.object(NATIVE.subprocess, 'run', connect):
                self.assertTrue(self.main_exit({**self.SAFE, **change}).startswith('refused:'))

    def test_busy_or_non_tls_server_is_refused_before_bootstrap(self):
        for change in ({'databases': ['hindsight', 'postgres', 'template0', 'template1']}, {'roles': 1},
                       {'schemas': 1}, {'relations': 1}, {'clients': 1}, {'superuser': False}, {'major': 17},
                       {'reserved_user': True}):
            replies = iter([json.dumps({**NATIVE.EMPTY_SERVER, **change})])
            with self.subTest(change=change):
                code = self.main_exit(self.SAFE, ok=mock.Mock(side_effect=lambda *a, **k: next(replies)))
                self.assertTrue(code.startswith('refused:'), code)
        # A plaintext session to an otherwise empty server must fail, never pass vacuously.
        replies = iter([json.dumps(NATIVE.EMPTY_SERVER), 'f\n'])
        code = self.main_exit(self.SAFE, ok=mock.Mock(side_effect=lambda *a, **k: next(replies)))
        self.assertIn('not using TLS', code)

    def test_unavailable_server_fails_instead_of_skipping(self):
        down = mock.Mock(returncode=2, stdout='', stderr='could not connect to server')
        with mock.patch.object(NATIVE.NATIVE, 'psql', return_value=down):
            self.assertEqual(self.main_exit(self.SAFE), 'native TLS PostgreSQL fixture unavailable')

    def test_apply_runs_the_committed_file_with_its_own_sslmode(self):
        env = NATIVE.checker_env({**self.SAFE, 'PGSSLMODE': 'disable', 'PGOPTIONS': '-c ssl=off'})
        with mock.patch.object(NATIVE.subprocess, 'run') as run:
            NATIVE.apply(env)
        command, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(command[-2:], ['-f', str(NATIVE.SQL)])
        self.assertNotIn('input', kwargs)
        self.assertEqual({key: kwargs['env'].get(key) for key in ('PGSSLMODE', 'PGDATABASE', 'PGUSER', 'PGOPTIONS')},
                         {'PGSSLMODE': 'require', 'PGDATABASE': 'hindsight', 'PGUSER': 'hindsight', 'PGOPTIONS': None})


if __name__ == '__main__':
    unittest.main()
