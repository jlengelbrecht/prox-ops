"""Run the pinned Alloy binary on the Helm-rendered collector config against real pod log files."""
import concurrent.futures
import datetime
import http.server
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

import yaml

import test_promtail_native as legacy

ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE = ROOT / 'kubernetes/apps/observability/loki/app/alloy-helmrelease.yaml'
RBAC = RELEASE.parent / 'alloy-rbac.yaml'
TOOLING = ROOT / '_bmad-output/implementation-artifacts/tooling'
CHART = pathlib.Path(os.environ.get('ALLOY_CHART', TOOLING / 'alloy-1.13.0.tgz'))
ALLOY = os.environ.get('ALLOY_BIN') or str(TOOLING / 'alloy')
PROMTAIL = os.environ.get('PROMTAIL_BIN') or str(TOOLING / 'promtail-linux-amd64')
UUID = legacy.UUID
CRI_TS = '2026-10-03T09:00:00Z'
EVENTS = ('deferred until', 'scheduled for retry at', 'timed out:', 'failed:')
PRIVATE_MARKERS = ('synthetic-secret', 'nonworker-secret', 'near-secret', 'missing-secret', 'poller-secret',
                   'note text', 'Traceback', '{bad json', 'X' * 100, 'P' * 100)
SAFE_LABELS = {'namespace': 'ai', 'app': 'hindsight', 'container': 'api', 'stream': 'stdout'}
SENTINEL = 'ordinary-sentinel'
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
# Dry-run pads cells with tabs and emits none after a cell whose width is a multiple of eight.
DRY_RUN = re.compile(r'(?P<ts>\d{4}-\d\d-\d\dT[\d:.]+[+-]\d{4})\t*(?P<labels>\{(?:' + LABEL.pattern + r'(?:, )?)*\})\t*(?P<line>.*)')

def uid(n):
    return f'00000000-0000-4000-8000-{n:012d}'

OVERSIZED_UUID,PREFIX_UUID, RESTART_UUID = uid(96), uid(97), uid(98)
HINDSIGHT_REPLICASET = 'hindsight-api-755c9d'

def pod(namespace, name, controller, app, instance, component, container, pod_uid, **extra):
    meta = {'namespace': namespace, 'pod_name': name, 'pod_controller_kind': 'ReplicaSet',
            'pod_controller_name': controller, 'pod_label_app_kubernetes_io_name': app,
            'pod_label_app_kubernetes_io_instance': instance, 'pod_label_app_kubernetes_io_component': component,
            'pod_container_name': container, 'pod_node_name': 'work-1', 'pod_uid': pod_uid, **extra}
    return {'__meta_kubernetes_' + k: v for k, v in meta.items()}

def hindsight(pod_uid, **changes):
    meta = pod('ai', f'{HINDSIGHT_REPLICASET}-abcde', HINDSIGHT_REPLICASET, 'hindsight', 'hindsight', 'api',
               'api', pod_uid, pod_phase='Running')
    return {**meta, **changes}

ORDINARY = pod('media', 'web-6d4c8f7b9-xyz12', 'web-6d4c8f7b9', 'web', 'web-prod', 'frontend', 'web', uid(99))
ORDINARY_LABELS = {'app': 'web', 'instance': 'web-prod', 'component': 'frontend', 'node_name': 'work-1',
                   'namespace': 'media', 'job': 'media/web', 'pod': 'web-6d4c8f7b9-xyz12', 'container': 'web', 'stream': 'stdout'}
ORDINARY_LINES = [('2026-10-03T09:00:01.123456789Z', 'ordinary line one'), ('2026-10-03T09:00:02Z', SENTINEL)]
# Same matrix as the Promtail suite: (broad path forwards, safe path forwards).
ROUTES = [({}, (False, True)),
          ({'__meta_kubernetes_pod_phase': 'Failed'}, (False, True)),
          ({'__meta_kubernetes_pod_phase': 'Succeeded'}, (False, True)),
          ({'__meta_kubernetes_pod_label_app_kubernetes_io_name': 'changed',
            '__meta_kubernetes_pod_label_app_kubernetes_io_component': ''}, (False, False)),
          ({'__meta_kubernetes_pod_controller_name': 'other'}, (False, False)),
          ({'__meta_kubernetes_pod_container_name': 'sidecar'}, (False, False)),
          ({'__meta_kubernetes_pod_label_app_kubernetes_io_instance': 'unrelated'}, (False, False)),
          ({'__meta_kubernetes_pod_name': 'unrelated-abcde'}, (True, False)),
          ({'__meta_kubernetes_namespace': 'other'}, (True, False))]

