"""The collector ships through three Flux entries: its digest-named code ConfigMap, a one-shot qualification Job that
must complete, then the daemon. Every tree is rendered with the real kustomize; nothing here reaches a cluster.

A rendered Job is not a run: the real poll is proven only by the Job completing in the cluster after merge."""
import ast
import hashlib
import importlib.util
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
BACKLOG = ROOT / 'kubernetes/apps/database/hindsight-backlog'
QUALIFICATION, DAEMON = BACKLOG / 'collector-qualification', BACKLOG / 'collector-runtime'
CORE, RUNTIME = BACKLOG / 'collector/collector.py', BACKLOG / 'collector/runtime.py'
WORKFLOW = ROOT / '.github/workflows/hindsight-baseline.yaml'
KUSTOMIZE = os.environ.get('HINDSIGHT_KUSTOMIZE') or shutil.which('kustomize') or 'kustomize'
IMAGE = ('ghcr.io/cloudnative-pg/postgresql@sha256:'
         '368c1a13935ff8850d76cfb427d5c29877a74de0531e198a7a683d4cfb97260b')
DIGEST = hashlib.sha256(CORE.read_bytes() + b'\0' + RUNTIME.read_bytes()).hexdigest()[:12]
CODE = 'hindsight-backlog-collector-code-' + DIGEST
APP, QUAL = 'hindsight-backlog-collector', 'hindsight-backlog-collector-qualification'
READER_SECRET = 'hindsight-backlog-metrics-db-credentials'
SPEC = importlib.util.spec_from_file_location('backlog_collector_deployment_runtime', RUNTIME)
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)

POD = {'automountServiceAccountToken': False, 'enableServiceLinks': False, 'terminationGracePeriodSeconds': 30,
       'securityContext': {'runAsNonRoot': True, 'runAsUser': 26, 'runAsGroup': 26, 'fsGroup': 26,
                           'seccompProfile': {'type': 'RuntimeDefault'}}}
CONTAINER_SECURITY = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                      'capabilities': {'drop': ['ALL']}}
MOUNTS = [{'name': 'code', 'mountPath': '/code', 'readOnly': True},
          {'name': 'credentials', 'mountPath': '/secrets', 'readOnly': True},
          {'name': 'ca', 'mountPath': '/tls', 'readOnly': True}, {'name': 'tmp', 'mountPath': '/tmp'}]
