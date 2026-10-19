"""Bounded regression checks for the isolated Groschen Renovate pass."""

import json
import os
from pathlib import Path
import re
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'kubernetes/apps/groschen/app/deployment.yaml'
IMAGE = 'ghcr.io/jlengelbrecht/groschen'
FAST_CONFIG = '.github/renovate/groschen.json5'
FAST_WORKFLOW = '.github/workflows/renovate-groschen.yaml'


def json5(path):
    source = (ROOT / path).read_text()
    expression = 'process.stdout.write(JSON.stringify(require("vm").runInNewContext("("+process.argv[1]+")",{}, {timeout:1000})))'
    return json.loads(subprocess.check_output(['node', '-e', expression, source], text=True))


def renovate_module():
    """Find the exact CLI version used by the workflow; never silently test another."""
    paths = [ROOT / 'node_modules' / 'renovate']
    if os.environ.get('RENOVATE_MODULE_DIR'):
        paths.insert(0, Path(os.environ['RENOVATE_MODULE_DIR']))
    paths.extend((Path.home() / '.npm' / '_npx').glob('*/node_modules/renovate'))
    for path in paths:
        manifest = path / 'package.json'
        if manifest.is_file() and json.loads(manifest.read_text())['version'] == '44.154.0':
            return path
    raise AssertionError('Renovate 44.154.0 must be installed (set RENOVATE_MODULE_DIR)')


def extracted_policy(config):
    """Run Renovate's pinned Kubernetes extractor and package-rule engine."""
    source = r'''
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const base = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const load = (path) => import(pathToFileURL(`${base}/dist/${path}`).href);
const { extractPackageFile } = await load('modules/manager/kubernetes/extract.js');
const { applyPackageRules } = await load('util/package-rules/index.js');
const extracted = extractPackageFile(input.content, input.file, {});
const types = ['digest', 'pinDigest', 'major', 'minor', 'patch', 'pin',
  'rollback', 'bump', 'replacement'];
const results = [];
for (const [index, dep] of extracted.deps.entries()) {
  for (const updateType of types) {
    const effective = await applyPackageRules({
      ...input.config, ...dep, packageFile: input.file, manager: 'kubernetes',
      updateType, packageName: dep.packageName ?? dep.depName,
    });
    results.push({index, updateType, datasource: dep.datasource,
      depName: dep.depName, currentValue: dep.currentValue,
      enabled: effective.enabled !== false && !effective.skipReason,
      automerge: effective.automerge ?? false,
      automergeType: effective.automergeType ?? null,
      ignoreTests: effective.ignoreTests ?? null,
      platformAutomerge: effective.platformAutomerge ?? null,
      allowedVersions: effective.allowedVersions ?? null,
      pinDigests: effective.pinDigests ?? null,
      groupName: effective.groupName ?? null,
      groupSlug: effective.groupSlug ?? null,
      minimumReleaseAge: effective.minimumReleaseAge ?? null});
  }
}
const alternate = await applyPackageRules({
  ...input.config, datasource: 'docker', depName: input.image,
  packageName: input.image, packageFile: input.file, manager: 'kubernetes',
  currentValue: 'v1.0.0', updateType: 'digest',
});
process.stdout.write(JSON.stringify({deps: extracted.deps, results,
  alternateEnabled: alternate.enabled !== false && !alternate.skipReason}));
'''
    payload = {'config': config, 'content': (ROOT / TARGET).read_text(),
               'file': TARGET, 'image': IMAGE}
    output = subprocess.check_output(
        ['node', '--input-type=module', '-e', source, str(renovate_module())],
        input=json.dumps(payload), text=True, timeout=15)
    return json.loads(output)


class GroschenPassTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fast = json5(FAST_CONFIG)
        cls.general = json5('.github/renovate.json5')
        cls.workflow_text = (ROOT / FAST_WORKFLOW).read_text()
        cls.workflow = yaml.safe_load(cls.workflow_text)
        cls.actual = extracted_policy(cls.fast)

    def test_general_pass_cannot_write_groschen_branch(self):
        self.assertIn(TARGET, self.general['ignorePaths'])
        self.assertNotIn(TARGET, self.fast.get('ignorePaths', []))
        self.assertEqual(self.fast['requireConfig'], 'ignored')

    def test_fast_pass_uses_its_own_unlimited_repo_queue(self):
        self.assertEqual(self.fast['includePaths'], [TARGET])
        self.assertEqual(self.fast['enabledManagers'], ['kubernetes'])
        self.assertFalse(self.fast['dependencyDashboard'])
        for field in ('prConcurrentLimit', 'branchConcurrentLimit', 'prHourlyLimit',
                      'commitHourlyLimit'):
            self.assertEqual(self.fast[field], 0, field)
            self.assertGreater(self.general.get(field, 1), 0, field)
        self.assertNotIn('prPriority', self.fast)
        for rule in self.fast['packageRules']:
            self.assertFalse(any(key.endswith('Limit') for key in rule))
            self.assertNotIn('vulnerabilityAlerts', rule)

    def test_only_main_digest_of_production_image_is_enabled(self):
        deps = self.actual['deps']
        self.assertEqual(len(deps), 6)
        self.assertEqual([dep['datasource'] for dep in deps].count('kubernetes-api'), 2)
        self.assertEqual([dep['depName'] for dep in deps].count(IMAGE), 2)
        self.assertEqual([dep['depName'] for dep in deps].count('busybox'), 2)
        self.assertFalse(self.actual['alternateEnabled'])
        for result in self.actual['results']:
            allowed = (result['datasource'] == 'docker' and
                       result['depName'] == IMAGE and result['currentValue'] == 'main' and
                       result['updateType'] in ('digest', 'pinDigest'))
            with self.subTest(index=result['index'], update=result['updateType']):
                self.assertEqual(result['enabled'], allowed)
                self.assertEqual(result['automerge'], allowed)
                if allowed:
                    self.assertEqual(result['automergeType'], 'pr')
                    self.assertFalse(result['ignoreTests'])
                    self.assertTrue(result['platformAutomerge'])
                    self.assertEqual(result['allowedVersions'], '/^main$/')
                    self.assertTrue(result['pinDigests'])
                    self.assertEqual(result['groupName'], 'Groschen image')
                    self.assertEqual(result['groupSlug'], 'groschen-image')
                    self.assertEqual(result['minimumReleaseAge'], '0')

    def test_unrelated_and_retired_workloads_are_excluded(self):
        self.assertEqual(self.fast['includePaths'], [TARGET])
        for path in ('kubernetes/apps/groschen-test/groschen/app/deployment.yaml',
                     'kubernetes/apps/groschen-test/groschen/admin/job.yaml',
                     'kubernetes/apps/other/app/deployment.yaml'):
            self.assertNotIn(path, self.fast['includePaths'])

    def test_paired_production_images_remain_pinned(self):
        documents = list(yaml.safe_load_all((ROOT / TARGET).read_text()))
        deployments = [doc for doc in documents if doc and doc.get('kind') == 'Deployment']
        self.assertEqual({doc['metadata']['name'] for doc in deployments},
                         {'groschen-api', 'groschen-worker'})
        for doc in deployments:
            images = [container['image'] for container in
                      doc['spec']['template']['spec']['containers']]
            self.assertEqual(len(images), 1)
            self.assertRegex(images[0], rf'^{re.escape(IMAGE)}:main@sha256:[0-9a-f]{{64}}$')
        self.assertEqual(deployments[0]['spec']['template']['spec']['containers'][0]['image'],
                         deployments[1]['spec']['template']['spec']['containers'][0]['image'])

    def test_schedule_auth_and_writer_serialization(self):
        events = self.workflow.get('on', self.workflow.get(True))
        self.assertIn('workflow_dispatch', events)
        self.assertEqual(events['schedule'], [{'cron': '*/5 * * * *'}])
        self.assertEqual(self.workflow['concurrency'],
                         {'group': 'renovate-groschen', 'cancel-in-progress': False})
        self.assertEqual(self.workflow['permissions'], {'contents': 'read'})
        steps = self.workflow['jobs']['renovate']['steps']
        self.assertEqual(steps[0]['uses'], 'actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349')
        self.assertEqual(steps[1]['uses'], 'actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09')
        action = steps[-1]
        self.assertEqual(action['uses'], 'renovatebot/github-action@eaf12548c13069dcc28bb75c4ee4610cdbe400c5')
        self.assertEqual(action['with']['configurationFile'], FAST_CONFIG)
        self.assertEqual(action['with']['renovate-version'], '44.154.0')
        self.assertIn('steps.app-token.outputs.token', action['with']['token'])
        self.assertIn('secrets.GHCR_READ_PAT', action['env']['RENOVATE_HOST_RULES'])
        self.assertIn('secrets.BOT_APP_ID', str(steps[0]['with']))
        self.assertIn('secrets.BOT_APP_PRIVATE_KEY', str(steps[0]['with']))


if __name__ == '__main__':
    unittest.main()
