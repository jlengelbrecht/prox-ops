"""The reader's temp_file_limit cap is one guarded ALTER ROLE, not yet mapped to any Job, and its native TLS
proof refuses anything but an empty fixture."""
import contextlib
import importlib.util
import json
import pathlib
import re
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'backlog_role_limits_native', ROOT / 'tests/hindsight-observability/check_postgres_backlog_role_limits.py')
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)
ACL_TESTS = importlib.util.spec_from_file_location(
    'backlog_db_acl_tests', ROOT / 'tests/hindsight-observability/test_backlog_db_acl.py')
ACL = importlib.util.module_from_spec(ACL_TESTS)
ACL_TESTS.loader.exec_module(ACL)
SQL = NATIVE.SQL.read_text()
TREE = NATIVE.SQL.parent
ALTER = "ALTER ROLE hindsight_backlog_metrics SET temp_file_limit = '128MB';"
PREIMAGE = "set_config('hindsight_role_limits.preimage',"


def violations(sql):
    """Every way besides the one top-level ALTER ROLE line that the file could change state."""
    code = ACL.statements(sql)
    found = [line for line in code.splitlines() if re.search(r'\b(alter|grant|revoke|reset)\b', line, re.I)]
    problems = [] if found == [ALTER.replace("'128MB'", "''")] else [f'state statements {found}']
    problems += re.findall(r'\b(?:execute|perform|format|dblink\w*|query_to_xml\w*|gexec|create|drop|insert|update|'
                           r'delete|truncate|copy|comment|security|system|session|set\s+role)\b', code, re.I)
    # The one transaction-local placeholder that carries the pre-image to the verify.
    if code.count('set_config(') != 1 or code.count(PREIMAGE.replace("'hindsight_role_limits.preimage'", "''")) != 1:
        problems.append('set_config')
    if '\\' in code:
        problems.append('backslash')
    if sorted(re.findall(r'\$\w*\$', code)) != ['$guard$', '$guard$', '$verify$', '$verify$']:
        problems.append('dollar quotes')
    return problems


