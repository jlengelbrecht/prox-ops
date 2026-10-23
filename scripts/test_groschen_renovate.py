"""Bounded regression checks for shared-hourly Groschen Renovate."""

import json
from pathlib import Path
import re
import subprocess
import unittest

import yaml
from test_spektr_renovate import REMOVED, json5, renovate_module, shared_config

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'kubernetes/apps/groschen/app/deployment.yaml'
IMAGE = 'ghcr.io/jlengelbrecht/groschen'
WORKFLOW = '.github/workflows/renovate.yaml'


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
const { getManagerConfig } = await load('config/index.js');
const { getMatchingFiles } = await load('workers/repository/extract/file-match.js');
const { resolveUpdateDigests } = await load('workers/repository/process/lookup/digest.js');
const { stripNoopUpdates } = await load('workers/repository/process/lookup/utils.js');
const { setCount, isLimitReached } = await load('workers/global/limits.js');
const { sortBranches } = await load('workers/repository/process/sort.js');
const fetchSource = fs.readFileSync(`${base}/dist/workers/repository/process/fetch.js`, 'utf8');
const disabledStopsLookup = /if \(depConfig\.enabled === false\) \{[\s\S]*?dep\.skipReason = "disabled";[\s\S]*?return Result\.ok\(dep\);/.test(fetchSource);
const managerConfig = getManagerConfig(input.config, 'kubernetes');
const matchedFiles = getMatchingFiles(managerConfig, [
  input.file, 'kubernetes/apps/groschen-test/groschen/app/deployment.yaml',
  'kubernetes/apps/other/app/deployment.yaml',
]);
const extracted = extractPackageFile(input.content, input.file, {});
const types = ['digest', 'pinDigest', 'major', 'minor', 'patch', 'pin',
  'rollback', 'bump', 'replacement'];
const results = [];
const preLookup = [];
for (const [index, dep] of extracted.deps.entries()) {
  const effective = await applyPackageRules({
    ...input.config, ...dep, packageFile: input.file, manager: 'kubernetes',
    packageName: dep.packageName ?? dep.depName,
  }, 'pre-lookup');
  preLookup.push({index, datasource: dep.datasource, depName: dep.depName,
    currentValue: dep.currentValue, hasUpdateType: 'updateType' in dep,
    enabled: effective.enabled !== false && !effective.skipReason,
    skipReason: effective.skipReason ?? null});
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
      minimumReleaseAge: effective.minimumReleaseAge ?? null,
      branchConcurrentLimit: effective.branchConcurrentLimit ?? null,
      prConcurrentLimit: effective.prConcurrentLimit ?? null,
      prHourlyLimit: effective.prHourlyLimit ?? null,
      prPriority: effective.prPriority ?? null});
  }
}
const alternate = await applyPackageRules({
  ...input.config, datasource: 'docker', depName: input.image,
  packageName: input.image, packageFile: input.file, manager: 'kubernetes',
  currentValue: 'v1.0.0', updateType: 'digest',
});
const digestUpdates = [];
for (const dep of extracted.deps.filter(dep => dep.depName === input.image)) {
  const nextDigest = `sha256:${'a'.repeat(64)}`;
  const config = {...input.config, ...dep, packageFile:input.file,
    manager:'kubernetes', packageName:dep.packageName ?? dep.depName,
    pinDigests:true};
  const result = {updates:[{updateType:'digest', newValue:dep.currentValue}], warnings:[]};
  await resolveUpdateDigests(config, result,
    {releases:[{version:dep.currentValue, newDigest:nextDigest}]});
  digestUpdates.push({oldDigest:dep.currentDigest,
    updates:stripNoopUpdates(config, result.updates)});
}
const isolation = [];
for (const [packageFile, depName, currentValue] of [
  ['kubernetes/apps/groschen-test/groschen/app/deployment.yaml', input.image, 'main'],
  ['kubernetes/apps/groschen-test/groschen/admin/job.yaml', input.image, 'main'],
  [input.file, input.image, 'v1.0.0'],
  [input.file, 'busybox', 'main'],
]) {
  const policy = await applyPackageRules({...input.config, manager:'kubernetes',
    packageFile, datasource:'docker', depName, packageName:depName,
    currentValue, updateType:'digest'});
  isolation.push({packageFile, depName, currentValue,
    enabled:policy.enabled !== false, automerge:policy.automerge ?? false});
}
const groschen = results.filter(result => result.datasource === 'docker' &&
  result.depName === input.image && result.currentValue === 'main' &&
  result.updateType === 'digest');
