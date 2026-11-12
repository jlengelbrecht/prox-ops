"""Exercise Nyctr extraction and release policy with the pinned Renovate engine."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml
from test_spektr_renovate import renovate_module, shared_config

ROOT = Path(__file__).resolve().parents[1]
APP = 'kubernetes/apps/security/nyctr/app/'


class NyctrReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = shared_config()
        script = r'''
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const load = p => import(pathToFileURL(`${process.argv[1]}/dist/${p}`).href);
const {extractPackageFile: imageExtract} = await load('modules/manager/custom/regex/index.js');
const {extractPackageFile: fluxExtract} = await load('modules/manager/flux/extract.js');
const {applyPackageRules} = await load('util/package-rules/index.js');
const {filterVersions} = await load('workers/repository/process/lookup/filter.js');
const {api: semver} = await load('modules/versioning/semver/index.js');
const mgr = input.config.customManagers.find(m => m.description?.startsWith('Track Nyctr image'));
const image = imageExtract(input.image, input.path + 'helmrelease.yaml', mgr).deps;
const chart = fluxExtract(input.chart, input.path + 'ocirepository.yaml', {}).deps;
const result = {image, chart, rules:[], eligible:[]};
for (const [deps, manager, file] of [[image,'custom.regex','helmrelease.yaml'], [chart,'flux','ocirepository.yaml']]) {
  const dep = deps[0];
  const effective = await applyPackageRules({...input.config, ...dep, manager,
    packageFile:input.path+file, packageName:dep.packageName ?? dep.depName}, 'pre-lookup');
  // Policy fixtures stay fixed as the real manifests advance through releases.
  result.eligible.push(filterVersions(effective, 'v0.1.0-alpha.3', undefined,
    ['v0.1.0-alpha.4','v0.2.0-alpha.1','v0.1.0','latest','main'].map(version=>({version})), semver).map(r=>r.version));
  for (const updateType of ['major','minor','patch','digest','pinDigest']) {
    const rule = await applyPackageRules({...effective, updateType});
    result.rules.push({group:rule.groupName,automerge:rule.automerge,ignoreTests:rule.ignoreTests,enabled:rule.enabled,approval:rule.dependencyDashboardApproval,pin:rule.pinDigests});
  }
}
const dep = image[0];
const replaced = input.image.replace(dep.replaceString, dep.replaceString
  .replace(dep.currentValue,'v0.1.0-alpha.4').replace(dep.currentDigest,`sha256:${'a'.repeat(64)}`));
result.replaced = replaced;
result.next = imageExtract(replaced, input.path+'helmrelease.yaml', mgr).deps[0];
process.stdout.write(JSON.stringify(result));
'''
        cls.script = script
        cls.fixture_input = {'config': cls.config['config'], 'path': APP,
                             'image': (ROOT / APP / 'helmrelease.yaml').read_text(),
                             'chart': (ROOT / APP / 'ocirepository.yaml').read_text()}
        cls.result = json.loads(subprocess.check_output(
            ['node', '--input-type=module', '-e', script, str(renovate_module())],
            input=json.dumps({'config': cls.config['config'], 'path': APP,
                             'image': (ROOT / APP / 'helmrelease.yaml').read_text(),
                             'chart': (ROOT / APP / 'ocirepository.yaml').read_text()}),
            text=True, timeout=30))

    def test_chart_image_extraction_and_alignment(self):
        image, chart = self.result['image'], self.result['chart']
        self.assertEqual(len(image), 1)
        self.assertEqual(len(chart), 1)
        self.assertEqual(image[0]['depName'], 'ghcr.io/homelab-0/nyctr')
        self.assertEqual(chart[0]['depName'], 'ghcr.io/homelab-0/charts/nyctr')
        self.assertEqual(image[0]['currentValue'].removeprefix('v'), chart[0]['currentValue'])
        self.assertRegex(image[0]['currentDigest'], r'^sha256:[a-f0-9]{64}$')

    def test_prereleases_and_manual_group(self):
        for candidates in self.result['eligible']:
            self.assertIn('v0.1.0-alpha.4', candidates)
            self.assertIn('v0.2.0-alpha.1', candidates)
            self.assertNotIn('latest', candidates)
            self.assertNotIn('main', candidates)
        for rule in self.result['rules']:
            self.assertEqual(rule, dict(group='Nyctr release', automerge=False,
                                      ignoreTests=False, enabled=True, approval=False, pin=True))

    def test_policy_survives_release_progression(self):
        image_dep = self.result['image'][0]
        chart_dep = self.result['chart'][0]
        for version in ('0.1.0-alpha.4', '0.1.0'):
            with self.subTest(version=version):
                fixture = dict(self.fixture_input)
                fixture['image'] = fixture['image'].replace(
                    'version=' + image_dep['currentValue'], 'version=v' + version)
                fixture['chart'] = fixture['chart'].replace(
                    'tag: ' + chart_dep['currentValue'], 'tag: ' + version)
                result = json.loads(subprocess.check_output(
                    ['node', '--input-type=module', '-e', self.script, str(renovate_module())],
                    input=json.dumps(fixture), text=True, timeout=30))
                self.assertEqual(result['image'][0]['currentValue'], 'v' + version)
                self.assertEqual(result['chart'][0]['currentValue'], version)
                self.assertEqual(result['eligible'], self.result['eligible'])

    def test_version_digest_replacement_keeps_chart_contract(self):
        self.assertEqual(self.result['next']['currentValue'], 'v0.1.0-alpha.4')
        image = yaml.safe_load(self.result['replaced'])['spec']['values']['image']
        self.assertNotIn('tag', image)
        self.assertEqual(image['digest'], 'sha256:' + 'a' * 64)

    def test_retry_without_rollback(self):
        spec = yaml.safe_load((ROOT / APP / 'helmrelease.yaml').read_text())['spec']
        for action in ('install', 'upgrade'):
            self.assertEqual(spec[action]['strategy'], {'name': 'RetryOnFailure', 'retryInterval': '2m'})
            self.assertNotIn('remediation', spec[action])

    def test_cluster_defaults_preserve_nyctr_retry_strategy(self):
        cluster = yaml.safe_load((ROOT / 'kubernetes/flux/cluster/ks.yaml').read_text())
        child = yaml.safe_load(cluster['spec']['patches'][0]['patch'])
        defaults = child['spec']['patches']
        nyctr = yaml.safe_load((ROOT / APP / 'helmrelease.yaml').read_text())
        other = {'apiVersion': nyctr['apiVersion'], 'kind': 'HelmRelease',
                 'metadata': {'name': 'other'}, 'spec': {'interval': '1h'}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'resources.yaml').write_text(yaml.safe_dump_all([nyctr, other]))
            (root / 'kustomization.yaml').write_text(yaml.safe_dump({
                'apiVersion': 'kustomize.config.k8s.io/v1beta1',
                'kind': 'Kustomization', 'resources': ['resources.yaml'],
                'patches': defaults}))
            built = subprocess.check_output(['kubectl', 'kustomize', directory], text=True, timeout=20)
        objects = {o['metadata']['name']: o for o in yaml.safe_load_all(built)}
        self.assertEqual(objects['nyctr']['spec']['upgrade']['strategy']['name'], 'RetryOnFailure')
        self.assertNotIn('remediation', objects['nyctr']['spec']['upgrade'])
        self.assertEqual(objects['other']['spec']['upgrade']['strategy']['name'], 'RemediateOnFailure')
        self.assertEqual(objects['other']['spec']['upgrade']['remediation']['retries'], 2)

    def test_config_validator(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'renovate.json'
            path.write_text(json.dumps(self.config['validationConfig']))
            run = subprocess.run(['node', str(renovate_module() / 'dist/config-validator.js'),
                                  '--strict', '--no-global', str(path)],
                                 text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
