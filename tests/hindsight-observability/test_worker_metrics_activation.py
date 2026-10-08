"""Checks for the prometheus-adapter wiring and its read-only API qualification.

Renders the qualification tree with native kustomize and runs the Job's own
script against scripted API answers. The live APIService, aggregator TLS,
Cilium identities and real metric are proven only by the Job after merge; what
the HPA controller does on an empty or unknown metric is shown by neither.
"""
import ast
import contextlib
import io
import json
import pathlib
import re
import shutil
import subprocess
import unittest
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / '_bmad-output/implementation-artifacts'
OBSERVABILITY = ROOT / 'kubernetes/apps/observability'
ADAPTER = OBSERVABILITY / 'prometheus-adapter'
QUALIFICATION = ADAPTER / 'qualification'
HPA_FILE = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app/worker-autoscaling.yaml'
WORKFLOW = ROOT / '.github/workflows/hindsight-baseline.yaml'
PINS = json.loads((pathlib.Path(__file__).parent / 'fixtures/worker-metrics-tool-pins.json').read_text())
NATIVE_KUSTOMIZE = pathlib.Path.home() / '.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize'
NAME = 'hindsight-metrics-qualification'
LABELS = {'app.kubernetes.io/name': NAME}
SA = [{'kind': 'ServiceAccount', 'name': NAME, 'namespace': 'ai'}]
METRIC = 'hindsight_worker_runnable'
ABSENT = 'hindsight_qualification_absent'
APISERVICE = 'v1beta1.external.metrics.k8s.io'
HEX = re.compile(r'[0-9a-f]{64}')
# The collector daemon registers in its own change. Until that merges, its
# reviewed registration stands in where present; a clean checkout has neither.
COLLECTOR = ('hindsight-backlog-collector', 'database')
COLLECTOR_REFERENCE = ARTIFACTS / 'qualified-collector-registration-reference.yaml'


def load(path):
    return [x for x in yaml.safe_load_all(path.read_text()) if x]


