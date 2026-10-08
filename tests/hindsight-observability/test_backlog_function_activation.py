"""The backlog function is applied by one owner Job after the reader cap, and its native TLS proof refuses anything
but an empty fixture."""
import contextlib
import hashlib
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


NATIVE = load('backlog_function_activation_native',
              'tests/hindsight-observability/check_postgres_backlog_function_activation.py')
ACL = load('backlog_db_acl_tests', 'tests/hindsight-observability/test_backlog_db_acl.py')
SQL = NATIVE.SQL.read_text()
TREE = NATIVE.SQL.parent
APP_NAME = 'hindsight-backlog-function'
POD_LABELS = {'app.kubernetes.io/name': APP_NAME}
WORKFLOW = ROOT / '.github/workflows/hindsight-baseline.yaml'


def rendered(tree=TREE):
    return [doc for doc in yaml.safe_load_all(ACL.subprocess.run(
        [ACL.KUSTOMIZE, 'build', str(tree)], check=True, capture_output=True, text=True).stdout) if doc]


def as_function(doc):
    """An approved owner-Job tree document with only its names moved to this tree's."""
    text = yaml.safe_dump(doc).replace('hindsight-backlog-db-acl', APP_NAME).replace('db-acl.sql', 'runnable-backlog.sql')
    return yaml.safe_load(text)


class ActivationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = rendered()
        cls.job = ACL.one(cls.docs, 'Job')
        cls.pod = cls.job['spec']['template']['spec']
        cls.policy = ACL.one(cls.docs, 'CiliumNetworkPolicy')
        cls.approved = rendered(ACL.ACL_TREE)

    def test_renders_only_the_committed_sql_one_job_and_one_policy_deterministically(self):
        self.assertEqual(sorted(doc['kind'] for doc in self.docs), ['CiliumNetworkPolicy', 'ConfigMap', 'Job'])
        self.assertEqual(rendered(), self.docs)
        config = ACL.one(self.docs, 'ConfigMap')
        self.assertEqual(sorted(config), ['apiVersion', 'data', 'kind', 'metadata'])
        self.assertEqual(config['metadata'], {'name': APP_NAME})
        self.assertEqual(config['data'], {'runnable-backlog.sql': SQL})
        # The mounted file is the exact render, bound to the pinned API image.
        self.assertIsNone(NATIVE.FUNCTION.RENDERER.check(NATIVE.SQL))

    def test_job_name_is_the_sql_hash_and_a_revision_and_nothing_forces_a_rerun(self):
        digest = hashlib.sha256(NATIVE.SQL.read_bytes()).hexdigest()
        self.assertEqual(self.job['metadata'], {'name': f'{APP_NAME}-{digest[:12]}-r1'})
        spec = {key: value for key, value in self.job['spec'].items() if key != 'template'}
        # No ttlSecondsAfterFinished: the completed Job stays as the record Flux waits on.
        self.assertEqual(spec, {'backoffLimit': 2, 'activeDeadlineSeconds': 300, 'parallelism': 1, 'completions': 1})
        self.assertEqual(self.job['spec']['template']['metadata'], {'labels': POD_LABELS})
        self.assertNotIn('kustomize.toolkit.fluxcd.io/force', yaml.safe_dump(self.docs))

    def test_job_and_policy_are_the_approved_owner_job_with_only_names_changed(self):
        # Same digest-pinned client, UID 26, seccomp, dropped capabilities, read-only root, no token, resources,
        # verify-full against the CA's public half and the owner Secret, with only the mounted file renamed.
        approved = ACL.one(self.approved, 'Job')
        self.assertEqual(self.job['spec']['template'], as_function(approved['spec']['template']))
        self.assertEqual(self.policy, as_function(ACL.one(self.approved, 'CiliumNetworkPolicy')))
        self.assertEqual(self.policy['spec']['endpointSelector'], {'matchLabels': POD_LABELS})

    def test_psql_runs_the_mounted_file_with_fixed_argv_over_verified_tls_as_the_owner(self):
        [container] = self.pod['containers']
        self.assertEqual(container['image'], ACL.IMAGE)
        self.assertEqual(container['command'],
                         ['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-f', '/sql/runnable-backlog.sql'])
        self.assertNotIn('args', container)
        env = {item['name']: item.get('value', item.get('valueFrom')) for item in container['env']}
        self.assertEqual(len(env), len(container['env']))
        self.assertEqual(env, {
            'PGHOST': 'postgres-rw.database.svc.cluster.local', 'PGPORT': '5432', 'PGDATABASE': 'hindsight',
            'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tls/ca.crt', 'PGCONNECT_TIMEOUT': '10',
            'PGAPPNAME': APP_NAME,
            'PGUSER': {'secretKeyRef': {'name': 'hindsight-db-credentials', 'key': 'username'}},
            'PGPASSWORD': {'secretKeyRef': {'name': 'hindsight-db-credentials', 'key': 'password'}}})
        self.assertEqual(self.pod['volumes'], [
            {'name': 'sql', 'configMap': {'name': APP_NAME,
                                          'items': [{'key': 'runnable-backlog.sql', 'path': 'runnable-backlog.sql'}]}},
            {'name': 'ca', 'secret': {'secretName': 'postgres-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}])
        external = yaml.safe_load((ACL.DATABASE / 'cluster/app/externalsecret-hindsight.yaml').read_text())
        self.assertEqual(external['spec']['target']['name'], 'hindsight-db-credentials')
        self.assertEqual(ACL.secret_names(self.docs), {'postgres-ca', 'hindsight-db-credentials'})
        self.assertNotIn('Secret', [doc['kind'] for doc in self.docs])
        self.assertNotIn('superuser', yaml.safe_dump([doc for doc in self.docs if doc['kind'] != 'ConfigMap']).lower())

    def test_flux_entry_waits_on_the_cap_and_nothing_waits_on_it(self):
        acl, limits, function = yaml.safe_load_all((TREE.parent / 'ks.yaml').read_text())
        self.assertEqual(acl['spec']['dependsOn'], [{'name': 'postgres-cluster', 'namespace': 'database'}])
        self.assertEqual(limits['spec']['dependsOn'], [{'name': 'hindsight-backlog-db-acl', 'namespace': 'database'}])
        self.assertEqual((function['kind'], function['metadata']), ('Kustomization', {'name': APP_NAME,
                                                                                      'namespace': 'flux-system'}))
        self.assertEqual(function['spec'], {
            'targetNamespace': 'database', 'commonMetadata': {'labels': POD_LABELS},
            'dependsOn': [{'name': 'hindsight-backlog-role-limits', 'namespace': 'database'}],
            'path': './kubernetes/apps/database/hindsight-backlog/app', 'prune': True,
            'sourceRef': {'kind': 'GitRepository', 'name': 'flux-system', 'namespace': 'flux-system'},
            'wait': True, 'interval': '1h', 'retryInterval': '2m', 'timeout': '6m'})
        # The chain stays one way: nothing outside it waits on any of its entries, and nothing waits on the function.
        chain = {'hindsight-backlog-db-acl', 'hindsight-backlog-role-limits', APP_NAME}
        for path in (ROOT / 'kubernetes').rglob('ks.yaml'):
            for doc in yaml.safe_load_all(path.read_text()):
                name = (doc or {}).get('metadata', {}).get('name')
                depends = {entry['name'] for entry in (doc or {}).get('spec', {}).get('dependsOn', [])}
                with self.subTest(path=path.relative_to(ROOT), name=name):
                    self.assertNotIn(APP_NAME, depends)
                    if name not in chain:
                        self.assertEqual(depends & chain, set())


class WorkflowTests(unittest.TestCase):
    TEXT = WORKFLOW.read_text()

    def test_triggers_on_every_operational_file_of_the_function_tree_and_its_proofs(self):
        # PyYAML reads the bare `on` key as boolean true.
        triggers = yaml.safe_load(self.TEXT)[True]['pull_request']['paths']
        files = sorted(path.relative_to(ROOT).as_posix() for path in TREE.iterdir() if path.suffix != '.md')
        self.assertEqual(len(files), 4)
        for path in files + ['kubernetes/apps/database/hindsight-backlog/ks.yaml',
                             'tests/hindsight-observability/test_backlog_function_activation.py',
                             'tests/hindsight-observability/check_postgres_backlog_function_activation.py']:
            self.assertEqual(triggers.count(path), 1, path)

    def test_runs_the_unit_tests_and_the_native_proof_on_the_existing_tls_fixture(self):
        self.assertEqual(self.TEXT.count('      - run: python -m unittest discover -s tests/hindsight-observability '
                                         '-p test_backlog_function_activation.py -v\n'), 1)
        step = ('      - run: |\n'
                '          docker exec hindsight-tls-fixture cat /tls/server.crt > "$PGSSLROOTCERT"\n'
                '          python tests/hindsight-observability/check_postgres_backlog_function_activation.py\n'
                '        env:\n'
                "          HINDSIGHT_BACKLOG_FIXTURE: '1'\n"
                '          PGHOST: 127.0.0.1\n'
                "          PGPORT: '5433'\n"
                '          PGSSLROOTCERT: ${{ runner.temp }}/hindsight-fixture-ca.crt\n')
        self.assertEqual(self.TEXT.count(step), 1)
        limits = '      - run: python tests/hindsight-observability/check_postgres_backlog_role_limits.py\n'
        self.assertEqual(self.TEXT.count(limits), 1)
        self.assertLess(self.TEXT.index('-p 127.0.0.1:5433:5432'), self.TEXT.index(limits))
        self.assertLess(self.TEXT.index(limits), self.TEXT.index(step))
        self.assertLess(self.TEXT.index(step), self.TEXT.index('Remove TLS PostgreSQL fixture'))
        self.assertEqual(self.TEXT.count('check_postgres_backlog_function_activation.py'), 2)


class FixtureSafetyTests(unittest.TestCase):
    SAFE = {'HINDSIGHT_BACKLOG_FIXTURE': '1', 'PGHOST': '127.0.0.1', 'PGPORT': '5433', 'PGUSER': 'postgres',
            'PGSSLROOTCERT': '/tmp/fixture-ca.crt', 'PATH': '/usr/bin'}

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
                       {'PGUSER': 'hindsight_backlog_metrics'}, {'PGSSLROOTCERT': ''}):
            environ = {key: value for key, value in {**self.SAFE, **change}.items() if value != ''}
            with self.subTest(change=change), mock.patch.object(NATIVE.subprocess, 'run', connect):
                self.assertTrue(self.main_exit(environ).startswith('refused:'))

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
        with mock.patch.object(NATIVE.FUNCTION, 'psql', return_value=down):
            self.assertEqual(self.main_exit(self.SAFE), 'native TLS PostgreSQL fixture unavailable')

    def test_apply_feeds_the_committed_bytes_as_the_owner_over_verify_full(self):
        env = NATIVE.HARDENING.checker_env({**self.SAFE, 'PGSSLMODE': 'disable', 'PGOPTIONS': '-c ssl=off'})
        for user, expected in ((NATIVE.OWNER, 'hindsight'), (None, 'postgres')):
            with self.subTest(user=user), mock.patch.object(NATIVE.subprocess, 'run') as run:
                NATIVE.apply(env, user)
            command, kwargs = run.call_args[0][0], run.call_args[1]
            self.assertEqual(command, ['psql', '-X', '-q', '-A', '-t', '-w', '-v', 'ON_ERROR_STOP=1',
                                       '-v', 'VERBOSITY=verbose', '-f', '-'])
            self.assertEqual(kwargs['input'], SQL)
            self.assertEqual({key: kwargs['env'].get(key) for key in ('PGSSLMODE', 'PGSSLROOTCERT', 'PGDATABASE',
                                                                      'PGUSER', 'PGOPTIONS')},
                             {'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tmp/fixture-ca.crt',
                              'PGDATABASE': 'hindsight', 'PGUSER': expected, 'PGOPTIONS': None})

    def test_checker_never_names_the_real_service_or_its_credentials(self):
        source = pathlib.Path(NATIVE.__file__).read_text()
        for real in ('postgres-rw', 'svc.cluster.local', 'hindsight-db-credentials', 'postgres-superuser'):
            self.assertNotIn(real, source)

    def test_synthetic_tables_are_the_function_proof_rows_owned_by_the_owner(self):
        bootstrap = NATIVE.FUNCTION.bootstrap_sql().splitlines()
        tables = NATIVE.TABLES.splitlines()
        self.assertEqual(len(bootstrap) - len(tables), 4)
        self.assertEqual([line for line in tables if line.startswith(('CREATE ROLE', 'GRANT'))], [])
        self.assertEqual(NATIVE.TABLES.count('CREATE UNLOGGED TABLE public.async_operations ('), 1)
        self.assertEqual(NATIVE.TABLES.count('CREATE TABLE public.async_operations'), 0)
        self.assertIn("INSERT INTO public.alembic_version VALUES ('d1e2f3a4b5c6');", tables)
        self.assertEqual(NATIVE.CATALOG.count(f"has_function_privilege('{NATIVE.APP}', p.oid, 'EXECUTE')"), 1)
        self.assertNotIn(NATIVE.FUNCTION.OTHER, NATIVE.CATALOG)

    def test_spill_sizes_bracket_the_cap_and_the_cap_is_the_committed_one(self):
        self.assertEqual(NATIVE.CAP_BYTES, int(NATIVE.READER_SESSION[2]) * 1024)
        self.assertEqual(NATIVE.READER_SESSION[:3], ['128MB', 'user', '131072'])
        self.assertIn("SET temp_file_limit = '128MB';", NATIVE.LIMITS.SQL.read_text())
        self.assertEqual(len(NATIVE.SPILL_TYPE), NATIVE.SPILL_WIDTH)
        # The control stays well under the cap; the full set carries more than 1.1 times it in the wide column alone.
        self.assertLess(NATIVE.SPILL_UNDER * 1100, NATIVE.CAP_BYTES // 4)
        self.assertGreater(NATIVE.SPILL_OVER * NATIVE.SPILL_WIDTH, NATIVE.CAP_BYTES * 11 // 10)
        self.assertIn("'processing'", NATIVE.SPILL_ROWS)
        self.assertTrue(NATIVE.SPILL_CALL.startswith("SET work_mem = '64kB'; "))
        self.assertTrue(NATIVE.CANCEL_CALL.endswith('SET statement_timeout = 1; ' + NATIVE.CALL))

    def test_fixed_unknown_accepts_only_the_fixed_message(self):
        fixed = 'psql:<stdin>:1: ERROR:  HSB01: hindsight backlog unknown\nCONTEXT:  PL/pgSQL function\n'
        cases = ((3, '', fixed, False), (0, '', fixed, True), (3, '{}', fixed, True),
                 (3, '', fixed + 'DETAIL:  Key (bank_id)\n', True), (3, '', fixed + 'HINT:  No operator\n', True),
                 (3, '', fixed + 'ERROR:  53400: temporary file size exceeds temp_file_limit\n', True),
                 (3, '', fixed.replace('unknown', 'doc-a'), True))
        for returncode, stdout, stderr, leaks in cases:
            result = mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)
            with self.subTest(returncode=returncode, stdout=stdout, stderr=stderr), \
                    (self.assertRaises(NATIVE.Failed) if leaks else contextlib.nullcontext()):
                NATIVE.fixed_unknown(result, 'case')


if __name__ == '__main__':
    unittest.main()
