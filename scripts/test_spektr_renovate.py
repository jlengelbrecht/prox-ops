"""Bounded shared-hourly Spektr Renovate regression (44.154.0)."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
TARGET = 'kubernetes/apps/ai/agent-router/app/ocirepository.yaml'
IMAGE = 'ghcr.io/jlengelbrecht/agent-router-base'
CONFIG = '.github/renovate.json5'
WORKFLOW = '.github/workflows/renovate.yaml'
REMOVED = ('.github/workflows/renovate-spektr.yaml',
           '.github/workflows/renovate-groschen.yaml',
           '.github/renovate/spektr.json5',
           '.github/renovate/groschen.json5')


def json5(path):
    source = (ROOT / path).read_text()
    script = 'process.stdout.write(JSON.stringify(require("vm").runInNewContext("("+process.argv[1]+")",{}, {timeout:1000})))'
    return json.loads(subprocess.check_output(['node', '-e', script, source], text=True))


def renovate_module():
    paths = [ROOT / 'node_modules' / 'renovate']
    if os.environ.get('RENOVATE_MODULE_DIR'):
        paths.insert(0, Path(os.environ['RENOVATE_MODULE_DIR']))
    paths.extend((Path.home() / '.npm' / '_npx').glob('*/node_modules/renovate'))
    for path in paths:
        manifest = path / 'package.json'
        if manifest.is_file() and json.loads(manifest.read_text())['version'] == '44.154.0':
            return path
    raise AssertionError('Renovate 44.154.0 must be installed (set RENOVATE_MODULE_DIR)')


def shared_config():
    """Resolve the four checked-out presets in extends order with Renovate's merger."""
    script = r'''
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';
const base = process.argv[1];
const root = process.argv[2];
const {mergeChildConfig} = await import(pathToFileURL(`${base}/dist/config/utils.js`).href);
const {migrateConfig} = await import(pathToFileURL(`${base}/dist/config/migration.js`).href);
const parse = path => vm.runInNewContext(`(${fs.readFileSync(path, 'utf8')})`, {}, {timeout:1000});
const config = parse(`${root}/.github/renovate.json5`);
let merged = {};
for (const ref of config.extends) {
  if (!ref.startsWith('github>jlengelbrecht/prox-ops//')) continue;
  const path = ref.split('//')[1];
  merged = mergeChildConfig(merged, parse(`${root}/${path}`));
}
const localRefs = config.extends.filter(ref => ref.startsWith('github>jlengelbrecht/prox-ops//'));
merged = mergeChildConfig(merged, {...config, extends:config.extends.filter(ref => !localRefs.includes(ref))});
process.stdout.write(JSON.stringify({config:merged, validationConfig:migrateConfig(merged).migratedConfig, localRefs}));
'''
    result = subprocess.check_output(
        ['node', '--input-type=module', '-e', script, str(renovate_module()), str(ROOT)],
        text=True, timeout=15)
    return json.loads(result)


