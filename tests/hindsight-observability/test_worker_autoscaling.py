"""Checks for the inactive Hindsight worker autoscaling source.

Renders the qualified prometheus-adapter 5.3.0 archive with the committed
values, applies the HelmRelease post-render patches with native kustomize, and
runs the external rule over the fixture scrapes with the provenance-pinned
promtool. A running adapter, the APIService handshake and the HPA controller
need a cluster and are not run.
"""
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / '_bmad-output/implementation-artifacts'
QUALIFIED = json.loads((ARTIFACTS / 'adapter-artifact-qualification.json').read_text())
PROMTOOL_QUALIFIED = json.loads((ARTIFACTS / 'native-promtool-provenance.json').read_text())
ADAPTER = ROOT / 'kubernetes/apps/observability/prometheus-adapter/app'
HINDSIGHT = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app'
HPA_FILE = HINDSIGHT / 'worker-autoscaling.yaml'
FIXTURE = yaml.safe_load((pathlib.Path(__file__).parent / 'fixtures/worker-autoscaling.yaml').read_text())
CHART = pathlib.Path(os.environ.get('PROMETHEUS_ADAPTER_CHART', ARTIFACTS / 'prometheus-adapter-5.3.0.tgz'))
HELM = os.environ.get('PROMETHEUS_ADAPTER_HELM', 'helm')
NATIVE_KUSTOMIZE = pathlib.Path.home() / '.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize'
PROMTOOL = pathlib.Path(os.environ.get('PROMTOOL') or ARTIFACTS / 'tooling/promtool')

METRIC = 'hindsight_worker_runnable'
SOURCE = {'namespace': 'database', 'service': 'hindsight-backlog-collector'}
SOURCE_MATCHERS = 'namespace="database",service="hindsight-backlog-collector"'
SOURCE_SERIES = {'hindsight_backlog_runnable', 'hindsight_backlog_sample_valid', 'hindsight_backlog_capacity_exceeded',
                 'hindsight_backlog_observed_timestamp_seconds', 'up'}
PROMETHEUS_URL = 'http://kube-prometheus-stack-prometheus.observability.svc.cluster.local:9090'
POD_LABELS = {'app.kubernetes.io/name': 'prometheus-adapter', 'app.kubernetes.io/instance': 'prometheus-adapter'}
ADAPTER_SA = [{'kind': 'ServiceAccount', 'name': 'prometheus-adapter', 'namespace': 'observability'}]
RESOURCE_READER = {('ClusterRole', 'prometheus-adapter-resource-reader'),
                   ('ClusterRoleBinding', 'prometheus-adapter-resource-reader')}
REQUIRED_CASES = {'fresh_positive', 'known_zero', 'no_runnable_sample', 'missing_timestamp', 'stale_timestamp',
                  'clock_future', 'sample_invalid', 'capacity_exceeded', 'scrape_down', 'source_absent',
                  'wrong_namespace', 'wrong_service'}


