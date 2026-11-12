"""Positive and negative controls for the required Nyctr artifact gate."""
import copy
import hashlib
import contextlib
import io
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('check_nyctr_release', ROOT / 'scripts/check_nyctr_release.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class ReleaseMatchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = self.root / guard.APP
        self.app.mkdir(parents=True)
        self.hr = (ROOT / guard.APP / 'helmrelease.yaml').read_text()
        self.chart = yaml.safe_load((ROOT / guard.APP / 'ocirepository.yaml').read_text())
        self.image_version = guard.ANNOTATION.findall(self.hr)[0]
        self.save()

    def save(self, hr=None, chart=None):
        (self.app / 'helmrelease.yaml').write_text(hr or self.hr)
        (self.app / 'ocirepository.yaml').write_text(yaml.safe_dump(chart or self.chart))

    def test_current_release_passes(self):
        self.assertEqual(guard.check(self.root)[0], self.image_version)

    def test_matching_alpha_rc_stable_and_major_pass(self):
        for version in ('0.1.0-alpha.4', '0.2.0-rc.1', '1.0.0', '2.0.0'):
            with self.subTest(version=version):
                chart = copy.deepcopy(self.chart)
                chart['spec']['ref']['tag'] = version
                hr = self.hr.replace('version=' + self.image_version, 'version=v' + version)
                self.save(hr, chart)
                self.assertEqual(guard.check(self.root)[0], 'v' + version)

    def test_either_side_moving_alone_fails(self):
        chart = copy.deepcopy(self.chart)
        chart['spec']['ref']['tag'] = '99.0.0-rc.1'
        for hr, c in [(self.hr, chart), (self.hr.replace('version=' + self.image_version,
                                                     'version=v99.0.0-rc.1'), self.chart)]:
            with self.subTest(chart=c['spec']['ref']['tag']):
                self.save(hr, c)
                with self.assertRaises(guard.ReleaseError):
                    guard.check(self.root)

    def test_missing_duplicate_version_and_dropped_digest_fail(self):
        for hr in (self.hr.replace('version=' + self.image_version, 'version=latest'),
                   self.hr + '\n# renovate: datasource=docker depName=ghcr.io/homelab-0/nyctr version=v1.0.0\n',
                   self.hr.replace('digest: sha256:', 'digest: invalid:')):
            self.save(hr)
            with self.assertRaises(guard.ReleaseError):
                guard.check(self.root)

    def test_registry_digest_matches_and_mismatch_fails(self):
        manifest = b'{"schemaVersion":2}'
        digest = 'sha256:' + hashlib.sha256(manifest).hexdigest()
        for expected, passes in [(digest, True), ('sha256:' + '0' * 64, False)]:
            with patch.object(guard, 'fetch', side_effect=[json.dumps({'token': 'fixture'}).encode(), manifest]):
                if passes:
                    guard.verify_digest('v0.1.0-alpha.4', expected)
                else:
                    with self.assertRaises(guard.ReleaseError):
                        guard.verify_digest('v0.1.0-alpha.4', expected)

    def test_registry_failure_fails_closed_without_response_details(self):
        for error in (urllib.error.URLError('private fixture response'),
                      urllib.error.HTTPError('https://fixture.invalid', 404, 'private fixture response', {}, None)):
            with patch.object(guard, 'fetch', side_effect=error):
                with self.assertRaises(guard.ReleaseError) as caught:
                    guard.verify_digest('v0.1.0-alpha.4', 'sha256:' + '0' * 64)
                self.assertNotIn('private fixture response', str(caught.exception))

    def test_chart_override_selectors_and_invalid_pin_fail(self):
        for changes in ({'semver': '>=99.0.0'}, {'semverFilter': 'rc'}, {'digest': 'invalid'}):
            chart = copy.deepcopy(self.chart)
            chart['spec']['ref'].update(changes)
            self.save(chart=chart)
            with self.assertRaises(guard.ReleaseError):
                guard.check(self.root)

    def test_annotation_must_stay_adjacent_for_renovate_extraction(self):
        self.save(self.hr.replace('version=' + self.image_version + '\n',
                                  'version=' + self.image_version + '\n      # detached\n'))
        with self.assertRaises(guard.ReleaseError):
            guard.check(self.root)

    def test_ci_checks_effective_chart_pin_and_published_chart(self):
        image_body, chart_body = b'image manifest fixture', b'chart manifest fixture'
        image_digest = 'sha256:' + hashlib.sha256(image_body).hexdigest()
        chart_digest = 'sha256:' + hashlib.sha256(chart_body).hexdigest()
        original_digest = yaml.safe_load(self.hr)['spec']['values']['image']['digest']
        hr = self.hr.replace(original_digest, image_digest)
        old_chart_digest = 'sha256:' + hashlib.sha256(b'older chart fixture').hexdigest()
        cases = [(None, chart_body, 0), (chart_digest, chart_body, 0),
                 (old_chart_digest, chart_body, 1),
                 (chart_digest, urllib.error.HTTPError('https://fixture.invalid', 404, 'private', {}, None), 1)]
        for pin, response, expected in cases:
            with self.subTest(pin=pin, expected=expected):
                chart = copy.deepcopy(self.chart)
                if pin is not None:
                    chart['spec']['ref']['digest'] = pin
                self.save(hr, chart)
                responses = [b'{"token":"fixture"}', image_body, b'{"token":"fixture"}', response]
                output = io.StringIO()
                with patch.object(guard, 'fetch', side_effect=responses), \
                        patch.object(guard.sys, 'argv', ['guard', '--root', str(self.root), '--verify-digest']), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(guard.main(), expected)
                self.assertNotIn('private', output.getvalue())

    def test_workflow_required_aggregate_cannot_skip_guard(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/flux-local.yaml').read_text())
        jobs = workflow['jobs']
        gate = jobs['nyctr-release-match']
        self.assertNotIn('if', gate)
        self.assertIn('nyctr-release-match', jobs['flux-local-status']['needs'])
        condition = jobs['flux-local-status']['steps'][0]['if']
        self.assertIn("needs.nyctr-release-match.result != 'success'", condition)
        self.assertIn('python scripts/check_nyctr_release.py --verify-digest',
                      [step.get('run') for step in gate['steps']])
        test_condition = jobs['flux-local-status']['steps'][1]['if']
        self.assertIn("needs.pre-job.outputs.any_changed == 'true'", test_condition)
        self.assertIn("needs.test.result != 'success'", test_condition)
        policy = yaml.safe_load((ROOT / 'scripts/merge-steward/policy.yaml').read_text())
        self.assertIn({'name': 'Nyctr Release Match', 'app_id': 15368}, policy['required_checks'])


if __name__ == '__main__':
    unittest.main()
