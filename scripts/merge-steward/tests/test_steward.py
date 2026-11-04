"""End-to-end evaluation against an in-memory GitHub."""

import base64
import json
import re
from pathlib import Path

import pytest

import steward
from classify import load_policy
from ghapi import GitHub, GitHubError
from publish import LEGACY_COMMENT_AUTHOR, check_payload

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
SHA = "c" * 40
LABEL = "steward/human-approved"
SECRET_SHAPES = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_|-----BEGIN [A-Z ]*PRIVATE KEY")
@pytest.fixture(autouse=True)
def fresh_head(monkeypatch):
    """Keep the head commit inside the deadline, whatever the real clock says."""
    monkeypatch.setattr(steward, "head_age_minutes", lambda *a: 1)


LABELLED_AT = "2026-11-04T12:00:00Z"
COMMITTED_AT = "2026-11-04T11:00:00Z"
# Every check the policy requires, green, from the expected app.
REQUIRED = [{"name": c["name"], "app": {"id": c["app_id"]}, "status": "completed", "conclusion": "success"}
            for c in POLICY["required_checks"]]
def recorded(verdict="pass", reason="1 Deployment", app=5615799, sha=SHA, run_id=10):
    """An earlier merge-steward check run carrying a render record."""
    text = f"<!-- merge-steward-render sha={sha} verdict={verdict} reason={reason} -->"
    return {"id": run_id, "name": "merge-steward", "app": {"id": app}, "status": "completed",
            "conclusion": "failure", "output": {"text": text}}


FILES = [{"filename": "kubernetes/apps/observability/uptime-kuma/app/helmrelease.yaml", "status": "modified",
          "sha": "1" * 40, "patch": "@@ -1 +1 @@\n-tag: 2.4.0\n+tag: 2.4.1"}]


class FakeGitHub:
    def __init__(self, routes):
        self.routes, self.writes, self.reads = routes, [], []

    def get(self, path):
        self.reads.append(path)
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
            "head": {"sha": SHA, "ref": "renovate/uptime-kuma", "repo": {"full_name": "o/r"}},
            "base": {"ref": "main", "repo": {"full_name": "o/r"}}}


APP_COMMENT = {"id": 1, "user": {"login": "homebot-bouncer[bot]"}, "performed_via_github_app": {"id": 5615799}}
REAL = json.loads((Path(__file__).parent / "fixtures" / "commits.json").read_text())
OWN_COMMIT = dict(REAL["renovate_commit_pr1876"], sha=SHA)
# A head that is not Renovate's own: its commit, then the one that landed on top of it.
EARLIER = "b" * 40
RENOVATE_BELOW = dict(OWN_COMMIT, sha=EARLIER)
UPDATE_BRANCH = dict(REAL["update_branch_merge_pr1691"], sha=SHA)
REBASE = dict(REAL["server_side_rebase_pr1813"], sha=SHA)


def push(sha, login="renovate[bot]", kind="force_push", when="2026-11-04T11:00:00Z"):
    return {"after": sha, "activity_type": kind, "timestamp": when, "actor": {"login": login}}


OWN_PUSHES = [push(SHA)]
# The activity API lists the newest first.
PERSON_ON_TOP = [push(SHA, "owner", "push", "2026-11-04T11:30:00Z"), push(EARLIER)]


def routes(pr, runs=None, statuses=None, timeline=None, permission="admin", content="kind: Deployment\n",
           files=None, comments=None, commits=None, activity=None, renovate_files=None):
    return {
        "pulls/7": pr,
        "pulls/7/commits": commits or [OWN_COMMIT],
        f"commits/{SHA}": dict(OWN_COMMIT, files=renovate_files or files or FILES),
        f"commits/{EARLIER}": dict(RENOVATE_BELOW, files=renovate_files or FILES),
        "activity": OWN_PUSHES if activity is None else activity,
        f"commits/{SHA}/pulls": [],
        "pulls/7/files": files or FILES,
        "contents/": {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()},
        f"commits/{SHA}/check-runs": {"check_runs": runs if runs is not None else REQUIRED + [recorded()]},
        f"commits/{SHA}/status": {"statuses": statuses or []},
        "issues/7/timeline": timeline if timeline is not None else [COMMIT_EVENT],
        "collaborators/": permission if isinstance(permission, Exception) else {"permission": permission},
        "issues/7/comments": comments or [],
    }


