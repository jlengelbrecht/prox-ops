"""End-to-end evaluation against an in-memory GitHub."""

import base64
import json
import re
from pathlib import Path

import pytest

import steward
from classify import load_policy
from ghapi import GitHub, GitHubError
from publish import check_payload

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
SHA = "c" * 40
LABEL = "steward/human-approved"
SECRET_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|-----BEGIN [A-Z ]*PRIVATE KEY")
LABELLED_AT = "2026-11-04T12:00:00Z"
COMMITTED_AT = "2026-11-04T11:00:00Z"
# Every check the policy requires, green, from the expected app.
REQUIRED = [{"name": c["name"], "app": {"id": c["app_id"]}, "status": "completed", "conclusion": "success"}
            for c in POLICY["required_checks"]]
FILES = [{"filename": "kubernetes/apps/observability/uptime-kuma/app/helmrelease.yaml", "status": "modified",
          "patch": "@@ -1 +1 @@\n-tag: 2.4.0\n+tag: 2.4.1"}]


class FakeGitHub:
    def __init__(self, routes):
        self.routes, self.writes = routes, []

    def get(self, path):
        path = path.split("?")[0]
        keys = [k for k in self.routes if path == k or (k.endswith("/") and path.startswith(k))]
        if not keys:
            raise GitHubError(404, path)
        value = self.routes[max(keys, key=len)]
        if isinstance(value, Exception):
            raise value
        return value

    def paginate(self, path, key=None):
        data = self.get(path)
        return data[key] if key else data

    def post(self, path, body):
        self.writes.append(("POST", path, body))

    def patch(self, path, body):
        self.writes.append(("PATCH", path, body))

    def delete(self, path):
        self.writes.append(("DELETE", path, None))


def renovate_pr(update="patch", labels=None, login="renovate[bot]"):
    body = f"| Package | Update | Change |\n|---|---|---|\n| louislam/uptime-kuma | {update} | `2.4.0` → `2.4.1` |"
    return {"number": 7, "state": "open", "title": "fix(container): update uptime-kuma", "body": body,
            "labels": [{"name": n} for n in (labels or [f"update/{update}"])], "commits": 1, "changed_files": 1,
            "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "head": {"sha": SHA, "repo": {"full_name": "o/r"}}, "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


def routes(pr, runs=None, statuses=None, timeline=None, permission="admin", content="kind: Deployment\n",
           files=None, comments=None):
    return {
        "pulls/7": pr,
        "pulls/7/commits": [{"author": {"login": "renovate[bot]"}, "commit": {"committer": {"date": COMMITTED_AT}}}],
        "pulls/7/files": files or FILES,
        "contents/": {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()},
        f"commits/{SHA}/check-runs": {"check_runs": runs if runs is not None else REQUIRED},
        f"commits/{SHA}/status": {"statuses": statuses or []},
        "issues/7/timeline": timeline if timeline is not None else [COMMIT_EVENT],
        "collaborators/": permission if isinstance(permission, Exception) else {"permission": permission},
        "issues/7/comments": comments or [],
    }


def run(pr, labeled_sha=None, **kw):
    gh, app = FakeGitHub(routes(pr, **kw)), FakeGitHub({})
    verdict = steward.evaluate(gh, app, pr, POLICY, 0, False, labeled_sha)
    return verdict, (app.writes[0][2] if app.writes else None), gh


def test_tier_one_patch_posts_success():
    verdict, check, gh = run(renovate_pr())
    assert verdict == "pass" and check["conclusion"] == "success" and check["head_sha"] == SHA
    assert ("POST", "issues/7/labels", {"labels": ["steward/pass"]}) in gh.writes


def test_major_posts_failure_with_reason():
    verdict, check, _ = run(renovate_pr("major"))
    assert verdict == "needs-human" and "major" in check["output"]["title"]


def test_non_renovate_stops_after_identity():
    _, check, _ = run(renovate_pr(login="someone"))
    assert check["conclusion"] == "failure" and "not on the steward allowlist" in check["output"]["title"]


def test_soaking_pr_holds_in_progress():
    verdict, check, _ = run(renovate_pr(), statuses=[{"context": "renovate/stability-days", "state": "pending"}])
    assert verdict == "hold" and check["status"] == "in_progress"


def test_failing_verify_is_failure():
    _, check, _ = run(renovate_pr(), runs=REQUIRED + [{"name": "verify", "status": "completed", "conclusion": "failure"}])
    assert check["conclusion"] == "failure" and "verify" in check["output"]["title"]


def test_job_manifest_and_unreadable_manifest_are_failures():
    assert "Job manifest" in run(renovate_pr(), content="kind: Job\n")[1]["output"]["title"]
    assert "Job manifest" in run(renovate_pr(), content="")[1]["output"]["title"]


def test_rename_out_of_a_protected_path_is_caught():
    moved = [dict(FILES[0], status="renamed", previous_filename=".github/workflows/x.yaml")]
    assert "protected path" in run(renovate_pr(), files=moved)[1]["output"]["title"]


def test_truncated_pr_is_failure():
    pr = dict(renovate_pr(), changed_files=3001)
    assert "too large" in run(pr)[1]["output"]["title"]


def approved_pr():
    return renovate_pr("major", labels=["update/major", LABEL])


COMMIT_EVENT = {"event": "committed", "sha": SHA, "committer": {"date": COMMITTED_AT}}
LABEL_EVENT = {"event": "labeled", "id": 501, "created_at": LABELLED_AT, "label": {"name": LABEL},
               "actor": {"login": "owner"}}
EVENTS = [COMMIT_EVENT, LABEL_EVENT]


def test_admin_override_records_the_approved_diff_and_survives_a_clean_rerun():
    verdict, check, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    assert verdict == "pass" and check["output"]["title"] == "human-approved by owner"
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    assert "merge-steward-approval login=owner event=501 fp=" in body
    comment = {"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}
    verdict, _, _ = run(approved_pr(), timeline=EVENTS, comments=[comment])
    assert verdict == "pass"


def test_override_is_dropped_when_the_diff_changes():
    _, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    comment = {"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}
    changed = [dict(FILES[0], patch="-tag: 2.4.0\n+tag: 9.9.9")]
    verdict, check, gh = run(approved_pr(), timeline=EVENTS, comments=[comment], files=changed)
    assert verdict == "needs-human" and ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in gh.writes


def test_override_needs_the_label_on_the_current_head_and_an_admin():
    assert run(approved_pr(), labeled_sha="d" * 40, timeline=EVENTS)[0] == "needs-human"
    verdict, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS, permission="write")
    assert verdict == "needs-human" and ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in gh.writes


def test_head_moved_publishes_nothing():
    pr = renovate_pr()
    gh, app = FakeGitHub(routes(dict(pr, head=dict(pr["head"], sha="e" * 40)))), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) is None and app.writes == []


def test_api_error_fails_closed():
    broken = routes(renovate_pr())
    broken[f"commits/{SHA}/status"] = GitHubError(500, "status")
    gh, app = FakeGitHub(broken), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False) == "needs-human"
    assert "steward error" in app.writes[0][2]["output"]["title"]