def ns(text):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:?\d\d)', text.strip())
    if not match:
        raise ValueError(f'timestamp {text!r}')
    zone = '+00:00' if match[3] == 'Z' else match[3][:3] + ':' + match[3][-2:]
    seconds = int(datetime.datetime.fromisoformat(match[1] + zone).timestamp())
    return seconds * 10**9 + int((match[2] or '').ljust(9, '0'))

def cri(line, ts=CRI_TS):
    return f'{ts} stdout F {line}'

def event(event_uuid, kind, note):
    return json.dumps({'severity': 'ERROR', 'logger': 'hindsight_api.worker.poller',
                       'message': f'Task {event_uuid} {kind} {note}'})

def safe_line(kind, event_uuid=UUID):
    return f'{{"event":"{kind}","operation_id":"{event_uuid}"}}'

def sanitizer_payload():
    poller = 'hindsight_api.worker.poller'
    messages = [
        {'severity': 'INFO', 'logger': poller, 'message': f'Task {UUID} deferred until 2026-10-03: bearer synthetic-secret note text'},
        {'severity': 'WARNING', 'logger': poller, 'message': f'Task {UUID} scheduled for retry at 2026-10-03: token synthetic-secret'},
        {'severity': 'ERROR', 'logger': poller, 'message': f'Task {UUID} timed out: token synthetic-secret'},
        {'severity': 'ERROR', 'logger': poller, 'message': f'Task {UUID} failed: token synthetic-secret', 'exception': 'synthetic-secret'},
        {'severity': 'ERROR', 'logger': 'hindsight_api.not_a_worker', 'message': f'Task {UUID} failed: nonworker-secret'},
        {'severity': 'ERROR', 'logger': poller + '.extra', 'message': f'Task {UUID} failed: near-secret'},
        {'severity': 'ERROR', 'message': f'Task {UUID} failed: missing-secret'},
        {'severity': 'INFO', 'message': 'note text synthetic-secret'},
        {'severity': 'ERROR', 'message': 'Traceback synthetic-secret', 'exception': 'synthetic-secret'},
        {'severity': 'INFO', 'logger': poller, 'message': f'Task {UUID} started: poller-secret'},
    ]
    raw = [json.dumps(x) for x in messages] + ['{bad json', 'X' * 5000]
    # One format per file, as a runtime writes: in a mixed file Promtail's cri stage fudges docker lines +1ns.
    docker = [json.dumps({'log': line + '\n', 'stream': 'stdout', 'time': CRI_TS}) for line in raw[::2]]
    padded = event(OVERSIZED_UUID, 'deferred until', 'padded')
    docker.append(json.dumps({'log': padded + '\n', 'stream': 'stdout', 'time': CRI_TS, 'padding': 'P' * 4200}))
    return docker, [cri(line) for line in raw[1::2]]

def write_pod(root, meta, lines, name='0.log'):
    m = {k.removeprefix('__meta_kubernetes_'): v for k, v in meta.items()}
    path = pathlib.Path(root) / f"{m['namespace']}_{m['pod_name']}_{m['pod_uid']}" / m['pod_container_name'] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as stream:
        stream.write(''.join(line + '\n' for line in lines))
    return path

def build_tree(root):
    write_pod(root, ORDINARY, [cri(line, ts) for ts, line in ORDINARY_LINES])
    targets = [ORDINARY] + [hindsight(uid(n), **changes) for n, (changes, _) in enumerate(ROUTES)]
    for n, meta in enumerate(targets[2:], 1):
        write_pod(root, meta, [cri(event(UUID, 'failed:', f'route-{n}'))])
    for name, lines in zip(('0.log', '1.log'), sanitizer_payload()):
        write_pod(root, targets[1], lines, name)
    return targets

