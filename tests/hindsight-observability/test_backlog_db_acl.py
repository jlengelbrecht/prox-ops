"""The database TEMP revoke is static SQL applied by one owner Job, and its native TLS proof refuses anything
but an empty fixture."""
import contextlib
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    'backlog_hardening_native', ROOT / 'tests/hindsight-observability/check_postgres_backlog_hardening.py')
NATIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE)
SQL = NATIVE.SQL.read_text()
# The reviewed file; any edit is a new review, a new hash and so a new Job.
SQL_SHA256 = '3a358f9d8eebea39d529ec0a3de4b201096bafc5f77f53e29ade27911156934b'
ACL_TREE = NATIVE.SQL.parent
DATABASE = ROOT / 'kubernetes/apps/database'
KUSTOMIZE = os.environ.get('HINDSIGHT_KUSTOMIZE', 'kustomize')
IMAGE = ('ghcr.io/cloudnative-pg/postgresql@sha256:'
         '368c1a13935ff8850d76cfb427d5c29877a74de0531e198a7a683d4cfb97260b')
POD_LABELS = {'app.kubernetes.io/name': 'hindsight-backlog-db-acl'}
REVOKE = 'REVOKE TEMPORARY ON DATABASE hindsight FROM PUBLIC;'


def statements(sql):
    """Non-comment SQL with string literals blanked, so names inside messages and privilege names do not count."""
    code = '\n'.join(line for line in sql.splitlines() if not line.lstrip().startswith('--'))
    return re.sub(r"'[^']*'", "''", code)


def privilege_change_violations(sql):
    """Every way besides the one top-level REVOKE line that the file could change a privilege."""
    code = statements(sql)
    found = [line for line in code.splitlines() if re.search(r'\b(revoke|grant)\b', line, re.I)]
    violations = [] if found == [REVOKE] else [f'privilege statements {found}']
    # Dynamic SQL could run any statement from a literal, which statements() blanks.
    violations += re.findall(r'\b(?:execute|perform|format|dblink\w*|query_to_xml\w*|gexec)\b', code, re.I)
    if '\\' in code:
        violations.append('backslash')
    tags = re.findall(r'\$\w*\$', code)
    if sorted(tags) != ['$guard$', '$guard$', '$verify$', '$verify$']:
        violations.append(f'dollar quotes {tags}')
    return violations


def rendered():
    return [doc for doc in yaml.safe_load_all(subprocess.run(
        [KUSTOMIZE, 'build', str(ACL_TREE)], check=True, capture_output=True, text=True).stdout) if doc]


def secret_names(node):
    """Every Secret a manifest tree references, by env, envFrom or volume."""
    if isinstance(node, list):
        return set().union(*map(secret_names, node))
    if not isinstance(node, dict):
        return set()
    found = {node[key]['name'] for key in ('secretKeyRef', 'secretRef') if key in node}
    if isinstance(node.get('secret'), dict):
        found.add(node['secret']['secretName'])
    return found.union(*map(secret_names, node.values()))


def one(docs, kind):
    matching = [doc for doc in docs if doc['kind'] == kind]
    assert len(matching) == 1, (kind, len(matching))
    return matching[0]


