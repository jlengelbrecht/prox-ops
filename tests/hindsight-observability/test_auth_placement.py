"""The CODEX_HOME placement Job: one bounded pod on the API's node that mounts the API's own claim read-only and
prints only the auth.json inode metadata. Every expectation is bound to the Hindsight release and claim in this
repository and the tree is rendered with the real kustomize; nothing here reaches a cluster.

A rendered Job is not a placement proof: that is the Job's output compared with the same stat taken in the API pod."""
import ast
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
AI = ROOT / 'kubernetes/apps/ai'
TREE = AI / 'hermes-hindsight/auth-placement'
ENTRY = AI / 'hermes-hindsight/auth-placement.ks.yaml'
RELEASE = AI / 'hermes-hindsight/app/hindsight.yaml'
CLAIMS = AI / 'hermes-hindsight/storage/pvc.yaml'
WORKFLOW = ROOT / '.github/workflows/hindsight-baseline.yaml'
KUSTOMIZE = os.environ.get('HINDSIGHT_KUSTOMIZE') or shutil.which('kustomize') or 'kustomize'
NAME = 'hindsight-worker-auth-placement'
LABELS = {'app.kubernetes.io/name': NAME}
HR = yaml.safe_load(RELEASE.read_text())
VALUES = HR['spec']['values']
[API_VOLUME] = VALUES['api']['extraVolumes']
[API_MOUNT] = VALUES['api']['extraVolumeMounts']
HOME = VALUES['api']['env']['CODEX_HOME']
API_SELECTOR = {'app.kubernetes.io/name': HR['spec']['chart']['spec']['chart'],
                'app.kubernetes.io/instance': HR['metadata']['name'], 'app.kubernetes.io/component': 'api'}
TRIGGERS = [path.relative_to(ROOT).as_posix() for path in (
    RELEASE, CLAIMS, AI / 'kustomization.yaml', ENTRY, *sorted(TREE.iterdir()), pathlib.Path(__file__).resolve())]
RESULTS = {'ok', 'not-mounted', 'missing', 'not-regular', 'not-readable', 'mount-writable', 'error'}


def render():
    out = subprocess.run([KUSTOMIZE, 'build', str(TREE)], check=True, capture_output=True, text=True, timeout=60)
    docs = [doc for doc in yaml.safe_load_all(out.stdout) if doc]
    assert len({doc['kind'] for doc in docs}) == len(docs), docs
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


def run_script(script, home, mounted, read_only):
    """Runs the Job's script against a temporary CODEX_HOME. A temporary directory is neither a mount point nor
    read-only, so the in-cluster answers are stood in before the script's own `import os`."""
    prelude = 'import os\n'
    if mounted:
        prelude += 'os.path.ismount = lambda path: True\n'
    if read_only:
        prelude += 'os.statvfs = lambda path: type("V", (), {"f_flag": os.ST_RDONLY})()\n'
    pointed = script.replace(repr(HOME), repr(str(home)))
    return subprocess.run([sys.executable, '-I', '-S', '-c', prelude + pointed], capture_output=True, text=True,
                          timeout=30)


class FluxTests(unittest.TestCase):
    def test_one_entry_waits_on_the_existing_hindsight_app(self):
        [entry] = list(yaml.safe_load_all(ENTRY.read_text()))
        self.assertEqual(entry, {
            'apiVersion': 'kustomize.toolkit.fluxcd.io/v1', 'kind': 'Kustomization',
            'metadata': {'name': NAME, 'namespace': 'flux-system'},
            'spec': {'targetNamespace': 'ai', 'commonMetadata': {'labels': LABELS},
                     'dependsOn': [{'name': 'hermes-hindsight', 'namespace': 'ai'}],
                     'path': './kubernetes/apps/ai/hermes-hindsight/auth-placement', 'prune': True,
                     'sourceRef': {'kind': 'GitRepository', 'name': 'flux-system', 'namespace': 'flux-system'},
                     'wait': True, 'interval': '1h', 'retryInterval': '2m', 'timeout': '2m'}})
        # The dependency exists, and the AI root's namespace transform puts both CRs in `ai`.
        siblings = {doc['metadata']['name'] for doc in yaml.safe_load_all((AI / 'hermes-hindsight/ks.yaml').read_text())}
        self.assertIn('hermes-hindsight', siblings)
        root = yaml.safe_load((AI / 'kustomization.yaml').read_text())
        self.assertEqual(root['namespace'], 'ai')
        resources = root['resources']
        self.assertEqual(resources.count('./hermes-hindsight/auth-placement.ks.yaml'), 1)
        self.assertEqual(resources.index('./hermes-hindsight/auth-placement.ks.yaml'),
                         resources.index('./hermes-hindsight/ks.yaml') + 1)

    def test_nothing_waits_on_it_and_the_app_tree_does_not_include_it(self):
        waiting = []
        for path in (ROOT / 'kubernetes').rglob('*ks.yaml'):
            for doc in yaml.safe_load_all(path.read_text()):
                if NAME in {item['name'] for item in ((doc or {}).get('spec') or {}).get('dependsOn') or []}:
                    waiting.append(path.name)
        self.assertEqual(waiting, [])
        self.assertNotIn('auth-placement', (AI / 'hermes-hindsight/app/kustomization.yaml').read_text())
        self.assertEqual(sorted(path.name for path in TREE.iterdir()),
                         ['job.yaml', 'kustomization.yaml', 'networkpolicy.yaml'])


class JobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = render()
        cls.job = cls.docs['Job']
        cls.pod = cls.job['spec']['template']['spec']
        [cls.container] = cls.pod['containers']
        cls.script = cls.container['command'][4]

    def test_one_unretried_bounded_job_and_a_deny_all_policy(self):
        self.assertEqual(sorted(self.docs), ['CiliumNetworkPolicy', 'Job'])
        self.assertEqual(self.job['metadata'], {'name': NAME + '-v1'})
        # No ttlSecondsAfterFinished: the finished Job stays as the record Flux waits on.
        self.assertEqual({key: value for key, value in self.job['spec'].items() if key != 'template'},
                         {'backoffLimit': 0, 'activeDeadlineSeconds': 60, 'parallelism': 1, 'completions': 1})
        self.assertEqual(self.job['spec']['template']['metadata'], {'labels': LABELS})
        # The pod must never match the API's selector (Service endpoints, the API's network policy).
        self.assertFalse(API_SELECTOR.items() <= LABELS.items())
        self.assertEqual(self.docs['CiliumNetworkPolicy'], {
            'apiVersion': 'cilium.io/v2', 'kind': 'CiliumNetworkPolicy', 'metadata': {'name': NAME},
            'spec': {'endpointSelector': {'matchLabels': LABELS}, 'enableDefaultDeny': {'ingress': True, 'egress': True}}})

    def test_pod_runs_as_the_api_identity_without_token_and_beside_the_api(self):
        pod_security, container_security = VALUES['podSecurityContext'], VALUES['securityContext']
        self.assertEqual({key: value for key, value in self.pod.items() if key not in ('containers', 'volumes')}, {
            'restartPolicy': 'Never', 'automountServiceAccountToken': False, 'enableServiceLinks': False,
            'terminationGracePeriodSeconds': 15,
            'securityContext': {'runAsNonRoot': True, 'runAsUser': pod_security['runAsUser'],
                                'runAsGroup': pod_security['runAsGroup'], 'seccompProfile': {'type': 'RuntimeDefault'}},
            'affinity': {'podAffinity': {'requiredDuringSchedulingIgnoredDuringExecution': [
                {'labelSelector': {'matchLabels': API_SELECTOR}, 'topologyKey': 'kubernetes.io/hostname'}]}}})
        self.assertEqual((pod_security['runAsUser'], pod_security['runAsGroup'], container_security['runAsUser']),
                         (1000, 1000, 1000))
        image = VALUES['api']['image']
        self.assertEqual(sorted(self.container), ['command', 'image', 'imagePullPolicy', 'name', 'resources',
                                                  'securityContext', 'volumeMounts'])
        self.assertEqual(self.container['image'], f"{image['repository']}:{image['tag']}")
        self.assertRegex(image['tag'], r'^0\.9\.2@sha256:[0-9a-f]{64}$')
        self.assertEqual(self.container['securityContext'], {
            'runAsNonRoot': True, 'runAsUser': container_security['runAsUser'], 'allowPrivilegeEscalation': False,
            'readOnlyRootFilesystem': True, 'capabilities': {'drop': ['ALL']}})
        self.assertEqual(self.container['resources'], {'requests': {'cpu': '10m', 'memory': '32Mi'},
                                                       'limits': {'cpu': '200m', 'memory': '64Mi'}})

    def test_mounts_the_api_claim_whole_and_read_only_at_the_api_path(self):
        self.assertEqual(API_MOUNT['mountPath'], HOME)
        self.assertEqual(self.pod['volumes'], [{'name': API_VOLUME['name'], 'persistentVolumeClaim': {
            'claimName': API_VOLUME['persistentVolumeClaim']['claimName'], 'readOnly': True}}])
        self.assertEqual(self.container['volumeMounts'], [{**API_MOUNT, 'readOnly': True}])
        worker = VALUES.get('worker', {})
        if worker.get('extraVolumeMounts'):
            self.assertEqual((worker['extraVolumes'], worker['extraVolumeMounts']), ([API_VOLUME], [API_MOUNT]))
        # The claim keeps its identity: the retained RWO volume the API uses.
        claim = next(doc for doc in yaml.safe_load_all(CLAIMS.read_text())
                     if doc['metadata']['name'] == API_VOLUME['persistentVolumeClaim']['claimName'])
        self.assertEqual((claim['spec']['accessModes'], claim['metadata']['annotations']),
                         (['ReadWriteOnce'], {'kustomize.toolkit.fluxcd.io/prune': 'disabled'}))

    def test_nothing_else_is_read_granted_written_or_forced(self):
        self.assertEqual(secret_names(list(self.docs.values())), set())
        text = yaml.safe_dump(list(self.docs.values()))
        for word in ('kind: Secret', 'serviceAccountName', 'hostPath', 'subPath', 'emptyDir', 'initContainers',
                     'env:', 'envFrom', 'Probe', 'ports:', 'fsGroup', 'ttlSecondsAfterFinished', 'hostNetwork',
                     'Role', 'kustomize.toolkit.fluxcd.io/force', 'ingress:\n', 'egress:\n'):
            self.assertNotIn(word, text)

    def test_script_is_stdlib_only_and_never_opens_the_file(self):
        self.assertEqual(self.container['command'][:4], ['python3', '-I', '-S', '-c'])
        self.assertEqual(len(self.container['command']), 5)
        tree = ast.parse(self.script)
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertEqual(imports, {'json', 'os', 'stat'})
        self.assertFalse([node for node in ast.walk(tree) if isinstance(node, (ast.ImportFrom, ast.For, ast.While))])
        names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        self.assertFalse(names & {'open', 'exec', 'eval', 'compile', '__import__', 'input'})
        self.assertFalse(attrs & {'environ', 'getenv', 'read', 'read_text', 'read_bytes', 'readlink', 'system',
                                  'popen', 'open', 'listdir', 'scandir', 'walk', 'chown', 'chmod', 'replace'})
        strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        self.assertEqual(strings & RESULTS, RESULTS)
        self.assertEqual(strings - RESULTS, {HOME, '/auth.json', 'device', 'inode', 'uid', 'gid', 'mode',
                                             'readOnly', '%04o', ',', ':'})
        self.assertEqual(self.script.count(repr(HOME)), 1)

    def test_output_is_fixed_metadata_only_and_fails_closed(self):
        canary = 'refresh-token-canary'
        with tempfile.TemporaryDirectory() as folder:
            home = pathlib.Path(folder)
            auth = home / 'auth.json'
            auth.write_text('{"tokens": "%s"}' % canary)
            auth.chmod(0o600)
            run = run_script(self.script, home, True, True)
            info = os.lstat(auth)
            self.assertEqual((run.returncode, run.stderr), (0, ''))
            self.assertEqual(yaml.safe_load(run.stdout), {
                'result': 'ok', 'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid, 'gid': info.st_gid,
                'mode': '0600', 'readOnly': True})
            self.assertEqual(run.stdout.count('\n'), 1)
            self.assertNotIn(canary, run.stdout)

            def refused(result, mounted, read_only):
                run = run_script(self.script, home, mounted, read_only)
                self.assertEqual((run.returncode, run.stdout, run.stderr), (1, '{"result":"%s"}\n' % result, ''))

            refused('not-mounted', False, False)
            refused('mount-writable', True, False)
            if os.geteuid() != 0:  # root passes access(2) whatever the mode
                auth.chmod(0o000)
                refused('not-readable', True, True)
            auth.unlink()
            refused('missing', True, True)
            (home / 'elsewhere.json').write_text(canary)
            auth.symlink_to(home / 'elsewhere.json')
            refused('not-regular', True, True)
            auth.unlink()
            auth.mkdir()
            refused('not-regular', True, True)


class WorkflowTests(unittest.TestCase):
    def test_triggers_cover_every_owned_file_and_the_suite_runs_once(self):
        text = WORKFLOW.read_text()
        triggers = yaml.safe_load(text)[True]['pull_request']['paths']
        for path in TRIGGERS:
            self.assertEqual(triggers.count(path), 1, path)
        self.assertEqual(text.count(
            '      - run: python -m unittest discover -s tests/hindsight-observability -p test_auth_placement.py -v\n'), 1)


if __name__ == '__main__':
    unittest.main()