const foreign = await applyPackageRules({...input.config, manager:'kubernetes',
  packageFile:'kubernetes/apps/other/app/deployment.yaml', datasource:'docker',
  depName:'ghcr.io/other/image', packageName:'ghcr.io/other/image',
  currentValue:'v1.0.0', updateType:'patch'});
// A single grouped API + worker branch is checked against the engine's counters.
const groupedBranch = {upgrades:groschen};
const foreignBranch = {upgrades:[foreign]};
const limits = {};
for (const [scenario, counts, key] of [
  ['branches', [10, 0, 0], 'Branches'],
  ['concurrentPRs', [0, 5, 0], 'ConcurrentPRs'],
  ['hourlyPRs', [0, 0, 2], 'Branches'],
  ['fullQueue', [10, 5, 2], 'Branches'],
]) {
  for (const [name, count] of ['Branches', 'ConcurrentPRs', 'HourlyPRs'].map(
    (name, i) => [name, counts[i]])) setCount(name, count);
  limits[scenario] = {
    groschen:isLimitReached(key, groupedBranch),
    foreign:isLimitReached(key, foreignBranch),
  };
}
const order = [
  {prTitle:'Other patch', updateType:'patch', prPriority:foreign.prPriority},
  {prTitle:'Groschen digest', updateType:'digest', prPriority:groschen[0].prPriority},
];
sortBranches(order);
process.stdout.write(JSON.stringify({matchedFiles, deps: extracted.deps, preLookup,
  disabledStopsLookup, results,
  alternateEnabled: alternate.enabled !== false && !alternate.skipReason,
  alternateAutomerge: alternate.automerge ?? false, isolation, digestUpdates,
  groupedCount:groschen.length, foreign, limits, order:order.map(branch => branch.prTitle)}));
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
        """Resolve checked-out shared presets and inspect pinned Renovate."""
        cls.general = json5('.github/renovate.json5')
        cls.resolved = shared_config()['config']
        cls.workflow_text = (ROOT / WORKFLOW).read_text()
        cls.workflow = yaml.safe_load(cls.workflow_text)
        cls.actual = extracted_policy(cls.resolved)

    def test_shared_pass_discovers_groschen(self):
        self.assertNotIn(TARGET, self.general['ignorePaths'])
        self.assertEqual(self.general['prConcurrentLimit'], 5)
        self.assertEqual(self.general['branchConcurrentLimit'], 10)
        self.assertEqual(self.general['prHourlyLimit'], 2)
        for path in REMOVED:
            self.assertFalse((ROOT / path).exists(), path)

    def test_kubernetes_manager_discovers_production_deployment(self):
        self.assertIn(TARGET, self.actual['matchedFiles'])

    def test_shared_queue_and_policy_order(self):
        self.assertTrue(self.general['dependencyDashboard'])
        self.assertNotIn('prPriority', self.general)
        rules = self.resolved['packageRules']
        self.assertLess(next(i for i, r in enumerate(rules) if 'Global safety net' in r.get('description', '')),
                        next(i for i, r in enumerate(rules) if 'Auto-merge only Groschen' in r.get('description', '')))

    def test_only_main_digest_of_production_image_is_enabled(self):
        """Check pre-lookup eligibility and post-lookup update decisions."""
        deps = self.actual['deps']
        self.assertEqual(len(deps), 6)
        self.assertEqual([dep['datasource'] for dep in deps].count('kubernetes-api'), 2)
        self.assertEqual([dep['depName'] for dep in deps].count(IMAGE), 2)
        self.assertEqual([dep['depName'] for dep in deps].count('busybox'), 2)
        self.assertTrue(self.actual['alternateEnabled'])
        self.assertFalse(self.actual['alternateAutomerge'])
        self.assertTrue(self.actual['disabledStopsLookup'])
        for result in self.actual['preLookup']:
            with self.subTest(index=result['index'], stage='pre-lookup'):
                self.assertFalse(result['hasUpdateType'])
                self.assertEqual(result['enabled'], result['datasource'] == 'docker')
        for result in self.actual['results']:
            target = (result['datasource'] == 'docker' and
                      result['depName'] == IMAGE and result['currentValue'] == 'main')
            allowed = target and result['updateType'] in ('digest', 'pinDigest')
            with self.subTest(index=result['index'], update=result['updateType']):
                if target:
                    self.assertEqual(result['enabled'], allowed)
                    self.assertEqual(result['automerge'], allowed)
                elif result['datasource'] == 'kubernetes-api':
                    self.assertFalse(result['enabled'])
                else:
                    self.assertTrue(result['enabled'])
                    self.assertEqual(result['automerge'],
                                     result['updateType'] in ('patch', 'minor'))
                if allowed:
                    self.assertEqual(result['automergeType'], 'pr')
                    self.assertFalse(result['ignoreTests'])
                    self.assertTrue(result['platformAutomerge'])
                    self.assertEqual(result['allowedVersions'], '/^main$/')
                    self.assertTrue(result['pinDigests'])
                    self.assertEqual(result['groupName'], 'Groschen image')
                    self.assertEqual(result['groupSlug'], 'groschen-image')
                    self.assertEqual(result['minimumReleaseAge'], '0')
                    self.assertEqual(result['branchConcurrentLimit'], 0)
                    self.assertEqual(result['prConcurrentLimit'], 0)
                    self.assertEqual(result['prHourlyLimit'], 0)
                    self.assertEqual(result['prPriority'], 10)

    def test_full_queue_grouped_groschen_capacity(self):
        self.assertEqual(self.actual['groupedCount'], 2)
        self.assertEqual(self.actual['limits'], {
            scenario: {'groschen': False, 'foreign': True}
            for scenario in ('branches', 'concurrentPRs', 'hourlyPRs', 'fullQueue')
        })
        self.assertEqual(self.actual['order'], ['Groschen digest', 'Other patch'])

    def test_unrelated_and_retired_workloads_are_isolated(self):
        for result in self.actual['isolation']:
            with self.subTest(path=result['packageFile'], dep=result['depName']):
                self.assertFalse(result['automerge'])
                self.assertEqual(result['enabled'],
                                 result['packageFile'] not in (
                                     'kubernetes/apps/groschen-test/groschen/app/deployment.yaml',
                                     'kubernetes/apps/groschen-test/groschen/admin/job.yaml'))

    def test_both_main_images_generate_digest_refreshes(self):
        self.assertEqual(len(self.actual['digestUpdates']), 2)
        for result in self.actual['digestUpdates']:
            self.assertRegex(result['oldDigest'], r'^sha256:[a-f0-9]{64}$')
            self.assertEqual(len(result['updates']), 1)
            self.assertEqual(result['updates'][0]['updateType'], 'digest')
            self.assertEqual(result['updates'][0]['newValue'], 'main')
            self.assertEqual(result['updates'][0]['newDigest'], 'sha256:' + 'a' * 64)

    def test_paired_production_images_remain_pinned(self):
        """Require matching pinned main images in API and worker deployments."""
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
        """Verify the existing shared hourly trigger and token source."""
        events = self.workflow.get('on', self.workflow.get(True))
        self.assertIn('workflow_dispatch', events)
        self.assertEqual(events['schedule'], [{'cron': '0 * * * *'}])
        steps = self.workflow['jobs']['renovate']['steps']
        self.assertEqual(steps[0]['uses'], 'actions/create-github-app-token@fee1f7d63c2ff003460e3d139729b119787bc349')
        self.assertEqual(steps[1]['uses'], 'actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09')
        action = steps[-1]
        self.assertEqual(action['uses'], 'renovatebot/github-action@eaf12548c13069dcc28bb75c4ee4610cdbe400c5')
        self.assertEqual(action['with']['configurationFile'], '.github/renovate.json5')
        self.assertIn('steps.app-token.outputs.token', action['with']['token'])
        self.assertIn('secrets.GHCR_READ_PAT', action['env']['RENOVATE_HOST_RULES'])
        self.assertIn('secrets.BOT_APP_ID', str(steps[0]['with']))
        self.assertIn('secrets.BOT_APP_PRIVATE_KEY', str(steps[0]['with']))


if __name__ == '__main__':
    unittest.main()