class SqlTests(unittest.TestCase):
    def test_one_guarded_transaction_that_only_revokes_public_temporary(self):
        top = [line for line in SQL.splitlines() if not line.startswith((' ', '--', '$'))
               and line not in ('DECLARE', 'BEGIN', 'END')]
        self.assertEqual(top, [
            'BEGIN;', 'SET LOCAL search_path = pg_catalog, pg_temp;', "SET LOCAL lock_timeout = '5s';",
            "SET LOCAL statement_timeout = '30s';", 'DO $guard$', REVOKE, 'DO $verify$', 'COMMIT;'])
        self.assertEqual(hashlib.sha256(NATIVE.SQL.read_bytes()).hexdigest(), SQL_SHA256)
        self.assertEqual(privilege_change_violations(SQL), [])
        code = statements(SQL).lower()
        self.assertEqual(re.findall(r'\b(grant|create|alter|drop|insert|update|delete|truncate|copy|execute|'
                                    r'reset|set role|set session|comment|security)\b', code), [])
        # No psql meta-commands or variables: the Job passes only ON_ERROR_STOP.
        self.assertFalse(any(line.lstrip().startswith('\\') for line in SQL.splitlines()))
        self.assertEqual(re.findall(r'(?<!:):(?![:=])\S', code), [])

    def test_any_other_privilege_change_is_caught(self):
        guard_body = ' IF current_database() <> '
        mutations = {
            'nested copy': SQL.replace(guard_body, f' {REVOKE}\n{guard_body}', 1),
            'second top-level revoke': SQL.replace(REVOKE, f'{REVOKE}\n{REVOKE}', 1),
            'lowercase': SQL.replace(REVOKE, REVOKE.lower(), 1),
            'all privileges': SQL.replace(REVOKE, 'REVOKE ALL ON DATABASE hindsight FROM PUBLIC;', 1),
            'extra grantee': SQL.replace(REVOKE, REVOKE[:-1] + ', app;', 1),
            'grant': SQL.replace('COMMIT;', 'GRANT TEMPORARY ON DATABASE hindsight TO app;\nCOMMIT;', 1),
            'dynamic literal': SQL.replace(guard_body, " EXECUTE 'REVOKE CONNECT ON DATABASE hindsight FROM PUBLIC';\n"
                                           + guard_body, 1),
            'dollar-quoted block': SQL.replace('COMMIT;', 'DO $x$ BEGIN NULL; END $x$;\nCOMMIT;', 1),
            'psql gexec': SQL.replace('COMMIT;', "SELECT 'REVOKE CONNECT ON DATABASE hindsight FROM PUBLIC' \\gexec\n"
                                      'COMMIT;', 1),
            'perform': SQL.replace(guard_body, " PERFORM dblink_exec('x', 'y');\n" + guard_body, 1),
        }
        for label, mutated in mutations.items():
            with self.subTest(label):
                self.assertNotEqual(mutated, SQL)
                self.assertNotEqual(privilege_change_violations(mutated), [])

    def test_every_native_refusal_names_a_guard_the_sql_raises(self):
        raised = re.findall(r"RAISE EXCEPTION '([^']*)'", SQL)
        cases = {**NATIVE.PRE_APPLY_REFUSALS, **NATIVE.POST_APPLY_REFUSALS,
                 'held session': (None, None, None, NATIVE.SESSION_REFUSAL)}
        for label, (_, _, _, message) in cases.items():
            with self.subTest(label):
                self.assertTrue(any(message in text for text in raised[:-1]), message)

    def test_sql_is_mapped_only_by_its_own_tree_and_apart_from_the_function(self):
        self.assertEqual(sorted(path.name for path in ACL_TREE.iterdir()),
                         ['db-acl.sql', 'job.yaml', 'kustomization.yaml', 'networkpolicy.yaml'])
        mapping = {path.relative_to(ROOT).as_posix() for path in (ROOT / 'kubernetes').rglob('*.y*ml')
                   if 'db-acl' in path.read_text(errors='replace')}
        self.assertEqual(mapping, {f'kubernetes/apps/database/hindsight-backlog/{name}' for name in (
            'ks.yaml', 'db-acl/kustomization.yaml', 'db-acl/job.yaml', 'db-acl/networkpolicy.yaml')})
        self.assertEqual(re.findall(r'runnable[-_]backlog|hindsight_metrics|temp_file_limit', SQL), [])

    def test_declared_roster_is_the_managed_roles_plus_cnpg_builtins(self):
        roster = re.search(r'rolname NOT IN \(([^)]*)\)', SQL).group(1)
        release = yaml.safe_load((DATABASE / 'cluster/app/helmrelease.yaml').read_text())
        managed = [role['name'] for role in release['spec']['values']['cluster']['roles']]
        self.assertEqual(sorted(re.findall(r"'(\w+)'", roster)), sorted(managed + ['app', 'streaming_replica']))

    def test_workflow_triggers_on_every_file_the_activation_depends_on(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        for path in ('kubernetes/apps/database/hindsight-backlog/ks.yaml',
                     'kubernetes/apps/database/hindsight-backlog/db-acl/kustomization.yaml',
                     'kubernetes/apps/database/hindsight-backlog/db-acl/job.yaml',
                     'kubernetes/apps/database/hindsight-backlog/db-acl/networkpolicy.yaml',
                     'kubernetes/apps/database/kustomization.yaml',
                     'kubernetes/apps/database/cluster/app/helmrelease.yaml',
                     'kubernetes/apps/database/cluster/app/networkpolicy.yaml'):
            self.assertEqual(workflow.count('      - ' + path + '\n'), 1, path)

    def test_workflow_runs_the_native_proof_against_the_tls_fixture(self):
        workflow = (ROOT / '.github/workflows/hindsight-baseline.yaml').read_text()
        step = ('      - run: python tests/hindsight-observability/check_postgres_backlog_hardening.py\n'
                '        env:\n'
                "          HINDSIGHT_BACKLOG_FIXTURE: '1'\n"
                '          PGHOST: 127.0.0.1\n'
                "          PGPORT: '5433'\n")
        self.assertEqual(workflow.count(step), 1)
        self.assertLess(workflow.index('-p 127.0.0.1:5433:5432'), workflow.index(step))


class ActivationManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = rendered()
        cls.job = one(cls.docs, 'Job')
        cls.pod = cls.job['spec']['template']['spec']
        cls.policy = one(cls.docs, 'CiliumNetworkPolicy')

    def test_renders_one_configmap_one_job_and_one_policy_deterministically(self):
        self.assertEqual(sorted(doc['kind'] for doc in self.docs), ['CiliumNetworkPolicy', 'ConfigMap', 'Job'])
        self.assertEqual(rendered(), self.docs)
        config = one(self.docs, 'ConfigMap')
        self.assertEqual(config['metadata']['name'], 'hindsight-backlog-db-acl')
        self.assertEqual(config['data'], {'db-acl.sql': SQL})

    def test_job_name_is_the_sql_hash_and_a_revision_and_nothing_forces_a_rerun(self):
        digest = hashlib.sha256(NATIVE.SQL.read_bytes()).hexdigest()
        self.assertEqual(self.job['metadata'], {'name': f'hindsight-backlog-db-acl-{digest[:12]}-r1'})
        spec = {key: value for key, value in self.job['spec'].items() if key != 'template'}
        self.assertEqual(spec, {'backoffLimit': 2, 'activeDeadlineSeconds': 300, 'parallelism': 1, 'completions': 1})
        self.assertEqual(self.job['spec']['template']['metadata'], {'labels': POD_LABELS})

    def test_pod_runs_the_pinned_client_unprivileged_without_a_token(self):
        pod = self.pod
        self.assertEqual({key: pod[key] for key in pod if key not in ('containers', 'volumes')}, {
            'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 26, 'runAsGroup': 26,
                                'seccompProfile': {'type': 'RuntimeDefault'}}})
        [container] = pod['containers']
        self.assertEqual(container['image'], IMAGE)
        self.assertEqual(container['securityContext'], {'allowPrivilegeEscalation': False,
                                                        'readOnlyRootFilesystem': True,
                                                        'capabilities': {'drop': ['ALL']}})
        self.assertEqual(sorted(container), ['command', 'env', 'image', 'name', 'resources', 'securityContext',
                                             'volumeMounts'])

    def test_psql_runs_the_mounted_file_with_fixed_argv_over_verified_tls_as_the_owner(self):
        [container] = self.pod['containers']
        self.assertEqual(container['command'], ['psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-f', '/sql/db-acl.sql'])
        env = {item['name']: item.get('value', item.get('valueFrom')) for item in container['env']}
        self.assertEqual(len(env), len(container['env']))
        self.assertEqual(env, {
            'PGHOST': 'postgres-rw.database.svc.cluster.local', 'PGPORT': '5432', 'PGDATABASE': 'hindsight',
            'PGSSLMODE': 'verify-full', 'PGSSLROOTCERT': '/tls/ca.crt', 'PGCONNECT_TIMEOUT': '10',
            'PGAPPNAME': 'hindsight-backlog-db-acl',
            'PGUSER': {'secretKeyRef': {'name': 'hindsight-db-credentials', 'key': 'username'}},
            'PGPASSWORD': {'secretKeyRef': {'name': 'hindsight-db-credentials', 'key': 'password'}}})
        self.assertEqual(container['volumeMounts'], [{'name': 'sql', 'mountPath': '/sql', 'readOnly': True},
                                                     {'name': 'ca', 'mountPath': '/tls', 'readOnly': True}])
        self.assertEqual(self.pod['volumes'], [
            {'name': 'sql', 'configMap': {'name': 'hindsight-backlog-db-acl',
                                          'items': [{'key': 'db-acl.sql', 'path': 'db-acl.sql'}]}},
            {'name': 'ca', 'secret': {'secretName': 'postgres-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}])
        # The owner Secret is the database tree's existing one, and the only other Secret is the CA's public half.
        external = yaml.safe_load((DATABASE / 'cluster/app/externalsecret-hindsight.yaml').read_text())
        self.assertEqual(external['spec']['target']['name'], 'hindsight-db-credentials')
        self.assertEqual(secret_names(self.docs), {'postgres-ca', 'hindsight-db-credentials'})
        manifests = yaml.safe_dump([doc for doc in self.docs if doc['kind'] != 'ConfigMap'])
        self.assertNotIn('superuser', manifests.lower())

    def test_policy_admits_only_dns_and_the_cnpg_instances(self):
        cluster = yaml.safe_load((DATABASE / 'cluster/app/networkpolicy.yaml').read_text())['spec']
        dns = cluster['egress'][0]
        self.assertEqual(dns['toEndpoints'], [{'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system',
                                                               'k8s-app': 'kube-dns'}}])
        postgres = {'io.kubernetes.pod.namespace': 'database', **cluster['endpointSelector']['matchLabels']}
        self.assertEqual(self.policy['metadata'], {'name': 'hindsight-backlog-db-acl'})
        self.assertEqual(self.policy['spec'], {
            'endpointSelector': {'matchLabels': POD_LABELS},
            'enableDefaultDeny': {'ingress': True, 'egress': True},
            'egress': [dns, {'toEndpoints': [{'matchLabels': postgres}],
                             'toPorts': [{'ports': [{'port': '5432', 'protocol': 'TCP'}]}]}]})
        # The instances already admit 5432 from namespace database, so their policy needs no change.
        self.assertIn({'fromEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'database'}}],
                       'toPorts': [{'ports': [{'port': '5432', 'protocol': 'TCP'},
                                              {'port': '8000', 'protocol': 'TCP'},
                                              {'port': '9187', 'protocol': 'TCP'}]}]}, cluster['ingress'])

    def test_flux_entry_waits_on_postgres_cluster_and_nothing_waits_on_it(self):
        [ks] = list(yaml.safe_load_all((ACL_TREE.parent / 'ks.yaml').read_text()))
        self.assertEqual((ks['kind'], ks['metadata']['name']), ('Kustomization', 'hindsight-backlog-db-acl'))
        spec = ks['spec']
        self.assertEqual({key: spec[key] for key in ('targetNamespace', 'path', 'prune', 'wait', 'dependsOn')}, {
            'targetNamespace': 'database', 'path': './kubernetes/apps/database/hindsight-backlog/db-acl',
            'prune': True, 'wait': True, 'dependsOn': [{'name': 'postgres-cluster', 'namespace': 'database'}]})
        self.assertNotIn('force', spec)
        root = yaml.safe_load((DATABASE / 'kustomization.yaml').read_text())
        self.assertEqual((root['namespace'], root['resources'].count('hindsight-backlog/ks.yaml')), ('database', 1))
        self.assertEqual(root['resources'].index('hindsight-backlog/ks.yaml'),
                         root['resources'].index('cluster/ks.yaml') + 1)
        for path in (ROOT / 'kubernetes').rglob('ks.yaml'):
            for doc in yaml.safe_load_all(path.read_text()):
                with self.subTest(path=path.relative_to(ROOT)):
                    depends = (doc or {}).get('spec', {}).get('dependsOn', [])
                    self.assertNotIn('hindsight-backlog-db-acl', [entry['name'] for entry in depends])


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

    def test_owner_promoted_to_superuser_is_refused_and_reset_on_the_fixture_owner_only(self):
        arrange, undo, run_as, message = NATIVE.PRE_APPLY_REFUSALS['owner promoted to superuser']
        self.assertEqual((arrange, undo), ('ALTER ROLE hindsight SUPERUSER;', 'ALTER ROLE hindsight NOSUPERUSER;'))
        self.assertEqual((run_as, message), (NATIVE.APPLY, 'apply as a non-superuser session'))
        # The checker creates the owner itself and refuses to run as it, so only that fake is ever altered.
        self.assertIn(f'CREATE ROLE {NATIVE.OWNER} LOGIN;', NATIVE.BOOTSTRAP_ROLES)
        self.assertIn(NATIVE.OWNER, NATIVE.FIXTURE_ROLES)


if __name__ == '__main__':
    unittest.main()