class Clients:
    """Both clients of a run; writes holds everything either one wrote."""

    def __init__(self, gh, app):
        self.gh, self.app, self.writes = gh, app, gh.writes + app.writes


def is_label_write(write):
    return "labels" in write[1]


def run(pr, labeled_sha=None, **kw):
    """Returns (verdict, check run payload, clients). Labels go by the workflow token, the rest by the app."""
    gh, app = FakeGitHub(routes(pr, **kw)), FakeGitHub({})
    verdict = steward.evaluate(gh, app, pr, POLICY, 0, False, labeled_sha)
    assert all(is_label_write(w) for w in gh.writes) and not any(is_label_write(w) for w in app.writes)
    return verdict, next((w[2] for w in app.writes if w[1] == "check-runs"), None), Clients(gh, app)


def test_tier_one_patch_posts_success():
    verdict, check, gh = run(renovate_pr())
    assert verdict == "pass" and check["conclusion"] == "success" and check["head_sha"] == SHA
    assert ("POST", "issues/7/labels", {"labels": ["steward/pass"]}) in gh.writes


def test_major_posts_failure_with_reason():
    verdict, check, _ = run(renovate_pr("major"))
    assert verdict == "needs-human" and "major" in check["output"]["title"]


def test_same_repo_pr_by_someone_else_is_out_of_scope_and_judged_on_nothing():
    pr = renovate_pr("major", login="someone")
    broken = routes(pr, runs=[{"name": "verify", "status": "completed", "conclusion": "failure"}], content="kind: Job\n")
    gh, app = FakeGitHub(broken), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 1200, False) == "pass"
    check = app.writes[0][2]
    assert check["conclusion"] == "success" and check["output"]["title"] == "not a Renovate PR, out of scope"
    assert len(app.writes) == 1 and gh.writes == []  # no verdict label and no comment


def test_bot_with_another_login_is_needs_human_with_a_verdict_label():
    # #1809: an n8n minor from a retired app, with auto-merge enabled.
    verdict, check, clients = run(renovate_pr("minor", login="homebot-0[bot]"))
    assert verdict == "needs-human" and "not the Renovate app" in check["output"]["title"]
    assert ("POST", "issues/7/labels", {"labels": ["steward/needs-human"]}) in clients.gh.writes


def out_of_scope_run(title, conclusion="success"):
    return {"id": 20, "name": "merge-steward", "app": {"id": 5615799}, "status": "completed",
            "conclusion": conclusion, "output": {"title": title}}


def test_out_of_scope_is_not_posted_again_on_the_same_head():
    settled = [out_of_scope_run("not a Renovate PR, out of scope")]
    assert run(renovate_pr(login="someone"), runs=settled)[1] is None


def test_out_of_scope_is_posted_when_the_newest_run_says_otherwise():
    for newest in (out_of_scope_run("identity: x", "failure"), dict(out_of_scope_run("not a Renovate PR, out of scope"),
                                                                    app={"id": 999})):
        assert run(renovate_pr(login="someone"), runs=[newest])[1]["conclusion"] == "success"


def test_out_of_scope_never_replaces_the_verdict_of_a_pr_sharing_the_head():
    # A same-repo PR opened on a needs-human Renovate head must not turn that commit green.
    pr = renovate_pr(login="someone")
    wired = routes(pr)
    wired[f"commits/{SHA}/pulls"] = [dict(renovate_pr(), number=8), dict(pr, number=7)]
    gh, app = FakeGitHub(wired), FakeGitHub({})
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) is None and app.writes == []
    wired[f"commits/{SHA}/pulls"] = [dict(renovate_pr(login="other"), number=8), dict(renovate_pr(), number=9, state="closed")]
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == "pass"


