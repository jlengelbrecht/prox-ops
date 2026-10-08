"""Render checks for the dedicated Hindsight worker at zero replicas.

Everything here is read from the pinned chart render, the Flux post-render
patches and the committed manifests. Auth-file sharing between pods, the
provider budget under load, the database connection peak, drain recovery and
bank fairness need real pods and are not exercised here.
"""
import copy
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app'
DATABASE_RELEASE = ROOT / 'kubernetes/apps/database/cluster/app/helmrelease.yaml'
CHART = pathlib.Path(os.environ.get('HINDSIGHT_CHART', '/home/devbox/.cache/helm/repository/hindsight-0.9.2.tgz'))
HELM = os.environ.get('HINDSIGHT_HELM', 'helm')
NATIVE_KUSTOMIZE = pathlib.Path.home() / '.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize'

WORKER = 'hindsight-worker'
WORKER_ID = 'HINDSIGHT_API_WORKER_ID'
OVERRIDES = {
    'HINDSIGHT_API_LLM_MAX_CONCURRENT': '1',
    'HINDSIGHT_API_DB_POOL_MAX_SIZE': '5',
    'HINDSIGHT_API_WORKER_MAX_SLOTS': '2',
    'HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS': '0',
    'HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP': 'false',
}
# Worker env the chart sets itself (worker.service.targetPort and the
# worker.env defaults of chart 0.9.2).
CHART_WORKER_ENV = {
    'HINDSIGHT_API_PORT': '8889',
    'HINDSIGHT_API_WORKER_HTTP_PORT': '8889',
    'HINDSIGHT_API_WORKER_POLL_INTERVAL_MS': '500',
    'HINDSIGHT_API_WORKER_BATCH_SIZE': '10',
    'HINDSIGHT_API_WORKER_MAX_RETRIES': '3',
}
INLINE_WORKER = 'HINDSIGHT_API_WORKER_ENABLED'
# Connections kept free on the shared role for one-shot jobs (exports,
# maintenance) that log in as the same role.
JOB_RESERVE = 3
# Concurrent subscription calls the account is budgeted for today; raised only
# with a measured provider budget.
LLM_BUDGET = 2


def kustomize_binary():
    found = str(NATIVE_KUSTOMIZE) if NATIVE_KUSTOMIZE.is_file() else shutil.which('kustomize')
    if not found:
        raise AssertionError('native kustomize is required')
    return found


def release():
    return next(yaml.safe_load_all((APP / 'hindsight.yaml').read_text()))


def helm_template(values):
    out = subprocess.run(
        [HELM, 'template', 'hindsight', str(CHART), '--namespace', 'ai', '--values', '-'],
        input=yaml.safe_dump(values), capture_output=True, text=True, check=True, cwd=ROOT)
    return [x for x in yaml.safe_load_all(out.stdout) if x]


def post_render(rendered, patches):
    """Apply the HelmRelease postRenderers patches the way Flux does."""
    with tempfile.TemporaryDirectory() as tmp:
        directory = pathlib.Path(tmp)
        (directory / 'rendered.yaml').write_text(yaml.safe_dump_all(rendered))
        (directory / 'kustomization.yaml').write_text(yaml.safe_dump({
            'apiVersion': 'kustomize.config.k8s.io/v1beta1',
            'kind': 'Kustomization',
            'resources': ['rendered.yaml'],
            'patches': patches,
        }))
        out = subprocess.run([kustomize_binary(), 'build', str(directory)], capture_output=True, text=True)
    if out.returncode:
        raise AssertionError(f'post-render failed: {out.stderr.strip()}')
    return [x for x in yaml.safe_load_all(out.stdout) if x]


def guarded_removals(patch):
    """(path, expected value) for each `test` op followed by a `remove` of the same path."""
    ops = yaml.safe_load(patch['patch'])
    return [(a['path'], a['value']) for a, b in zip(ops, ops[1:])
            if a['op'] == 'test' and b == {'op': 'remove', 'path': a['path']}]


def check_removals(raw, patches):
    """Fail with the actual env order when a guarded removal no longer lines up with the chart."""
    for patch in patches:
        for path, expected in guarded_removals(patch):
            doc = find(raw, patch['target']['kind'], patch['target']['name'])
            *parent, index = path.strip('/').split('/')
            node = doc
            for part in parent:
                node = node[int(part)] if isinstance(node, list) else node[part]
            if int(index) >= len(node) or node[int(index)] != expected:
                order = [f"{i}:{x.get('name')}" for i, x in enumerate(node)]
                raise AssertionError(f'{key(doc)} {path} is not {expected}; rendered order {order}')