VOLUMES = [
    {'name': 'code', 'configMap': {'name': CODE, 'items': [{'key': name, 'path': name}
                                                           for name in ('collector.py', 'runtime.py')]}},
    {'name': 'credentials', 'secret': {'secretName': READER_SECRET, 'defaultMode': 0o440,
                                       'items': [{'key': name, 'path': name} for name in ('username', 'password')]}},
    {'name': 'ca', 'secret': {'secretName': 'postgres-ca', 'defaultMode': 0o440,
                              'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}},
    {'name': 'tmp', 'emptyDir': {'medium': 'Memory', 'sizeLimit': '16Mi'}}]
RESOURCES = {'requests': {'cpu': '10m', 'memory': '48Mi'}, 'limits': {'memory': '128Mi'}}
DNS = {'toEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}}],
       'toPorts': [{'ports': [{'port': '53', 'protocol': 'UDP'}, {'port': '53', 'protocol': 'TCP'}]}]}
POSTGRES = {'toEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'database', 'cnpg.io/cluster': 'postgres'}}],
            'toPorts': [{'ports': [{'port': '5432', 'protocol': 'TCP'}]}]}
TRIGGERS = [path.relative_to(ROOT).as_posix() for path in (
    CORE, RUNTIME, ROOT / 'tests/hindsight-observability/test_backlog_collector.py',
    ROOT / 'tests/hindsight-observability/test_backlog_collector_runtime.py', BACKLOG / 'kustomization.yaml',
    BACKLOG / 'collector.ks.yaml', *(QUALIFICATION / name for name in ('kustomization.yaml', 'job.yaml',
                                                                       'networkpolicy.yaml')),
    *(DAEMON / name for name in ('kustomization.yaml', 'deployment.yaml', 'service.yaml', 'servicemonitor.yaml',
                                 'networkpolicy.yaml')),
    pathlib.Path(__file__).resolve(), BACKLOG.parent / 'kustomization.yaml', WORKFLOW)]


def render(tree):
    out = subprocess.run([KUSTOMIZE, 'build', str(tree)], check=True, capture_output=True, text=True, timeout=60)
    docs = [doc for doc in yaml.safe_load_all(out.stdout) if doc]
    kinds = [doc['kind'] for doc in docs]
    assert len(set(kinds)) == len(kinds), kinds
    return {doc['kind']: doc for doc in docs}


def secret_names(node):
    if isinstance(node, list):
        return set().union(*map(secret_names, node))
    if not isinstance(node, dict):
        return set()
    found = {node[key]['name'] for key in ('secretKeyRef', 'secretRef') if key in node}
    if isinstance(node.get('secret'), dict):
        found.add(node['secret']['secretName'])
    return found.union(*map(secret_names, node.values()))


def policy(name, **rules):
    return {'apiVersion': 'cilium.io/v2', 'kind': 'CiliumNetworkPolicy', 'metadata': {'name': name},
            'spec': {'endpointSelector': {'matchLabels': {'app.kubernetes.io/name': name}},
                     'enableDefaultDeny': {'ingress': True, 'egress': True}, **rules, 'egress': [DNS, POSTGRES]}}


def adapter_run(adapter, poll, valid, data):
    """Runs the Job's adapter against a stand-in runtime; the real runtime is only exercised in the cluster."""
    with tempfile.TemporaryDirectory() as folder:
        fake = pathlib.Path(folder) / 'runtime.py'
        fake.write_text('import types\ndef version():\n    return "v"\nclass Collector:\n'
                        '    def __init__(self, version):\n'
                        f'        self.status = types.SimpleNamespace(valid={valid!r})\n'
                        f'        self.snapshot = types.SimpleNamespace(with_data={data!r})\n'
                        f'    def poll(self):\n        {poll}\n')
        pointed = adapter.replace("'/code/runtime.py'", repr(str(fake)))
        return subprocess.run([sys.executable, '-I', '-S', '-c', pointed], capture_output=True, text=True, timeout=30)


class CodeTests(unittest.TestCase):
    def test_root_renders_only_the_immutable_digest_named_code_configmap(self):
        self.assertEqual(yaml.safe_load((BACKLOG / 'kustomization.yaml').read_text()), {
            'apiVersion': 'kustomize.config.k8s.io/v1beta1', 'kind': 'Kustomization',
            'configMapGenerator': [{'name': CODE, 'files': ['collector/collector.py', 'collector/runtime.py']}],
            'generatorOptions': {'disableNameSuffixHash': True, 'immutable': True, 'annotations': {'kustomize.toolkit.fluxcd.io/prune': 'disabled'}}})
        docs = render(BACKLOG)
        self.assertEqual(list(docs), ['ConfigMap'])
        self.assertEqual(docs['ConfigMap'], {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': CODE, 'annotations': {'kustomize.toolkit.fluxcd.io/prune': 'disabled'}},
                                             'immutable': True, 'data': {'collector.py': CORE.read_text(),
                                                                         'runtime.py': RUNTIME.read_text()}})
        self.assertEqual(render(BACKLOG), docs)

    def test_collector_folder_holds_only_the_two_modules(self):
        files = sorted(path.relative_to(CORE.parent).as_posix() for path in CORE.parent.rglob('*')
                       if path.is_file() and '__pycache__' not in path.parts)
        self.assertEqual(files, ['collector.py', 'runtime.py'])

    def test_mounts_match_the_paths_and_port_the_unchanged_runtime_reads(self):
        self.assertEqual(R.core.CREDENTIALS, ('/secrets/username', '/secrets/password'))
        self.assertEqual(R.core.CHILD_ENV['PGSSLROOTCERT'], '/tls/ca.crt')
        self.assertEqual((R.PORT, R.DEADLINE + R.KILL_GRACE + 1.0 < POD['terminationGracePeriodSeconds']), (9810, True))


class FluxTests(unittest.TestCase):
    def test_three_entries_chain_code_then_qualification_then_daemon(self):
        entries = list(yaml.safe_load_all((BACKLOG / 'collector.ks.yaml').read_text()))
        expected = [('hindsight-backlog-collector-code', '', [], '2m'),
                    (QUAL, '/collector-qualification', ['hindsight-backlog-collector-code',
                                                        'hindsight-backlog-function'], '3m'),
                    (APP, '/collector-runtime', ['hindsight-backlog-collector-code', QUAL], '5m')]
        self.assertEqual(len(entries), len(expected))
        for entry, (name, path, depends, timeout) in zip(entries, expected):
            with self.subTest(name=name):
                spec = {'targetNamespace': 'database', 'commonMetadata': {'labels': {'app.kubernetes.io/name': name}},
                        'path': './kubernetes/apps/database/hindsight-backlog' + path, 'prune': True,
                        'sourceRef': {'kind': 'GitRepository', 'name': 'flux-system', 'namespace': 'flux-system'},
                        'wait': True, 'interval': '1h', 'retryInterval': '2m', 'timeout': timeout}
                if depends:
                    spec['dependsOn'] = [{'name': item, 'namespace': 'database'} for item in depends]
                self.assertEqual(entry, {'apiVersion': 'kustomize.toolkit.fluxcd.io/v1', 'kind': 'Kustomization',
                                         'metadata': {'name': name, 'namespace': 'flux-system'}, 'spec': spec})

    def test_registered_once_after_the_existing_entries_and_nothing_else_waits_on_them(self):
        root = yaml.safe_load((BACKLOG.parent / 'kustomization.yaml').read_text())['resources']
        self.assertEqual(root.count('hindsight-backlog/collector.ks.yaml'), 1)
        self.assertEqual(root.index('hindsight-backlog/collector.ks.yaml'), root.index('hindsight-backlog/ks.yaml') + 1)
        waiting = set()
        for path in (ROOT / 'kubernetes').rglob('*ks.yaml'):
            if path.name != 'ks.yaml' and not path.name.endswith('.ks.yaml'):
                continue
            for doc in yaml.safe_load_all(path.read_text()):
                depends = {item['name'] for item in (doc or {}).get('spec', {}).get('dependsOn', [])}
                if depends & {'hindsight-backlog-collector-code', QUAL, APP}:
                    waiting.add((path.name, doc['metadata']['name']))
        self.assertEqual(waiting, {('collector.ks.yaml', QUAL), ('collector.ks.yaml', APP)})


class PodTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qualification, cls.daemon = render(QUALIFICATION), render(DAEMON)

    def check_pod(self, pod, **extra):
        self.assertEqual({key: pod[key] for key in pod if key not in ('containers', 'volumes')}, {**POD, **extra})
        self.assertEqual(pod['volumes'], VOLUMES)
        [container] = pod['containers']
        self.assertEqual((container['image'], container['securityContext'], container['volumeMounts'],
                          container['resources']), (IMAGE, CONTAINER_SECURITY, MOUNTS, RESOURCES))
        return container

    def test_qualification_is_one_unretried_bounded_job_per_code_digest(self):
        self.assertEqual(sorted(self.qualification), ['CiliumNetworkPolicy', 'Job'])
        job = self.qualification['Job']
        self.assertEqual(job['metadata'], {'name': f'{QUAL}-{DIGEST}-r1'})
        # No ttlSecondsAfterFinished: the completed Job stays as the record Flux waits on.
        self.assertEqual({key: value for key, value in job['spec'].items() if key != 'template'},
                         {'backoffLimit': 0, 'activeDeadlineSeconds': 90, 'parallelism': 1, 'completions': 1})
        self.assertEqual(job['spec']['template']['metadata'], {'labels': {'app.kubernetes.io/name': QUAL}})
        container = self.check_pod(job['spec']['template']['spec'], restartPolicy='Never')
        self.assertEqual(sorted(container), ['command', 'image', 'name', 'resources', 'securityContext',
                                             'volumeMounts'])
        self.assertEqual(container['command'][:4], ['python3', '-I', '-S', '-c'])
        self.assertEqual(len(container['command']), 5)

    def test_adapter_polls_once_through_the_runtime_and_prints_one_boolean(self):
        adapter = self.qualification['Job']['spec']['template']['spec']['containers'][0]['command'][4]
        tree = ast.parse(adapter)
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertEqual(imports, {'importlib.util', 'sys'})
        self.assertFalse([node for node in ast.walk(tree) if isinstance(node, (ast.For, ast.While, ast.ImportFrom))])
        calls = [node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertEqual((calls.count('poll'), calls.count('Collector'), calls.count('version')), (1, 1, 1))
        strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertEqual(strings, {'hindsight_backlog_runtime', '/code/runtime.py', 'ok',
                                   'qualification adapter failed\n', 'qualification ok=%s\n'})
        cases = [('return "ok"', True, b'rows', 0), ('return "ok"', False, b'rows', 1), ('return "ok"', True, None, 1),
                 ('return "auth"', False, None, 1), ('return "malformed"', True, b'rows', 1),
                 ('raise RuntimeError("row-canary")', True, b'rows', 1)]
        for poll, valid, data, code in cases:
            with self.subTest(poll=poll, valid=valid, data=data):
                run = adapter_run(adapter, poll, valid, data)
                self.assertEqual((run.returncode, run.stdout), (code, f'qualification ok={code == 0}\n'))
                self.assertEqual(run.stderr, '' if 'raise' not in poll else 'qualification adapter failed\n')

    def test_daemon_is_one_recreated_replica_of_the_unchanged_runtime(self):
        self.assertEqual(sorted(self.daemon), ['CiliumNetworkPolicy', 'Deployment', 'Service', 'ServiceMonitor'])
        deployment = self.daemon['Deployment']
        labels = {'app.kubernetes.io/name': APP}
        self.assertEqual(deployment['metadata'], {'name': APP, 'labels': labels})
        self.assertEqual({key: value for key, value in deployment['spec'].items() if key != 'template'},
                         {'replicas': 1, 'revisionHistoryLimit': 2, 'strategy': {'type': 'Recreate'},
                          'selector': {'matchLabels': labels}})
        self.assertEqual(deployment['spec']['template']['metadata'], {'labels': labels})
        container = self.check_pod(deployment['spec']['template']['spec'])
        self.assertEqual(container['command'], ['python3', '-I', '-S', '/code/runtime.py'])
        self.assertEqual(container['ports'], [{'name': 'metrics', 'containerPort': R.PORT, 'protocol': 'TCP'}])
        budget = {}
        for probe in ('startupProbe', 'readinessProbe', 'livenessProbe'):
            self.assertEqual(container[probe]['httpGet'], {'path': '/healthz', 'port': 'metrics'})
            budget[probe] = container[probe]['periodSeconds'] * container[probe]['failureThreshold']
        # Seconds until each probe acts: startup covers boot only, and none is tied to poll success.
        self.assertEqual(budget, {'startupProbe': 60, 'readinessProbe': 30, 'livenessProbe': 90})

    def test_service_and_monitor_expose_only_the_metrics_port(self):
        self.assertEqual(self.daemon['Service']['spec'], {
            'type': 'ClusterIP', 'selector': {'app.kubernetes.io/name': APP},
            'ports': [{'name': 'metrics', 'port': R.PORT, 'targetPort': 'metrics', 'protocol': 'TCP'}]})
        monitor = self.daemon['ServiceMonitor']
        self.assertEqual(monitor['metadata'], {'name': APP, 'labels': {'prometheus': 'kube-prometheus-stack'}})
        self.assertEqual(monitor['spec'], {
            'namespaceSelector': {'matchNames': ['database']}, 'selector': {'matchLabels': {'app.kubernetes.io/name': APP}},
            'endpoints': [{'port': 'metrics', 'path': '/metrics', 'interval': '60s', 'scrapeTimeout': '10s',
                           'honorLabels': False}]})

    def test_policies_deny_by_default_and_admit_only_prometheus_to_the_daemon(self):
        self.assertEqual(self.qualification['CiliumNetworkPolicy'], policy(QUAL))
        prometheus = {'fromEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'observability',
                                                         'app.kubernetes.io/name': 'prometheus'}}],
                      'toPorts': [{'ports': [{'port': str(R.PORT), 'protocol': 'TCP'}]}]}
        self.assertEqual(self.daemon['CiliumNetworkPolicy'], policy(APP, ingress=[prometheus]))
        # The CNPG instances already admit 5432 from namespace database; their policy is not changed.
        cluster = yaml.safe_load((BACKLOG.parent / 'cluster/app/networkpolicy.yaml').read_text())['spec']
        self.assertEqual(cluster['endpointSelector']['matchLabels'], {'cnpg.io/cluster': 'postgres'})

    def test_only_existing_secrets_are_read_and_nothing_else_is_granted_or_forced(self):
        docs = [*self.qualification.values(), *self.daemon.values()]
        self.assertEqual(secret_names(docs), {READER_SECRET, 'postgres-ca'})
        text = yaml.safe_dump(docs)
        for word in ('kind: Secret', 'serviceAccountName', 'hostPath', 'persistentVolumeClaim', 'hostNetwork',
                     'ttlSecondsAfterFinished', 'kustomize.toolkit.fluxcd.io', 'HorizontalPodAutoscaler', 'env:'):
            self.assertNotIn(word, text)


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.workflow = yaml.safe_load(cls.text)

    def test_triggers_cover_every_collector_file_on_disk(self):
        triggers = self.workflow[True]['pull_request']['paths']
        for path in TRIGGERS:
            self.assertEqual(triggers.count(path), 1, path)
        on_disk = [path.relative_to(ROOT).as_posix() for tree in (QUALIFICATION, DAEMON, CORE.parent)
                   for path in tree.iterdir() if path.is_file()]
        self.assertEqual([path for path in on_disk if path not in triggers], [])

    def test_runs_core_runtime_and_deployment_tests_on_the_host_and_core_runtime_natively(self):
        for suite in ('test_backlog_collector.py', 'test_backlog_collector_runtime.py',
                      'test_backlog_collector_deployment.py'):
            self.assertEqual(self.text.count(
                f'      - run: python -m unittest discover -s tests/hindsight-observability -p {suite} -v\n'), 1)
        [step] = [step for step in self.workflow['jobs']['baseline']['steps']
                  if step.get('name') == 'Native Python 3.9 collector tests']
        self.assertEqual((step['env']['IMAGE'], step['timeout-minutes']), (IMAGE, 6))
        run = step['run']
        for flag in ('trap ', 'timeout 120 docker run --rm --name "$NAME" --network none --user 26:26 --read-only',
                     '--tmpfs /tmp:rw,size=16m', '-I -S -m unittest discover', "grep -Eq '^Ran [1-9][0-9]* tests? in '",
                     "if grep -q 'skipped=' \"$log\"; then exit 1; fi"):
            self.assertIn(flag, run)
        mounts = [line.strip() for line in run.splitlines() if line.strip().startswith('-v ')]
        self.assertEqual(len(mounts), 4)
        self.assertTrue(all(mount.endswith(':ro" \\') and '"$PWD/' in mount for mount in mounts), mounts)
        self.assertNotIn('/home/', run)