def test_out_of_scope_clears_what_an_earlier_verdict_left():
    old = {"id": 5, "user": {"login": LEGACY_COMMENT_AUTHOR}, "body": "<!-- merge-steward -->\n### old"}
    pr = renovate_pr(login="someone", labels=["steward/needs-human", "area/kubernetes"])
    _, _, clients = run(pr, comments=[old])
    assert ("DELETE", "issues/7/labels/steward%2Fneeds-human", None) in clients.gh.writes
    assert ("DELETE", "issues/comments/5", None) in clients.app.writes
    assert not [w for w in clients.writes if w[1] == "issues/7/labels" and w[0] == "POST"]


def test_out_of_scope_dry_run_writes_nothing_and_a_moved_head_publishes_nothing(capsys):
    pr = renovate_pr(login="someone")
    gh, app = FakeGitHub(routes(pr)), FakeGitHub({})
    assert steward.evaluate(gh, None, pr, POLICY, 0, True) == "pass"
    assert "out of scope" in capsys.readouterr().out
    moved = FakeGitHub(routes(dict(pr, head=dict(pr["head"], sha="e" * 40))))
    assert steward.evaluate(moved, app, pr, POLICY, 0, False) is None and app.writes == []


def forked(pr):
    return dict(pr, head=dict(pr["head"], repo={"full_name": "fork/r"}))


@pytest.mark.parametrize("login", ["someone", "renovate[bot]", "renovate"])
def test_fork_pr_is_needs_human_with_a_verdict_label(login):
    verdict, check, app = run(forked(renovate_pr(login=login)))
    assert verdict == "needs-human" and check["conclusion"] == "failure"
    assert "not in this repository" in check["output"]["title"]
    assert ("POST", "issues/7/labels", {"labels": ["steward/needs-human"]}) in app.writes


def test_unreadable_author_is_needs_human_not_out_of_scope():
    assert run(dict(renovate_pr(), user=None))[0] == "needs-human"
    assert run(dict(renovate_pr(), head=dict(renovate_pr()["head"], repo=None)))[0] == "needs-human"


def test_person_pushing_to_a_renovate_branch_is_needs_human():
    pushed = dict(OWN_COMMIT, sha="d" * 40, author={"login": "someone", "type": "User"})
    pr = dict(renovate_pr(), commits=2)
    verdict, check, _ = run(pr, commits=[OWN_COMMIT, pushed])
    assert verdict == "needs-human" and "not made by Renovate: someone" in check["output"]["title"]


def test_clean_update_branch_merge_passes():
    assert run(dict(renovate_pr(), commits=2), commits=[RENOVATE_BELOW, UPDATE_BRANCH], activity=PERSON_ON_TOP)[0] == "pass"


def test_server_side_rebase_with_the_same_diff_passes():
    # #1813: a person's rebase keeps Renovate as the author, without a signature.
    assert run(renovate_pr(), commits=[REBASE], activity=PERSON_ON_TOP)[0] == "pass"


SLIPPED = [FILES[0], {"filename": "kubernetes/apps/x/app/secret.yaml", "status": "added", "patch": "+evil: true"}]
EDITED = [dict(FILES[0], patch="@@ -1 +1 @@\n-tag: 2.4.0\n+tag: 2.4.2")]


@pytest.mark.parametrize("files", [SLIPPED, EDITED])
def test_merge_that_changes_the_diff_is_needs_human(files):
    # A resolved conflict has the shape of an update-branch merge; only its content differs.
    pr = dict(renovate_pr(), commits=2, changed_files=len(files))
    verdict, check, _ = run(pr, commits=[RENOVATE_BELOW, UPDATE_BRANCH], activity=PERSON_ON_TOP, files=files)
    assert verdict == "needs-human" and "differs from Renovate's last push" in check["output"]["title"]
    assert "pushed by owner" in check["output"]["title"]


@pytest.mark.parametrize("files", [SLIPPED, EDITED])
def test_commit_with_forged_renovate_names_is_needs_human(files):
    # Every name says Renovate; only the push record and the diff say otherwise.
    forged = dict(OWN_COMMIT, commit=dict(OWN_COMMIT["commit"], verification={"verified": False, "reason": "unsigned"}))
    pr = dict(renovate_pr(), changed_files=len(files))
    verdict, check, _ = run(pr, commits=[forged], activity=PERSON_ON_TOP, files=files)
    assert verdict == "needs-human" and "differs from Renovate's last push" in check["output"]["title"]


