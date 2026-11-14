// Run with HINDSIGHT_RENOVATE_ROOT pointing to an already installed Renovate package.
// Uses Renovate's actual Flux extraction and package-rule evaluator; no registry calls.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';

const renovate = process.env.HINDSIGHT_RENOVATE_ROOT;
assert.ok(renovate && fs.existsSync(path.join(renovate, 'package.json')),
  'Set HINDSIGHT_RENOVATE_ROOT to an installed Renovate package directory');
const load = async (module) => import(path.join(renovate, 'dist', module));
const requireFromRenovate = createRequire(path.join(renovate, 'package.json'));
const json5 = requireFromRenovate('json5');
const { GlobalConfig } = await load('config/global.js');
const { extractAllPackageFiles } = await load('modules/manager/flux/extract.js');
const { applyPackageRules } = await load('util/package-rules/index.js');
GlobalConfig.set({ localDir: process.cwd() });

const app = 'kubernetes/apps/ai/hermes-hindsight/app/';
const releaseFile = `${app}hindsight.yaml`;
const extracted = await extractAllPackageFiles({ registryAliases: {} },
  [`${app}sources.yaml`, releaseFile]);
const deps = extracted?.find((entry) => entry.packageFile === releaseFile)?.deps ?? [];
const packages = [
  'ghcr.io/vectorize-io/charts/hindsight',
  'ghcr.io/vectorize-io/hindsight-api',
  'ghcr.io/vectorize-io/hindsight-control-plane',
];
const hindsight = deps.filter((dep) => packages.includes(dep.packageName));
assert.deepEqual(hindsight.map((dep) => dep.packageName),
  [packages[0], packages[1], packages[1], packages[2]]);
assert.ok(hindsight.every((dep) => dep.datasource === 'docker' && dep.currentValue === '1.2.1'));
assert.equal(hindsight[0].currentDigest, undefined, 'the chart is version-tracked, without a digest pin');
assert.match(hindsight[1].currentDigest, /^sha256:[a-f0-9]{64}$/);
assert.equal(hindsight[1].currentDigest, hindsight[2].currentDigest);
assert.match(hindsight[3].currentDigest, /^sha256:[a-f0-9]{64}$/);

const readRules = (file) => json5.parse(fs.readFileSync(file, 'utf8')).packageRules;
const packageRules = [
  ...readRules('.github/renovate/autoMerge.json5'),
  ...readRules('.github/renovate.json5'),
];
for (const updateType of ['patch', 'minor', 'major']) {
  for (const dep of hindsight) {
    const effective = await applyPackageRules({
      ...dep, packageRules, manager: 'flux', packageFile: releaseFile,
      updateType, currentVersion: dep.currentValue,
    }, 'hindsight-regression');
    const label = `${dep.packageName} ${updateType}`;
    assert.equal(effective.automerge, false, label);
    assert.equal(effective.groupName, 'Hindsight release', label);
    if (updateType === 'major') {
      assert.equal(effective.dependencyDashboardApproval, true, label);
      assert.equal(effective.prConcurrentLimit, 10, label);
      assert.equal(effective.branchConcurrentLimit, 15, label);
    }
  }
}
const unrelated = await applyPackageRules({
  datasource: 'docker', packageName: 'ghcr.io/example/unrelated',
  packageRules, manager: 'flux', packageFile: releaseFile,
  updateType: 'major', currentValue: '1.2.1', currentVersion: '1.2.1',
}, 'hindsight-regression');
assert.notEqual(unrelated.groupName, 'Hindsight release', 'unrelated Docker package');
console.log('PASS: Flux extracted chart, API anchor twice and control plane; patch/minor/major rules are grouped and manual, majors need approval; unrelated package is excluded');
