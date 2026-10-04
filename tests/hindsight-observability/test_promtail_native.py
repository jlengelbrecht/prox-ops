"""Check pinned chart routing and native fail-closed log processing."""
import json
import importlib.util
import hashlib
import zipfile
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
PROMTAIL = ROOT / 'kubernetes/apps/observability/loki/app/promtail-helmrelease.yaml'
HINDSIGHT = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml'
CHART = pathlib.Path(os.environ.get('PROMTAIL_CHART', ROOT / '_bmad-output/implementation-artifacts/tooling/promtail-6.17.1.tgz'))
HELM = os.environ.get('HINDSIGHT_HELM', 'helm')
UUID = '12345678-1234-1234-1234-123456789abc'

ORIGINAL_RUNTIME = yaml.safe_load('''
server:
  log_level: info
  log_format: logfmt
  http_listen_port: 3101
positions:
  filename: /run/promtail/positions.yaml
clients:
- backoff_config:
    max_period: 5m
    max_retries: 10
    min_period: 500ms
  batchsize: 102400
  batchwait: 1s
  tenant_id: 1
  timeout: 10s
  url: http://loki:3100/loki/api/v1/push
broad:
  job_name: kubernetes-pods
  pipeline_stages:
  - cri: {}
  kubernetes_sd_configs:
  - role: pod
  relabel_configs:
  - source_labels:
    - __meta_kubernetes_pod_controller_name
    regex: ([0-9a-z-.]+?)(-[0-9a-f]{8,10})?
    action: replace
    target_label: __tmp_controller_name
  - source_labels:
    - __meta_kubernetes_pod_label_app_kubernetes_io_name
    - __meta_kubernetes_pod_label_app
    - __tmp_controller_name
    - __meta_kubernetes_pod_name
    regex: ^;*([^;]+)(;.*)?$
    action: replace
    target_label: app
  - source_labels:
    - __meta_kubernetes_pod_label_app_kubernetes_io_instance
    - __meta_kubernetes_pod_label_instance
    regex: ^;*([^;]+)(;.*)?$
    action: replace
    target_label: instance
  - source_labels:
    - __meta_kubernetes_pod_label_app_kubernetes_io_component
    - __meta_kubernetes_pod_label_component
    regex: ^;*([^;]+)(;.*)?$
    action: replace
    target_label: component
  - action: replace
    source_labels:
    - __meta_kubernetes_pod_node_name
    target_label: node_name
  - action: replace
    source_labels:
    - __meta_kubernetes_namespace
    target_label: namespace
  - action: replace
    replacement: $1
    separator: /
    source_labels:
    - namespace
    - app
    target_label: job
  - action: replace
    source_labels:
    - __meta_kubernetes_pod_name
    target_label: pod
  - action: replace
    source_labels:
    - __meta_kubernetes_pod_container_name
    target_label: container
  - action: replace
    replacement: /var/log/pods/*$1/*.log
    separator: /
    source_labels:
    - __meta_kubernetes_pod_uid
    - __meta_kubernetes_pod_container_name
    target_label: __path__
  - action: replace
    regex: true/(.*)
    replacement: /var/log/pods/*$1/*.log
    separator: /
    source_labels:
    - __meta_kubernetes_pod_annotationpresent_kubernetes_io_config_hash
    - __meta_kubernetes_pod_annotation_kubernetes_io_config_hash
    - __meta_kubernetes_pod_container_name
    target_label: __path__
''')

def render(values):
    rendered = subprocess.run([HELM, 'template', 'promtail', str(CHART), '--namespace',
                               'observability', '--values', '-'], input=yaml.safe_dump(values),
                              text=True, capture_output=True, check=True)
    secret = next(doc for doc in yaml.safe_load_all(rendered.stdout)
                  if doc and doc.get('kind') == 'Secret' and doc['metadata']['name'] == 'promtail')
    return yaml.safe_load(secret['stringData']['promtail.yaml'])