def rendered(**overrides):
    values = {**yaml.safe_load(RELEASE.read_text())['spec']['values'], **overrides}
    result = subprocess.run([legacy.HELM, 'template', 'alloy', str(CHART), '--namespace', 'observability',
                             '--values', '-'], input=yaml.safe_dump(values), text=True, capture_output=True,
                            check=True, timeout=120)
    return [d for d in yaml.safe_load_all(result.stdout) if d]

def token_problems(docs, grant):
    found = {d['kind'] for d in docs if d['kind'] in ('Role', 'RoleBinding', 'ClusterRole', 'ClusterRoleBinding')}
    account = next(d for d in docs if d['kind'] == 'DaemonSet')['spec']['template']['spec'].get('serviceAccountName')
    api = 'rbac.authorization.k8s.io'
    if [d for d in yaml.safe_load_all(grant) if d] != [
            {'apiVersion': f'{api}/v1', 'kind': 'ClusterRole', 'metadata': {'name': 'alloy'},
             'rules': [{'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']}]},
            {'apiVersion': f'{api}/v1', 'kind': 'ClusterRoleBinding', 'metadata': {'name': 'alloy'},
             'roleRef': {'apiGroup': api, 'kind': 'ClusterRole', 'name': 'alloy'},
             'subjects': [{'kind': 'ServiceAccount', 'name': account, 'namespace': 'observability'}]}]:
        found.add('grant')
    return found

def swap(text, old, new, count):
    if text.count(old) != count:
        raise AssertionError(f'expected {count} x {old!r} in rendered config')
    return text.replace(old, new)

def harness(config, root, targets, push_url, legacy_positions):
    """Swap only the cluster endpoints: discovery input, file root, legacy file and Loki URL."""
    lines = config.split('\n')
    start = lines.index('discovery.kubernetes "pods" {')
    config = '\n'.join(lines[:start] + lines[lines.index('}', start) + 1:])
    literal = '[' + ', '.join('{' + ', '.join(f'{json.dumps(k)} = {json.dumps(v)}' for k, v in t.items()) + '}'
                              for t in targets) + ']'
    config = swap(config, 'discovery.kubernetes.pods.targets', literal, 2)
    config = swap(config, '"/var/log/pods/', f'"{root}/', 3)
    config = swap(config, '"/run/promtail/positions.yaml"', json.dumps(str(legacy_positions)), 2)
    return swap(config, '"http://loki:3100/loki/api/v1/push"', json.dumps(push_url), 1)

def without(config, component, header, *markers):
    """Remove the first block under a top-level component whose body contains every marker."""
    lines = config.split('\n')
    start = lines.index(component + ' {')
    for head in range(start + 1, lines.index('}', start)):
        text = ' '.join(lines[head].split())
        if text == header + ' {}' and not markers:
            return '\n'.join(lines[:head] + lines[head + 1:])
        if text == header + ' {':
            close = lines.index(lines[head][:len(lines[head]) - len(lines[head].lstrip())] + '}', head)
            body = [' '.join(x.split()) for x in lines[head + 1:close]]
            if all(any(marker in x for x in body) for marker in markers):
                return '\n'.join(lines[:head] + lines[close + 1:])
    raise AssertionError(f'mutation target missing: {component} {header} {markers}')

def varint(data, i):
    shift = value = 0
    while i < len(data):
        value, i, shift = value | (data[i] & 0x7F) << shift, i + 1, shift + 7
        if data[i - 1] < 0x80:
            return value, i
    raise ValueError('truncated varint')

