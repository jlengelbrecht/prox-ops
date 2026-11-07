"""One named in-repo app counts like a person: out of scope, with an advisory review. Nothing wider."""

import copy
from pathlib import Path

import pytest
import yaml

import fold
import review
import steward
from classify import NEEDS, identity, load_policy, out_of_scope
from test_classify import RENOVATE_COMMIT, make_pr, set_author
from test_review import advisory, artifact, LOW
from test_review_workflow import GITHUB
from test_steward import POLICY, SHA, FakeGitHub, fresh_head, renovate_pr, routes  # noqa: F401

APP = "homelab0-intern[bot]"
POLICY_FILE = Path(__file__).resolve().parents[1] / "policy.yaml"


def app_pr(login=APP, kind="Bot"):
    return set_author(make_pr(""), login, kind)


def test_the_policy_names_exactly_one_app_and_leaves_authors_alone():
    assert POLICY["trusted_apps"] == [APP] and POLICY["authors"] == ["renovate[bot]"]


def test_the_named_app_in_this_repository_is_out_of_scope():
    assert out_of_scope(app_pr(), POLICY) is True


@pytest.mark.parametrize("login", [
    "Homelab0-Intern[bot]", "HOMELAB0-INTERN[BOT]", "homelab0-intern[Bot]", "homelab0-intern[bot] ", " homelab0-intern[bot]",
    "homelab0-intern-2[bot]", "homelab0-intern", "xhomelab0-intern[bot]", "homelab0-inte[bot]", "homelab0[bot]",
    "homelab0-intern[bot]​", "homelab0‑intern[bot]",
])
def test_a_lookalike_of_the_app_is_needs_human(login):
    pr = app_pr(login)
    assert out_of_scope(pr, POLICY) is False
    assert identity(pr, RENOVATE_COMMIT, POLICY).verdict == NEEDS


@pytest.mark.parametrize("login", ["dependabot[bot]", "homebot-0[bot]", "github-actions[bot]",
                                   "renovate-bot[bot]", "copilot[bot]"])
def test_every_other_bot_is_still_needs_human(login):
    pr = app_pr(login)
    assert out_of_scope(pr, POLICY) is False
    assert identity(pr, RENOVATE_COMMIT, POLICY).verdict == NEEDS


@pytest.mark.parametrize("kind", ["Mannequin", "Organization", "Enterprise", "", None, "bot", "BOT"])
def test_the_app_login_with_a_type_other_than_bot_or_user_is_needs_human(kind):
    assert out_of_scope(app_pr(kind=kind), POLICY) is False


def test_a_fork_pr_from_the_app_login_is_needs_human():
    for head in ({"full_name": "fork/r"}, {"full_name": "fork/r", "fork": True}, None, {}):
        pr = app_pr()
        pr["head"]["repo"] = head
        assert out_of_scope(pr, POLICY) is False
        assert identity(pr, RENOVATE_COMMIT, POLICY).verdict == NEEDS


def test_a_head_that_only_looks_like_this_repository_is_needs_human():
    for head in ("O/R", "o/r ", "o/r2", "o/"):
        pr = app_pr()
        pr["head"]["repo"] = {"full_name": head}
        assert out_of_scope(pr, POLICY) is False


@pytest.mark.parametrize("change", [{"user": None}, {"user": {}}, {"user": {"type": "Bot"}}, {"user": {"login": APP}},
                                    {"user": {"login": None, "type": "Bot"}}, {"head": {"sha": SHA}},
                                    {"base": {"ref": "main"}}, {"head": None}, {"base": None}])
def test_an_unreadable_field_is_needs_human(change):
    pr = dict(app_pr(), **change)
    assert out_of_scope(pr, POLICY) is False


def test_renovate_is_judged_exactly_as_before():
    renovate = make_pr("")
    assert out_of_scope(renovate, POLICY) is False
    assert identity(renovate, RENOVATE_COMMIT, POLICY).verdict == "pass"
    for login, kind in (("renovate[bot]", "User"), ("Renovate[bot]", "Bot"), ("renovate", "User")):
        assert out_of_scope(set_author(make_pr(""), login, kind), POLICY) is (kind == "User" and login == "renovate")


def test_a_person_is_still_out_of_scope_and_a_renovate_lookalike_person_is_not():
    assert out_of_scope(set_author(make_pr(""), "someone", "User"), POLICY) is True
    assert out_of_scope(set_author(make_pr(""), "Renovate[bot]", "User"), POLICY) is False


# The loader refuses anything wider.

def policy_with(**changes):
    raw = yaml.safe_load(POLICY_FILE.read_text())
    raw.update(changes)
    return raw