def route(job, labels):
    for rule in job['relabel_configs']:
        if rule.get('action') not in ('drop', 'keep'):
            continue
        value = rule.get('separator', ';').join(labels.get(name, '') for name in rule['source_labels'])
        matches = bool(re.fullmatch(rule['regex'], value))
        if rule['action'] == 'drop' and matches or rule['action'] == 'keep' and not matches:
            return False
    return True


class NativeCollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CHART.is_file():
            raise AssertionError('pinned Promtail chart required')
        cls.values = yaml.safe_load(PROMTAIL.read_text())['spec']['values']
        cls.runtime = render(cls.values)
        cls.broad, cls.safe = cls.runtime['scrape_configs']

    def test_unrelated_chart_job_is_effectively_unchanged(self):
        api_env = yaml.safe_load(HINDSIGHT.read_text())['spec']['values']['api']['env']
        self.assertEqual(api_env['HINDSIGHT_API_LOG_LEVEL'], 'info')
        self.assertEqual(api_env['HINDSIGHT_API_LOG_FORMAT'], 'json')
        self.assertEqual(api_env['HINDSIGHT_API_LOG_JSON_FIELDS'], 'severity,message,timestamp,logger')
        for key in ('server', 'positions', 'clients'):
            self.assertEqual(self.runtime[key], ORIGINAL_RUNTIME[key])
        original = ORIGINAL_RUNTIME['broad']
        self.assertEqual(self.broad['job_name'], 'kubernetes-pods')
        self.assertEqual(self.broad['pipeline_stages'], original['pipeline_stages'])
        self.assertEqual(self.broad['relabel_configs'][:-2], original['relabel_configs'])
        self.assertEqual(self.broad['relabel_configs'][-1]['action'], 'drop')
        self.assertEqual(self.broad['relabel_configs'][-1]['source_labels'],
                         ['__meta_kubernetes_namespace', '__meta_kubernetes_pod_name'])
        self.assertEqual(self.safe['job_name'], 'hindsight-api-safe')
        self.assertEqual(original['relabel_configs'][-1], self.broad['relabel_configs'][-3])
        verified = 'ai;hindsight-api-.+;ReplicaSet;hindsight-api-.+;hindsight;hindsight;api'
        self.assertEqual(self.broad['relabel_configs'][-2]['regex'], verified)
        self.assertTrue(any(x.get('action') == 'keep' and x.get('regex') == verified
                            for x in self.safe['relabel_configs']))
        self.assertFalse(any('__meta_kubernetes_pod_phase' in x.get('source_labels', [])
                             for x in self.safe['relabel_configs']))
        self.assertFalse(any(x.get('action') == 'labelmap' for x in self.safe['relabel_configs']))
        self.assertEqual({x['target_label'] for x in self.safe['relabel_configs'] if 'target_label' in x} - {'__path__'},
                         {'namespace', 'app', 'container', 'pod_uid'})
        self.assertTrue(any(x.get('drop', {}).get('longer_than') == 4096 and
                            x.get('drop', {}).get('drop_counter_reason') == 'hindsight_oversized'
                            for x in self.safe['pipeline_stages']))
        self.assertIn('drop', self.safe['pipeline_stages'][0])
        self.assertEqual(self.safe['pipeline_stages'][0]['drop']['longer_than'], 4096)
        self.assertTrue(any(x.get('match', {}).get('drop_counter_reason') == 'hindsight_unrecognized'
                            for x in self.safe['pipeline_stages']))

    def test_both_rendered_jobs_route_drift_without_raw_path(self):
        base = {'__meta_kubernetes_namespace': 'ai',
                '__meta_kubernetes_pod_name': 'hindsight-api-755c9d-abcde',
                '__meta_kubernetes_pod_controller_kind': 'ReplicaSet',
                '__meta_kubernetes_pod_controller_name': 'hindsight-api-755c9d',
                '__meta_kubernetes_pod_label_app_kubernetes_io_name': 'hindsight',
                '__meta_kubernetes_pod_label_app_kubernetes_io_instance': 'hindsight',
                '__meta_kubernetes_pod_label_app_kubernetes_io_component': 'api',
                '__meta_kubernetes_pod_container_name': 'api',
                '__meta_kubernetes_pod_phase': 'Running'}
        for changes, expected in [({}, (False, True)),
                                  ({'__meta_kubernetes_pod_phase': 'Failed'}, (False, True)),
                                  ({'__meta_kubernetes_pod_phase': 'Succeeded'}, (False, True)),
                                  ({'__meta_kubernetes_pod_label_app_kubernetes_io_name': 'changed',
                                    '__meta_kubernetes_pod_label_app_kubernetes_io_component': ''}, (False, False)),
                                  ({'__meta_kubernetes_pod_controller_name': 'other'}, (False, False)),
                                  ({'__meta_kubernetes_pod_container_name': 'sidecar'}, (False, False)),
                                  ({'__meta_kubernetes_pod_label_app_kubernetes_io_instance': 'unrelated'}, (False, False)),
                                  ({'__meta_kubernetes_pod_name': 'unrelated-abcde'}, (True, False)),
                                  ({'__meta_kubernetes_namespace': 'other'}, (True, False))]:
            labels = {**base, **changes}
            with self.subTest(changes=changes):
                self.assertEqual((route(self.broad, labels), route(self.safe, labels)), expected)

    def test_rendered_safe_path_replacement(self):
        rule = next(x for x in self.safe['relabel_configs'] if x.get('target_label') == '__path__')
        self.assertEqual(rule['source_labels'], ['__meta_kubernetes_pod_uid', '__meta_kubernetes_pod_container_name'])
        def path(uid, container):
            value = rule['separator'].join((uid, container))
            match = re.fullmatch(rule.get('regex', '(.*)'), value)
            self.assertIsNotNone(match)
            return rule['replacement'].replace('$1', match.group(1))
        self.assertEqual(path('12345678-1234-1234-1234-123456789abc', 'api'),
                         '/var/log/pods/*12345678-1234-1234-1234-123456789abc/api/*.log')
        with self.assertRaises(AssertionError):
            self.assertEqual(path('drift', 'sidecar'), '/var/log/pods/*drift/api/*.log')

    def test_synthetic_lines_are_sanitized_or_dropped(self):
        binary = os.environ.get('PROMTAIL_BIN') or shutil.which('promtail') or str(ROOT / '_bmad-output/implementation-artifacts/tooling/promtail-linux-amd64')
        self.assertTrue(pathlib.Path(binary).is_file(), 'native Promtail binary required')
        config = {'server': {'disable': True},
                  'positions': {'filename': '/tmp/promtail-hindsight-native-positions.yaml'},
                  'clients': [{'url': 'http://127.0.0.1:1/loki/api/v1/push'}],
                  'scrape_configs': [{'job_name': 'hindsight-api-safe-fixture',
                                      'static_configs': [{'targets': ['localhost'], 'labels': {'job': 'fixture'}}],
                                      'pipeline_stages': self.safe['pipeline_stages']}]}
        messages = [
            {'severity': 'INFO', 'logger': 'hindsight_api.worker.poller', 'message': f'Task {UUID} deferred until 2026-10-03: bearer synthetic-secret note text'},
            {'severity': 'WARNING', 'logger': 'hindsight_api.worker.poller', 'message': f'Task {UUID} scheduled for retry at 2026-10-03: token synthetic-secret'},
            {'severity': 'ERROR', 'logger': 'hindsight_api.worker.poller', 'message': f'Task {UUID} timed out: token synthetic-secret'},
            {'severity': 'ERROR', 'logger': 'hindsight_api.worker.poller', 'message': f'Task {UUID} failed: token synthetic-secret', 'exception': 'synthetic-secret'},
            {'severity': 'ERROR', 'logger': 'hindsight_api.not_a_worker', 'message': f'Task {UUID} failed: nonworker-secret'},
            {'severity': 'ERROR', 'logger': 'hindsight_api.worker.poller.extra', 'message': f'Task {UUID} failed: near-secret'},
            {'severity': 'ERROR', 'message': f'Task {UUID} failed: missing-secret'},
            {'severity': 'INFO', 'message': 'note text synthetic-secret'},
            {'severity': 'ERROR', 'message': 'Traceback synthetic-secret', 'exception': 'synthetic-secret'},
        ]
        raw = [json.dumps(x) for x in messages] + ['{bad json', 'X' * 5000]
        payload = '\n'.join(
            json.dumps({'log': line + '\n', 'stream': 'stdout', 'time': '2026-10-03T09:00:00Z'})
            if index % 2 == 0 else f'2026-10-03T09:00:00Z stdout F {line}'
            for index, line in enumerate(raw)) + '\n'
        payload += json.dumps({'log': json.dumps(messages[0]) + '\n', 'stream': 'stdout',
                               'time': '2026-10-03T09:00:00Z', 'padding': 'P' * 4200}) + '\n'
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'promtail.yaml'
            path.write_text(yaml.safe_dump(config))
            result = subprocess.run([binary, '--dry-run', '--stdin', f'--config.file={path}'],
                                    input=payload, text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = result.stdout
        self.assertEqual(output.count('"operation_id"'), 4, output)
        for marker in (UUID, 'deferred until', 'scheduled for retry at', 'timed out:', 'failed:'):
            self.assertIn(marker, output)
        for marker in ('synthetic-secret', 'nonworker-secret', 'near-secret', 'missing-secret',
                       'note text', 'Traceback', '{bad json', 'X' * 100, 'P' * 100):
            self.assertNotIn(marker, output)
        self.assertLess(max(map(len, output.splitlines())), 1024)

    def test_retained_terminal_file_line_uses_native_safe_pipeline(self):
        binary = os.environ.get('PROMTAIL_BIN') or shutil.which('promtail') or str(ROOT / '_bmad-output/implementation-artifacts/tooling/promtail-linux-amd64')
        self.assertTrue(pathlib.Path(binary).is_file(), 'native Promtail binary required')
        config = {'server': {'disable': True},
                  'positions': {'filename': '/tmp/promtail-terminal-fixture-positions.yaml'},
                  'clients': [{'url': 'http://127.0.0.1:1/loki/api/v1/push'}],
                  'scrape_configs': [{'job_name': 'terminal-safe-fixture',
                                      'static_configs': [{'targets': ['localhost'], 'labels': {'job': 'fixture'}}],
                                      'pipeline_stages': self.safe['pipeline_stages']}]}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            retained = path / 'final.log'
            retained.write_text('2026-10-03T09:00:00Z stdout F ' + json.dumps({
                'logger': 'hindsight_api.worker.poller',
                'message': f'Task {UUID} failed: synthetic-secret', 'exception': 'synthetic-secret'}) + '\n')
            cfg = path / 'promtail.yaml'
            cfg.write_text(yaml.safe_dump(config))
            result = subprocess.run([binary, '--dry-run', '--stdin', f'--config.file={cfg}'],
                                    input=retained.read_text(), text=True,
                                    capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"event":"failed:"', result.stdout)
        self.assertIn(UUID, result.stdout)
        self.assertNotIn('synthetic-secret', result.stdout)

    def test_logging_archive_rejects_extra_member_and_workflow_is_read_only(self):
        workflow = (ROOT / '.github/workflows/hindsight-logging.yaml').read_text()
        self.assertIn('permissions:\n  contents: read', workflow)
        self.assertIn('hindsight-logging-ci-assets.py', workflow)
        self.assertIn('persist-credentials: false', workflow)
        spec = importlib.util.spec_from_file_location('logging_assets', ROOT / 'scripts/hindsight-logging-ci-assets.py')
        assets = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(assets)
        with tempfile.TemporaryDirectory() as directory:
            archive = pathlib.Path(directory) / 'promtail.zip'
            with zipfile.ZipFile(archive, 'w') as zf:
                zf.writestr('promtail-linux-amd64', b'synthetic')
                zf.writestr('unexpected', b'extra')
            with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
                assets.verified_promtail(archive, pathlib.Path(directory) / 'binary')
            with mock.patch.object(assets, 'digest', return_value=assets.PROMTAIL_ARCHIVE):
                with self.assertRaisesRegex(ValueError, 'unsafe archive members'):
                    assets.verified_promtail(archive, pathlib.Path(directory) / 'binary')


if __name__ == '__main__':
    unittest.main()