def unsnappy(data):
    length, i = varint(data, 0)
    out = bytearray()
    while i < len(data):
        tag, i = data[i], i + 1
        if tag & 3 == 0:
            size = tag >> 2
            if size >= 60:
                size, i = int.from_bytes(data[i:i + size - 59], 'little'), i + size - 59
            out, i = out + data[i:i + size + 1], i + size + 1
            continue
        if tag & 3 == 1:
            size, offset, i = ((tag >> 2) & 7) + 4, ((tag >> 5) << 8) | data[i], i + 1
        else:
            width = 2 if tag & 3 == 2 else 4
            size, offset, i = (tag >> 2) + 1, int.from_bytes(data[i:i + width], 'little'), i + width
        if not 0 < offset <= len(out):
            raise ValueError('snappy offset')
        for _ in range(size):
            out.append(out[-offset])
    if len(out) != length:
        raise ValueError('snappy length')
    return bytes(out)

def fields(data):
    i = 0
    while i < len(data):
        key, i = varint(data, i)
        if key & 7 == 0:
            value, i = varint(data, i)
        elif key & 7 == 2:
            size, i = varint(data, i)
            value, i = data[i:i + size], i + size
        elif key & 7 in (1, 5):
            width = 8 if key & 7 == 1 else 4
            value, i = data[i:i + width], i + width
        else:
            raise ValueError('wire type')
        yield key >> 3, value

def labels_of(text):
    return {k: json.loads(f'"{v}"') for k, v in LABEL.findall(text)}

def push_entries(payload):
    """Decode a Loki PushRequest: streams(1){labels(1), entries(2){timestamp(1){s(1), ns(2)}, line(2)}}."""
    for number, stream in fields(payload):
        parts = list(fields(stream)) if number == 1 else []
        labels = labels_of(next((v.decode() for k, v in parts if k == 1), ''))
        for entry in (v for k, v in parts if k == 2):
            values = dict(fields(entry))
            stamp = dict(fields(values.get(1, b'')))
            yield labels, stamp.get(1, 0) * 10**9 + stamp.get(2, 0), values.get(2, b'').decode()

class Receiver:
    def __init__(self):
        self.entries, self.tenants, self.errors = [], set(), []
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                try:
                    owner.entries += list(push_entries(unsnappy(body)))
                    owner.tenants.add(self.headers.get('X-Scope-OrgID'))
                    self.send_response(204)
                except ValueError as exc:
                    owner.errors.append(str(exc))
                    self.send_response(400)
                self.end_headers()

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}/loki/api/v1/push'

def run_until(command, output, progress, done, settle=2.0, timeout=40):
    """Run a collector until done(progress()) holds and output has been stable for `settle` seconds."""
    with output.open('w+') as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline, seen, changed = time.monotonic() + timeout, None, time.monotonic()
            while time.monotonic() < deadline and proc.poll() is None:
                current = progress()
                if current != seen:
                    seen, changed = current, time.monotonic()
                elif done(current) and time.monotonic() - changed >= settle:
                    break
                time.sleep(0.2)
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(20)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        log.seek(0)
        return log.read()

def run_alloy(config, work, done, root, targets, legacy_positions):
    work.mkdir(parents=True, exist_ok=True)
    receiver = Receiver()
    try:
        (path := work / 'config.alloy').write_text(harness(config, root, targets, receiver.url, legacy_positions))
        for _ in range(3):  # a probed port can be taken by a parallel run before Alloy binds it
            with socket.socket() as probe:  # memberlist needs a concrete port even with clustering off
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            log = run_until([ALLOY, 'run', '--disable-reporting', f'--storage.path={work / "data"}',
                             f'--server.http.listen-addr=127.0.0.1:{port}', str(path)],
                            work / 'alloy.log', lambda: list(receiver.entries), done)
            if 'address already in use' not in log:
                break
    finally:
        receiver.server.shutdown()
        receiver.server.server_close()
    if receiver.errors or not done(receiver.entries):
        raise AssertionError(f'Alloy run incomplete {receiver.errors}: ' + re.sub(r'.*level=info .*\n?', '', log)[-3000:])
    return receiver

def has_sentinel(entries):
    return any(SENTINEL in line for _, _, line in entries)

