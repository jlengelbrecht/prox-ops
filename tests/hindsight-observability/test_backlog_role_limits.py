"""The reader's temp_file_limit cap is one guarded ALTER ROLE applied by one superuser Job, and its native TLS
proof refuses anything but an empty fixture."""
import contextlib
import hashlib
import importlib.util
import json
import pathlib
import re
import unittest
from unittest import mock

import yaml

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
APP_NAME = 'hindsight-backlog-role-limits'
POD_LABELS = {'app.kubernetes.io/name': APP_NAME}
# The only lines that may name the backlog function or its schema: the grant shape the guard accepts.
ACCEPTED_GRANTS = ["      'function hindsight_metrics.runnable_backlog() owner EXECUTE from owner',",
                   "      'schema hindsight_metrics owner USAGE from owner'] END) THEN"]


def rendered():
    return [doc for doc in yaml.safe_load_all(ACL.subprocess.run(
        [ACL.KUSTOMIZE, 'build', str(TREE)], check=True, capture_output=True, text=True).stdout) if doc]


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
        cases = {**NATIVE.PRE_APPLY_REFUSALS, **NATIVE.METRICS_REFUSALS, **NATIVE.POST_APPLY_REFUSALS,
                 'revoke': (None, None, None, NATIVE.REVOKE_MISSING), 'hba': (None, None, None, NATIVE.HBA_MISSING),
                 'held session': (None, None, None, NATIVE.SESSION_REFUSAL)}
        for label, (_, _, _, message) in cases.items():
            with self.subTest(label):
                self.assertTrue(any(message in text for text in raised[:-1]), message)

    def test_accepted_defaults_are_exactly_the_five_the_native_proof_writes(self):
        literal = re.search(r"NOT IN \('\{\}', ARRAY\[('default[^]]*)\]\)", SQL).group(1)
        self.assertEqual(re.findall(r"'([^']*)'", literal), sorted(NATIVE.DEFAULTS))
        self.assertEqual(len(set(entry.split('=')[0] for entry in NATIVE.DEFAULTS)), 5)

    def test_sql_is_mapped_only_by_its_own_tree_and_names_the_function_only_in_the_accepted_grants(self):
        self.assertEqual(sorted(path.name for path in TREE.iterdir()),
                         ['README.md', 'job.yaml', 'kustomization.yaml', 'networkpolicy.yaml', 'role-limits.sql'])
        mapping = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'kubernetes').rglob('*.y*ml')
                   if 'role-limits' in path.read_text(errors='replace')}
        self.assertEqual(mapping, {f'kubernetes/apps/database/hindsight-backlog/{name}' for name in (
            'ks.yaml', 'role-limits/kustomization.yaml', 'role-limits/job.yaml', 'role-limits/networkpolicy.yaml')})
        named = [line for line in SQL.splitlines() if re.search(r'runnable|hindsight_metrics|backlog\(', line)]
        self.assertEqual(named, ACCEPTED_GRANTS)
        self.assertEqual(re.findall(r'runnable-backlog|db-acl|\bGRANT\b|\bCREATE (?:SCHEMA|FUNCTION)', SQL), [])

    def test_accepted_grants_are_the_function_ddl_grants_without_grant_option(self):
        ddl = (ROOT / 'kubernetes/apps/database/hindsight-backlog/app/runnable-backlog.sql').read_text()
        self.assertIn('GRANT USAGE ON SCHEMA hindsight_metrics TO hindsight_backlog_metrics;', ddl)
        self.assertIn('GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO hindsight_backlog_metrics;',
                      ddl)
        self.assertNotIn('GRANT OPTION', ddl)
        # The native positive case grants exactly the same, as the owner.
        self.assertEqual(re.findall(r'(?m)^GRANT .*$', NATIVE.METRICS),
                         ['GRANT USAGE ON SCHEMA hindsight_metrics TO hindsight_backlog_metrics;',
                          'GRANT EXECUTE ON FUNCTION hindsight_metrics.runnable_backlog() TO hindsight_backlog_metrics;'])
        # Every way the shape can differ is a native refusal: object, owner, privilege, grant option, PUBLIC.
        self.assertEqual(sorted(NATIVE.METRICS_REFUSALS), sorted([
            'reader CREATE on the function schema', 'PUBLIC CREATE on the function schema',
            'schema USAGE with grant option', 'function EXECUTE with grant option',
            'another function in the schema granted', 'same name with an argument in its place',
            'function schema owned by another role']))
        self.assertIn('two owner grants on other objects', NATIVE.PRE_APPLY_REFUSALS)
        self.assertIn('PUBLIC CREATE on schema public', NATIVE.PRE_APPLY_REFUSALS)

    def test_hba_guard_claims_presence_and_order_only(self):
        raised = re.findall(r"RAISE EXCEPTION '([^']*)'", SQL)
        self.assertIn('pg_hba lacks the reader TLS rule ahead of the reader reject rule, or has an error', raised)
        self.assertNotRegex(SQL.lower(), r'confine')
        # The fixture puts the production rules ahead of its own catch-all, and the earlier permissive rule
        # case shows the guard passing while that rule decides reader logins.
        self.assertEqual(NATIVE.READER_HBA, ['hostssl hindsight hindsight_backlog_metrics all scram-sha-256',
                                             'host all hindsight_backlog_metrics all reject'])
        release = yaml.safe_load((ROOT / 'kubernetes/apps/database/cluster/app/helmrelease.yaml').read_text())
        self.assertEqual(release['spec']['values']['cluster']['postgresql']['pg_hba'], NATIVE.READER_HBA)
        self.assertEqual(NATIVE.PERMISSIVE_HBA, 'host all hindsight_backlog_metrics all trust')

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


class ActivationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = rendered()
        cls.job = ACL.one(cls.docs, 'Job')
        cls.pod = cls.job['spec']['template']['spec']
        cls.policy = ACL.one(cls.docs, 'CiliumNetworkPolicy')

    def test_renders_one_configmap_one_job_and_one_policy_deterministically(self):
        self.assertEqual(sorted(doc['kind'] for doc in self.docs), ['CiliumNetworkPolicy', 'ConfigMap', 'Job'])
        self.assertEqual(rendered(), self.docs)
        config = ACL.one(self.docs, 'ConfigMap')
        self.assertEqual(config['metadata']['name'], APP_NAME)
        self.assertEqual(config['data'], {'role-limits.sql': SQL})

    def test_job_name_is_the_sql_hash_and_a_revision_and_nothing_forces_a_rerun(self):
        digest = hashlib.sha256(NATIVE.SQL.read_bytes()).hexdigest()
        self.assertEqual(self.job['metadata'], {'name': f'{APP_NAME}-{digest[:12]}-r1'})
        spec = {key: value for key, value in self.job['spec'].items() if key != 'template'}
        # No ttlSecondsAfterFinished: the completed Job stays as the record Flux waits on.
        self.assertEqual(spec, {'backoffLimit': 2, 'activeDeadlineSeconds': 300, 'parallelism': 1, 'completions': 1})
        self.assertEqual(self.job['spec']['template']['metadata'], {'labels': POD_LABELS})
        self.assertNotIn('kustomize.toolkit.fluxcd.io/force', yaml.safe_dump(self.docs))

    def test_pod_runs_the_pinned_client_unprivileged_without_a_token(self):
        pod = self.pod
        self.assertEqual({key: pod[key] for key in pod if key not in ('containers', 'volumes')}, {
            'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 26, 'runAsGroup': 26,
                                'seccompProfile': {'type': 'RuntimeDefault'}}})
        [container] = pod['containers']
        self.assertEqual(container['image'], ACL.IMAGE)
        self.assertEqual(container['securityContext'], {'allowPrivilegeEscalation': False,
                                                        'readOnlyRootFilesystem': True,
                                                        'capabilities': {'drop': ['ALL']}})
        self.assertEqual(sorted(container), ['command', 'env', 'image', 'name', 'resources', 'securityContext',
                                             'volumeMounts'])
        self.assertEqual(container['resources'], {'requests': {'cpu': '10m', 'memory': '32Mi'},
                                                  'limits': {'memory': '128Mi'}})

    def test_psql_runs_the_mounted_file_with_fixed_argv_over_verified_tls_as_the_superuser(self):
        [container] = self.pod['containers']
        self.assertEqual(container['command'], ['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-f', '/sql/role-limits.sql'])
        env = {item['name']: item.get('value', item.get('valueFrom')) for item in container['env']}
        self.assertEqual(len(env), len(container['env']))
        self.assertEqual(env, {
            'PGHOST': 'postgres-rw.database.svc.cluster.local', 'PGPORT': '5432', 'PGDATABASE': 'hindsight',
            'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tls/ca.crt', 'PGCONNECT_TIMEOUT': '10',
            'PGAPPNAME': APP_NAME,
            'PGUSER': {'secretKeyRef': {'name': 'postgres-superuser', 'key': 'username'}},
            'PGPASSWORD': {'secretKeyRef': {'name': 'postgres-superuser', 'key': 'password'}}})
        self.assertEqual(container['volumeMounts'], [{'name': 'sql', 'mountPath': '/sql', 'readOnly': True},
                                                     {'name': 'ca', 'mountPath': '/tls', 'readOnly': True}])
        self.assertEqual(self.pod['volumes'], [
            {'name': 'sql', 'configMap': {'name': APP_NAME,
                                          'items': [{'key': 'role-limits.sql', 'path': 'role-limits.sql'}]}},
            {'name': 'ca', 'secret': {'secretName': 'postgres-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}])
        # The cluster's existing superuser Secret in the same namespace, and the CA's public half; no Secret copy.
        external = yaml.safe_load((ACL.DATABASE / 'cluster/app/externalsecret.yaml').read_text())
        self.assertEqual((external['metadata']['namespace'], external['spec']['target']['name']),
                         ('database', 'postgres-superuser'))
        release = yaml.safe_load((ACL.DATABASE / 'cluster/app/helmrelease.yaml').read_text())
        self.assertEqual(release['spec']['values']['cluster']['superuserSecret'], 'postgres-superuser')
        self.assertEqual(ACL.secret_names(self.docs), {'postgres-ca', 'postgres-superuser'})
        self.assertNotIn('Secret', [doc['kind'] for doc in self.docs])

    def test_policy_admits_only_dns_and_the_cnpg_instances(self):
        cluster = yaml.safe_load((ACL.DATABASE / 'cluster/app/networkpolicy.yaml').read_text())['spec']
        dns = cluster['egress'][0]
        self.assertEqual(dns['toEndpoints'], [{'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system',
                                                               'k8s-app': 'kube-dns'}}])
        postgres = {'io.kubernetes.pod.namespace': 'database', **cluster['endpointSelector']['matchLabels']}
        self.assertEqual(self.policy['metadata'], {'name': APP_NAME})
        self.assertEqual(self.policy['spec'], {
            'endpointSelector': {'matchLabels': POD_LABELS},
            'enableDefaultDeny': {'ingress': True, 'egress': True},
            'egress': [dns, {'toEndpoints': [{'matchLabels': postgres}],
                             'toPorts': [{'ports': [{'port': '5432', 'protocol': 'TCP'}]}]}]})

    def test_flux_entry_waits_on_the_revoke_and_nothing_waits_on_it(self):
        acl, limits = yaml.safe_load_all((TREE.parent / 'ks.yaml').read_text())
        self.assertEqual(acl['metadata']['name'], 'hindsight-backlog-db-acl')
        self.assertEqual(acl['spec']['dependsOn'], [{'name': 'postgres-cluster', 'namespace': 'database'}])
        self.assertEqual((limits['kind'], limits['metadata']), ('Kustomization', {'name': APP_NAME,
                                                                                  'namespace': 'flux-system'}))
        self.assertEqual(limits['spec'], {
            'targetNamespace': 'database', 'commonMetadata': {'labels': POD_LABELS},
            'dependsOn': [{'name': 'hindsight-backlog-db-acl', 'namespace': 'database'}],
            'path': './kubernetes/apps/database/hindsight-backlog/role-limits', 'prune': True,
            'sourceRef': {'kind': 'GitRepository', 'name': 'flux-system', 'namespace': 'flux-system'},
            'wait': True, 'interval': '1h', 'retryInterval': '2m', 'timeout': '6m'})
        for path in (ROOT / 'kubernetes').rglob('ks.yaml'):
            for doc in yaml.safe_load_all(path.read_text()):
                with self.subTest(path=path.relative_to(ROOT)):
                    depends = [entry['name'] for entry in (doc or {}).get('spec', {}).get('dependsOn', [])]
                    self.assertNotIn(APP_NAME, depends)
                    if (doc or {}).get('metadata', {}).get('name') != APP_NAME:
                        self.assertNotIn('hindsight-backlog-db-acl', depends)


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