def renovate_decisions(config, content):
    script = r'''
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
const base = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const load = (path) => import(pathToFileURL(`${base}/dist/${path}`).href);
const {extractPackageFile} = await load('modules/manager/custom/regex/index.js');
const {applyPackageRules} = await load('util/package-rules/index.js');
const {filterVersions} = await load('workers/repository/process/lookup/filter.js');
const {resolveUpdateDigests} = await load('workers/repository/process/lookup/digest.js');
const {stripNoopUpdates} = await load('workers/repository/process/lookup/utils.js');
const {getMatchingFiles} = await load('workers/repository/extract/file-match.js');
const {api: semver} = await load('modules/versioning/semver/index.js');
const mgr = input.config.customManagers.find(m => m.description?.includes('Spektr deployment base'));
const files = getMatchingFiles(mgr, [input.file, 'kubernetes/apps/other/app/ocirepository.yaml']);
const deps = extractPackageFile(input.content, input.file, mgr)?.deps ?? [];
const dep = deps[0];
const effective = dep ? await applyPackageRules({
  ...input.config, ...dep, manager:'custom.regex', packageFile:input.file,
  packageName:dep.packageName ?? dep.depName,
}, 'pre-lookup') : null;
const types = ['major','minor','patch','digest','pinDigest'];
const rules = {};
for (const updateType of types) rules[updateType] = await applyPackageRules({
  ...input.config, ...dep, manager:'custom.regex', packageFile:input.file,
  packageName:dep.packageName ?? dep.depName, updateType,
});
const releases = ['v0.8.1-rc.2','v0.8.1','v0.8.2','v0.9.0-alpha.1',
  'v0.9.0-beta.1','v0.9.0','v1.0.0-rc.1','v1.0.0','latest','main','sha-abcdef'];
const eligible = {};
for (const current of ['v0.8.0','v0.8.1-rc.1','v0.8.1-rc.2'])
  eligible[current] = filterVersions({...effective, depName:dep.depName}, current,
    'v0.8.1', releases.map(version => ({version})), semver).map(r => r.version);
const oldDigest = dep.currentDigest;
const nextDigest = `sha256:${'a'.repeat(64)}`;
// Exercise the same digest resolution and no-op filtering used by lookupUpdates.
// The fixture release carries its digest, so no registry or production write is needed.
const digestConfig = {...effective, currentDigest:oldDigest, pinDigests:true};
const digestResult = {updates:[{updateType:'digest', newValue:dep.currentValue}], warnings:[]};
await resolveUpdateDigests(digestConfig, digestResult,
  {releases:[{version:dep.currentValue, newDigest:nextDigest}]});
digestResult.updates = stripNoopUpdates(digestConfig, digestResult.updates);
const unchangedDigestResult = {updates:[{updateType:'digest', newValue:dep.currentValue}], warnings:[]};
await resolveUpdateDigests(digestConfig, unchangedDigestResult,
  {releases:[{version:dep.currentValue, newDigest:oldDigest}]});
unchangedDigestResult.updates = stripNoopUpdates(digestConfig, unchangedDigestResult.updates);
const digestRule = await applyPackageRules({...digestConfig, updateType:'digest'});
const replacePair = (value, digest) => input.content.replace(dep.replaceString,
  dep.replaceString.replace(dep.currentValue, value).replace(dep.currentDigest, digest));
const paired = replacePair('v0.8.1-rc.2', nextDigest);
const digestOnly = replacePair(dep.currentValue, nextDigest);
const pairedExtract = extractPackageFile(paired, input.file, mgr)?.deps ?? [];
const digestExtract = extractPackageFile(digestOnly, input.file, mgr)?.deps ?? [];
const foreign = await applyPackageRules({...input.config, manager:'custom.regex',
  packageFile:input.file, datasource:'docker', depName:'ghcr.io/other/image',
  packageName:'ghcr.io/other/image', currentValue:'v1.0.0', updateType:'patch'});
process.stdout.write(JSON.stringify({files,deps,effective,rules,eligible,paired,digestOnly,
  oldDigest, nextDigest, foreign, pairedExtract, digestExtract,
  digestUpdates:digestResult.updates, unchangedDigestUpdates:unchangedDigestResult.updates,
  digestRule}));
'''
    payload = {'config': config, 'content': content, 'file': TARGET}
    result = subprocess.check_output(
        ['node', '--input-type=module', '-e', script, str(renovate_module())],
        input=json.dumps(payload), text=True, timeout=20)
    return json.loads(result)


class SpektrPassTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.general = json5(CONFIG)
        cls.resolved = shared_config()
        cls.content = (ROOT / TARGET).read_text()
        cls.actual = renovate_decisions(cls.resolved['config'], cls.content)
        cls.workflow = yaml.safe_load((ROOT / WORKFLOW).read_text())

    def test_shared_extraction_and_policy_order(self):
        self.assertNotIn(TARGET, self.general['ignorePaths'])
        self.assertEqual(len(self.resolved['localRefs']), 4)
        rules = self.resolved['config']['packageRules']
        self.assertLess(next(i for i, r in enumerate(rules) if 'Global safety net' in r.get('description', '')),
                        next(i for i, r in enumerate(rules) if 'Auto-merge Spektr' in r.get('description', '')))
        self.assertEqual(self.actual['files'], [TARGET])
        self.assertEqual(len(self.actual['deps']), 1)
        dep = self.actual['deps'][0]
        self.assertEqual(dep['depName'], IMAGE)
        self.assertEqual(dep['currentValue'], 'v0.8.1-rc.1')
        self.assertRegex(dep['currentDigest'], r'^sha256:[a-f0-9]{64}$')
        self.assertIsNot(self.actual['foreign'].get('enabled'), False)
        self.assertTrue(self.actual['foreign']['automerge'])
        self.assertEqual(self.actual['foreign']['minimumReleaseAge'], '3 days')
        self.assertNotIn('spektr', self.actual['foreign'].get('addLabels', []))

    def test_version_policy_and_ci(self):
        effective = self.actual['effective']
        self.assertFalse(effective['ignoreUnstable'])
        self.assertFalse(effective['respectLatest'])
        self.assertEqual(effective['minimumReleaseAge'], '0')
        self.assertTrue(effective['pinDigests'])
        self.assertTrue(effective['enabled'])
        self.assertTrue(self.resolved['config']['platformAutomerge'])
        for kind, rule in self.actual['rules'].items():
            with self.subTest(kind=kind):
                self.assertTrue(rule['automerge'])
                self.assertEqual(rule['automergeType'], 'pr')
                self.assertFalse(rule['ignoreTests'])
                self.assertTrue(rule['platformAutomerge'])
        eligible = self.actual['eligible']
        self.assertIn('v0.9.0-alpha.1', eligible['v0.8.0'])
        self.assertIn('v0.9.0-beta.1', eligible['v0.8.0'])
        self.assertIn('v1.0.0-rc.1', eligible['v0.8.0'])
        self.assertIn('v0.8.1-rc.2', eligible['v0.8.1-rc.1'])
        self.assertIn('v0.8.1', eligible['v0.8.1-rc.2'])
        for version in ('v0.8.2', 'v0.9.0', 'v1.0.0'):
            self.assertIn(version, eligible['v0.8.1-rc.1'])
        for candidates in eligible.values():
            for invalid in ('latest', 'main', 'sha-abcdef'):
                self.assertNotIn(invalid, candidates)

    def test_paired_replacement(self):
        actual = self.actual
        self.assertIn('tag: v0.8.1-rc.2', actual['paired'])
        self.assertIn('digest: ' + actual['nextDigest'], actual['paired'])
        self.assertNotIn(actual['oldDigest'], actual['paired'])
        self.assertIn('tag: v0.8.1-rc.1', actual['digestOnly'])
        self.assertIn('digest: ' + actual['nextDigest'], actual['digestOnly'])
        self.assertEqual(actual['pairedExtract'][0]['currentValue'], 'v0.8.1-rc.2')
        self.assertEqual(actual['pairedExtract'][0]['currentDigest'], actual['nextDigest'])
        self.assertEqual(actual['digestExtract'][0]['currentValue'], 'v0.8.1-rc.1')
        self.assertEqual(actual['digestExtract'][0]['currentDigest'], actual['nextDigest'])

    def test_same_tag_digest_generation(self):
        updates = self.actual['digestUpdates']
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]['updateType'], 'digest')
        self.assertEqual(updates[0]['newValue'], self.actual['deps'][0]['currentValue'])
        self.assertEqual(updates[0]['newDigest'], self.actual['nextDigest'])
        self.assertEqual(self.actual['unchangedDigestUpdates'], [])
        self.assertTrue(self.actual['digestRule']['automerge'])
        self.assertFalse(self.actual['digestRule']['ignoreTests'])

    def test_pinned_config_validator(self):
        validator = renovate_module() / 'dist' / 'config-validator.js'
        with tempfile.TemporaryDirectory() as directory:
            flattened = Path(directory) / 'renovate.json'
            flattened.write_text(json.dumps(self.resolved['validationConfig']))
            completed = subprocess.run(
                ['node', str(validator), '--strict', '--no-global', str(flattened)],
                cwd=ROOT, text=True, capture_output=True, timeout=20)
        self.assertEqual(completed.returncode, 0,
                         completed.stdout + completed.stderr)

    def test_shared_hourly_workflow_and_queue(self):
        events = self.workflow.get('on', self.workflow.get(True))
        self.assertEqual(events['schedule'], [{'cron': '0 * * * *'}])
        self.assertIn('workflow_dispatch', events)
        self.assertEqual(self.general['prConcurrentLimit'], 5)
        self.assertEqual(self.general['branchConcurrentLimit'], 10)
        self.assertEqual(self.general['prHourlyLimit'], 2)
        self.assertEqual(self.general['schedule'], ['at any time'])
        for path in REMOVED:
            self.assertFalse((ROOT / path).exists(), path)
        steps = self.workflow['jobs']['renovate']['steps']
        self.assertEqual(steps[-1]['with']['configurationFile'], CONFIG)
        self.assertIn('steps.app-token.outputs.token', steps[-1]['with']['token'])
        self.assertIn('secrets.GHCR_READ_PAT', steps[-1]['env']['RENOVATE_HOST_RULES'])
        check = (ROOT / '.github/workflows/spektr-upgrade-check.yaml').read_text()
        self.assertIn('name: Spektr Upgrade Check', check)
        self.assertIn('- kubernetes/apps/ai/agent-router/app/ocirepository.yaml', check)


if __name__ == '__main__':
    unittest.main()