def load(tmp_path, raw):
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_policy(path)


def test_the_loader_accepts_the_shipped_policy_and_an_empty_list(tmp_path):
    assert load(tmp_path, policy_with())["trusted_apps"] == [APP]
    assert load(tmp_path, policy_with(trusted_apps=[]))["trusted_apps"] == []


@pytest.mark.parametrize("value", [
    None, "homelab0-intern[bot]", {"a": "b"}, [1], [None], ["*"], ["*[bot]"], ["homelab0-*[bot]"], ["/homelab0/"], ["homelab0-intern"],
    ["homelab0 intern[bot]"], ["[bot]"], ["homelab0-intern[bot]", "homelab0-intern[bot]"],
    ["homelab0-intern[bot]", "Homelab0-Intern[bot]"], ["renovate[bot]"], ["Renovate[bot]"], [""], ["a[bot]\n"],
])
def test_the_loader_refuses_a_missing_wide_or_overlapping_entry(tmp_path, value):
    raw = policy_with()
    if value is None:
        raw.pop("trusted_apps")
    else:
        raw["trusted_apps"] = value
    with pytest.raises(ValueError):
        load(tmp_path, raw)


# What the rest of the steward does with such a PR.

def test_the_named_app_gets_success_and_a_stale_needs_human_label_and_comment_are_cleared():
    pr = renovate_pr(login=APP, labels=["steward/needs-human"])
    sticky = {"id": 77, "body": fold.MARKER + "\nold verdict", "performed_via_github_app": {"id": POLICY["steward_app_id"]}}
    gh, app = FakeGitHub(routes(pr, comments=[sticky])), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == "pass"
    check = next(w[2] for w in app.writes if w[1] == "check-runs")
    assert check["conclusion"] == "success" and "out of scope" in check["output"]["title"]
    assert ("DELETE", "issues/7/labels/steward%2Fneeds-human", None) in gh.writes
    assert ("DELETE", "issues/comments/77", None) in app.writes


@pytest.mark.parametrize("login", ["Homelab0-Intern[bot]", "dependabot[bot]"])
def test_a_lookalike_or_other_bot_still_fails_the_check(login):
    pr = renovate_pr(login=login)
    gh, app = FakeGitHub(routes(pr)), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == NEEDS
    check = next(w[2] for w in app.writes if w[1] == "check-runs")
    assert check["conclusion"] == "failure" and "not the Renovate app" in check["output"]["title"]


def test_a_fork_pr_from_the_app_login_still_fails_the_check():
    pr = renovate_pr(login=APP)
    pr["head"]["repo"] = {"full_name": "fork/r", "fork": True}
    gh, app = FakeGitHub(routes(pr)), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == NEEDS
    assert next(w[2] for w in app.writes if w[1] == "check-runs")["conclusion"] == "failure"


def test_the_app_never_gets_the_renovate_treatment():
    pr = renovate_pr(login=APP)
    gh, app = FakeGitHub(routes(pr)), FakeGitHub({})
    steward.evaluate(gh, app, pr, POLICY, 0, False)
    assert not any("render" in str(w) or "commits/" in w[1] for w in gh.writes + app.writes if w[1] != "check-runs")
    assert review.mode(pr, POLICY) == review.ADVISORY and review.mode(renovate_pr(), POLICY) == review.VERDICT


def test_the_named_app_gets_an_advisory_review_and_a_fork_of_it_does_not(tmp_path):
    result = dict(LOW, findings=[{"file": "kubernetes/apps/observability/uptime-kuma/app/helmrelease.yaml", "text": "Note: x."}])
    gh, app = advisory(renovate_pr(login=APP), tmp_path, result)
    assert any(w[1] == "issues/7/comments" and fold.ADVISORY_MARKER in w[2]["body"] for w in app.writes)
    fork = renovate_pr(login=APP)
    fork["head"]["repo"] = {"full_name": "fork/r", "fork": True}
    assert review.mode(fork, POLICY) == review.OFF


def test_the_advisory_jobs_do_not_depend_on_the_author_being_a_person():
    workflow = yaml.safe_load((GITHUB / "workflows" / "merge-steward.yaml").read_text())["jobs"]
    for name in [n for n in ("advisory-review", "advisory-render") if n in workflow]:
        condition = " ".join(workflow[name]["if"].split())
        assert "user.type" not in condition and "user.login != 'renovate[bot]'" in condition
        assert "head.repo.fork == false" in condition and "head.repo.full_name == github.repository" in condition
    # The verdict jobs stay Renovate only.
    for name in ("render", "review"):
        assert "user.login == 'renovate[bot]'" in " ".join(workflow[name]["if"].split())