MOVED = [dict(FILES[0], sha="2" * 40, patch="@@ -40 +40 @@\n-tag: 2.4.0\n+tag: 2.4.1")]


@pytest.mark.parametrize("commits", [[RENOVATE_BELOW, UPDATE_BRANCH], [REBASE]])
def test_same_patch_in_another_place_is_needs_human(commits):
    # Bumping a different image block with identical context leaves the patch text unchanged.
    assert steward.fingerprint(MOVED) == steward.fingerprint(FILES)
    pr = dict(renovate_pr(), commits=len(commits))
    verdict, check, _ = run(pr, commits=commits, activity=PERSON_ON_TOP, files=MOVED)
    assert verdict == "needs-human" and "differs from Renovate's last push" in check["output"]["title"]


def test_changed_file_without_a_blob_is_needs_human():
    bare = [{k: v for k, v in FILES[0].items() if k != "sha"}]
    assert run(renovate_pr(), commits=[REBASE], activity=PERSON_ON_TOP, files=bare)[0] == "needs-human"


def test_renovate_push_that_is_gone_is_needs_human():
    # A force push can leave the log naming a commit the API no longer serves.
    wired = routes(renovate_pr(), commits=[REBASE], activity=PERSON_ON_TOP)
    wired[f"commits/{EARLIER}"] = GitHubError(404, "commits")
    gh, app = FakeGitHub(wired), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False) == "needs-human"
    assert "can no longer be read" in app.writes[0][2]["output"]["title"]


def test_other_api_errors_on_the_renovate_push_are_steward_errors():
    wired = routes(renovate_pr(), commits=[REBASE], activity=PERSON_ON_TOP)
    wired[f"commits/{EARLIER}"] = GitHubError(500, "commits")
    gh, app = FakeGitHub(wired), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False) == "needs-human"
    assert "steward error" in app.writes[0][2]["output"]["title"]


@pytest.mark.parametrize("branch,encoded", [
    ("renovate/uptime-kuma", "refs%2Fheads%2Frenovate%2Fuptime-kuma"),
    ("renovate/a&b=c#d+e", "refs%2Fheads%2Frenovate%2Fa%26b%3Dc%23d%2Be"),
])
def test_activity_ref_is_encoded(branch, encoded):
    pr = dict(renovate_pr(), head=dict(renovate_pr()["head"], ref=branch))
    assert f"activity?ref={encoded}" in run(pr)[2].gh.reads


def test_the_newest_renovate_push_is_the_reference_whatever_the_list_order():
    older = "a" * 40
    log = [push(older, when="2026-11-04T01:00:00Z"), push(SHA, "owner", when="2026-11-04T11:30:00Z"), push(EARLIER)]
    gh_routes = routes(renovate_pr(), commits=[REBASE], activity=log)
    gh_routes[f"commits/{older}"] = dict(OWN_COMMIT, files=EDITED)
    gh, app = FakeGitHub(gh_routes), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False) == "pass"


def test_branch_renovate_never_pushed_to_is_needs_human():
    verdict, check, _ = run(renovate_pr(), activity=[push(SHA, "someone")])
    assert verdict == "needs-human" and "never pushed" in check["output"]["summary"]


def test_renovate_push_that_is_not_signed_is_needs_human():
    # The log says the app pushed, but the commit it pushed is not one the app made through the API.
    gh_routes = routes(renovate_pr())
    gh_routes[f"commits/{SHA}"] = dict(REBASE, files=FILES)
    gh, app = FakeGitHub(gh_routes), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False) == "needs-human"
    assert "not a commit signed for Renovate" in app.writes[0][2]["output"]["title"]


def test_branch_deletion_is_not_a_push():
    log = [{"after": "0" * 40, "activity_type": "branch_deletion", "timestamp": "2026-11-04T12:00:00Z",
            "actor": {"login": "renovate[bot]"}}] + OWN_PUSHES
    assert run(renovate_pr(), activity=log)[0] == "pass"


def test_missing_push_record_holds_then_needs_human(monkeypatch):
    assert run(renovate_pr(), activity=[push("d" * 40)])[0] == "hold"
    monkeypatch.setattr(steward, "head_age_minutes", lambda *a: POLICY["required_checks_deadline_minutes"] + 1)
    verdict, check, _ = run(renovate_pr(), activity=[])
    assert verdict == "needs-human" and "no record of who pushed" in check["output"]["summary"]


