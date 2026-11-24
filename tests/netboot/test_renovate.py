"""Renovate tracks every netboot.xyz pin, and the PXE Talos copy moves with the cluster.

Extraction and versioning run through the pinned Renovate engine (44.154.0, as
scripts/test_spektr_renovate.py) from RENOVATE_MODULE_DIR or the repo's
node_modules. With NETBOOT_REQUIRE_RENOVATE=1, as in CI, a missing engine fails
instead of skipping.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOYMENT = 'kubernetes/apps/network/netbootxyz/app/deployment.yaml'
CONFIGMAP = 'kubernetes/apps/network/netbootxyz/app/configmap.yaml'
TALOS_FILES = ('talos/topf.yaml', CONFIGMAP)
RENOVATE_VERSION = '44.154.0'
IMAGE_VERSIONING = 'netboot.xyz image: <webapp semver>-nbxyz<build> tags only'
ENGINE = r'''
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const base = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const load = path => import(pathToFileURL(`${base}/dist/${path}`).href);
const {extractPackageFile} = await load('modules/manager/custom/regex/index.js');
const {getMatchingFiles} = await load('workers/repository/extract/file-match.js');
const {get} = await load('modules/versioning/index.js');
const deps = {};
for (const mgr of input.managers) {
  for (const file of getMatchingFiles(mgr, Object.keys(input.files))) {
    const result = await Promise.resolve(extractPackageFile(input.files[file], file, mgr));
    for (const dep of result?.deps ?? []) (deps[file] ??= []).push(
      {depName: dep.depName, currentValue: dep.currentValue, datasource: dep.datasource});
  }
}
const v = get(input.versioning);
const tags = ['0.7.6-nbxyz28', '0.7.6-nbxyz29', '0.7.7-nbxyz1', 'latest', '0.7.6'];
process.stdout.write(JSON.stringify({deps,
  valid: Object.fromEntries(tags.map(t => [t, v.isValid(t)])),
  newer: v.isGreaterThan('0.7.6-nbxyz29', '0.7.6-nbxyz28')}));
'''


def load_json5(path):
    """Parse the repository's JSON5 subset: double-quoted strings, // comments, trailing commas."""
    source, out, i = (ROOT / path).read_text(), [], 0
    while i < len(source):
        char = source[i]
        if char == '"':
            end = i + 1
            while source[end] != '"':
                end += 2 if source[end] == '\\' else 1
            out.append(source[i:end + 1])
            i = end + 1
        elif source.startswith('//', i):
            i = source.index('\n', i)
        else:
            out.append(char)
            i += 1
    return json.loads(re.sub(r',(\s*[}\]])', r'\1', ''.join(out)))


def renovate_module():
    paths = [ROOT / 'node_modules' / 'renovate']
    if os.environ.get('RENOVATE_MODULE_DIR'):
        paths.insert(0, pathlib.Path(os.environ['RENOVATE_MODULE_DIR']))
    for path in paths:
        manifest = path / 'package.json'
        if manifest.is_file() and json.loads(manifest.read_text())['version'] == RENOVATE_VERSION:
            return path
    return None


class RenovatePolicy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.custom = load_json5('.github/renovate/customManagers.json5')
        cls.groups = load_json5('.github/renovate/groups.json5')['packageRules']
        cls.automerge = load_json5('.github/renovate/autoMerge.json5')['packageRules']
        cls.image_rule = next(r for r in cls.custom['packageRules']
                              if r.get('description') == IMAGE_VERSIONING)
        cls.engine = None

    def run_engine(self):
        if self.engine is None:
            module, node = renovate_module(), shutil.which('node')
            if not (module and node):
                if os.environ.get('NETBOOT_REQUIRE_RENOVATE') == '1':
                    self.fail(f'Renovate {RENOVATE_VERSION} and node are required')
                self.skipTest(f'Renovate {RENOVATE_VERSION} module or node not available')
            files = {p: (ROOT / p).read_text() for p in (DEPLOYMENT,) + TALOS_FILES}
            payload = {'managers': self.custom['customManagers'], 'files': files,
                       'versioning': self.image_rule['versioning']}
            type(self).engine = json.loads(subprocess.check_output(
                [node, '--input-type=module', '-e', ENGINE, str(module)],
                input=json.dumps(payload), text=True, timeout=30))
        return self.engine

    def deps(self, path, name=None):
        return [d for d in self.run_engine()['deps'].get(path, []) if name in (None, d['depName'])]

    def test_menu_and_signed_ipxe_pins_are_extracted(self):
        deps = {d['depName']: d for d in self.deps(DEPLOYMENT)}
        env = dict(re.findall(r'- name: (MENU_VERSION|IPXE_SB_VERSION)\n\s*value: "([^"]+)"',
                              (ROOT / DEPLOYMENT).read_text()))
        self.assertEqual(deps['netbootxyz/netboot.xyz']['currentValue'], env['MENU_VERSION'])
        self.assertEqual(deps['ipxe/ipxe']['currentValue'], env['IPXE_SB_VERSION'])
        for dep in deps.values():
            self.assertEqual(dep['datasource'], 'github-releases')

    def test_pxe_talos_copy_matches_cluster_version(self):
        values = {}
        for path in TALOS_FILES:
            talos = self.deps(path, 'siderolabs/talos')
            self.assertEqual(len(talos), 1, path)
            values[path] = talos[0]['currentValue']
        self.assertEqual(len(set(values.values())), 1, values)

    def test_pxe_talos_copy_shares_the_cluster_pr_and_merge_rules(self):
        talos_group = next(r for r in self.groups if r.get('groupSlug') == 'talos-cluster-version')
        self.assertIn(CONFIGMAP, talos_group['matchFileNames'])
        talos_rules = [r for r in self.automerge
                       if r.get('matchPackageNames') == ['siderolabs/talos']]
        self.assertEqual(len(talos_rules), 2)
        for rule in talos_rules:
            self.assertEqual(set(rule['matchFileNames']), set(talos_group['matchFileNames']))
        root = (ROOT / '.github/renovate.json5').read_text()
        self.assertEqual(root.count(f"'{CONFIGMAP}'"),
                         root.count("'kubernetes/apps/system-upgrade/tuppr/upgrades/talosupgrade.yaml'"))

    def test_image_tags_version_by_build_and_keep_the_digest(self):
        self.assertTrue(self.image_rule['pinDigests'])
        self.assertEqual(self.run_engine()['valid'], {
            '0.7.6-nbxyz28': True, '0.7.6-nbxyz29': True, '0.7.7-nbxyz1': True,
            'latest': False, '0.7.6': False})
        self.assertTrue(self.run_engine()['newer'])


if __name__ == '__main__':
    unittest.main()
