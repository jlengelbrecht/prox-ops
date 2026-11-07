"""Leaving the advisory comment in place when a push does not change the PR's diff."""

import pytest

import fold
import review
import review_job
import steward
from test_review import LOW, artifact
from test_steward import FILES, POLICY, SHA, FakeGitHub, fresh_head, renovate_pr, routes  # noqa: F401

ALIAS = POLICY["ai"]["model"]["alias"]
REVIEWED = dict(LOW, model=ALIAS)
OLD = "a" * 40
BOT = {"login": POLICY["steward_app_login"], "type": "Bot"}
# The same change after a rebase: only the hunk header moved.
REBASED = [dict(FILES[0], sha="2" * 40, patch=FILES[0]["patch"].replace("@@ -1 +1 @@", "@@ -9 +9 @@"))]
CHANGED = [dict(FILES[0], patch="@@ -1 +1 @@\n-tag: 2.4.0\n+tag: 2.5.0")]


def human_pr(sha=SHA, **kw):
    return dict(renovate_pr(login="someone"), head=dict(renovate_pr()["head"], sha=sha), **kw)


def posted_body(tmp_path):
    """What the steward app writes for a review of FILES on the commit before the push."""
    pr = human_pr(OLD)
    gh, app = FakeGitHub(dict(routes(pr), **{"pulls/7": pr})), FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, REVIEWED, sha=OLD), POLICY, False)
    return next(w[2]["body"] for w in app.writes if w[1] == "issues/7/comments")


def gate(comments, files=REBASED, pr=None, sha=SHA):
    data = routes(pr or human_pr(), files=files, comments=comments)
    return review_job.advisory_gate(FakeGitHub(data), type("Args", (), dict(pr=7, sha=sha)), POLICY)


def comment(body, user=BOT):
    return {"id": 5, "user": user, "body": body, "performed_via_github_app": {"id": POLICY["steward_app_id"]}}


def test_the_comment_records_the_diff_and_the_model(tmp_path):
    body = posted_body(tmp_path)
    assert body.startswith(fold.ADVISORY_MARKER) and f"Reviewed commit `{OLD[:12]}`" in body
    assert body.endswith(f"<!-- merge-steward-review-diff fp={steward.fingerprint(FILES)} model={ALIAS} -->")


def test_the_same_diff_after_a_rebase_skips_the_review(tmp_path):
    assert gate([comment(posted_body(tmp_path))]) is True


def test_a_changed_diff_is_reviewed(tmp_path):
    assert gate([comment(posted_body(tmp_path))], files=CHANGED) is False


def test_no_earlier_comment_is_reviewed():
    assert gate([]) is False


@pytest.mark.parametrize("user", [
    {"login": "someone", "type": "User"},
    {"login": "github-actions[bot]", "type": "Bot"},
    {"login": POLICY["steward_app_login"], "type": "User"},
    None,
])
def test_a_record_from_anyone_but_the_steward_app_is_ignored(tmp_path, user):
    assert gate([comment(posted_body(tmp_path), user)]) is False


def test_the_steward_comment_is_read_even_when_others_copy_its_marker(tmp_path):
    body = posted_body(tmp_path)
    assert gate([comment(body, {"login": "someone", "type": "User"}), comment(body)]) is True


FP = steward.fingerprint(FILES)
RECORD = f"<!-- merge-steward-review-diff fp={FP} model={ALIAS} -->"


@pytest.mark.parametrize("body", [
    f"{fold.ADVISORY_MARKER}\nno record",                                        # from before records
    f"{fold.ADVISORY_MARKER}\n<!-- merge-steward-review-diff fp={FP[:-1]} model={ALIAS} -->",
    f"{fold.ADVISORY_MARKER}\n<!-- merge-steward-review-diff fp={FP} -->",        # no model
    f"{fold.ADVISORY_MARKER}\n<!-- merge-steward-review-diff fp={FP} model=other -->",
    f"{fold.ADVISORY_MARKER}\n{RECORD}\n{RECORD}",                               # two records
    f"{fold.ADVISORY_MARKER}\n x {RECORD}",                                       # not on its own line
    f"{fold.ADVISORY_MARKER}\n<!-- merge-steward-review-diff fp={FP.upper()} model={ALIAS} -->",
    f"{RECORD}\n{fold.ADVISORY_MARKER}",                                          # not an advisory comment
    "",
    None,
])
def test_a_malformed_record_is_reviewed(body):
    assert gate([comment(body)], files=FILES) is False


def test_the_well_formed_record_alone_is_reused():
    assert gate([comment(f"{fold.ADVISORY_MARKER}\n{RECORD}")], files=FILES) is True


def test_two_steward_comments_are_ambiguous():
    body = f"{fold.ADVISORY_MARKER}\n{RECORD}"
    assert gate([comment(body), dict(comment(body), id=6)], files=FILES) is False


def test_a_diff_the_api_cannot_list_whole_is_reviewed():
    body = f"{fold.ADVISORY_MARKER}\n{RECORD}"
    assert gate([comment(body)], files=FILES, pr=human_pr(changed_files=2)) is False


def test_only_the_current_head_of_an_open_advisory_pr_is_compared():
    body = f"{fold.ADVISORY_MARKER}\n{RECORD}"
    assert gate([comment(body)], files=FILES, sha="d" * 40) is False
    assert gate([comment(body)], files=FILES, pr=human_pr(state="closed")) is False
    assert gate([comment(body)], files=FILES, pr=renovate_pr()) is False  # a verdict PR


def test_a_comment_without_a_fingerprint_writes_no_record(tmp_path):
    assert review.advisory_record(None, REVIEWED) == ""
    assert review.advisory_record(FP, LOW) == ""  # no model named


def test_an_advisory_gate_that_crashes_says_review(monkeypatch, tmp_path):
    outputs = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(review_job, "advisory_gate", lambda *a: 1 / 0)
    assert review_job.main(["advisory-gate", "--pr", "7", "--sha", SHA]) == 0
    assert outputs.read_text() == "reuse=false\n"