def test_obsidian_mcp_digest_is_needs_human():
    # The package table from #1812.
    pr = renovate_pr("digest")
    pr["body"] = ("| Package | Update | Change |\n|---|---|---|\n"
                  "| ghcr.io/jlengelbrecht/obsidian-mcp | digest | `fe17d0d` → `a533e03` |")
    verdict, check, _ = run(pr)
    assert verdict == "needs-human" and "build pipeline" in check["output"]["title"]


def test_soaking_pr_holds_in_progress():
    verdict, check, _ = run(renovate_pr(), statuses=[{"context": "renovate/stability-days", "state": "pending"}])
    assert verdict == "hold" and check["status"] == "in_progress"


def test_failing_verify_is_failure():
    _, check, _ = run(renovate_pr(), runs=REQUIRED + [recorded(), {"name": "verify", "status": "completed", "conclusion": "failure"}])
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
    comment = dict(APP_COMMENT, body=body)
    verdict, _, _ = run(approved_pr(), timeline=EVENTS, comments=[comment])
    assert verdict == "pass"


def test_override_is_dropped_when_the_diff_changes():
    _, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    body = [w[2]["body"] for w in gh.writes if w[1] == "issues/7/comments"][0]
    comment = dict(APP_COMMENT, body=body)
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
    return dict(APP_COMMENT, body=body)


def test_approval_survives_a_clean_rebase_but_binds_to_the_label_event():
    _, _, gh = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    rebased = EVENTS + [{"event": "head_ref_force_pushed", "created_at": "2026-11-04T13:00:00Z"}]
    assert run(approved_pr(), timeline=rebased, comments=[sticky(gh)])[0] == "pass"
    relabelled = rebased + [dict(LABEL_EVENT, id=502, created_at="2026-11-04T12:30:00Z")]
    # A newer label event has no recorded fingerprint, and it predates the force push.
    assert run(approved_pr(), timeline=relabelled, comments=[sticky(gh)])[0] == "needs-human"


def test_override_works_end_to_end_with_the_app_as_writer():
    verdict, _, app = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    assert verdict == "pass" and "issues/7/comments" in [w[1] for w in app.writes]
    verdict, _, app = run(approved_pr(), timeline=EVENTS, comments=[sticky(app)])
    assert verdict == "pass" and ("PATCH", "issues/comments/1") not in [w[:2] for w in app.writes]


def test_approval_is_trusted_by_app_id_and_not_by_login():
    _, _, clients = run(approved_pr(), labeled_sha=SHA, timeline=EVENTS)
    record = sticky(clients)
    for forged in (dict(record, user={"login": LEGACY_COMMENT_AUTHOR}, performed_via_github_app={"id": 15368}),
                   dict(record, user={"login": "someone"}, performed_via_github_app=None),
                   dict(record, performed_via_github_app={"id": 999}),  # the app's name, another app
                   {k: v for k, v in record.items() if k != "performed_via_github_app"}):
        assert run(approved_pr(), timeline=EVENTS, comments=[forged])[0] == "needs-human"


def test_override_label_is_removed_with_the_workflow_token():
    _, _, clients = run(approved_pr(), timeline=EVENTS)
    assert ("DELETE", "issues/7/labels/steward%2Fhuman-approved", None) in clients.gh.writes


def test_legacy_comment_is_replaced_once():
    old = {"id": 5, "user": {"login": LEGACY_COMMENT_AUTHOR}, "body": "<!-- merge-steward -->\n### old"}
    other = {"id": 6, "user": {"login": LEGACY_COMMENT_AUTHOR}, "body": "unrelated bot comment"}
    _, _, app = run(renovate_pr(), comments=[old, other])
    assert ("DELETE", "issues/comments/5", None) in app.writes
    assert ("DELETE", "issues/comments/6", None) not in app.writes
    assert "issues/7/comments" in [w[1] for w in app.writes]
    # The next run finds the app's comment and has nothing left to remove.
    _, _, again = run(renovate_pr(), comments=[sticky(app), other])
    assert not [w for w in again.writes if w[0] == "DELETE"]
    assert "issues/7/comments" not in [w[1] for w in again.writes]


