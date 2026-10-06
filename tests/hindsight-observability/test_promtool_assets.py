"""Checks for the digest-pinned Prometheus archive that supplies CI promtool."""
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import re
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW_PATH = '.github/workflows/hindsight-metrics.yaml'
WORKFLOW = ROOT / WORKFLOW_PATH
MANAGERS = ROOT / '.github/renovate/customManagers.json5'
TOOLING = ROOT / '_bmad-output/implementation-artifacts/tooling'
# Official v3.15.0 receipt: the archive digest is the release checksum-file entry;
# the promtool digest was measured from that archive.
RECEIPT = {'version': 'v3.15.0',
           'archive': '2a542df32eac02ee17b9d844fb2aa1de00dafa5476579ba8a3ba862e9d572ea0',
           'promtool': 'c736d55d3ccd959fe48329965fb5cb671d45585a9483954766448035100ff75c'}
PAYLOAD = b'synthetic-promtool'


def load_assets():
    spec = importlib.util.spec_from_file_location('metrics_assets', ROOT / 'scripts/hindsight-metrics-ci-assets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ASSETS = load_assets()


def workflow_env():
    return yaml.safe_load(WORKFLOW.read_text())['jobs']['metrics']['env']


def renovate_config():
    # The preset uses only whole-line JSON5 comments, so dropping them leaves JSON.
    lines = [line for line in MANAGERS.read_text().splitlines() if not line.lstrip().startswith('//')]
    return json.loads('\n'.join(lines))


def extract(manager, path, text):
    """Mirror Renovate's regex manager: file patterns gate, every matchString applies."""
    if not any(re.search(p[1:-1], path) for p in manager['managerFilePatterns']):
        return []
    return [m for s in manager['matchStrings'] for m in re.finditer(s.replace('(?<', '(?P<'), text)]


def prometheus_manager():
    managers = [m for m in renovate_config()['customManagers'] if m.get('depNameTemplate') == 'prometheus/prometheus']
    assert len(managers) == 1, managers
    return managers[0]


def build(path, members):
    with tarfile.open(path, 'w:gz') as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.mode = 0o755
            if kind == 'file':
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {'dir': tarfile.DIRTYPE, 'symlink': tarfile.SYMTYPE,
                         'hardlink': tarfile.LNKTYPE, 'fifo': tarfile.FIFOTYPE}[kind]
            info.linkname = data or ''
            tar.addfile(info)


def prom_archive(directory, version='v3.15.0', extra=(), promtool=('file', PAYLOAD)):
    root = f'prometheus-{version[1:]}.linux-amd64'
    path = directory / f'{root}-{len(list(directory.iterdir()))}.tar.gz'
    build(path, [(root, 'dir', None), (f'{root}/promtool', *promtool),
                 (f'{root}/LICENSE', 'file', b'license'), *extra])
    return path, root


class WorkflowPinTests(unittest.TestCase):
    def test_version_and_digest_are_one_annotated_source(self):
        env = workflow_env()
        version, sha = env['PROMETHEUS_VERSION'], env['PROMETHEUS_ARCHIVE_SHA256']
        self.assertRegex(version, r'^v\d+\.\d+\.\d+$')
        self.assertRegex(sha, r'^[0-9a-f]{64}$')
        self.assertGreaterEqual(tuple(map(int, version[1:].split('.'))), (3, 15, 0))
        if version == RECEIPT['version']:
            self.assertEqual(sha, RECEIPT['archive'])
        text = WORKFLOW.read_text()
        self.assertEqual(text.count(version[1:]), 1)
        self.assertEqual(text.count(sha), 1)
        self.assertNotIn('3.8.1', text)
        self.assertIn('releases/download/${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION#v}.linux-amd64.tar.gz', text)
        self.assertIn('"$PROMETHEUS_VERSION" "$PROMETHEUS_ARCHIVE_SHA256"', text)
        self.assertLess(text.index('=~ ^v[0-9]+'), text.index('curl -fsSL -o "$work/prometheus.tar.gz"'))

    def test_helm_pins_unchanged_and_prometheus_constants_removed(self):
        text = WORKFLOW.read_text()
        self.assertIn('https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz', text)
        self.assertEqual(ASSETS.HELM_ARCHIVE, 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496')
        self.assertEqual(ASSETS.HELM_BINARY, 'e4722a77de9df824214aaf19d43687c49f7cbcf3b107905acea002173777d486')
        self.assertEqual(ASSETS.CHART_ARCHIVE, 'c8a7eb0a27bcc38463a144bea9716897c1cce281ba98d49838f449e75098e257')
        self.assertEqual((ASSETS.MAX_MEMBER, ASSETS.MAX_TOTAL), (200_000_000, 450_000_000))
        self.assertFalse(hasattr(ASSETS, 'PROM_ARCHIVE') or hasattr(ASSETS, 'PROM_BINARY'))

    def test_workflow_triggers_on_owned_files(self):
        paths = yaml.safe_load(WORKFLOW.read_text())[True]['pull_request']['paths']
        for owned in ('scripts/hindsight-metrics-ci-assets.py', WORKFLOW_PATH,
                      '.github/renovate/customManagers.json5',
                      'tests/hindsight-observability/test_promtool_assets.py'):
            self.assertIn(owned, paths)
        self.assertIn('-p test_promtool_assets.py', WORKFLOW.read_text())


class RenovateTests(unittest.TestCase):
    def test_extracts_version_and_digest_pair(self):
        manager = prometheus_manager()
        self.assertEqual(manager['datasourceTemplate'], 'github-release-attachments')
        self.assertEqual(manager['versioningTemplate'], 'semver')
        self.assertEqual(manager['managerFilePatterns'], ['/^\\.github/workflows/hindsight-metrics\\.yaml$/'])
        matches = extract(manager, WORKFLOW_PATH, WORKFLOW.read_text())
        self.assertEqual(len(matches), 1)
        env = workflow_env()
        self.assertEqual((matches[0]['currentValue'], matches[0]['currentDigest']),
                         (env['PROMETHEUS_VERSION'], env['PROMETHEUS_ARCHIVE_SHA256']))
        for other in ('.github/workflows/other.yaml', '.github/workflows/hindsight-metrics.yml',
                      'x/.github/workflows/hindsight-metrics.yaml'):
            self.assertEqual(extract(manager, other, WORKFLOW.read_text()), [])

    def test_version_without_digest_is_not_extracted(self):
        text = WORKFLOW.read_text()
        sha = workflow_env()['PROMETHEUS_ARCHIVE_SHA256']
        self.assertEqual(extract(prometheus_manager(), WORKFLOW_PATH, text.replace(sha, '')), [])

    def test_unrelated_managers_extract_nothing_here(self):
        config = renovate_config()
        text = WORKFLOW.read_text()
        others = [m for m in config['customManagers'] if m.get('depNameTemplate') != 'prometheus/prometheus']
        self.assertEqual([m['description'] for m in others if extract(m, WORKFLOW_PATH, text)], [])
        # Other release-attachment pins may exist, but must be gated away from this
        # workflow and unable to read the Prometheus pair even if the gate widened.
        for manager in (m for m in others if 'github-release-attachments' in json.dumps(m)):
            with self.subTest(manager['description']):
                self.assertFalse(any(re.search(p[1:-1], WORKFLOW_PATH) for p in manager['managerFilePatterns']))
                self.assertEqual([s for s in manager['matchStrings']
                                  if re.search(s.replace('(?<', '(?P<'), text)], [])
                self.assertNotEqual(manager.get('packageNameTemplate'), 'prometheus/prometheus')
        self.assertNotIn('postUpgradeTasks', MANAGERS.read_text())

    def test_sibling_release_pin_is_not_captured_as_promtool(self):
        text = WORKFLOW.read_text()
        env = workflow_env()
        anchor = '# renovate: datasource=github-release-attachments depName=prometheus/prometheus'
        self.assertEqual(text.count(anchor), 1)
        sibling = (f'# renovate: datasource=github-release-attachments depName=grafana/alloy\n'
                   f'          ALLOY_VERSION: "v1.20.1"\n'
                   f'          ALLOY_ARCHIVE_SHA256: "{hashlib.sha256(b"other release archive").hexdigest()}"\n'
                   f'          ')
        matches = extract(prometheus_manager(), WORKFLOW_PATH, text.replace(anchor, sibling + anchor))
        self.assertEqual([(m['currentValue'], m['currentDigest']) for m in matches],
                         [(env['PROMETHEUS_VERSION'], env['PROMETHEUS_ARCHIVE_SHA256'])])

    def test_update_rewrites_version_and_digest_together(self):
        text = WORKFLOW.read_text()
        old = workflow_env()
        match, = extract(prometheus_manager(), WORKFLOW_PATH, text)
        new_version = f'v{int(old["PROMETHEUS_VERSION"][1:].split(".")[0]) + 1}.0.0'
        new_sha = hashlib.sha256(b'next release archive').hexdigest()
        updated = (text[:match.start('currentValue')] + new_version
                   + text[match.end('currentValue'):match.start('currentDigest')] + new_sha
                   + text[match.end('currentDigest'):])
        again = extract(prometheus_manager(), WORKFLOW_PATH, updated)
        self.assertEqual([(m['currentValue'], m['currentDigest']) for m in again], [(new_version, new_sha)])
        env = yaml.safe_load(updated)['jobs']['metrics']['env']
        self.assertEqual((env['PROMETHEUS_VERSION'], env['PROMETHEUS_ARCHIVE_SHA256']), (new_version, new_sha))
        self.assertNotIn(old['PROMETHEUS_VERSION'][1:], updated)
        self.assertNotIn(old['PROMETHEUS_ARCHIVE_SHA256'], updated)

    def test_updates_are_manual(self):
        rules = [r for r in renovate_config()['packageRules'] if r.get('matchPackageNames') == ['prometheus/prometheus']]
        self.assertEqual(len(rules), 1)
        rule = rules[0]
        self.assertIs(rule['automerge'], False)
        self.assertEqual(rule['matchFileNames'], [WORKFLOW_PATH])
        self.assertEqual(rule['matchManagers'], ['custom.regex'])
        self.assertNotIn('matchUpdateTypes', rule)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, prom, version, sha):
        helm = self.dir / 'helm.tar.gz'
        build(helm, [('linux-amd64/helm', 'file', b'helm')])
        out = pathlib.Path(tempfile.mkdtemp(dir=self.dir)) / 'bin'
        with mock.patch.object(ASSETS, 'HELM_ARCHIVE', ASSETS.digest(helm)), \
                mock.patch.object(ASSETS, 'HELM_BINARY', hashlib.sha256(b'helm').hexdigest()):
            ASSETS.main(['assets', str(helm), str(prom), str(out), version, sha])
        return out

    def verify(self, archive, root, destination=None):
        ASSETS.verified_tar(archive, ASSETS.digest(archive), f'{root}/promtool', None,
                            destination or self.dir / 'promtool', ASSETS.prometheus_member(root))

    def test_extracts_promtool_from_digest_pinned_archive(self):
        prom, _ = prom_archive(self.dir)
        out = self.run_main(prom, 'v3.15.0', ASSETS.digest(prom))
        self.assertEqual((out / 'promtool').read_bytes(), PAYLOAD)
        self.assertEqual(stat.S_IMODE((out / 'promtool').stat().st_mode), 0o700)

    def test_malformed_pins_fail_before_any_archive_is_read(self):
        missing = str(self.dir / 'absent.tar.gz')
        for version, sha in [('v3.15.0', ''), ('v3.15.0', 'abc'), ('v3.15.0', 'A' * 64),
                             ('v3.15.0', '0' * 65), ('v3.15.0', '0' * 64 + '\n'), ('3.15.0', '0' * 64),
                             ('v3.15', '0' * 64), ('v3.15.0-rc.0', '0' * 64), ('v03.15.0', '0' * 64),
                             ('v3.15.0/../x', '0' * 64), ('v3.15.0\n', '0' * 64), ('', '0' * 64)]:
            with self.subTest(version=version, sha=sha), self.assertRaisesRegex(ValueError, 'invalid prometheus pin'):
                ASSETS.main(['assets', missing, missing, str(self.dir / 'bin'), version, sha])
        with self.assertRaisesRegex(ValueError, 'usage'):
            ASSETS.main(['assets', missing, missing, str(self.dir / 'bin')])

    def test_version_only_bump_with_stale_digest_fails_closed(self):
        current, _ = prom_archive(self.dir)
        bumped, _ = prom_archive(self.dir, 'v3.16.0')
        with self.assertRaisesRegex(ValueError, 'archive checksum mismatch'):
            self.run_main(bumped, 'v3.16.0', ASSETS.digest(current))
        with self.assertRaisesRegex(ValueError, 'unsafe archive members'):
            self.run_main(current, 'v3.16.0', ASSETS.digest(current))
        self.assertEqual(list(self.dir.glob('**/promtool')), [])

    def test_unsafe_members_are_rejected(self):
        root = 'prometheus-3.15.0.linux-amd64'
        cases = {
            'symlink': [(f'{root}/prometheus', 'symlink', '/etc/passwd')],
            'hardlink': [(f'{root}/NOTICE', 'hardlink', f'{root}/promtool')],
            'special file': [(f'{root}/prometheus.yml', 'fifo', None)],
            'duplicate': [(f'{root}/promtool', 'file', b'shadow')],
            'traversal': [(f'{root}/consoles/../../evil', 'file', b'x')],
            'outside root': [('prometheus-3.8.1.linux-amd64/promtool', 'file', b'x')],
            'unlisted': [(f'{root}/extra', 'file', b'x')],
        }
        for label, extra in cases.items():
            archive, _ = prom_archive(self.dir, extra=extra)
            with self.subTest(label), self.assertRaisesRegex(ValueError, 'unsafe archive members'):
                self.verify(archive, root)
        for label, promtool in {'target symlink': ('symlink', 'prometheus'),
                                'target directory': ('dir', None)}.items():
            archive, _ = prom_archive(self.dir, promtool=promtool)
            with self.subTest(label), self.assertRaisesRegex(ValueError, 'unsafe archive members'):
                self.verify(archive, root)
        self.assertFalse((self.dir / 'promtool').exists())

    def test_member_total_and_destination_bounds_hold(self):
        archive, root = prom_archive(self.dir)
        with mock.patch.object(ASSETS, 'MAX_MEMBER', len(PAYLOAD) - 1), \
                self.assertRaisesRegex(ValueError, 'unsafe archive members'):
            self.verify(archive, root)
        with mock.patch.object(ASSETS, 'MAX_TOTAL', len(PAYLOAD)), \
                self.assertRaisesRegex(ValueError, 'unsafe archive members'):
            self.verify(archive, root)
        existing = self.dir / 'existing'
        existing.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'destination exists'):
            self.verify(archive, root, existing)
        self.assertEqual(existing.read_bytes(), b'keep')