def key(doc):
    return doc['kind'], doc['metadata']['name']


def find(docs, kind, name):
    found = [x for x in docs if key(x) == (kind, name)]
    if len(found) != 1:
        raise AssertionError(f'expected one {kind}/{name}, found {len(found)}')
    return found[0]


def pod(workload):
    return workload['spec']['template']


def pod_labels(workload):
    return (workload.get('spec', {}).get('template', {}).get('metadata') or {}).get('labels') or {}


def only_container(workload):
    containers = pod(workload)['spec']['containers']
    if len(containers) != 1:
        raise AssertionError(f'{key(workload)} must render one container, found {len(containers)}')
    return containers[0]


def env_by_name(container):
    entries = container.get('env') or []
    names = [x['name'] for x in entries]
    if len(names) != len(set(names)):
        raise AssertionError(f'duplicate env names: {sorted(n for n in names if names.count(n) > 1)}')
    return {x['name']: x for x in entries}


def selects(match_labels, labels):
    return bool(match_labels) and all(labels.get(k) == v for k, v in match_labels.items())


def role_connection_limit(name):
    def walk(node):
        if isinstance(node, dict):
            if node.get('name') == name and 'connectionLimit' in node:
                yield node['connectionLimit']
            for value in node.values():
                yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)
    limits = list(walk(yaml.safe_load(DATABASE_RELEASE.read_text())))
    if len(limits) != 1:
        raise AssertionError(f'expected one managed role {name}, found {len(limits)}')
    return int(limits[0])


def db_fits(api_pool, worker_pool, replicas, limit):
    return api_pool + replicas * worker_pool + JOB_RESERVE <= limit


def llm_fits(api_global, worker_global, replicas):
    return api_global + replicas * worker_global <= LLM_BUDGET


class WorkerTopologyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CHART.is_file():
            raise AssertionError(f'pinned chart missing at {CHART}; set HINDSIGHT_CHART to hindsight-0.9.2.tgz')
        if shutil.which(HELM) is None and not pathlib.Path(HELM).is_file():
            raise AssertionError(f'helm not found ({HELM})')
        cls.release = release()
        chart = yaml.safe_load(subprocess.run([HELM, 'show', 'chart', str(CHART)],
                                              capture_output=True, text=True, check=True).stdout)
        pinned = cls.release['spec']['chart']['spec']
        if (chart['name'], chart['version']) != (pinned['chart'], pinned['version']) or pinned['version'] != '0.9.2':
            raise AssertionError(f"archive {chart['name']}-{chart['version']} is not the pinned hindsight 0.9.2")
        cls.values = cls.release['spec']['values']
        cls.patches = cls.release['spec']['postRenderers'][0]['kustomize']['patches']
        disabled = copy.deepcopy(cls.values)
        disabled['worker'] = {'enabled': False}
        # The baseline is the release as it rendered before the worker: no
        # worker values and none of the patches that exist only because of it.
        worker_only = [p for p in cls.patches
                       if p['target'].get('kind') == 'StatefulSet' or INLINE_WORKER in p['patch']]
        if len(worker_only) != 2:
            raise AssertionError(f'expected the StatefulSet patch and the {INLINE_WORKER} removal, '
                                 f'found {len(worker_only)} worker-only patches')
        cls.raw = helm_template(cls.values)
        check_removals(cls.raw, cls.patches)
        cls.rendered = post_render(cls.raw, cls.patches)
        cls.baseline = post_render(helm_template(disabled), [p for p in cls.patches if p not in worker_only])
        cls.worker = find(cls.rendered, 'StatefulSet', WORKER)
        cls.api = find(cls.rendered, 'Deployment', 'hindsight-api')

    @classmethod
    def tearDownClass(cls):
        print('\nRENDERED: chart 0.9.2 + Flux post-render, worker at zero replicas.'
              '\nNOT RUN (needs real pods): auth-file sharing, provider budget under load,'
              ' database connection peak, drain recovery, bank fairness.')

    def test_ac1_worker_renders_as_statefulset(self):
        workers = [x for x in self.raw if pod_labels(x).get('app.kubernetes.io/component') == 'worker']
        self.assertEqual([key(x) for x in workers], [('StatefulSet', WORKER)],
                         'chart must render the worker as a StatefulSet for a stable per-pod identity')
        raw = find(self.raw, 'StatefulSet', WORKER)
        container = only_container(raw)
        print(f"\nworker container={container['name']} command={container.get('command')} "
              f"args={container.get('args')} chart replicas={raw['spec'].get('replicas')} "
              f"worker value keys={sorted(self.values['worker'])}")

    def test_ac2_existing_render_unchanged(self):
        baseline = {key(x): x for x in self.baseline}
        current = {key(x): x for x in self.rendered}
        self.assertIn(('Deployment', 'hindsight-api'), baseline)
        self.assertTrue(any(pod_labels(x).get('app.kubernetes.io/component') == 'control-plane'
                            for x in self.baseline if x['kind'] == 'Deployment'))
        raw_api = env_by_name(only_container(find(self.raw, 'Deployment', 'hindsight-api')))
        self.assertEqual(raw_api[INLINE_WORKER].get('value'), 'false',
                         'chart no longer disables the API worker; drop the removal patch')
        self.assertNotIn(INLINE_WORKER, env_by_name(only_container(self.api)),
                         'API in-process worker must stay on while the worker has zero replicas')
        self.maxDiff = None
        for name, doc in baseline.items():
            self.assertEqual(current.get(name), doc, f'{name} changed when the worker was added')
        added = sorted(set(current) - set(baseline))
        print(f'\nadded by worker.enabled: {added}')
        self.assertIn(('StatefulSet', WORKER), added)
        self.assertFalse([x for x in added if x[0] == 'Deployment'])
        served = [pod_labels(x) for x in self.baseline if x['kind'] == 'Deployment']
        for kind, name in added:
            if kind == 'Service':
                selector = current[(kind, name)]['spec'].get('selector') or {}
                self.assertFalse(any(selects(selector, labels) for labels in served),
                                 f'new Service {name} selects an existing pod')

    def test_ac3_statefulset_post_render(self):
        spec = self.worker['spec']
        template = pod(self.worker)['spec']
        self.assertEqual(spec.get('replicas'), 0)
        self.assertNotIn('volumeClaimTemplates', spec)
        self.assertIs(template.get('automountServiceAccountToken'), False)
        self.assertEqual(template['dnsConfig'], {'options': [{'name': 'ndots', 'value': '1'}]})
        self.assertEqual(template['terminationGracePeriodSeconds'], 60)
        affinity = template['affinity']
        self.assertEqual(affinity['nodeAffinity'], pod(self.api)['spec']['affinity']['nodeAffinity'])
        terms = affinity['podAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        self.assertEqual(len(terms), 1)
        self.assertEqual(terms[0]['topologyKey'], 'kubernetes.io/hostname')
        selector = terms[0]['labelSelector']['matchLabels']
        self.assertTrue(selects(selector, pod_labels(self.api)), 'podAffinity must select the rendered API pod')
        self.assertFalse(selects(selector, pod_labels(self.worker)), 'podAffinity must not select the worker itself')

        container = only_container(self.worker)
        api_container = only_container(self.api)
        self.assertEqual(container['image'], api_container['image'])
        self.assertIn('@sha256:', container['image'])
        self.assertEqual(container.get('resources'), api_container.get('resources'))
        claims = {v['name']: v['persistentVolumeClaim']['claimName']
                  for v in template.get('volumes', []) if 'persistentVolumeClaim' in v}
        self.assertEqual(list(claims.values()), ['hindsight-codex-auth'], 'only the original CODEX_HOME claim')
        mounts = [m for m in container.get('volumeMounts', []) if m['name'] in claims]
        self.assertEqual(len(mounts), 1)
        self.assertNotIn('subPath', mounts[0], 'auth.json is replaced by rename; mount the directory')
        self.assertEqual(mounts[0]['mountPath'], env_by_name(container)['CODEX_HOME']['value'])

    def test_ac4_worker_env(self):
        api = env_by_name(only_container(self.api))
        worker = env_by_name(only_container(self.worker))
        self.assertEqual(worker[WORKER_ID], {'name': WORKER_ID,
                                             'valueFrom': {'fieldRef': {'fieldPath': 'metadata.name'}}})
        self.assertEqual(api[WORKER_ID]['value'], 'hindsight-api')
        for name, value in {**OVERRIDES, **CHART_WORKER_ENV}.items():
            self.assertEqual(worker[name], {'name': name, 'value': value})
        skip = set(OVERRIDES) | set(CHART_WORKER_ENV) | {WORKER_ID}
        self.assertEqual({k: v for k, v in worker.items() if k not in skip},
                         {k: v for k, v in api.items() if k not in skip})
        self.assertEqual(only_container(self.worker).get('envFrom'), only_container(self.api).get('envFrom'))

        external = self.values['postgresql']['external']
        urls = [v['value'] for k, v in worker.items() if k.endswith('DATABASE_URL')]
        self.assertEqual(len(urls), 1)
        for part in (external['host'], f"/{external['database']}", f"{external['username']}:"):
            self.assertIn(part, urls[0])
        secret_refs = [v['valueFrom']['secretKeyRef'] for v in worker.values()
                       if 'secretKeyRef' in v.get('valueFrom', {})]
        self.assertTrue(secret_refs, 'database password must arrive by secretKeyRef')
        self.assertEqual({x['name'] for x in secret_refs}, {self.values['existingSecret']})
        for name, entry in worker.items():
            if 'PASSWORD' in name:
                self.assertNotIn('value', entry)
                self.assertIn('secretKeyRef', entry['valueFrom'])

    def test_ac5_connection_and_llm_budget(self):
        api = {k: v.get('value') for k, v in env_by_name(only_container(self.api)).items()}
        worker = {k: v.get('value') for k, v in env_by_name(only_container(self.worker)).items()}
        limit = role_connection_limit('hindsight')
        replicas = self.worker['spec']['replicas']
        api_pool, worker_pool = int(api['HINDSIGHT_API_DB_POOL_MAX_SIZE']), int(worker['HINDSIGHT_API_DB_POOL_MAX_SIZE'])
        self.assertTrue(db_fits(api_pool, worker_pool, replicas, limit))
        # Two workers next to today's API pool would overrun the role; any
        # activation must lower the API pool and re-run this budget.
        self.assertFalse(db_fits(api_pool, worker_pool, 2, limit))

        api_global, worker_global = int(api['HINDSIGHT_API_LLM_MAX_CONCURRENT']), int(worker['HINDSIGHT_API_LLM_MAX_CONCURRENT'])
        self.assertTrue(llm_fits(api_global, worker_global, replicas))
        self.assertFalse(llm_fits(api_global, worker_global, 1))
        per_operation = {k: int(v) for k, v in worker.items()
                         if re.fullmatch(r'HINDSIGHT_API_[A-Z]+_LLM_MAX_CONCURRENT', k)}
        self.assertEqual(set(per_operation), {'HINDSIGHT_API_RETAIN_LLM_MAX_CONCURRENT',
                                              'HINDSIGHT_API_REFLECT_LLM_MAX_CONCURRENT',
                                              'HINDSIGHT_API_CONSOLIDATION_LLM_MAX_CONCURRENT'})
        for name, cap in per_operation.items():
            self.assertLessEqual(cap, worker_global, name)
        self.assertLessEqual(int(worker['HINDSIGHT_API_WORKER_CONSOLIDATION_RESERVED_SLOTS']),
                             int(worker['HINDSIGHT_API_WORKER_MAX_SLOTS']))

    def test_ac6_worker_network_policy(self):
        policies = [x for x in yaml.safe_load_all((APP / 'networkpolicy.yaml').read_text()) if x]
        by_name = {x['metadata']['name']: x for x in policies}
        cnp = by_name[WORKER]['spec']
        labels = pod_labels(self.worker)
        self.assertTrue(selects(cnp['endpointSelector']['matchLabels'], labels))
        for other in self.baseline:
            if other['kind'] == 'Deployment':
                self.assertFalse(selects(cnp['endpointSelector']['matchLabels'], pod_labels(other)))
        self.assertEqual([x['metadata']['name'] for x in policies
                          if x['metadata']['namespace'] == 'ai'
                          and selects(x['spec']['endpointSelector'].get('matchLabels'), labels)], [WORKER])
        self.assertEqual(set(cnp), {'endpointSelector', 'ingress', 'egress'})
        self.assertEqual(cnp['ingress'], [{}], 'no ingress peer: default-deny form only')
        self.assertEqual(cnp['egress'], by_name['hindsight-api']['spec']['egress'])

    def test_ac8_kustomize_output_keeps_values(self):
        out = subprocess.run([kustomize_binary(), 'build', str(APP)], capture_output=True, text=True, check=True)
        built = find([x for x in yaml.safe_load_all(out.stdout) if x], 'HelmRelease', 'hindsight')
        self.assertEqual(built['spec']['values'], self.values, 'anchors and merge keys must expand as Flux applies them')
        self.assertEqual(built['spec']['postRenderers'], self.release['spec']['postRenderers'])


if __name__ == '__main__':
    unittest.main()
