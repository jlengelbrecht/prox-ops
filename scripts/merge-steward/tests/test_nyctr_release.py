"""Nyctr paired-release exceptions do not widen other package or render rules."""
import copy
from pathlib import Path

import pytest

from classify import NEEDS, PASS, load_policy, policy_stage
from render import object_flags
from test_classify import make_pr, table

POLICY = load_policy(Path(__file__).resolve().parents[1] / 'policy.yaml')
RULE = next(r for r in POLICY['release_exceptions'] if r['oci_repository']['name'] == 'nyctr')
MEMBERS = [RULE, *RULE['companions']]
FILES = [m['file'] for m in MEMBERS]


def pr(kind='patch', foreign=False):
    rows = [f"| {m['package']} | {kind} | `0.1.0-alpha.3` → `0.2.0-alpha.1` |" for m in MEMBERS]
    if foreign:
        rows.append('| other/image | minor | `0.1.0` → `0.2.0` |')
    return make_pr(table(*rows), labels=('breaking', 'update/' + kind))


@pytest.mark.parametrize('kind', ['patch', 'minor', 'major', 'digest', 'pinDigest'])
def test_nyctr_paired_release_can_pass(kind):
    assert policy_stage(pr(kind), FILES, [], POLICY).verdict == PASS


@pytest.mark.parametrize('kind', ['rollback', 'replacement', 'bump'])
def test_nyctr_exception_never_allows_replacement_or_rollback(kind):
    assert policy_stage(pr(kind), FILES, [], POLICY).verdict == NEEDS


def test_nyctr_exception_refuses_foreign_package_file_and_partial_release():
    assert policy_stage(pr(foreign=True), FILES, [], POLICY).verdict == NEEDS
    assert policy_stage(pr(), FILES + ['kubernetes/apps/other/file.yaml'], [], POLICY).verdict == NEEDS
    assert policy_stage(pr(), FILES[:1], [], POLICY).verdict == NEEDS


def test_only_the_exact_nyctr_oci_object_gets_render_exception():
    before = {'kind': 'OCIRepository', 'metadata': {'namespace': 'security', 'name': 'nyctr'},
              'spec': {'url': RULE['oci_repository']['url'], 'ref': {'tag': '0.1.0-alpha.3'}}}
    after = copy.deepcopy(before)
    after['spec']['ref'] = {'tag': '1.0.0-rc.1', 'digest': 'sha256:' + 'a' * 64}
    assert object_flags('OCIRepository', before, after, exceptions=POLICY['release_exceptions']) == []
    after['spec']['url'] = 'oci://foreign/image'
    assert object_flags('OCIRepository', before, after, exceptions=POLICY['release_exceptions'])


@pytest.mark.parametrize('old,new', [('1.0.0-rc.9', '1.0.0-alpha.1'),
                                     ('1.0.0-rc.10', '1.0.0-rc.2'),
                                     ('1.0.0', '1.0.0-rc.1')])
def test_render_refuses_prerelease_downgrades(old, new):
    before = {'kind': 'OCIRepository', 'metadata': {'namespace': 'security', 'name': 'nyctr'},
              'spec': {'url': RULE['oci_repository']['url'], 'ref': {'tag': old}}}
    after = copy.deepcopy(before)
    after['spec']['ref']['tag'] = new
    assert object_flags('OCIRepository', before, after, exceptions=POLICY['release_exceptions'])