def problems(receiver):
    """Classify every semantic failure of one run; an empty set means the pipeline behaved."""
    found, entries = set(), receiver.entries
    if receiver.tenants != {'1'}:
        found.add('tenant')
    if any(marker in line for _, _, line in entries for marker in PRIVATE_MARKERS):
        found.add('filtering')
    if any(OVERSIZED_UUID in line or len(line) >= 1024 for _, _, line in entries):
        found.add('length')
    safe = [e for e in entries if e[0].get('pod_uid') == uid(0)]
    if sorted(line for _, _, line in safe) != sorted(safe_line(kind) for kind in EVENTS):
        found.add('filtering')
    ordinary = [(ts, line) for labels, ts, line in entries if uid(99) in labels.get('filename', '')]
    if any(ts != ns(CRI_TS) for _, ts, _ in safe) or ordinary != [(ns(ts), x) for ts, x in ORDINARY_LINES]:
        found.add('timestamps')
    for labels, _, line in entries:
        if line.startswith('{"event"') and (set(labels) != {*SAFE_LABELS, 'pod_uid', 'filename'} or
                                            {k: labels[k] for k in SAFE_LABELS} != SAFE_LABELS):
            found.add('labels')
        if uid(99) in labels.get('filename', '') and {k: v for k, v in labels.items() if k != 'filename'} != ORDINARY_LABELS:
            found.add('labels')
    for n, (_, expected) in enumerate(ROUTES):
        broad = any('pod_uid' not in labels and uid(n) in labels.get('filename', '') for labels, _, _ in entries)
        if (broad, any(labels.get('pod_uid') == uid(n) for labels, _, _ in entries)) != expected:
            found.add('routing')
    return found

class NativeAlloyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not pathlib.Path(ALLOY).is_file():
            raise AssertionError(f'native Alloy binary required: {ALLOY}')
        cls.docs = rendered()
        if broad := token_problems(cls.docs, RBAC.read_text()):
            raise AssertionError(f'collector token is not limited to pod reads: {broad}')
        cls.config = next(d for d in cls.docs if d['kind'] == 'ConfigMap' and 'config.alloy' in d.get('data', {}))['data']['config.alloy']
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.dir = pathlib.Path(cls.tmp.name)
        cls.root = cls.dir / 'pods'
        cls.targets = build_tree(cls.root)
        cls.base = cls.collect(cls.config, 'base')

    @classmethod
    def collect(cls, config, name):
        # The legacy file is absent here, as it is on a node after reboot or once Promtail is gone.
        return run_alloy(config, cls.dir / name, has_sentinel, cls.root, cls.targets, cls.dir / 'absent.yaml')

    def test_binary_matches_image_tag_and_formats_config(self):
        tag = yaml.safe_load(RELEASE.read_text())['spec']['values']['image']['tag']
        result = subprocess.run([ALLOY, '--version'], text=True, capture_output=True, timeout=30)
        self.assertIn(f'version {tag} ', result.stdout + result.stderr)
        self.assertIn('"{\\"event\\":\\"{{ .event }}\\",\\"operation_id\\":\\"{{ .operation_id }}\\"}"', self.config)
        (self.dir / 'fmt.alloy').write_text(self.config)
        result = subprocess.run([ALLOY, 'fmt', str(self.dir / 'fmt.alloy')], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rendered_chart_matches_release_chart_version(self):
        version = yaml.safe_load(RELEASE.read_text())['spec']['chart']['spec']['version']
        daemonset = next(d for d in self.docs if d['kind'] == 'DaemonSet')
        self.assertEqual(daemonset['metadata']['labels'].get('helm.sh/chart'), f'alloy-{version}')

    def test_chart_role_or_widened_grant_is_rejected(self):
        self.assertIn('ClusterRole', token_problems(rendered(rbac={'create': True}), RBAC.read_text()))
        self.assertEqual(token_problems(self.docs, swap(RBAC.read_text(), '[pods]', '[pods, secrets]', 1)), {'grant'})

    def test_sanitizer_routing_labels_timestamps_and_tenant(self):
        self.assertEqual(problems(self.base), set())

    def test_each_unsafe_mutation_fails_semantically(self):
        broad, safe, out = 'discovery.relabel "broad"', 'discovery.relabel "hindsight_safe"', 'loki.process "hindsight_safe"'
        one_drop = without(self.config, broad, 'rule', 'regex = "ai;hindsight-api-.+"')
        mutations = {
            'broad namespace drop': (one_drop, {'routing'}),
            'both broad drops': (without(one_drop, broad, 'rule', 'action = "drop"'), {'routing', 'filtering'}),
            'event match drop': (without(self.config, out, 'stage.match', 'event!~'), {'filtering'}),
            'worker match drop': (without(self.config, out, 'stage.match', 'worker_logger!='), {'filtering'}),
            'template and output': (without(without(self.config, out, 'stage.template', 'safe_line'),
                                            out, 'stage.output', 'safe_line'), {'filtering'}),
            'length guard': (without(self.config, out, 'stage.drop', 'longer_than'), {'length'}),
            'broad cri': (without(self.config, 'loki.process "broad"', 'stage.cri'), {'timestamps'}),
            'safe pod_uid label': (without(self.config, safe, 'rule', 'target_label = "pod_uid"'), {'labels'}),
            'tenant': (re.sub(r'(tenant_id\s*=\s*)"1"', r'\1"2"', self.config), {'tenant'}),
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            runs = {name: pool.submit(self.collect, config, f'mutation-{i}')
                    for i, (name, (config, _)) in enumerate(mutations.items())}
        for name, (_, expected) in mutations.items():
            with self.subTest(mutation=name):
                self.assertTrue(expected <= problems(runs[name].result()), name)

    def test_labels_timestamps_and_lines_match_promtail(self):
        self.assertTrue(pathlib.Path(PROMTAIL).is_file(), f'native Promtail binary required: {PROMTAIL}')
        runtime = legacy.render(yaml.safe_load(legacy.PROMTAIL.read_text())['spec']['values'])
        jobs = [json.loads(json.dumps(job).replace('/var/log/pods/', f'{self.root}/')) for job in runtime['scrape_configs']]
        for job in jobs:
            del job['kubernetes_sd_configs']
            job['static_configs'] = [{'targets': ['localhost'], 'labels': t} for t in self.targets]
        (work := self.dir / 'promtail').mkdir()
        (work / 'promtail.yaml').write_text(yaml.safe_dump({
            'server': {'disable': True}, 'scrape_configs': jobs, 'positions': {'filename': str(work / 'positions.yaml')},
            'clients': [{'url': 'http://127.0.0.1:1/loki/api/v1/push'}]}))
        out = work / 'promtail.out'

        def entries():
            text = re.sub(r'\x1b\[[0-9;]*m', '', out.read_text()) if out.exists() else ''
            return [(labels_of(m['labels']), ns(m['ts']), m['line']) for m in map(DRY_RUN.fullmatch, text.splitlines()) if m]

        log = run_until([PROMTAIL, '--dry-run', '--log.level=warn', f'--config.file={work / "promtail.yaml"}'], out, entries, has_sentinel)
        self.assertTrue(has_sentinel(entries()), log[-3000:])
        self.assertEqual(*(sorted((sorted(labels.items()), ts, line) for labels, ts, line in run)
                           for run in (entries(), self.base.entries)))

    def test_legacy_positions_import_once_then_own_storage(self):
        root, work, api = self.dir / 'positions-pods', self.dir / 'positions', hindsight(uid(0))
        files = [write_pod(root, ORDINARY, [cri('prefix one'), cri('prefix two')]),
                 write_pod(root, api, [cri(event(PREFIX_UUID, kind, 'old')) for kind in EVENTS[:2]])]
        offsets = {str(path): str(path.stat().st_size) for path in files}
        write_pod(root, ORDINARY, [cri('suffix one'), cri(SENTINEL)])
        write_pod(root, api, [cri(event(UUID, kind, 'new')) for kind in EVENTS[2:]])
        legacy_file = self.dir / 'promtail-positions.yaml'
        legacy_file.write_text(yaml.safe_dump({'positions': offsets}))
        first = run_alloy(self.config, work, lambda e: has_sentinel(e) and sum('operation_id' in x[2] for x in e) >= 2,
                          root, [ORDINARY, api], legacy_file)
        self.assertEqual(sorted(x[2] for x in first.entries),
                         sorted(['suffix one', SENTINEL] + [safe_line(k) for k in EVENTS[2:]]))
        write_pod(root, ORDINARY, [cri('after restart ' + SENTINEL)])
        write_pod(root, api, [cri(event(RESTART_UUID, 'failed:', 'restart'))])
        second = run_alloy(self.config, work, lambda e: has_sentinel(e) and any(RESTART_UUID in x[2] for x in e),
                           root, [ORDINARY, api], legacy_file)
        self.assertEqual(sorted(x[2] for x in second.entries), sorted(['after restart ' + SENTINEL, safe_line('failed:', RESTART_UUID)]))

    def test_short_lived_files_born_after_discovery_are_kept_whole(self):
        """A short Job's files appear after the first scan and are deleted five seconds later."""
        root, lines = self.dir / 'short-pods', 177
        job = pod('media', 'report-29310240-q7xkz', 'report-29310240', 'report', 'report-prod', 'cron', 'report',
                  uid(94), pod_controller_kind='Job')
        api = hindsight(uid(95))
        stamps = [f'2026-10-03T09:01:00.{n:06d}Z' for n in range(lines)]
        kinds = [EVENTS[n % len(EVENTS)] for n in range(lines)]
        write_pod(root, ORDINARY, [cri(SENTINEL)])

        def churn():
            paths = [write_pod(root, job, [cri(f'short line {n}', ts) for n, ts in enumerate(stamps)]),
                     write_pod(root, api, [cri(event(uid(1000 + n), kind, 'synthetic-secret'), ts)
                                           for n, (kind, ts) in enumerate(zip(kinds, stamps))])]
            time.sleep(5)
            for path in paths:
                shutil.rmtree(path.parents[1])

        writer = threading.Thread(target=churn)

        def done(entries):
            # The sentinel proves the source is tailing, so both files are born after discovery.
            if has_sentinel(entries) and writer.ident is None:
                writer.start()
            short = sum(uid(94) in labels.get('filename', '') or labels.get('pod_uid') == uid(95) for labels, _, _ in entries)
            return short >= 2 * lines and writer.ident is not None and not writer.is_alive()

        try:
            run = run_alloy(self.config, self.dir / 'short-lived', done, root, [ORDINARY, job, api], self.dir / 'absent.yaml')
        finally:
            if writer.ident is not None:
                writer.join()
        broad = [(labels, ts, line) for labels, ts, line in run.entries if uid(94) in labels.get('filename', '')]
        self.assertEqual(sorted((ts, line) for _, ts, line in broad),
                         sorted((ns(ts), f'short line {n}') for n, ts in enumerate(stamps)))
        self.assertEqual({tuple(sorted((k, v) for k, v in labels.items() if k != 'filename')) for labels, _, _ in broad},
                         {tuple(sorted({'app': 'report', 'instance': 'report-prod', 'component': 'cron', 'node_name': 'work-1',
                                        'namespace': 'media', 'job': 'media/report', 'pod': 'report-29310240-q7xkz',
                                        'container': 'report', 'stream': 'stdout'}.items()))})
        safe = [(labels, ts, line) for labels, ts, line in run.entries if labels.get('pod_uid') == uid(95)]
        self.assertEqual(sorted((ts, line) for _, ts, line in safe),
                         sorted((ns(ts), safe_line(kind, uid(1000 + n))) for n, (kind, ts) in enumerate(zip(kinds, stamps))))
        self.assertTrue(all(set(labels) == {*SAFE_LABELS, 'pod_uid', 'filename'} and SAFE_LABELS.items() <= labels.items()
                            for labels, _, _ in safe))
        self.assertFalse([line for labels, _, line in run.entries
                          if 'synthetic-secret' in line or (uid(95) in labels.get('filename', '') and 'pod_uid' not in labels)])

if __name__ == '__main__':
    unittest.main()