def test_duplicate_app_comments_collapse_to_the_first():
    first = dict(APP_COMMENT, body="<!-- merge-steward -->\na")
    second = dict(APP_COMMENT, id=2, body="<!-- merge-steward -->\nb")
    _, _, app = run(renovate_pr(), comments=[first, second])
    assert ("DELETE", "issues/comments/2", None) in app.writes
    assert [w[1] for w in app.writes if w[0] == "PATCH"] == ["issues/comments/1"]


def test_a_refused_comment_delete_does_not_stop_the_run(capsys):
    old = {"id": 5, "user": {"login": LEGACY_COMMENT_AUTHOR}, "body": "<!-- merge-steward -->\n### old"}
    pr = renovate_pr()
    gh, app = FakeGitHub(routes(pr, comments=[old])), FakeGitHub({})
    def refuse(path):
        raise GitHubError(403, path)
    app.delete = refuse
    assert steward.evaluate(gh, app, pr, POLICY, 0, False) == "pass"
    assert "could not delete comment 5" in capsys.readouterr().err


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
    partial = [r for r in REQUIRED if r["name"] != "Labeler"] + [recorded()]
    verdict, check, _ = run(renovate_pr(), runs=partial)
    assert verdict == "hold" and "Labeler" in check["output"]["title"]
    monkeypatch.setattr(steward, "head_age_minutes", lambda *a: POLICY["required_checks_deadline_minutes"] + 1)
    verdict, check, _ = run(renovate_pr(), runs=partial)
    assert verdict == "needs-human" and "Labeler (never reported)" in check["output"]["title"]


def test_required_check_from_another_app_does_not_count():
    spoofed = [dict(r, app={"id": 999}) if r["name"] == "Security Gate" else r for r in REQUIRED] + [recorded()]
    assert run(renovate_pr(), runs=spoofed)[0] == "hold"


def test_empty_check_set_never_passes():
    assert run(renovate_pr(), runs=[recorded()])[0] == "hold"


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
    gh.routes["pulls"] = [{"number": 7, "user": {"login": "renovate[bot]"}}, {"number": 9, "user": {"login": "someone"}},
                          {"number": 10, "user": None}]
    fake_env(monkeypatch, gh)
    assert steward.main(["--sweep", "--dry-run"]) == 0
    assert [json.loads(l)["pr"] for l in capsys.readouterr().out.splitlines()] == [7]


def test_render_flags_reach_the_verdict():
    verdict, check, _ = run(renovate_pr(), runs=REQUIRED + [recorded("needs-human", "CRD: CustomResourceDefinition x")])
    assert verdict == "needs-human" and check["output"]["title"].startswith("render: CRD")
    assert "verdict=needs-human" in check["output"]["text"]  # the record is carried forward


def test_render_record_from_another_app_or_commit_is_ignored(monkeypatch):
    for other in (recorded(app=15368), recorded(sha="d" * 40)):
        verdict, check, _ = run(renovate_pr(), runs=REQUIRED + [other])
        assert verdict is None and check is None  # nothing published while the PR run is pending


def test_newest_render_record_wins():
    runs = REQUIRED + [recorded("needs-human", "major: x", run_id=10), recorded("pass", "1 Deployment", run_id=11)]
    assert run(renovate_pr(), runs=runs)[0] == "pass"


def test_missing_render_evidence_needs_human_after_the_deadline(monkeypatch):
    monkeypatch.setattr(steward, "head_age_minutes", lambda *a: POLICY["required_checks_deadline_minutes"] + 1)
    verdict, check, _ = run(renovate_pr(), runs=REQUIRED)
    assert verdict == "needs-human" and "render evidence unavailable" in check["output"]["summary"]


def artifact(tmp_path, head=SHA, failed=0, helmrelease=()):
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": head, "failed": failed}))
    (tmp_path / "helmrelease.json").write_text(json.dumps(list(helmrelease)))
    (tmp_path / "kustomization.json").write_text("[]")
    return str(tmp_path)


