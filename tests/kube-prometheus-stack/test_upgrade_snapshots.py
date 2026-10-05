"""Fail a kube-prometheus-stack change that weakens its protected upgrade.

Flux holds the release until the snapshot Kustomization has applied the same
revision, but snapshots that already exist are ready at once. Only names that
carry the target chart version force fresh snapshots for each upgrade. The
release also opts out of the cluster-wide rollback defaults, because a
rollback would start the old Grafana on a migrated database.
"""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
OBS = ROOT / 'kubernetes/apps/observability'
SOURCES = {
    'grafana': 'kube-prometheus-stack-grafana',
    'prometheus-db': 'prometheus-kube-prometheus-stack-prometheus-db-prometheus-kube-prometheus-stack-prometheus-0',
    'alertmanager-db': 'alertmanager-kube-prometheus-stack-alertmanager-db-alertmanager-kube-prometheus-stack-alertmanager-0',
}
VOLUMES = set(SOURCES)
OPT_OUT = 'homelab0.org/helm-defaults'
FLUX_DEFAULTS = ('kubernetes/flux/cluster/ks.yaml', 'templates/config/kubernetes/flux/cluster/ks.yaml.j2')
SNAPSHOT_CLASSES = 'kubernetes/apps/kube-system/snapshot-controller/class/volumesnapshotclass.yaml'
WATCHED = ('kubernetes/apps/observability/kube-prometheus-stack/app/helmrelease.yaml',
           'kubernetes/apps/observability/kube-prometheus-stack/ks.yaml',
           'kubernetes/apps/observability/kube-prometheus-stack-upgrade-snapshots/**',
           SNAPSHOT_CLASSES, *FLUX_DEFAULTS,
           'tests/kube-prometheus-stack/test_upgrade_snapshots.py', '.github/workflows/hindsight-metrics.yaml')


def snapshot_problems(version, snapshots):
    prefix = 'kps-pre-' + version.replace('.', '-') + '-'
    problems, dates, volumes = [], set(), []
    for snap in snapshots:
        name = snap['metadata']['name']
        match = re.fullmatch(re.escape(prefix) + r'(\d{8})-(.+)', name)
        if not match:
            problems.append(f'{name} does not carry chart {version}; add snapshots named {prefix}<yyyymmdd>-<volume>')
            continue
        dates.add(match[1])
        volumes.append(match[2])
        if snap['metadata'].get('labels', {}).get('app.kubernetes.io/part-of') != prefix + match[1]:
            problems.append(f'{name} part-of label does not match its name')
    if not problems and sorted(volumes) != sorted(VOLUMES):
        problems.append(f'expected one snapshot per volume {sorted(VOLUMES)}, found {sorted(volumes)}')
    if len(dates) > 1:
        problems.append(f'snapshots carry different dates {sorted(dates)}')
    return problems


def helmrelease_targets(path):
    """Targets of the HelmRelease defaults that cluster-apps nests inside each child Kustomization."""
    targets = []
    for outer in yaml.safe_load((ROOT / path).read_text())['spec']['patches']:
        for inner in yaml.safe_load(outer['patch'])['spec'].get('patches', []):
            if yaml.safe_load(inner['patch'])['kind'] == 'HelmRelease':
                targets.append(inner.get('target'))
    return targets


class UpgradeSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.release = yaml.safe_load((OBS / 'kube-prometheus-stack/app/helmrelease.yaml').read_text())
        self.version = self.release['spec']['chart']['spec']['version']
        docs = yaml.safe_load_all((OBS / 'kube-prometheus-stack-upgrade-snapshots/app/volumesnapshots.yaml').read_text())
        self.snapshots = [d for d in docs if d and d.get('kind') == 'VolumeSnapshot']

    def test_release_waits_for_snapshots(self):
        ks = yaml.safe_load((OBS / 'kube-prometheus-stack/ks.yaml').read_text())
        self.assertIn({'name': 'kube-prometheus-stack-upgrade-snapshots', 'namespace': 'flux-system'},
                      ks['spec']['dependsOn'])

    def test_workflow_runs_on_every_input(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/hindsight-metrics.yaml').read_text())
        # PyYAML reads the bare 'on' key as a boolean
        paths = set(workflow[True]['pull_request']['paths'])
        for path in WATCHED:
            self.assertIn(path, paths)
        runs = [step.get('run', '') for step in workflow['jobs']['metrics']['steps']]
        self.assertIn('python -m unittest discover -s tests/kube-prometheus-stack -p test_upgrade_snapshots.py -v', runs)

    def test_snapshot_names_carry_chart_version(self):
        self.assertEqual(len(self.snapshots), len(VOLUMES))
        self.assertEqual(snapshot_problems(self.version, self.snapshots), [])

    def test_chart_bump_without_new_snapshots_fails(self):
        major, rest = self.version.split('.', 1)
        bumped = f'{int(major) + 1}.{rest}'
        problems = snapshot_problems(bumped, self.snapshots)
        self.assertEqual(len(problems), len(VOLUMES))
        self.assertTrue(all(f'does not carry chart {bumped}' in p for p in problems))

    def test_mismatched_label_and_mixed_dates_fail(self):
        relabelled = [dict(s, metadata=dict(s['metadata'], labels={'app.kubernetes.io/part-of': 'other'}))
                      for s in self.snapshots]
        self.assertTrue(snapshot_problems(self.version, relabelled))
        first = self.snapshots[0]['metadata']['name']
        moved = dict(self.snapshots[0], metadata=dict(self.snapshots[0]['metadata'],
                                                      name=re.sub(r'-\d{8}-', '-19990101-', first)))
        self.assertIn('snapshots carry different dates', ' '.join(snapshot_problems(
            self.version, [moved] + self.snapshots[1:])))

    def test_release_stays_forward_only(self):
        # Both the committed file and the template it is rendered from must skip opted-out releases
        expected = [{'group': 'helm.toolkit.fluxcd.io', 'kind': 'HelmRelease',
                     'annotationSelector': f'{OPT_OUT}!=disabled'}]
        for path in FLUX_DEFAULTS:
            self.assertEqual(helmrelease_targets(path), expected, path)
        spec = self.release['spec']
        self.assertEqual(self.release['metadata']['annotations'][OPT_OUT], 'disabled')
        self.assertEqual(spec['upgrade']['remediation'], {'retries': 0, 'remediateLastFailure': False})
        self.assertEqual((spec['install']['crds'], spec['upgrade']['crds']), ('Skip', 'Skip'))

    def test_snapshots_are_retained_and_gate_on_readiness(self):
        classes = {d['metadata']['name']: d for d in yaml.safe_load_all((ROOT / SNAPSHOT_CLASSES).read_text()) if d}
        for snap in self.snapshots:
            name = snap['metadata']['name']
            self.assertEqual(classes[snap['spec']['volumeSnapshotClassName']]['deletionPolicy'], 'Retain', name)
            self.assertEqual(snap['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'], 'disabled', name)
            self.assertEqual(snap['metadata']['namespace'], 'observability', name)
            volume = next(v for v in SOURCES if name.endswith('-' + v))
            self.assertEqual(snap['spec']['source'], {'persistentVolumeClaimName': SOURCES[volume]}, name)
        ks = yaml.safe_load((OBS / 'kube-prometheus-stack-upgrade-snapshots/ks.yaml').read_text())['spec']
        self.assertEqual((ks['wait'], ks['prune']), (True, False))
        self.assertEqual(ks['healthCheckExprs'], [{
            'apiVersion': 'snapshot.storage.k8s.io/v1', 'kind': 'VolumeSnapshot',
            'current': 'has(status.readyToUse) && status.readyToUse == true', 'failed': 'has(status.error)'}])
        # Flux compares the dependency's applied revision only when both share a source
        release_ks = yaml.safe_load((OBS / 'kube-prometheus-stack/ks.yaml').read_text())['spec']
        self.assertEqual(ks['sourceRef'], release_ks['sourceRef'])


if __name__ == '__main__':
    unittest.main()
