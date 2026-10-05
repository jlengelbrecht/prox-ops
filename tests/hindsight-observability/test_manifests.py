"""Offline manifest checks for the measured Hindsight rollout."""
import json
import importlib.util
import hashlib
import io
import tarfile
import tempfile
import os
import pathlib
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app'
OBS = ROOT / 'kubernetes/apps/observability/kube-prometheus-stack/app'
CHART = pathlib.Path(os.environ.get('HINDSIGHT_CHART', '/home/devbox/.cache/helm/repository/hindsight-0.9.2.tgz'))
HELM = os.environ.get('HINDSIGHT_HELM', 'helm')


def docs(path):
    return list(yaml.safe_load_all(path.read_text()))


def monitor_discovered(spec, monitor):
    def matches(selector, labels):
        return (isinstance(selector, dict) and set(selector) <= {'matchLabels'}
                and all(labels.get(k) == v for k, v in selector.get('matchLabels', {}).items()))

    return (matches(spec.get('serviceMonitorSelector', {}), monitor['metadata']['labels'])
            and matches(spec.get('serviceMonitorNamespaceSelector', {}),
                        {'kubernetes.io/metadata.name': monitor['metadata']['namespace']}))


class ManifestTests(unittest.TestCase):
    def test_chart_and_scrape_selectors(self):
        self.assertTrue(CHART.exists(), 'Pinned chart archive unavailable; render must be reported')
        release = docs(APP / 'hindsight.yaml')[0]
        values = release['spec']['values']
        rendered = list(yaml.safe_load_all(subprocess.check_output(
            [HELM, 'template', 'hindsight', str(CHART), '--namespace', 'ai',
             '--values', '-'], input=yaml.safe_dump(values).encode(), cwd=ROOT)))
        svc = next(x for x in rendered if x and x.get('kind') == 'Service' and x['metadata']['name'] == 'hindsight-api')
        dep = next(x for x in rendered if x and x.get('kind') == 'Deployment' and x['metadata']['name'] == 'hindsight-api')
        mon = docs(APP / 'servicemonitor.yaml')[0]
        self.assertTrue(all(svc['metadata']['labels'].get(k) == v for k, v in mon['spec']['selector']['matchLabels'].items()))
        self.assertEqual(mon['spec']['selector']['matchLabels'], svc['spec']['selector'])
        self.assertEqual(mon['spec']['endpoints'][0]['port'], svc['spec']['ports'][0]['name'])
        self.assertEqual(mon['spec']['endpoints'][0]['path'], '/metrics')
        env = {x['name']: x.get('value') for x in dep['spec']['template']['spec']['containers'][0]['env']}
        for key, value in {'HINDSIGHT_API_LLM_MAX_CONCURRENT': '2',
                           'HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT': '1',
                           'HINDSIGHT_API_METRICS_BACKLOG_ENABLED': 'true',
                           'HINDSIGHT_API_METRICS_INCLUDE_BANK_ID': 'true'}.items():
            self.assertEqual(env[key], value)
        self.assertFalse(any(x and x.get('kind') == 'StatefulSet' for x in rendered))
        self.assertEqual(values['worker']['enabled'], False)
        self.assertEqual(values['api']['replicaCount'], 1)
        self.assertEqual(values['api']['extraVolumes'][0]['persistentVolumeClaim']['claimName'], 'hindsight-codex-auth')
        self.assertIn('@sha256:', values['api']['image']['tag'])

    def test_narrow_ingress(self):
        policies = docs(APP / 'networkpolicy.yaml')
        api = next(x for x in policies if x['metadata']['name'] == 'hindsight-api')
        port_lanes = [lane for lane in api['spec']['ingress'] if any(
            str(port.get('port')) == '8888' for item in lane.get('toPorts', [])
            for port in item.get('ports', []))]
        self.assertEqual(len(port_lanes), 4)
        approved = [
            {'io.kubernetes.pod.namespace': 'ai', 'app.kubernetes.io/name': 'hindsight',
             'app.kubernetes.io/instance': 'hindsight', 'app.kubernetes.io/component': 'control-plane'},
            {'io.kubernetes.pod.namespace': 'ai', 'app.kubernetes.io/name': 'hermes-agent',
             'app.kubernetes.io/instance': 'hermes'},
            {'io.kubernetes.pod.namespace': 'network',
             'gateway.envoyproxy.io/owning-gateway-name': 'envoy-internal'},
            {'io.kubernetes.pod.namespace': 'observability',
             'io.cilium.k8s.policy.serviceaccount': 'kube-prometheus-stack-prometheus'},
        ]
        self.assertEqual([lane['fromEndpoints'] for lane in port_lanes],
                         [[{'matchLabels': labels}] for labels in approved])
        for lane in port_lanes:
            self.assertEqual(lane['toPorts'][0]['ports'], [{'port': '8888', 'protocol': 'TCP'}])
        for lane in port_lanes[:3]:
            self.assertNotIn('rules', lane['toPorts'][0])
        allowed_http = port_lanes[3]['toPorts'][0]['rules']['http']
        self.assertEqual(allowed_http, [{'method': 'GET', 'path': '/metrics'}])
        monitor = docs(APP / 'servicemonitor.yaml')[0]
        self.assertEqual(monitor['spec']['endpoints'][0]['path'], allowed_http[0]['path'])

    def test_dashboard_and_rules_are_included(self):
        for folder, name in [('alerts', 'hindsight-alerts.yaml'), ('dashboards', 'hindsight-dashboard.yaml')]:
            resources = docs(OBS / folder / 'kustomization.yaml')[0]['resources']
            self.assertIn(name, resources)
        workflow = (ROOT / '.github/workflows/hindsight-metrics.yaml').read_text()
        self.assertIn('app/alerts/kustomization.yaml', workflow)
        self.assertIn('app/dashboards/kustomization.yaml', workflow)
        self.assertIn('kube-prometheus-stack/app/helmrelease.yaml', workflow)
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('hindsight-metrics-ci-assets.py', workflow)
        rule = docs(OBS / 'alerts/hindsight-alerts.yaml')[0]
        exprs = '\n'.join(x['expr'] for x in rule['spec']['groups'][0]['rules'])
        self.assertIn('hindsight_operation_operations_total', exprs)
        self.assertIn('HindsightMetricSeriesGrowth', [x['alert'] for x in rule['spec']['groups'][0]['rules']])
        self.assertIn('operation_type="retain"', exprs)
        self.assertNotIn('operation_type="batch_retain"', exprs)
        dash = docs(OBS / 'dashboards/hindsight-dashboard.yaml')[0]
        release = docs(OBS / 'helmrelease.yaml')[0]
        folder_key = release['spec']['values']['grafana']['sidecar']['dashboards']['folderAnnotation']
        self.assertEqual(dash['metadata']['labels']['grafana_dashboard'], '1')
        self.assertEqual(dash['metadata']['annotations'][folder_key], 'Observability')
        data = json.loads(dash['data']['hindsight-ingestion.json'])
        self.assertGreaterEqual(len(data['panels']), 8)
        self.assertTrue(any('batch_retain' in p['targets'][0]['expr'] for p in data['panels']))

    def test_prometheus_discovers_hindsight_monitor(self):
        release = docs(OBS / 'helmrelease.yaml')[0]
        self.assertEqual(release['spec']['chart']['spec']['version'], '91.9.0')
        spec = release['spec']['values']['prometheus']['prometheusSpec']
        # Pinned chart 91.9.0 renders these absent selectors as {}; the flag
        # prevents a release-label selector from replacing the empty monitor selector.
        self.assertIs(spec['serviceMonitorSelectorNilUsesHelmValues'], False)
        monitor = docs(APP / 'servicemonitor.yaml')[0]
        self.assertEqual((monitor['metadata']['namespace'], monitor['metadata']['name']),
                         ('ai', 'hindsight-api'))
        self.assertTrue(monitor_discovered(spec, monitor))
        self.assertFalse(monitor_discovered(dict(spec, serviceMonitorSelector={
            'matchLabels': {'prometheus': 'other'}}), monitor))
        self.assertFalse(monitor_discovered(dict(spec, serviceMonitorNamespaceSelector={
            'matchLabels': {'kubernetes.io/metadata.name': 'other'}}), monitor))

    def test_metrics_archive_rejects_mismatch_and_extra_member(self):
        path = ROOT / 'scripts/hindsight-metrics-ci-assets.py'
        spec = importlib.util.spec_from_file_location('metrics_assets', path)
        assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assets)
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / 'tool.tar.gz'
            target = 'linux-amd64/helm'
            payload = b'synthetic-binary'
            def make(extra=False):
                with tarfile.open(archive, 'w:gz') as tar:
                    for name in ([target, 'linux-amd64/extra'] if extra else [target]):
                        data = payload
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        tar.addfile(info, io.BytesIO(data))
            make()
            with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
                assets.verified_tar(archive, '0' * 64, target, hashlib.sha256(payload).hexdigest(),
                                    pathlib.Path(directory) / 'helm', assets.helm_member)
            make(extra=True)
            with self.assertRaisesRegex(ValueError, 'unsafe archive members'):
                assets.verified_tar(archive, assets.digest(archive), target,
                                    hashlib.sha256(payload).hexdigest(),
                                    pathlib.Path(directory) / 'helm', assets.helm_member)


if __name__ == '__main__':
    unittest.main()