def kustomize_build(path):
    binary = str(NATIVE_KUSTOMIZE) if NATIVE_KUSTOMIZE.is_file() else shutil.which('kustomize')
    if not binary:
        raise AssertionError('native kustomize is required')
    out = subprocess.run([binary, 'build', str(path)], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(f'kustomize build {path.name} failed: {out.stderr.strip()[-500:]}')
    return [x for x in yaml.safe_load_all(out.stdout) if x]


def helm_template(values):
    out = subprocess.run(
        [HELM, 'template', 'prometheus-adapter', str(CHART), '--namespace', 'observability',
         '--api-versions', 'apiregistration.k8s.io/v1', '--values', '-'],
        input=yaml.safe_dump(values), capture_output=True, text=True, cwd=ROOT)
    if out.returncode:
        # stderr only: rendered documents are never echoed into failures.
        raise AssertionError(f'helm template failed: {out.stderr.strip()[-500:]}')
    return [x for x in yaml.safe_load_all(out.stdout) if x]


def post_render(docs, spec):
    """What helm-controller's kustomize post-renderer does with the release patches."""
    patches = [p for renderer in spec.get('postRenderers', []) for p in renderer['kustomize']['patches']]
    with tempfile.TemporaryDirectory() as directory:
        pathlib.Path(directory, 'chart.yaml').write_text(yaml.safe_dump_all(docs))
        pathlib.Path(directory, 'kustomization.yaml').write_text(
            yaml.safe_dump({'resources': ['chart.yaml'], 'patches': patches}))
        return kustomize_build(pathlib.Path(directory))


def load(path):
    return [x for x in yaml.safe_load_all(path.read_text()) if x]


def key(doc):
    return doc['kind'], doc['metadata']['name']


def find(docs, kind, name):
    found = [x for x in docs if key(x) == (kind, name)]
    if len(found) != 1:
        raise AssertionError(f'expected one {kind}/{name}, found {len(found)}')
    return found[0]


def release():
    return load(ADAPTER / 'helmrelease.yaml')[0]


def selectors(text):
    """(metric, matcher text) for each `name{...}` in a query, a pin on the text rather than PromQL semantics."""
    return re.findall(r'([A-Za-z_]\w*)\{([^}]*)\}', text)


def qualified_promtool():
    """The provenance-pinned promtool; a missing or different binary fails, never skips."""
    if os.environ.get('HINDSIGHT_REQUIRE_NATIVE_PROMQL') == '1' and not os.path.isabs(os.environ.get('PROMTOOL', '')):
        raise AssertionError('HINDSIGHT_REQUIRE_NATIVE_PROMQL=1 needs PROMTOOL set to an absolute path')
    if not PROMTOOL.is_file():
        raise AssertionError(f'native promtool missing at {PROMTOOL}; set PROMTOOL')
    digest = hashlib.sha256(PROMTOOL.read_bytes()).hexdigest()
    if digest != PROMTOOL_QUALIFIED['binary_sha256']:
        raise AssertionError(f'promtool sha256 {digest} is not the qualified binary')
    out = subprocess.run([str(PROMTOOL), '--version'], capture_output=True, text=True)
    if PROMTOOL_QUALIFIED['version'] not in out.stdout + out.stderr:
        raise AssertionError('promtool --version does not report the qualified version')
    return PROMTOOL_QUALIFIED['version']


def case_series(case):
    """(metric, labels, values) for one case, starting from the healthy collector scrape."""
    labels = {**FIXTURE['source'], **case.get('labels', {})}
    series = [(name, labels, case.get('set', {}).get(name, values))
              for name, values in FIXTURE['healthy'].items() if name not in case.get('drop', [])]
    if 'peer' in case:
        peer = {**labels, 'instance': FIXTURE['peer_instance']}
        series += [(name, peer, case['peer'].get(name, values)) for name, values in FIXTURE['healthy'].items()]
    return series + [(x['metric'], {**FIXTURE['source'], **x.get('labels', {})}, x['values'])
                     for x in case.get('extra', [])]


def series_text(name, labels):
    return name + '{' + ','.join(f'{k}="{v}"' for k, v in sorted(labels.items())) + '}'


def promtool(expr, check=False):
    """Run `promtool test rules` with every fixture case as one test of expr (after `check rules` if asked)."""
    tests = [{'name': c['name'], 'interval': FIXTURE['interval'],
              'input_series': [{'series': series_text(n, l), 'values': v} for n, l, v in case_series(c)],
              'promql_expr_test': [{'expr': expr, 'eval_time': FIXTURE['eval_time'],
                                    'exp_samples': [{'labels': '{}', 'value': v} for v in c['expect']]}]}
             for c in FIXTURE['cases']]
    with tempfile.TemporaryDirectory() as directory:
        rules, fixture = pathlib.Path(directory, 'rules.yaml'), pathlib.Path(directory, 'fixture.yaml')
        rules.write_text(yaml.safe_dump({'groups': [{'name': 'fixture', 'rules': [
            {'record': f'{METRIC}:fixture', 'expr': expr}]}]}))
        fixture.write_text(yaml.safe_dump({'rule_files': [str(rules)], 'evaluation_interval': FIXTURE['interval'],
                                           'tests': tests}))
        commands = ([['check', 'rules', str(rules)]] if check else []) + [['test', 'rules', str(fixture)]]
        return [subprocess.run([str(PROMTOOL), *c], capture_output=True, text=True) for c in commands]


class AdapterRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CHART.is_file():
            raise AssertionError(f'qualified chart archive missing at {CHART}; set PROMETHEUS_ADAPTER_CHART')
        digest = hashlib.sha256(CHART.read_bytes()).hexdigest()
        if digest != QUALIFIED['archive_sha256'] or f'sha256:{digest}' not in QUALIFIED['oci_layer_digests']:
            raise AssertionError(f'{CHART.name} sha256 {digest} is not the qualified archive')
        if shutil.which(HELM) is None and not pathlib.Path(HELM).is_file():
            raise AssertionError(f'helm not found ({HELM})')
        chart = yaml.safe_load(subprocess.run([HELM, 'show', 'chart', str(CHART)],
                                              capture_output=True, text=True, check=True).stdout)
        if (chart['name'], chart['version'], chart['appVersion']) != (
                'prometheus-adapter', QUALIFIED['chart_version'], QUALIFIED['appVersion']):
            raise AssertionError(f"archive is {chart['name']} {chart['version']}, not the qualified chart")
        cls.release = release()
        cls.values = cls.release['spec']['values']
        cls.chart = helm_template(cls.values)
        cls.rendered = post_render(cls.chart, cls.release['spec'])
        cls.config = yaml.safe_load(find(cls.rendered, 'ConfigMap', 'prometheus-adapter')['data']['config.yaml'])
        cls.deployment = find(cls.rendered, 'Deployment', 'prometheus-adapter')

    @classmethod
    def tearDownClass(cls):
        print('\nRENDERED: prometheus-adapter 5.3.0 archive (sha256 verified) with the committed values,'
              ' post-render patches applied by native kustomize.'
              '\nNOT RUN (needs a cluster): Flux post-render, adapter start without the resource-reader,'
              ' APIService Available, cert-manager injection, Cilium identity of aggregator traffic,'
              ' HPA controller on an empty metric.')

    def test_chart_and_image_pinned_by_digest(self):
        source, = load(ADAPTER / 'ocirepository.yaml')
        self.assertEqual(key(source), ('OCIRepository', 'prometheus-adapter'))
        self.assertEqual(source['spec']['url'], QUALIFIED['oci_repository'])
        self.assertEqual(source['spec']['ref'], {'digest': QUALIFIED['oci_manifest_digest']}, 'digest only, no tag')
        self.assertEqual(source['spec']['layerSelector']['mediaType'], 'application/vnd.cncf.helm.chart.content.v1.tar+gzip')
        self.assertEqual(self.release['spec']['chartRef'], {'kind': 'OCIRepository', 'name': 'prometheus-adapter'})
        repository, tag = QUALIFIED['image'].rsplit(':', 1)
        self.assertEqual(tag, QUALIFIED['appVersion'])
        self.assertEqual(self.values['image'], {'repository': repository, 'tag': f"{tag}@{QUALIFIED['image_digest']}"})
        container, = self.deployment['spec']['template']['spec']['containers']
        self.assertEqual(container['image'], f"{repository}:{tag}@{QUALIFIED['image_digest']}")
        print(f'\nchart value keys={sorted(self.values)} rules keys={sorted(self.values["rules"])}')

    def test_only_the_external_api_service_and_its_tls(self):
        self.assertEqual(sorted(key(x) for x in self.rendered), [
            ('APIService', 'v1beta1.external.metrics.k8s.io'),
            ('Certificate', 'prometheus-adapter-cert'), ('Certificate', 'prometheus-adapter-root-cert'),
            ('ClusterRole', 'prometheus-adapter-external-metrics'),
            ('ClusterRoleBinding', 'prometheus-adapter-hpa-controller-external-metrics'),
            ('ClusterRoleBinding', 'prometheus-adapter-system-auth-delegator'),
            ('ConfigMap', 'prometheus-adapter'), ('Deployment', 'prometheus-adapter'),
            ('Issuer', 'prometheus-adapter-root-issuer'), ('Issuer', 'prometheus-adapter-self-signed-issuer'),
            ('RoleBinding', 'prometheus-adapter-auth-reader'), ('Service', 'prometheus-adapter'),
            ('ServiceAccount', 'prometheus-adapter'),
        ], 'no Secret (cert-manager issues the serving key), no custom/resource metrics API, no Ingress or Route')
        api = find(self.rendered, 'APIService', 'v1beta1.external.metrics.k8s.io')
        self.assertEqual(api['apiVersion'], 'apiregistration.k8s.io/v1')
        spec = api['spec']
        self.assertEqual((spec['group'], spec['version']), ('external.metrics.k8s.io', 'v1beta1'))
        self.assertEqual(spec['service'], {'name': 'prometheus-adapter', 'namespace': 'observability'})
        self.assertNotIn('insecureSkipTLSVerify', spec)
        self.assertNotIn('caBundle', spec, 'cainjector fills the CA; none is committed')
        self.assertEqual(api['metadata']['annotations']['cert-manager.io/inject-ca-from'],
                         'observability/prometheus-adapter-root-cert')
        self.assertIs(find(self.rendered, 'Certificate', 'prometheus-adapter-root-cert')['spec']['isCA'], True)
        serving = find(self.rendered, 'Certificate', 'prometheus-adapter-cert')['spec']
        self.assertEqual((serving['secretName'], serving['issuerRef']),
                         ('prometheus-adapter', {'name': 'prometheus-adapter-root-issuer'}))
        self.assertIn('prometheus-adapter.observability.svc', serving['dnsNames'])
        service = find(self.rendered, 'Service', 'prometheus-adapter')['spec']
        self.assertEqual((service['type'], service['selector']), ('ClusterIP', POD_LABELS))
        self.assertEqual(service['ports'], [{'port': 443, 'name': 'https', 'protocol': 'TCP', 'targetPort': 'https'}])

    def test_post_render_removes_only_the_resource_reader(self):
        # The pinned chart renders it whenever rbac.create is on, which the auth bindings also need.
        self.assertEqual(find(self.chart, 'ClusterRole', 'prometheus-adapter-resource-reader')['rules'], [
            {'apiGroups': [''], 'resources': ['namespaces', 'pods', 'services', 'configmaps'],
             'verbs': ['get', 'list', 'watch']}])
        self.assertEqual(find(self.chart, 'ClusterRoleBinding', 'prometheus-adapter-resource-reader')['subjects'],
                         ADAPTER_SA)
        self.assertEqual({key(x) for x in self.chart} - {key(x) for x in self.rendered}, RESOURCE_READER)
        self.assertEqual(len(self.chart) - len(self.rendered), len(RESOURCE_READER))

    def test_rendered_roles(self):
        # Pins what renders, not an access boundary: bootstrap RBAC already grants external metrics.
        roles = {x['metadata']['name']: x['rules'] for x in self.rendered if x['kind'] in ('ClusterRole', 'Role')}
        self.assertEqual(roles, {'prometheus-adapter-external-metrics': [
            {'apiGroups': ['external.metrics.k8s.io'], 'resources': [METRIC], 'verbs': ['list', 'get', 'watch']}]})
        bindings = {x['metadata']['name']: (x['metadata'].get('namespace'), x['roleRef']['kind'],
                                            x['roleRef']['name'], x['subjects'])
                    for x in self.rendered if x['kind'] in ('ClusterRoleBinding', 'RoleBinding')}
        self.assertEqual(bindings, {
            'prometheus-adapter-hpa-controller-external-metrics': (
                None, 'ClusterRole', 'prometheus-adapter-external-metrics',
                [{'kind': 'ServiceAccount', 'name': 'horizontal-pod-autoscaler', 'namespace': 'kube-system'}]),
            # TokenReview/SubjectAccessReview for delegated authn/authz.
            'prometheus-adapter-system-auth-delegator': (None, 'ClusterRole', 'system:auth-delegator', ADAPTER_SA),
            # The built-in role reads only the extension-apiserver-authentication configmap.
            'prometheus-adapter-auth-reader': (
                'kube-system', 'Role', 'extension-apiserver-authentication-reader', ADAPTER_SA),
        }, 'the adapter keeps only the delegation bindings; no cluster-wide configmap read')

    def test_deployment_is_restricted_and_queries_the_stack_prometheus(self):
        pod = self.deployment['spec']['template']
        spec = pod['spec']
        self.assertEqual(self.deployment['spec']['replicas'], 1)
        self.assertEqual({k: pod['metadata']['labels'][k] for k in POD_LABELS}, POD_LABELS)
        # Delegated authn/authz (TokenReview, SubjectAccessReview) calls the API
        # server as this account, so the token stays mounted.
        self.assertIs(spec['automountServiceAccountToken'], True)
        self.assertIs(find(self.rendered, 'ServiceAccount', 'prometheus-adapter')['automountServiceAccountToken'], True)
        self.assertEqual(spec['serviceAccountName'], 'prometheus-adapter')
        self.assertNotIn('hostNetwork', spec)
        container, = spec['containers']
        self.assertEqual(container['args'], [
            '/adapter', '--secure-port=6443',
            '--tls-cert-file=/var/run/serving-cert/tls.crt', '--tls-private-key-file=/var/run/serving-cert/tls.key',
            '--cert-dir=/tmp/cert', f'--prometheus-url={PROMETHEUS_URL}', '--metrics-relist-interval=1m',
            '--v=2', '--config=/etc/adapter/config.yaml'])
        self.assertEqual(container['ports'], [{'containerPort': 6443, 'name': 'https'}])
        security = container['securityContext']
        self.assertEqual({k: security[k] for k in ('allowPrivilegeEscalation', 'readOnlyRootFilesystem', 'runAsNonRoot')},
                         {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True, 'runAsNonRoot': True})
        self.assertEqual((security['capabilities'], security['seccompProfile']),
                         ({'drop': ['ALL']}, {'type': 'RuntimeDefault'}))
        self.assertEqual(container['resources'], self.values['resources'])
        volumes = {v['name']: v for v in spec['volumes']}
        self.assertEqual(set(volumes), {'config', 'tmp', 'volume-serving-cert'})
        self.assertEqual(volumes['volume-serving-cert']['secret'], {'secretName': 'prometheus-adapter'})
        self.assertFalse([v for v in volumes.values() if 'hostPath' in v])

    def test_config_exposes_one_bounded_external_metric(self):
        self.assertEqual(set(self.config), {'externalRules'}, 'no default, custom or resource rules')
        self.assertEqual(self.config['externalRules'], self.values['rules']['external'])
        rule, = self.config['externalRules']
        self.assertEqual(set(rule), {'seriesQuery', 'resources', 'name', 'metricsQuery'})
        self.assertEqual(rule['resources'], {'namespaced': False}, 'source in database, HPA in ai')
        self.assertEqual(rule['name'], {'matches': '^hindsight_backlog_runnable$', 'as': METRIC})
        self.assertEqual(selectors(rule['seriesQuery']), [('hindsight_backlog_runnable', SOURCE_MATCHERS)])
        query = rule['metricsQuery']
        self.assertIsNone(re.search(r'<<|=~|!~|!=|\b(or|unless|absent\w*|vector|clamp\w*|i?rate|sum)\b', query),
                          'no HPA-supplied selector, regex matcher or fallback reaches the query')
        found = selectors(query)
        self.assertEqual({name for name, _ in found}, SOURCE_SERIES)
        self.assertEqual({body for _, body in found}, {SOURCE_MATCHERS})
        print(f"\nseriesQuery={rule['seriesQuery']}\nmetricsQuery={' '.join(query.split())}")


class RunnableQueryTests(unittest.TestCase):
    """Native promtool is the only PromQL oracle here."""

    @classmethod
    def setUpClass(cls):
        cls.version = qualified_promtool()
        rule, = release()['spec']['values']['rules']['external']
        cls.text = rule['metricsQuery']

    def test_fixture_cases(self):
        names = [c['name'] for c in FIXTURE['cases']]
        self.assertEqual(len(names), len(set(names)))
        self.assertLessEqual(REQUIRED_CASES, set(names))
        check, test = promtool(self.text, check=True)
        self.assertEqual(check.returncode, 0, (check.stdout + check.stderr)[-2000:])
        self.assertEqual(test.returncode, 0, (test.stdout + test.stderr)[-2000:])
        self.assertIn('SUCCESS', test.stdout)
        print(f'\nNATIVE: {self.version}; {len(names)} fixture cases passed')

    def test_every_guard_and_fallback_is_caught(self):
        lines = self.text.splitlines()
        guards = [i for i, line in enumerate(lines) if line.strip().startswith('and on (namespace, service, job, instance) ')]
        self.assertEqual(len(guards), 4)
        mutants = {f"without {re.search(r'(\w+)\{', lines[i]).group(1)}": '\n'.join(lines[:i] + lines[i + 1:])
                   for i in guards}
        for label, old, new in [('negative counts', '} >= 0\n', '}\n'), ('clock-future guard', ') >= 0 < 180', ') < 180'),
                                ('180 s bound', '< 180', '< 300'), ('sum of targets', 'max(', 'sum(')]:
            self.assertIn(old, self.text, label)
            mutants[label] = self.text.replace(old, new, 1)
        mutants['zero fallback'] = self.text.rstrip() + ' or vector(0)'
        for label, text in mutants.items():
            with self.subTest(label):
                test, = promtool(text)
                # A mismatch, not a parse or evaluation error, must be what fails.
                self.assertNotEqual(test.returncode, 0, f'no fixture case notices the query {label}')
                self.assertIn('got:', test.stdout + test.stderr, label)


class InactiveWiringTests(unittest.TestCase):
    def test_hpa_is_bounded_scale_up_only(self):
        hpa, = load(HPA_FILE)
        self.assertEqual((hpa['apiVersion'], hpa['kind']), ('autoscaling/v2', 'HorizontalPodAutoscaler'))
        self.assertEqual(hpa['metadata'], {'name': 'hindsight-worker', 'namespace': 'ai'})
        self.assertEqual(hpa['spec'], {
            'scaleTargetRef': {'apiVersion': 'apps/v1', 'kind': 'StatefulSet', 'name': 'hindsight-worker'},
            'minReplicas': 1,
            'maxReplicas': 2,
            'metrics': [{'type': 'External', 'external': {
                'metric': {'name': METRIC}, 'target': {'type': 'AverageValue', 'averageValue': '1'}}}],
            'behavior': {
                'scaleUp': {'stabilizationWindowSeconds': 0, 'policies': [{'type': 'Pods', 'value': 1, 'periodSeconds': 60}]},
                'scaleDown': {'selectPolicy': 'Disabled'}},
        })

    def test_worker_foundation_still_pins_zero_replicas(self):
        hindsight = load(HINDSIGHT / 'hindsight.yaml')[0]
        patches = [p for p in hindsight['spec']['postRenderers'][0]['kustomize']['patches']
                   if p['target'] == {'kind': 'StatefulSet', 'name': 'hindsight-worker'}]
        self.assertEqual(len(patches), 1, 'the HPA target must be the worker StatefulSet the release renders')
        self.assertIn({'op': 'add', 'path': '/spec/replicas', 'value': 0}, yaml.safe_load(patches[0]['patch']))

    def test_hpa_and_adapter_are_unreferenced(self):
        listed = yaml.safe_load((HINDSIGHT / 'kustomization.yaml').read_text())['resources']
        self.assertFalse([x for x in listed if x.endswith('worker-autoscaling.yaml')])
        self.assertFalse([x for x in kustomize_build(HINDSIGHT) if x['kind'] == 'HorizontalPodAutoscaler'])
        self.assertEqual(sorted(p.name for p in ADAPTER.parent.iterdir()), ['app'], 'no ks.yaml')
        owned = {HPA_FILE, *ADAPTER.iterdir()}
        hits = [str(p.relative_to(ROOT)) for p in (ROOT / 'kubernetes').rglob('*.y*ml')
                if p.is_file() and p not in owned
                and re.search(r'prometheus-adapter|worker-autoscaling', p.read_text(errors='ignore'))]
        self.assertEqual(hits, [])

    def test_adapter_build_and_network_policy(self):
        built = kustomize_build(ADAPTER)
        self.assertEqual(sorted((x['kind'], x['metadata']['name'], x['metadata']['namespace']) for x in built), [
            ('CiliumNetworkPolicy', 'prometheus-adapter', 'observability'),
            ('HelmRelease', 'prometheus-adapter', 'observability'),
            ('OCIRepository', 'prometheus-adapter', 'observability')])
        self.assertEqual(find(built, 'HelmRelease', 'prometheus-adapter')['spec'], release()['spec'])
        policy = find(built, 'CiliumNetworkPolicy', 'prometheus-adapter')['spec']
        self.assertEqual(set(policy), {'endpointSelector', 'ingress', 'egress'})
        self.assertEqual(policy['endpointSelector'], {'matchLabels': POD_LABELS})

        def tcp(port):
            return [{'ports': [{'port': str(port), 'protocol': 'TCP'}]}]
        self.assertEqual(policy['ingress'], [{'fromEntities': ['kube-apiserver'], 'toPorts': tcp(6443)}],
                         'aggregator only; kubelet probes ride the allow-localhost default')
        self.assertEqual(policy['egress'], [
            {'toEndpoints': [{'matchLabels': {'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}}],
             'toPorts': [{'ports': [{'port': '53', 'protocol': 'UDP'}, {'port': '53', 'protocol': 'TCP'}]}]},
            {'toEndpoints': [{'matchLabels': {
                'io.kubernetes.pod.namespace': 'observability', 'app.kubernetes.io/name': 'prometheus',
                'io.cilium.k8s.policy.serviceaccount': 'kube-prometheus-stack-prometheus'}}],
             'toPorts': tcp(PROMETHEUS_URL.rsplit(':', 1)[1])},
            {'toEntities': ['kube-apiserver']},
        ])


if __name__ == '__main__':
    unittest.main()
