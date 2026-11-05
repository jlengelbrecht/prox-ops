"""A burst of runs must neither pass a PR by mistake nor leave its check missing."""

import json

import pytest

import steward
from classify import HOLD, NEEDS, PASS
from ghapi import GitHubError
from test_steward import FILES, POLICY, REQUIRED, SHA, FakeGitHub, fresh_head, recorded, renovate_pr, routes  # noqa: F401

LIMITED = GitHubError(403, "pulls/7/commits", rate_limited=True)
DENIED = GitHubError(403, "pulls/7/commits")


def evaluate(pr, **kw):
    gh, app = FakeGitHub(routes(pr, **kw)), FakeGitHub({})
    verdict = steward.evaluate(gh, app, pr, POLICY, 0, False)
    check = next((w[2] for w in app.writes if w[1] == "check-runs"), None)
    return verdict, check, gh, app


def test_a_rate_limited_read_leaves_a_pending_check_not_a_failure_or_a_pass():
    pr = renovate_pr()
    gh, app = FakeGitHub(dict(routes(pr), **{"pulls/7/commits": LIMITED})), FakeGitHub({})
    verdict = steward.evaluate(gh, app, pr, POLICY, 0, False)
    check = next(w[2] for w in app.writes if w[1] == "check-runs")
    assert verdict == HOLD
    assert check["status"] == "in_progress" and "conclusion" not in check
    assert check["output"]["title"] == steward.RATE_LIMITED
    assert ("POST", "issues/7/labels", {"labels": ["steward/hold"]}) in gh.writes


def test_a_permission_failure_still_fails_closed_as_before():
    pr = renovate_pr()
    gh, app = FakeGitHub(dict(routes(pr), **{"pulls/7/commits": DENIED})), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == NEEDS
    check = next(w[2] for w in app.writes if w[1] == "check-runs")
    assert check["conclusion"] == "failure" and "steward error" in check["output"]["title"]


@pytest.mark.parametrize("error", [LIMITED, DENIED, GitHubError(500, "x"), GitHubError(404, "x"), RuntimeError("boom"),
                                   KeyError("k"), TimeoutError()])
def test_no_error_can_produce_a_pass(error):
    pr = renovate_pr()
    for route in ("pulls/7/commits", "pulls/7/files", f"commits/{SHA}/check-runs", f"commits/{SHA}/status"):
        gh, app = FakeGitHub(dict(routes(pr), **{route: error})), FakeGitHub({})
        verdict = steward.evaluate(gh, app, pr, POLICY, 0, False)
        checks = [w[2] for w in app.writes if w[1] == "check-runs"]
        assert verdict != PASS
        assert all(c.get("conclusion") != "success" for c in checks), route


def test_the_pending_check_is_replaced_by_the_next_evaluation():
    pr = renovate_pr()
    first = FakeGitHub(dict(routes(pr), **{"pulls/7/commits": LIMITED}))
    steward.evaluate(first, FakeGitHub({}), pr, POLICY, 0, False)
    verdict, check, _, _ = evaluate(pr)
    assert verdict == PASS and check["conclusion"] == "success"


# The first read can fail before there is a PR to publish anything for.

def two_clients(monkeypatch, workflow, app):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "workflow-token")
    monkeypatch.setenv("STEWARD_APP_TOKEN", "app-token")
    monkeypatch.setattr(steward, "GitHub", lambda token, repo, **kw: {"workflow-token": workflow, "app-token": app}[token])


def test_a_failed_first_read_exits_with_a_message_and_writes_nothing(monkeypatch, capsys):
    workflow = FakeGitHub({"pulls/7": GitHubError(403, "pulls/7", rate_limited=True)})
    app = FakeGitHub({"pulls/7": GitHubError(403, "pulls/7", rate_limited=True)})
    two_clients(monkeypatch, workflow, app)
    assert steward.main(["--pr", "7"]) == 1
    err = capsys.readouterr().err
    assert "later event or sweep evaluates them" in err and "Traceback" not in err
    assert workflow.writes == [] and app.writes == []


def test_reads_use_the_app_token_and_labels_stay_on_the_workflow_token(monkeypatch):
    pr = renovate_pr()
    workflow, app = FakeGitHub(routes(pr)), FakeGitHub(routes(pr))
    two_clients(monkeypatch, workflow, app)
    assert steward.main(["--pr", "7"]) == 0
    assert "pulls/7" in app.reads and "pulls/7/files" in app.reads and "pulls/7/commits" in app.reads
    assert any(r.startswith(f"commits/{SHA}/check-runs") for r in app.reads)
    # Not the apps's permissions: statuses, file contents and the branch activity log.
    assert not any(r.startswith(("commits/%s/status" % SHA, "contents/", "activity")) for r in app.reads)
    assert any(r.startswith(f"commits/{SHA}/status") for r in workflow.reads)
    assert not any(r.startswith(("pulls", f"commits/{SHA}/check-runs")) for r in workflow.reads)
    assert workflow.writes and all("labels" in w[1] for w in workflow.writes)
    assert any(w[1] == "check-runs" for w in app.writes) and not any("labels" in w[1] for w in app.writes)


