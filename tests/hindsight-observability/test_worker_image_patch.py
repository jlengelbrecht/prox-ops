"""The worker image patch, run against tiny standalone fixtures: pinned-bytes guard, exact anchors and the real
transformation. Nothing here builds, pulls or starts an image; signal and drain behaviour stay unproven."""
import contextlib
import hashlib
import importlib.util
import pathlib
import re
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True
ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICE = ROOT / 'services/hindsight-worker'
RELEASE = ROOT / 'kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml'
WORKFLOW = ROOT / '.github/workflows/hindsight-worker-image.yaml'

_spec = importlib.util.spec_from_file_location('patch_worker', SERVICE / 'patch_worker.py')
patch_worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(patch_worker)

SERVER_LINE = b'        server = uvicorn.Server(uvicorn_config)\n'
FIXTURE = (b'import argparse\nimport asyncio\nimport atexit\n\n\ndef build(uvicorn, uvicorn_config):\n'
           b'    if True:\n' + SERVER_LINE + b'        return server\n')


def sha(data):
    return hashlib.sha256(data).hexdigest()


class FakeServer:
    captured = False

    def __init__(self, config):
        self.config = config

    @contextlib.contextmanager
    def capture_signals(self):
        self.captured = True
        yield


class PatchSource(unittest.TestCase):
    def test_transformation_hands_signals_to_the_worker(self):
        patched = patch_worker.patch_source(FIXTURE, sha(FIXTURE))
        self.assertEqual(patched, patch_worker.patch_source(FIXTURE, sha(FIXTURE)))
        self.assertIn(b'import atexit\nimport contextlib\n', patched)
        self.assertNotIn(SERVER_LINE, patched)
        namespace = {}
        exec(compile(patched, 'fixture', 'exec'), namespace)
        server = namespace['build'](types.SimpleNamespace(Server=FakeServer), 'config')
        self.assertTrue(isinstance(server, FakeServer) and type(server) is not FakeServer)
        self.assertEqual(server.config, 'config')
        with server.capture_signals():
            pass
        self.assertFalse(server.captured)

    def test_refusals(self):
        cases = {
            'pinned upstream hash': (FIXTURE, patch_worker.ORIGINAL_SHA256, 'does not match'),
            'one changed byte': (FIXTURE + b'\n', sha(FIXTURE), 'does not match'),
            'missing server anchor': (FIXTURE.replace(SERVER_LINE, b''), None, 'found 0'),
            'missing import anchor': (FIXTURE.replace(b'import atexit\n', b''), None, 'found 0'),
            'duplicate anchor': (FIXTURE.replace(SERVER_LINE, SERVER_LINE * 2), None, 'found 2'),
            'uncompilable result': (FIXTURE + b'def broken(:\n', None, 'does not compile'),
        }
        for name, (source, expected, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(patch_worker.PatchRefused, message):
                patch_worker.patch_source(source, expected or sha(source))


class PatchFile(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.target = self.tmp / 'hindsight_api/worker/main.py'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(FIXTURE)

    def test_refusal_leaves_the_file_untouched(self):
        with self.assertRaisesRegex(SystemExit, 'refused'):
            patch_worker.main(['--target', str(self.target)])
        self.assertEqual(self.target.read_bytes(), FIXTURE)
        broken = FIXTURE.replace(SERVER_LINE, SERVER_LINE * 2)
        self.target.write_bytes(broken)
        with self.assertRaises(patch_worker.PatchRefused):
            patch_worker.patch_file(self.target, sha(broken))
        self.assertEqual(self.target.read_bytes(), broken)

    def test_patch_writes_drops_stale_bytecode_and_reports_digest(self):
        stale = self.target.parent / '__pycache__/main.cpython-399.pyc'
        stale.parent.mkdir()
        stale.write_bytes(b'stale')
        digest = patch_worker.patch_file(self.target, sha(FIXTURE))
        self.assertEqual(self.target.read_bytes(), patch_worker.patch_source(FIXTURE, sha(FIXTURE)))
        self.assertEqual(digest, sha(self.target.read_bytes()))
        self.assertFalse(stale.exists())

    def test_target_must_be_installed_exactly_once(self):
        found = patch_worker.find_target([str(self.tmp), str(self.tmp), str(self.tmp / 'absent')])
        self.assertEqual(found, self.target.resolve())
        other = self.tmp / 'other/hindsight_api/worker'
        other.mkdir(parents=True)
        (other / 'main.py').write_bytes(FIXTURE)
        for paths in ([str(self.tmp / 'absent')], [str(self.tmp), str(self.tmp / 'other')]):
            with self.subTest(paths=paths), self.assertRaises(patch_worker.PatchRefused):
                patch_worker.find_target(paths)


class BuildRecipe(unittest.TestCase):
    def test_base_is_the_api_release_pin(self):
        [api_tag] = re.findall(r'repository: ghcr\.io/vectorize-io/hindsight-api\n(?:.*\n){0,5}?\s*tag: "([^"]+)"',
                               RELEASE.read_text())
        self.assertRegex(api_tag, r'^0\.9\.2@sha256:[0-9a-f]{64}$')
        recipe = (SERVICE / 'Dockerfile').read_text()
        self.assertEqual(re.findall(r'^FROM (\S+)$', recipe, re.M), [f'ghcr.io/vectorize-io/hindsight-api:{api_tag}'])
        self.assertIn('python3 /tmp/hindsight-worker-patch/patch_worker.py', recipe)

    def test_workflow_publishes_only_by_manual_main_dispatch(self):
        workflow = WORKFLOW.read_text()
        for absent in ('pull_request_target', 'push:', 'inputs:'):
            self.assertNotIn(absent, workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertEqual(workflow.count('packages: write'), 1)


if __name__ == '__main__':
    unittest.main()