def test_client_refuses_links_off_the_api_host():
    try:
        GitHub("t", "o/r").request("GET", "https://evil.example/x")
    except GitHubError as err:
        assert err.status == "refused"
    else:
        raise AssertionError("expected a refusal")


def test_output_carries_no_secret_shaped_strings(capsys):
    _, _, gh = run(renovate_pr())
    written = capsys.readouterr().out + "".join(str(w) for w in gh.writes)
    assert not SECRET_SHAPES.search(written)


def test_check_payload_never_neutral_or_skipped():
    assert check_payload("pass", "r", "s")["conclusion"] == "success"
    assert check_payload("needs-human", "r", "s")["conclusion"] == "failure"
    hold = check_payload("hold", "r", "s")
    assert hold["status"] == "in_progress" and "conclusion" not in hold


def test_fingerprint_covers_files_without_a_patch():
    a = [{"filename": "x.bin", "status": "modified", "sha": "1" * 40}]
    b = [{"filename": "x.bin", "status": "modified", "sha": "2" * 40}]
    assert steward.fingerprint(a) != steward.fingerprint(b)


def sticky(gh):
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    return {"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}


def test_approval_survives_a_clean_rebase_but_binds_to_the_label_event():
    _, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    rebased = EVENTS + [{"event": "head_ref_force_pushed", "created_at": "2026-11-04T13:00:00Z"}]
    assert run(approved_pr(), timeline=rebased, comments=[sticky(gh)])[0] == "pass"
    relabelled = rebased + [dict(LABEL_EVENT, id=502, created_at="2026-11-04T12:30:00Z")]
    # A newer label event has no recorded fingerprint, and it predates the force push.
    assert run(approved_pr(), timeline=relabelled, comments=[sticky(gh)])[0] == "needs-human"


def test_label_without_a_recorded_approval_is_not_approved_by_other_runs():
    verdict, check, gh = run(approved_pr(), timeline=EVENTS)
    assert verdict == "needs-human" and "no recorded approval yet" in check["output"]["summary"]
    assert ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in gh.writes
    verdict, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    assert verdict == "pass" and "event=501" in sticky(gh)["body"]


def test_commit_after_the_label_without_a_recorded_approval_is_reported_truthfully():
    later = EVENTS + [{"event": "committed", "sha": "d" * 40, "committer": {"date": "2026-11-04T12:05:00Z"}}]
    verdict, check, _ = run(approved_pr(), labeled_sha=SHA, timeline=later)
    assert verdict == "needs-human" and "changed after" in check["output"]["summary"]
    undated = EVENTS + [{"event": "committed", "sha": "d" * 40}]
    assert run(approved_pr(), labeled_sha=SHA, timeline=undated)[0] == "needs-human"


def test_label_without_a_timeline_event_is_reported():
    verdict, check, _ = run(approved_pr(), timeline=[COMMIT_EVENT])
    assert verdict == "needs-human" and "no labelling event" in check["output"]["summary"]


def test_app_labeller_404_counts_as_not_admin():
    bot = [COMMIT_EVENT, dict(LABEL_EVENT, actor={"login": "renovate[bot]"})]
    verdict, check, gh = run(approved_pr(), timeline=bot, permission=GitHubError(404, "collaborators"))
    assert verdict == "needs-human" and "not a collaborator" in check["output"]["summary"]
    assert ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in gh.writes


def test_permission_lookup_403_fails_closed_and_keeps_the_label():
    verdict, check, gh = run(approved_pr(), timeline=EVENTS, permission=GitHubError(403, "collaborators"))
    assert verdict == "needs-human" and "steward error" in check["output"]["title"]
    assert not [w for w in gh.writes if w[0] == "DELETE"]


def test_missing_required_check_holds_then_needs_human_after_the_deadline(monkeypatch):
    partial = [r for r in REQUIRED if r["name"] != "Labeler"]
    verdict, check, _ = run(renovate_pr(), runs=partial)
    assert verdict == "hold" and "Labeler" in check["output"]["title"]
    monkeypatch.setattr(steward, "head_age_minutes", lambda *a: POLICY["required_checks_deadline_minutes"] + 1)
    verdict, check, _ = run(renovate_pr(), runs=partial)
    assert verdict == "needs-human" and "Labeler (never reported)" in check["output"]["title"]


def test_required_check_from_another_app_does_not_count():
    spoofed = [dict(r, app={"id": 999}) if r["name"] == "Security Gate" else r for r in REQUIRED]
    assert run(renovate_pr(), runs=spoofed)[0] == "hold"


def test_empty_check_set_never_passes():
    assert run(renovate_pr(), runs=[])[0] == "hold"


@pytest.mark.parametrize("text", ["kind: Job\n", 'kind: "Job"\n', "kind: 'Job'  # one-off\n", "  - kind: Job\n"])
def test_job_kind_variants_are_caught(text):
    assert "Job manifest" in run(renovate_pr(), content=text)[1]["output"]["title"]


def test_cronjob_is_not_a_job_manifest():
    assert run(renovate_pr(), content="kind: CronJob\n")[0] == "pass"


def fake_env(monkeypatch, gh):
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setattr(steward, "GitHub", lambda *a, **k: gh)


def test_main_sha_evaluates_only_open_prs_on_that_head(monkeypatch, capsys):
    gh = FakeGitHub(routes(renovate_pr()))
    gh.routes[f"commits/{SHA}/pulls"] = [{"number": 7, "head": {"sha": SHA}}, {"number": 8, "head": {"sha": "e" * 40}}]
    fake_env(monkeypatch, gh)
    assert steward.main(["--sha", SHA, "--dry-run"]) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(l["pr"], l["verdict"]) for l in lines] == [(7, "pass")] and gh.writes == []


def test_main_sweep_skips_other_authors(monkeypatch, capsys):
    gh = FakeGitHub(routes(renovate_pr()))
    gh.routes["pulls"] = [{"number": 7, "user": {"login": "renovate[bot]"}}, {"number": 9, "user": {"login": "someone"}}]
    fake_env(monkeypatch, gh)
    assert steward.main(["--sweep", "--dry-run"]) == 0
    assert [json.loads(l)["pr"] for l in capsys.readouterr().out.splitlines()] == [7]