def test_an_app_token_that_cannot_read_pull_requests_falls_back_to_the_workflow_token(monkeypatch, capsys):
    pr = renovate_pr()
    denied = {k: GitHubError(403, k) for k in ("pulls/7", "pulls/7/files", "pulls/7/commits")}
    workflow, app = FakeGitHub(routes(pr)), FakeGitHub(dict(routes(pr), **denied))
    two_clients(monkeypatch, workflow, app)
    assert steward.main(["--pr", "7"]) == 0
    assert any(w[1] == "check-runs" and w[2]["conclusion"] == "success" for w in app.writes)
    assert "pulls/7" in workflow.reads and "app token refused for pulls reads" in capsys.readouterr().err


def test_a_dry_run_needs_no_app_token_and_reads_with_the_workflow_token(monkeypatch):
    pr = renovate_pr()
    workflow = FakeGitHub(routes(pr))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "workflow-token")
    monkeypatch.delenv("STEWARD_APP_TOKEN", raising=False)
    monkeypatch.setattr(steward, "GitHub", lambda token, repo, **kw: workflow)
    assert steward.main(["--pr", "7", "--dry-run"]) == 0
    assert "pulls/7" in workflow.reads and workflow.writes == []


# Sweeps pick up what a failed run left behind.

def listed(number, login, sha=SHA, kind="User"):
    return {"number": number, "user": {"login": login, "type": kind}, "head": {"sha": sha}}


def sweep_client(open_prs, runs_by_sha):
    table = {"pulls": open_prs}
    for sha, runs in runs_by_sha.items():
        table[f"commits/{sha}/check-runs"] = {"check_runs": runs}
    return FakeGitHub(table)


MINE = recorded()
OTHER = "d" * 40


def numbers(found):
    return [p["number"] for p in found]


def test_a_full_sweep_evaluates_renovate_prs_and_any_pr_without_a_check():
    gh = sweep_client([listed(7, "renovate[bot]"), listed(8, "someone", OTHER), listed(9, "someone")],
                      {SHA: [MINE], OTHER: []})
    assert numbers(steward.sweep_candidates(gh, POLICY, missing_only=False)) == [7, 8]


def test_a_missing_only_sweep_leaves_renovate_prs_that_already_have_a_check():
    gh = sweep_client([listed(7, "renovate[bot]"), listed(8, "someone", OTHER), listed(9, "renovate[bot]", "e" * 40)],
                      {SHA: [MINE], OTHER: [], "e" * 40: []})
    assert numbers(steward.sweep_candidates(gh, POLICY, missing_only=True)) == [8, 9]


def test_only_the_steward_apps_checks_count_as_a_verdict():
    foreign = dict(MINE, app={"id": 15368})
    gh = sweep_client([listed(8, "someone")], {SHA: [foreign]})
    assert numbers(steward.sweep_candidates(gh, POLICY, missing_only=True)) == [8]


def test_a_head_held_for_the_rate_limit_is_evaluated_again_but_an_ordinary_hold_is_not():
    held = dict(MINE, status="in_progress", output={"title": steward.RATE_LIMITED})
    waiting = dict(MINE, status="in_progress", output={"title": "stability-days: soaking"})
    gh = sweep_client([listed(7, "renovate[bot]"), listed(8, "renovate[bot]", OTHER)], {SHA: [held], OTHER: [waiting]})
    assert numbers(steward.sweep_candidates(gh, POLICY, missing_only=True)) == [7]


def test_the_newest_check_decides():
    held = dict(MINE, id=10, status="in_progress", output={"title": steward.RATE_LIMITED})
    done = dict(MINE, id=11, status="completed", conclusion="success", output={"title": "all stages passed"})
    gh = sweep_client([listed(7, "someone")], {SHA: [held, done]})
    assert steward.sweep_candidates(gh, POLICY, missing_only=True) == []


def test_the_workflow_runs_a_missing_only_sweep_between_the_hourly_full_ones():
    import yaml

    from test_review_workflow import GITHUB

    workflow = yaml.safe_load((GITHUB / "workflows" / "merge-steward.yaml").read_text())
    crons = [entry["cron"] for entry in workflow.get("on", workflow.get(True))["schedule"]]
    assert "17 * * * *" in crons and len(crons) == 2
    step = next(s for s in workflow["jobs"]["evaluate"]["steps"] if s.get("name") == "Evaluate")
    assert step["env"]["SCHEDULE"] == "${{ github.event.schedule }}" and "${{" not in step["run"]
    assert "python steward.py --sweep --missing-only" in step["run"]
    assert '[ "$SCHEDULE" = "17 * * * *" ]' in step["run"]
    assert "STEWARD_APP_TOKEN" in step["env"]


def test_main_sweep_missing_only_evaluates_a_human_pr_with_no_check(monkeypatch, capsys):
    human = dict(renovate_pr(login="someone"), number=7)
    table = routes(human)
    table["pulls"] = [listed(7, "someone")]
    table[f"commits/{SHA}/check-runs"] = {"check_runs": []}
    table[f"commits/{SHA}/pulls"] = []
    gh = FakeGitHub(table)
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("STEWARD_APP_TOKEN", "y")
    monkeypatch.setattr(steward, "GitHub", lambda *a, **k: gh)
    assert steward.main(["--sweep", "--missing-only"]) == 0
    assert any(w[1] == "check-runs" and w[2]["conclusion"] == "success" for w in gh.writes)
    assert json.loads(capsys.readouterr().out.splitlines()[0])["reason"] == "not a Renovate PR, out of scope"