class SqlTests(unittest.TestCase):
    def test_one_guarded_transaction_that_only_sets_the_reader_cap(self):
        top = [line for line in SQL.splitlines() if not line.startswith((' ', '--', '$'))
               and line not in ('DECLARE', 'BEGIN', 'END')]
        self.assertEqual(top, [
            'BEGIN;', 'SET LOCAL search_path = pg_catalog, pg_temp;', "SET LOCAL lock_timeout = '5s';",
            "SET LOCAL statement_timeout = '30s';", 'DO $guard$', ALTER, 'DO $verify$', 'COMMIT;'])
        self.assertEqual(violations(SQL), [])
        # No psql variables either: the Job passes only ON_ERROR_STOP.
        self.assertEqual(re.findall(r'(?<!:):(?![:=])\S', ACL.statements(SQL)), [])
        # The superuser is recognised by attribute; its private name never appears.
        self.assertIn('rolsuper FROM pg_roles WHERE rolname = current_user', SQL)
        self.assertNotIn('postgres', ACL.statements(SQL).replace('PostgreSQL', ''))

    def test_any_other_state_change_is_caught(self):
        guard_body = ' IF current_database() <> '
        mutations = {
            'second alter': SQL.replace(ALTER, ALTER + '\n' + ALTER.replace('128MB', '1GB'), 1),
            'other role': SQL.replace(ALTER, ALTER.replace('hindsight_backlog_metrics', 'hindsight'), 1),
            'nested alter': SQL.replace(guard_body, f' {ALTER}\n{guard_body}', 1),
            'reset': SQL.replace('COMMIT;', 'ALTER ROLE hindsight_backlog_metrics RESET work_mem;\nCOMMIT;', 1),
            'parameter grant': SQL.replace('COMMIT;', 'GRANT SET ON PARAMETER temp_file_limit TO hindsight;\nCOMMIT;'),
            'alter system': SQL.replace('COMMIT;', "ALTER SYSTEM SET temp_file_limit = '1GB';\nCOMMIT;", 1),
            'alter database': SQL.replace('COMMIT;', "ALTER DATABASE hindsight SET temp_file_limit = -1;\nCOMMIT;", 1),
            'per-database role': SQL.replace(ALTER, ALTER.replace(' SET', ' IN DATABASE hindsight SET'), 1),
            'set role': SQL.replace('DO $guard$', 'SET ROLE hindsight;\nDO $guard$', 1),
            'dynamic literal': SQL.replace(guard_body, " EXECUTE 'ALTER ROLE app SUPERUSER';\n" + guard_body, 1),
            'second set_config': SQL.replace(guard_body, " PERFORM set_config('work_mem', '1GB', false);\n"
                                             + guard_body, 1),
            'psql gexec': SQL.replace('COMMIT;', "SELECT 'DROP ROLE app' \\gexec\nCOMMIT;", 1),
            'dollar-quoted block': SQL.replace('COMMIT;', 'DO $x$ BEGIN NULL; END $x$;\nCOMMIT;', 1),
        }
        for label, mutated in mutations.items():
            with self.subTest(label):
                self.assertNotEqual(mutated, SQL)
                self.assertNotEqual(violations(mutated), [])

    def test_every_native_refusal_names_a_guard_the_sql_raises(self):
        raised = re.findall(r"RAISE EXCEPTION '([^']*)'", SQL)
        cases = {**NATIVE.PRE_APPLY_REFUSALS, **NATIVE.POST_APPLY_REFUSALS,
                 'revoke': (None, None, None, NATIVE.REVOKE_MISSING), 'hba': (None, None, None, NATIVE.HBA_MISSING),
                 'held session': (None, None, None, NATIVE.SESSION_REFUSAL)}
        for label, (_, _, _, message) in cases.items():
            with self.subTest(label):
                self.assertTrue(any(message in text for text in raised[:-1]), message)

    def test_accepted_defaults_are_exactly_the_five_the_native_proof_writes(self):
        literal = re.search(r"NOT IN \('\{\}', ARRAY\[('default[^]]*)\]\)", SQL).group(1)
        self.assertEqual(re.findall(r"'([^']*)'", literal), sorted(NATIVE.DEFAULTS))
        self.assertEqual(len(set(entry.split('=')[0] for entry in NATIVE.DEFAULTS)), 5)

    def test_sql_is_inactive_and_never_names_the_function(self):
        # Nothing applies the file yet: no manifest, Flux entry or Job maps it.
        self.assertEqual(sorted(path.name for path in TREE.iterdir()), ['role-limits.sql'])
        self.assertEqual([path for path in (ROOT / 'kubernetes').rglob('*.y*ml')
                          if 'role-limits' in path.read_text(errors='replace')], [])
        self.assertEqual(re.findall(r'runnable[-_]backlog|hindsight_metrics|db-acl', SQL), [])

    def test_workflow_triggers_on_the_sql_and_runs_the_native_proof_on_the_tls_fixture(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        for path in (NATIVE.SQL.relative_to(ROOT).as_posix(), 'tests/hindsight-observability/test_backlog_role_limits.py',
                     'tests/hindsight-observability/check_postgres_backlog_role_limits.py'):
            self.assertEqual(workflow.count('      - ' + path + '\n'), 1, path)
        self.assertEqual(workflow.count('role-limits/'), 1)
        self.assertEqual(workflow.count(
            '      - run: python -m unittest discover -s tests/hindsight-observability -p test_backlog_role_limits.py -v\n'
        ), 1)
        step = ('      - run: python tests/hindsight-observability/check_postgres_backlog_role_limits.py\n'
                '        env:\n'
                "          HINDSIGHT_BACKLOG_FIXTURE: '1'\n"
                '          PGHOST: 127.0.0.1\n'
                "          PGPORT: '5433'\n")
        self.assertEqual(workflow.count(step), 1)
        self.assertLess(workflow.index('-p 127.0.0.1:5433:5432'), workflow.index(step))
        self.assertLess(workflow.index(step), workflow.index('Remove TLS PostgreSQL fixture'))
        self.assertEqual(workflow.count('pip install PyYAML==6.0.3'), 1)


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
                       {'PGHOST': 'postgres-rw.database.svc.cluster.local'}, {'PGSERVICE': 'prod'},
                       {'PGDATABASE': 'hindsight'}, {'PGUSER': 'app'}, {'PGUSER': 'hindsight'},
                       {'PGUSER': 'hindsight_backlog_metrics'}):
            with self.subTest(change=change), mock.patch.object(NATIVE.subprocess, 'run', connect):
                self.assertTrue(self.main_exit({**self.SAFE, **change}).startswith('refused:'))

    def test_busy_or_non_tls_server_is_refused_before_any_write(self):
        cases = [(change, [0, 0]) for change in (
            {'databases': ['hindsight', 'postgres', 'template0', 'template1']}, {'roles': 1}, {'schemas': 1},
            {'relations': 1}, {'clients': 1}, {'superuser': False}, {'major': 17}, {'reserved_user': True})]
        cases += [({}, [1, 0]), ({}, [0, 1])]
        for change, leftovers in cases:
            replies = iter([json.dumps({**NATIVE.HARDENING.EMPTY_SERVER, **change}), json.dumps(leftovers)])
            with self.subTest(change=change, leftovers=leftovers):
                code = self.main_exit(self.SAFE, ok=mock.Mock(side_effect=lambda *a, **k: next(replies)))
                self.assertTrue(code.startswith('refused:'), code)
        replies = iter([json.dumps(NATIVE.HARDENING.EMPTY_SERVER), '[0, 0]', 'f\n'])
        self.assertIn('not using TLS', self.main_exit(self.SAFE, ok=mock.Mock(side_effect=lambda *a, **k: next(replies))))

    def test_unavailable_server_fails_instead_of_skipping(self):
        down = mock.Mock(returncode=2, stdout='', stderr='could not connect to server')
        with mock.patch.object(NATIVE.HARDENING.NATIVE, 'psql', return_value=down):
            self.assertEqual(self.main_exit(self.SAFE), 'native TLS PostgreSQL fixture unavailable')

    def test_apply_runs_the_committed_file_as_the_fixture_superuser_with_its_own_sslmode(self):
        env = NATIVE.HARDENING.checker_env({**self.SAFE, 'PGSSLMODE': 'verify-full', 'PGOPTIONS': '-c ssl=off'})
        with mock.patch.object(NATIVE.subprocess, 'run') as run:
            NATIVE.apply(env)
        command, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertEqual(command[-2:], ['-f', str(NATIVE.SQL)])
        self.assertNotIn('input', kwargs)
        self.assertEqual({key: kwargs['env'].get(key) for key in ('PGSSLMODE', 'PGDATABASE', 'PGUSER', 'PGOPTIONS')},
                         {'PGSSLMODE': 'require', 'PGDATABASE': 'hindsight', 'PGUSER': 'postgres', 'PGOPTIONS': None})
        # The spill is sized past twice the cap at 1 kB a row, the control well under it.
        self.assertTrue(NATIVE.SPILL_UNDER * 1100 < 2 ** 27 < 2 ** 28 < NATIVE.SPILL_OVER * NATIVE.SPILL_WIDTH)
        # The width only reaches temporary files if it is stored per row: a materialized CTE of a per-row value,
        # with the width read back so a narrower row cannot be counted.
        spill = NATIVE.SPILL.format(rows=1)
        self.assertIn('WITH wide AS MATERIALIZED (', spill)
        self.assertIn('repeat(md5(g::text), 32) AS s', spill)
        self.assertEqual(32 * 32, NATIVE.SPILL_WIDTH)
        self.assertTrue(spill.endswith(f'FROM wide WHERE length(s) = {NATIVE.SPILL_WIDTH};'))
        self.assertNotIn('ORDER BY', spill)
        # The only literal startup option given to the reader lifts the read-only default, never the cap.
        self.assertEqual(NATIVE.WRITABLE, '-c default_transaction_read_only=off')


if __name__ == '__main__':
    unittest.main()