def run_with_artifact(directory, runs=None):
    gh, app = FakeGitHub(routes(renovate_pr(), runs=runs if runs is not None else REQUIRED)), FakeGitHub({})
    verdict = steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False, render_dir=directory)
    return verdict, (app.writes[0][2] if app.writes else None)


def test_pull_request_run_classifies_the_artifact_and_records_it(tmp_path):
    verdict, check = run_with_artifact(artifact(tmp_path))
    assert verdict == "pass" and f"merge-steward-render sha={SHA} verdict=pass" in check["output"]["text"]


def test_artifact_failures_are_needs_human(tmp_path):
    assert run_with_artifact(artifact(tmp_path, failed=1))[0] == "needs-human"
    assert run_with_artifact(str(tmp_path / "absent"))[0] == "needs-human"


def test_artifact_for_an_older_head_publishes_nothing(tmp_path):
    verdict, check = run_with_artifact(artifact(tmp_path, head="d" * 40))
    assert verdict is None and check is None


def test_artifact_beats_an_older_record(tmp_path):
    hr = {"kind": "Deployment", "name": "a", "namespace": "n", "diff": "@@ -0,0 +1,2 @@\n+kind: Deployment\n+x: 1"}
    verdict, check = run_with_artifact(artifact(tmp_path, helmrelease=[{"diffs": [hr]}]), REQUIRED + [recorded()])
    assert verdict == "needs-human" and check["output"]["title"].startswith("render: added")


def test_superseded_event_is_skipped(monkeypatch, capsys):
    gh = FakeGitHub(routes(renovate_pr()))
    fake_env(monkeypatch, gh)
    assert steward.main(["--pr", "7", "--event-sha", "d" * 40, "--dry-run"]) == 0
    assert capsys.readouterr().out == ""


def test_head_moving_while_waiting_publishes_nothing(monkeypatch):
    pr = renovate_pr()
    gh, app = FakeGitHub(routes(pr, runs=REQUIRED + [recorded(), {"name": "x", "status": "queued"}])), FakeGitHub({})
    gh.routes["pulls/7"] = dict(pr, head=dict(pr["head"], sha="e" * 40))
    monkeypatch.setattr(steward.time, "sleep", lambda s: None)
    assert steward.evaluate(gh, app, pr, POLICY, 600, False) is None and app.writes == []


def test_stale_merge_ref_publishes_hold_without_a_record(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": SHA, "stale": True}))
    verdict, check = run_with_artifact(str(tmp_path))
    assert verdict == "hold" and check["status"] == "in_progress" and "stale" in check["output"]["title"]
    assert "text" not in check["output"]


SPEKTR_FILE = "kubernetes/apps/ai/agent-router/app/ocirepository.yaml"


def spektr_pr(package="ghcr.io/jlengelbrecht/agent-router-base"):
    body = f"| Package | Update | Change |\n|---|---|---|\n| {package} | minor | `v0.8.3` → `v0.9.0-rc.2` |"
    return dict(renovate_pr(), body=body, title=f"feat(container)!: Update {package} ( v0.8.3 → v0.9.0-rc.2 )",
                labels=[{"name": n} for n in ("area/kubernetes", "update/minor", "renovate", "spektr", "breaking")])


def spektr_verdict(tmp_path, pr):
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": SHA, "failed": 0}))
    (tmp_path / "helmrelease.json").write_text("")
    fixture = Path(__file__).parent / "fixtures" / "pr-1880-agent-router-base.json"
    (tmp_path / "kustomization.json").write_text(fixture.read_text())
    files = [dict(FILES[0], filename=SPEKTR_FILE)]
    gh, app = FakeGitHub(routes(pr, runs=REQUIRED, files=files)), FakeGitHub({})
    return steward.evaluate(gh, app, pr, POLICY, 0, False, render_dir=str(tmp_path)), app.writes[0][2]


def test_spektr_release_like_1880_passes_every_stage(tmp_path):
    verdict, check = spektr_verdict(tmp_path, spektr_pr())
    assert verdict == "pass" and check["conclusion"] == "success"


def test_the_same_bump_on_another_package_is_needs_human(tmp_path):
    verdict, check = spektr_verdict(tmp_path, spektr_pr("ghcr.io/jlengelbrecht/other-base"))
    assert verdict == "needs-human" and "breaking" in check["output"]["title"]