def kustomize_build(path):
    binary = str(NATIVE_KUSTOMIZE) if NATIVE_KUSTOMIZE.is_file() else shutil.which('kustomize')
    if not binary:
        raise AssertionError('native kustomize is required')
    out = subprocess.run([binary, 'build', str(path)], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(f'kustomize build {path.name} failed: {out.stderr.strip()[-500:]}')
    return [x for x in yaml.safe_load_all(out.stdout) if x]


def flux_kustomizations():
    """Every Flux Kustomization under kubernetes/apps with the namespace its CR actually lands in."""
    found = {}
    for path in (ROOT / 'kubernetes/apps').rglob('ks.yaml'):
        tree = ROOT / 'kubernetes/apps' / path.relative_to(ROOT / 'kubernetes/apps').parts[0] / 'kustomization.yaml'
        transform = (yaml.safe_load(tree.read_text()) or {}).get('namespace') if tree.is_file() else None
        for doc in load(path):
            if isinstance(doc, dict) and doc.get('apiVersion', '').startswith('kustomize.toolkit.fluxcd.io/'):
                found[(doc['metadata']['name'], transform or doc['metadata'].get('namespace'))] = doc
    if COLLECTOR_REFERENCE.is_file():
        # It lives under kubernetes/apps/database, whose root transform is namespace: database.
        for doc in load(COLLECTOR_REFERENCE):
            found.setdefault((doc['metadata']['name'], 'database'), doc)
    return found


class WiringTests(unittest.TestCase):
    def test_root_registers_the_adapter_once(self):
        resources = yaml.safe_load((OBSERVABILITY / 'kustomization.yaml').read_text())['resources']
        self.assertEqual([x for x in resources if 'prometheus-adapter' in x], ['./prometheus-adapter/ks.yaml'])

    def test_kustomizations_and_their_real_dependencies(self):
        docs = {x['metadata']['name']: x for x in load(ADAPTER / 'ks.yaml')}
        self.assertEqual(sorted(docs), ['prometheus-adapter', 'prometheus-adapter-qualification'])
        known = flux_kustomizations()
        pending = set() if COLLECTOR_REFERENCE.is_file() else {COLLECTOR}
        expected = {
            'prometheus-adapter': ('app', 'observability', [('cert-manager-operator', 'cert-manager'),
                                                            ('kube-prometheus-stack', 'flux-system')]),
            'prometheus-adapter-qualification': ('qualification', 'ai', [('prometheus-adapter', 'flux-system'),
                                                                         COLLECTOR]),
        }
        for name, (path, namespace, depends) in expected.items():
            with self.subTest(name):
                doc = docs[name]
                spec = doc['spec']
                self.assertEqual(doc['metadata']['namespace'], 'flux-system')
                self.assertIn((name, 'flux-system'), known, 'the CR must land where dependents name it')
                self.assertEqual(spec['path'], f'./{(ADAPTER / path).relative_to(ROOT)}')
                self.assertTrue((ADAPTER / path / 'kustomization.yaml').is_file())
                self.assertEqual(spec['targetNamespace'], namespace)
                self.assertEqual([(x['name'], x['namespace']) for x in spec['dependsOn']], depends)
                self.assertEqual([d for d in depends if d not in known and d not in pending], [],
                                 'every dependency is an existing Kustomization')
                self.assertEqual((spec['prune'], spec['wait']), (True, True))
                self.assertEqual(spec['sourceRef'], {'kind': 'GitRepository', 'name': 'flux-system',
                                                     'namespace': 'flux-system'})
                self.assertFalse({'force', 'patches', 'postBuild', 'components', 'healthChecks'} & set(spec))
        # Flux reports the Job's own outcome rather than racing its deadline.
        timeout = docs['prometheus-adapter-qualification']['spec']['timeout']
        self.assertGreater(int(timeout.rstrip('m')) * 60, 120)

    def test_nothing_depends_on_the_qualification(self):
        for (name, _), doc in flux_kustomizations().items():
            self.assertNotIn('prometheus-adapter-qualification',
                             [x['name'] for x in doc.get('spec', {}).get('dependsOn') or []], name)

    def test_hpa_stays_unreferenced(self):
        hits = [str(p.relative_to(ROOT)) for p in (ROOT / 'kubernetes').rglob('*.y*ml')
                if p.is_file() and p != HPA_FILE and p.parent != ADAPTER / 'app'
                and 'worker-autoscaling' in p.read_text(errors='ignore')]
        self.assertEqual(hits, [])


class QualificationRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = kustomize_build(QUALIFICATION)
        cls.objects = {(x['kind'], x['metadata']['name']): x for x in cls.docs}
        job_name, = [name for kind, name in cls.objects if kind == 'Job']
        cls.job = cls.objects[('Job', job_name)]
        cls.pod = cls.job['spec']['template']['spec']
        cls.container, = cls.pod['containers']

    def test_only_the_owned_objects(self):
        self.assertEqual(len(self.docs), len(self.objects))
        self.assertEqual(sorted(self.objects), sorted([
            ('CiliumNetworkPolicy', NAME), ('ClusterRole', NAME), ('ClusterRoleBinding', NAME),
            ('Job', self.job['metadata']['name']), ('Role', NAME), ('RoleBinding', NAME), ('ServiceAccount', NAME)]))
        self.assertRegex(self.job['metadata']['name'], rf'^{NAME}-r[1-9][0-9]*$', 'a rerun is a renamed revision')
        for (kind, name), doc in self.objects.items():
            if not kind.startswith('Cluster'):
                self.assertEqual(doc['metadata']['namespace'], 'ai', f'{kind}/{name}')

    def test_rbac_is_two_metric_names_and_one_apiservice(self):
        self.assertIs(self.objects[('ServiceAccount', NAME)]['automountServiceAccountToken'], False)
        self.assertEqual(self.objects[('Role', NAME)]['rules'], [
            {'apiGroups': ['external.metrics.k8s.io'], 'resources': [METRIC, ABSENT], 'verbs': ['list']}])
        self.assertEqual(self.objects[('ClusterRole', NAME)]['rules'], [
            {'apiGroups': ['apiregistration.k8s.io'], 'resources': ['apiservices'], 'resourceNames': [APISERVICE],
             'verbs': ['get']}])
        for kind in ('RoleBinding', 'ClusterRoleBinding'):
            binding = self.objects[(kind, NAME)]
            self.assertEqual(binding['subjects'], SA)
            self.assertEqual(binding['roleRef'], {'apiGroup': 'rbac.authorization.k8s.io',
                                                  'kind': kind.replace('Binding', ''), 'name': NAME})
        rules = [r for kind in ('Role', 'ClusterRole') for r in self.objects[(kind, NAME)]['rules']]
        self.assertFalse([r for r in rules if '*' in json.dumps(r)], 'no wildcards')

    def test_absent_name_is_not_served(self):
        rule, = load(ADAPTER / 'app/helmrelease.yaml')[0]['spec']['values']['rules']['external']
        self.assertEqual(rule['name']['as'], METRIC)
        self.assertIsNone(re.search(rule['name']['matches'], ABSENT))
        hpa, = load(HPA_FILE)
        self.assertEqual(hpa['spec']['metrics'][0]['external']['metric']['name'], METRIC)

    def test_job_is_bounded_and_runs_once(self):
        spec = self.job['spec']
        self.assertEqual({k: spec[k] for k in ('backoffLimit', 'activeDeadlineSeconds', 'parallelism', 'completions')},
                         {'backoffLimit': 0, 'activeDeadlineSeconds': 120, 'parallelism': 1, 'completions': 1})
        self.assertNotIn('ttlSecondsAfterFinished', spec, 'the failed or passed Job stays as the record')
        self.assertEqual(self.job['spec']['template']['metadata']['labels'], LABELS)
        self.assertEqual({k: self.pod[k] for k in ('restartPolicy', 'serviceAccountName', 'automountServiceAccountToken',
                                                   'enableServiceLinks', 'terminationGracePeriodSeconds')},
                         {'restartPolicy': 'Never', 'serviceAccountName': NAME, 'automountServiceAccountToken': False,
                          'enableServiceLinks': False, 'terminationGracePeriodSeconds': 30})
        self.assertFalse({'hostNetwork', 'hostPID', 'hostIPC', 'initContainers'} & set(self.pod))

    def test_container_is_restricted_and_reuses_the_pinned_image(self):
        backlog, = [x for x in load(ROOT / 'kubernetes/apps/database/hindsight-backlog/app/job.yaml')]
        pinned, = backlog['spec']['template']['spec']['containers']
        self.assertEqual(self.container['image'], pinned['image'])
        self.assertRegex(self.container['image'], r'@sha256:[0-9a-f]{64}$')
        self.assertEqual(self.pod['securityContext'], {'runAsNonRoot': True, 'runAsUser': 26, 'runAsGroup': 26,
                                                       'seccompProfile': {'type': 'RuntimeDefault'}})
        self.assertEqual(self.container['securityContext'], {'allowPrivilegeEscalation': False,
                                                             'readOnlyRootFilesystem': True,
                                                             'capabilities': {'drop': ['ALL']}})
        self.assertEqual(self.container['command'][:4], ['python3', '-I', '-B', '-c'])
        self.assertFalse({'env', 'envFrom', 'args', 'ports'} & set(self.container))
        self.assertIn('memory', self.container['resources']['limits'])

    def test_volumes_are_a_short_token_the_cluster_ca_and_bounded_tmp(self):
        volumes = {v['name']: v for v in self.pod['volumes']}
        self.assertEqual(set(volumes), {'api', 'tmp'})
        self.assertEqual(volumes['api']['projected']['sources'], [
            {'serviceAccountToken': {'path': 'token', 'expirationSeconds': 600}},
            {'configMap': {'name': 'kube-root-ca.crt', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}}])
        self.assertEqual(volumes['tmp'], {'name': 'tmp', 'emptyDir': {'sizeLimit': '8Mi'}})
        self.assertEqual({m['name']: (m['mountPath'], m.get('readOnly', False)) for m in self.container['volumeMounts']},
                         {'api': ('/var/run/qualification', True), 'tmp': ('/tmp', False)})
        self.assertNotIn('secret', json.dumps(self.pod['volumes']))

    def test_network_policy_is_dns_and_api_server_only(self):
        policy = self.objects[('CiliumNetworkPolicy', NAME)]['spec']
        self.assertEqual(set(policy), {'endpointSelector', 'enableDefaultDeny', 'egress'}, 'no ingress rule at all')
        self.assertEqual(policy['endpointSelector'], {'matchLabels': LABELS})
        self.assertEqual(policy['enableDefaultDeny'], {'ingress': True, 'egress': True})
        self.assertEqual(policy['egress'], [
            {'toEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}}],
             'toPorts': [{'ports': [{'port': '53', 'protocol': 'UDP'}, {'port': '53', 'protocol': 'TCP'}]}]},
            {'toEntities': ['kube-apiserver'],
             'toPorts': [{'ports': [{'port': '443', 'protocol': 'TCP'}, {'port': '6443', 'protocol': 'TCP'}]}]}])


def now_stamp(offset=0):
    return datetime.fromtimestamp(NOW - offset, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


NOW = 1_800_000_000
SENTINEL_VALUE = '4242'


def metric_list(*items):
    return 200, {'kind': 'ExternalMetricValueList', 'apiVersion': 'external.metrics.k8s.io/v1beta1', 'items': list(items)}


def item(**change):
    return {'metricName': METRIC, 'metricLabels': {}, 'timestamp': now_stamp(5), 'value': SENTINEL_VALUE, **change}


AVAILABLE = (200, {'kind': 'APIService', 'status': {'conditions': [{'type': 'Available', 'status': 'True'}]}})
PENDING = (200, {'kind': 'APIService', 'status': {'conditions': [{'type': 'Available', 'status': 'False'}]}})
NOT_FOUND = (404, {'kind': 'Status', 'reason': 'NotFound', 'code': 404})


class QualificationScriptTests(unittest.TestCase):
    """The Job's script, run in-process against scripted API answers on a fake clock."""

    @classmethod
    def setUpClass(cls):
        job, = [x for x in kustomize_build(QUALIFICATION) if x['kind'] == 'Job']
        cls.source = job['spec']['template']['spec']['containers'][0]['command'][-1]
        # The pinned image runs Python 3.9.
        ast.parse(cls.source, feature_version=(3, 9))

    def run_script(self, answers):
        """answers: path suffix -> list of (status, body) or exceptions, consumed in order (last one repeats)."""
        scope = {'__name__': 'qualification'}
        exec(compile(self.source, 'job.yaml', 'exec'), scope)
        clock, calls = [0.0], []

        def get(path):
            suffix = next(k for k in answers if path.endswith(k))
            calls.append(suffix)
            queue = answers[suffix]
            answer = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(answer, Exception):
                raise answer
            return answer

        def sleep(seconds):
            clock[0] += seconds

        real = scope['qualify']
        scope['qualify'] = lambda: real(get=get, now=lambda: NOW, monotonic=lambda: clock[0], sleep=sleep)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = scope['main']()
        printed = out.getvalue()
        self.assertNotIn(SENTINEL_VALUE, printed, 'no metric value is printed')
        self.assertNotIn('database', printed)
        self.assertLessEqual(clock[0], scope['POLL_SECONDS'] + 5, 'polling stops at the deadline')
        return code, printed.strip(), calls

    def answers(self, metric=None, absent=None, apiservice=None):
        return {'v1beta1.external.metrics.k8s.io': [apiservice or AVAILABLE],
                f'/{METRIC}': [metric or metric_list(item())], f'/{ABSENT}': [absent or NOT_FOUND]}

    def test_passes_only_on_available_one_valid_item_and_a_real_404(self):
        code, printed, calls = self.run_script(self.answers())
        self.assertEqual((code, printed), (0, 'QUALIFIED: available valid not-found'))
        self.assertEqual(calls, ['v1beta1.external.metrics.k8s.io', f'/{METRIC}', f'/{ABSENT}'])
        for value in ('0', '3', '1500m', '2.5'):
            with self.subTest(value):
                self.assertEqual(self.run_script(self.answers(metric_list(item(value=value))))[0], 0)

    def test_waits_for_readiness_within_the_deadline(self):
        answers = self.answers()
        answers['v1beta1.external.metrics.k8s.io'] = [PENDING, OSError('refused'), AVAILABLE]
        answers[f'/{METRIC}'] = [metric_list(), (404, NOT_FOUND[1]), metric_list(item())]
        self.assertEqual(self.run_script(answers)[:2], (0, 'QUALIFIED: available valid not-found'))

    def test_fails_closed(self):
        cases = {
            'apiservice never available': (self.answers(apiservice=PENDING), 'FAILED: apiservice-not-available'),
            'apiservice forbidden': (self.answers(apiservice=(403, {'kind': 'Status'})), 'FAILED: apiservice-http-403'),
            'metric unknown': (self.answers(metric=NOT_FOUND), 'FAILED: available metric-http-404'),
            'empty': (self.answers(metric=metric_list()), 'FAILED: available metric-items-none'),
            'two items': (self.answers(metric=metric_list(item(), item())), 'FAILED: available metric-items-many'),
            'negative': (self.answers(metric=metric_list(item(value='-1'))), 'FAILED: available metric-invalid-value'),
            'not a quantity': (self.answers(metric=metric_list(item(value=3))), 'FAILED: available metric-invalid-value'),
            'other metric': (self.answers(metric=metric_list(item(metricName='up'))),
                             'FAILED: available metric-unexpected-series'),
            'labelled': (self.answers(metric=metric_list(item(metricLabels={'namespace': 'database'}))),
                         'FAILED: available metric-unexpected-series'),
            'stale': (self.answers(metric=metric_list(item(timestamp=now_stamp(181)))), 'FAILED: available metric-stale'),
            'future': (self.answers(metric=metric_list(item(timestamp=now_stamp(-31)))), 'FAILED: available metric-stale'),
            'no zone': (self.answers(metric=metric_list(item(timestamp='2027-01-15T08:00:00'))),
                        'FAILED: available metric-no-timestamp'),
            'absent forbidden': (self.answers(absent=(403, {'kind': 'Status', 'reason': 'Forbidden'})),
                                 'FAILED: available valid absent-http-403'),
            'absent served': (self.answers(absent=metric_list()), 'FAILED: available valid absent-http-200'),
            'absent aggregator down': (self.answers(absent=(503, None)), 'FAILED: available valid absent-http-503'),
            'absent bare 404': (self.answers(absent=(404, None)), 'FAILED: available valid absent-http-404'),
        }
        for label, (answers, expected) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.run_script(answers)[:2], (1, expected))

    def test_errors_print_only_a_class_name(self):
        answers = self.answers(absent=RuntimeError(f'Bearer secret-token {SENTINEL_VALUE}'))
        self.assertEqual(self.run_script(answers)[:2], (1, 'FAILED: error-RuntimeError'))


class ToolPinTests(unittest.TestCase):
    def test_chart_pins_are_the_deployed_release(self):
        chart = PINS['chart']
        source, = load(ADAPTER / 'app/ocirepository.yaml')
        self.assertEqual((chart['oci_repository'], chart['oci_manifest_digest']),
                         (source['spec']['url'], source['spec']['ref']['digest']))
        image = load(ADAPTER / 'app/helmrelease.yaml')[0]['spec']['values']['image']
        self.assertEqual(f"{chart['image']}@{chart['image_digest']}", f"{image['repository']}:{image['tag']}")
        self.assertEqual(chart['image'].rsplit(':', 1)[1], chart['appVersion'])
        self.assertRegex(chart['archive_sha256'], HEX)
        # CI verifies both binaries against these, so they must be content digests.
        tool = PINS['promtool']
        self.assertRegex(tool['image'], r'^quay\.io/prometheus/prometheus@sha256:[0-9a-f]{64}$')
        self.assertRegex(tool['sha256'], HEX)
        self.assertTrue(tool['path'].startswith('/') and tool['version'].startswith('promtool, version '))

    def test_pins_match_the_local_provenance_when_present(self):
        # The CI leg writes these files from the pins; locally they are the qualified originals.
        provenance = ARTIFACTS / 'native-promtool-provenance.json'
        if provenance.is_file():
            local = json.loads(provenance.read_text())
            tool = PINS['promtool']
            self.assertEqual((local['image'], local['binary_sha256'], local['version']),
                             (tool['image'], tool['sha256'], tool['version']))
        qualified = ARTIFACTS / 'adapter-artifact-qualification.json'
        if qualified.is_file():
            local = json.loads(qualified.read_text())
            self.assertEqual({k: local[k] for k in PINS['chart']}, PINS['chart'])
            self.assertIn(f"sha256:{PINS['chart']['archive_sha256']}", local['oci_layer_digests'])


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.workflow = yaml.safe_load(cls.text)
        trigger = cls.workflow.get('on', cls.workflow.get(True))
        cls.paths = trigger['pull_request']['paths']
        cls.steps = cls.workflow['jobs']['baseline']['steps']

    def covered(self, path):
        relative = str(path.relative_to(ROOT))
        return any(relative == p or (p.endswith('/**') and relative.startswith(p[:-2])) for p in self.paths)

    def test_paths_cover_the_owned_and_parent_files(self):
        files = [OBSERVABILITY / 'kustomization.yaml', HPA_FILE, pathlib.Path(__file__).resolve(),
                 ROOT / 'tests/hindsight-observability/test_worker_autoscaling.py',
                 ROOT / 'tests/hindsight-observability/fixtures/worker-autoscaling.yaml',
                 ROOT / 'tests/hindsight-observability/fixtures/worker-metrics-tool-pins.json',
                 *[p for p in ADAPTER.rglob('*') if p.is_file()]]
        self.assertEqual([str(p.relative_to(ROOT)) for p in files if not self.covered(p)], [])

    def test_native_leg_runs_on_312_with_pinned_tools(self):
        fetch, = [s for s in self.steps if s.get('name') == 'Fetch pinned promtool and adapter chart']
        native, = [s for s in self.steps if 'test_worker_autoscaling.py' in s.get('run', '')]
        for step in (fetch, native):
            self.assertEqual(step.get('if'), "matrix.python-version == '3.12'")
        self.assertEqual(native['env']['HINDSIGHT_REQUIRE_NATIVE_PROMQL'], '1')
        self.assertTrue(native['env']['PROMTOOL'].startswith('${{ github.workspace }}/'), 'an absolute path')
        self.assertLess(self.steps.index(fetch), self.steps.index(native))
        script = fetch['run']
        self.assertIn('set -euo pipefail', script)
        self.assertRegex(script, r'docker create --network none --read-only ')
        self.assertIn("trap 'docker rm -f -v", script)
        self.assertNotRegex(script, r'docker (run|start|exec)')
        self.assertEqual(script.count('| sha256sum -c -'), 2, 'promtool and chart archive both verified')
        self.assertIn('Digest: $(jq -r .chart.oci_manifest_digest', script)
        self.assertIn('promtool" --version', script)

    def test_activation_tests_run_on_every_leg(self):
        step, = [s for s in self.steps if 'test_worker_metrics_activation.py' in s.get('run', '')]
        self.assertNotIn('if', step)

    def test_no_new_actions_secrets_or_skips(self):
        self.assertEqual(sorted({s['uses'].split('@')[0] for s in self.steps if 'uses' in s}),
                         ['actions/checkout', 'actions/setup-python'])
        self.assertNotIn('secrets.', self.text)
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        native = (ROOT / 'tests/hindsight-observability/test_worker_autoscaling.py').read_text()
        self.assertIsNone(re.search(r'skipTest|unittest\.skip|expectedFailure', native))


if __name__ == '__main__':
    unittest.main()