class NativeAssetTests(unittest.TestCase):
    """Run against the real release archive and promtool when CI or a local cache supplies them."""

    def test_archive_digest_covers_promtool_bytes(self):
        version, sha = workflow_env()['PROMETHEUS_VERSION'], workflow_env()['PROMETHEUS_ARCHIVE_SHA256']
        root = f'prometheus-{version[1:]}.linux-amd64'
        archive = pathlib.Path(os.environ.get('PROMETHEUS_ARCHIVE') or TOOLING / f'{root}.tar.gz')
        if not archive.is_file():
            self.skipTest(f'native archive unavailable: {archive}')
        with tempfile.TemporaryDirectory() as directory:
            out = pathlib.Path(directory) / 'promtool'
            ASSETS.verified_tar(archive, sha, f'{root}/promtool', None, out, ASSETS.prometheus_member(root))
            if version == RECEIPT['version']:
                self.assertEqual(ASSETS.digest(out), RECEIPT['promtool'])

    def test_native_promtool_reports_pinned_version(self):
        version = workflow_env()['PROMETHEUS_VERSION']
        fallback = TOOLING / 'promtool315' if version == RECEIPT['version'] else None
        binary = os.environ.get('PROMTOOL_BIN') or fallback
        if not binary or not pathlib.Path(binary).is_file():
            self.skipTest('native promtool unavailable')
        result = subprocess.run([str(binary), '--version'], capture_output=True, text=True, check=True)
        self.assertIn(f'version {version[1:]} ', result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
