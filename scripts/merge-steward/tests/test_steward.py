"""End-to-end evaluation against an in-memory GitHub."""

import base64
import re
from pathlib import Path

import steward
from classify import load_policy
from ghapi import GitHub, GitHubError
from publish import check_payload

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
SHA = "c" * 40
LABEL = "steward/human-approved"
SECRET_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|-----BEGIN [A-Z ]*PRIVATE KEY")
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


def routes(pr, runs=None, statuses=None, events=None, permission="admin", content="kind: Deployment\n",
           files=None, comments=None):
    return {
        "pulls/7": pr,
        "pulls/7/commits": [{"author": {"login": "renovate[bot]"}}],
        "pulls/7/files": files or FILES,
        "contents/": {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()},
        f"commits/{SHA}/check-runs": {"check_runs": runs if runs is not None else [
            {"name": "Flux Local Success", "status": "completed", "conclusion": "success"}]},
        f"commits/{SHA}/status": {"statuses": statuses or []},
        "issues/7/events": events or [],
        "collaborators/": {"permission": permission},
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
    _, check, _ = run(renovate_pr(), runs=[{"name": "verify", "status": "completed", "conclusion": "failure"}])
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


EVENTS = [{"event": "labeled", "label": {"name": LABEL}, "actor": {"login": "owner"}}]


def test_admin_override_records_the_approved_diff_and_survives_a_clean_rerun():
    verdict, check, gh = run(approved_pr(), labeled_sha=SHA, events=EVENTS)
    assert verdict == "pass" and check["output"]["title"] == "human-approved by owner"
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    assert "merge-steward-approval login=owner fp=" in body
    comment = {"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}
    verdict, _, _ = run(approved_pr(), events=EVENTS, comments=[comment])
    assert verdict == "pass"


def test_override_is_dropped_when_the_diff_changes():
    _, _, gh = run(approved_pr(), labeled_sha=SHA, events=EVENTS)
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    comment = {"id": 1, "user": {"login": "github-actions[bot]"}, "body": body}
    changed = [dict(FILES[0], patch="-tag: 2.4.0\n+tag: 9.9.9")]
    verdict, check, gh = run(approved_pr(), events=EVENTS, comments=[comment], files=changed)
    assert verdict == "needs-human" and ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in gh.writes


def test_override_needs_the_label_on_the_current_head_and_an_admin():
    assert run(approved_pr(), labeled_sha="d" * 40, events=EVENTS)[0] == "needs-human"
    verdict, _, gh = run(approved_pr(), labeled_sha=SHA, events=EVENTS, permission="write")
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
