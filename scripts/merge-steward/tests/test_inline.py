"""Inline comments on an advisory review: anchoring to the diff, the app's identity and reruns."""

import json

import pytest

import fold
import inline
import review
import review_job
import steward
from ghapi import GitHubError
from test_review import LOW, artifact
from test_steward import POLICY, SHA, FakeGitHub, fresh_head, renovate_pr, routes  # noqa: F401

APP = POLICY["steward_app_id"]
BOT = {"login": POLICY["steward_app_login"], "type": "Bot"}
PATCH = "\n".join([
    "@@ -10,4 +10,5 @@ spec:",
    "   replicas: 1",
    "-  tag: 1.0.0",
    "+  tag: 1.1.0",
    "+  pullPolicy: Always",
    "   other: x",
    "@@ -40,2 +41,3 @@ more",
    "   keep: a",
    "+  added: b",
    "\\ No newline at end of file",
])
FILE = {"filename": "kubernetes/apps/a/helmrelease.yaml", "status": "modified", "sha": "1" * 40, "patch": PATCH}
OTHER = "kubernetes/apps/b/other.yaml"


def finding(start, end=None, text="Possible Bug: the tag moves.", path=FILE["filename"]):
    return {"file": path, "text": text, "start": start, "end": end or start}


ASSESSMENT = {"file": FILE["filename"], "text": "Assessment: low risk."}


def test_visible_lines_are_the_head_side_of_each_hunk():
    lines = inline.visible(PATCH)
    # Context and added lines count, the removed line has no head number, hunk two restarts at 41.
    assert lines == {10: "  replicas: 1", 11: "  tag: 1.1.0", 12: "  pullPolicy: Always", 13: "  other: x",
                     41: "  keep: a", 42: "  added: b"}


def test_a_trimmed_blank_context_line_still_advances_the_count():
    assert inline.visible("@@ -1,3 +1,3 @@\n a\n\n+b") == {1: "a", 2: "", 3: "b"}
    assert inline.visible(None) == {} and inline.visible("") == {}
    assert inline.visible("+orphan line before any hunk") == {}


def test_a_finding_on_a_changed_line_is_anchored_and_the_assessment_is_not():
    found = inline.anchors([ASSESSMENT, finding(11)], [FILE])
    assert [(a["index"], a["path"], a["start"], a["end"]) for a in found] == [(1, FILE["filename"], 11, 11)]
    assert inline.anchors([finding(11)], [FILE]) == []  # position 0 is the assessment, whatever it says


@pytest.mark.parametrize("row", [
    finding(14),            # between the hunks
    finding(500),           # past the file
    finding(1),             # before the first hunk
    finding(11, path=OTHER),                       # a file the PR does not change
    {"file": "", "text": "Possible Bug: x"},       # the filter dropped the path
    {"file": FILE["filename"], "text": "no lines"},  # the model gave no line
])
def test_a_line_outside_the_diff_stays_in_the_summary(row):
    assert inline.anchors([ASSESSMENT, row], [FILE]) == []


def test_a_removed_or_patchless_file_takes_no_comment():
    gone = dict(FILE, status="removed")
    big = {k: v for k, v in FILE.items() if k != "patch"}
    assert inline.anchors([ASSESSMENT, finding(11)], [gone]) == []
    assert inline.anchors([ASSESSMENT, finding(11)], [big]) == []


def test_a_range_must_lie_in_the_diff_or_it_falls_back_to_its_first_line():
    assert inline.anchors([ASSESSMENT, finding(11, 13)], [FILE])[0]["end"] == 13
    assert inline.anchors([ASSESSMENT, finding(11, 20)], [FILE])[0]["end"] == 11   # runs past the hunk
    assert inline.anchors([ASSESSMENT, finding(10, 10 + inline.MAX_SPAN + 5)], [FILE])[0]["end"] == 10
    assert inline.anchors([ASSESSMENT, finding(13, 41)], [FILE])[0]["end"] == 13   # spans the gap
    assert inline.anchors([ASSESSMENT, finding(14, 41)], [FILE]) == []             # starts outside


def test_at_most_three_inline_comments_and_one_per_spot():
    rows = [ASSESSMENT] + [finding(n) for n in (10, 11, 12, 13)]
    assert len(inline.anchors(rows, [FILE])) == inline.MAX_INLINE == 3
    same = inline.anchors([ASSESSMENT, finding(11), finding(11, text="Reworded: same line")], [FILE])
    assert len(same) == 1


def test_the_key_follows_the_code_not_the_wording_or_the_line_number():
    moved = dict(FILE, patch=PATCH.replace("@@ -10,4 +10,5 @@", "@@ -30,4 +30,5 @@"))
    a = inline.anchors([ASSESSMENT, finding(11, text="First wording.")], [FILE])[0]
    b = inline.anchors([ASSESSMENT, finding(31, text="Second wording.")], [moved])[0]
    assert a["key"] == b["key"] and len(a["key"]) == 24
    assert inline.anchors([ASSESSMENT, finding(12)], [FILE])[0]["key"] != a["key"]


def test_the_payload_targets_the_head_side_with_a_range_when_there_is_one():
    anchor = inline.anchors([ASSESSMENT, finding(11, 12)], [FILE])[0]
    body = inline.payload(anchor, "Possible Bug: x.", SHA)
    assert (body["commit_id"], body["path"], body["line"], body["start_line"], body["side"], body["start_side"]) == (
        SHA, FILE["filename"], 12, 11, "RIGHT", "RIGHT")
    assert body["body"].endswith(inline.INLINE_MARKER.format(key=anchor["key"]))
    single = inline.payload(inline.anchors([ASSESSMENT, finding(11)], [FILE])[0], "x", SHA)
    assert "start_line" not in single and single["line"] == 11


def test_only_the_apps_own_marker_counts_as_posted():
    marker = inline.INLINE_MARKER.format(key="a" * 24)
    mine = {"body": f"text\n{marker}", "user": BOT, "path": "p", "line": 3}
    forged = {"body": f"text\n{marker}", "user": {"login": "someone"}}
    other_app = {"body": f"text\n{marker}", "user": {"login": "another-app[bot]", "type": "Bot"}}
    reply = dict(mine, in_reply_to_id=5)
    person = {"body": f"text\n{marker}", "user": {"login": BOT["login"], "type": "User"}}
    assert [c["key"] for c in inline.steward_inline([mine], BOT["login"])] == ["a" * 24]
    assert inline.steward_inline([forged, other_app, reply, person, {"body": None}, {"body": "no marker", "user": BOT}],
                                 BOT["login"]) == []


# The model's line numbers reach the stored result only as validated integers.

def review_with(**issue):
    base = {"relevant_file": FILE["filename"], "issue_header": "Possible Bug", "issue_content": "The tag moves."}
    return {"risk_level": "low", "merge_recommendation": "needs_review",
            "key_issues_to_review": [dict(base, issue_header="Assessment", issue_content="Low."), dict(base, **issue)]}


def test_pr_agents_line_numbers_survive_the_filter():
    row = review.result_from_review(review_with(start_line=11, end_line=12), {FILE["filename"]})["findings"][1]
    assert (row["start"], row["end"]) == (11, 12)
    assert "start" not in review.result_from_review(review_with(start_line=11), {FILE["filename"]})["findings"][0]
    assert review.result_from_review(review_with(start_line="12", end_line="13"))["findings"][1]["start"] == 12


@pytest.mark.parametrize("start", [True, 0, -3, 1.5, "x", None, 10 ** 9, [3], "１２", "12 or 1=1"])
def test_bad_line_numbers_are_dropped(start):
    row = review.result_from_review(review_with(start_line=start, end_line=5))["findings"][1]
    assert "start" not in row and "end" not in row


@pytest.mark.parametrize("end", [None, 3, True, "z", 10 ** 9])
def test_a_bad_end_line_collapses_to_the_start(end):
    row = review.result_from_review(review_with(start_line=11, end_line=end))["findings"][1]
    assert (row["start"], row["end"]) == (11, 11)


def test_lines_without_a_path_the_pr_changed_are_dropped_and_survive_revalidation():
    row = review.result_from_review(review_with(relevant_file="elsewhere.yaml", start_line=3), {FILE["filename"]})["findings"][1]
    assert row["file"] == "" and "start" not in row
    stored = {"available": True, "risk": "low", "recommendation": "needs_review",
              "findings": [ASSESSMENT, finding(11, 12), dict(finding(13), start="no", end=[1])]}
    rows = review.checked(stored, {FILE["filename"]})["findings"]
    assert (rows[1]["start"], rows[1]["end"]) == (11, 12) and "start" not in rows[2]


def test_collect_writes_the_lines_to_the_result_file(tmp_path):
    (tmp_path / "review.json").write_text(json.dumps({"review": review_with(start_line=11, end_line=11), "usage": {}}))
    review_job.collect(type("A", (), {"out": str(tmp_path), "status": "ok", "sha": SHA, "model": "m"}))
    saved = json.loads((tmp_path / "result.json").read_text())
    assert saved["findings"][1]["start"] == 11


# The publish side, against an in-memory GitHub.

def pr_and_files(files=None):
    return renovate_pr(login="someone"), files or [FILE]


class Refusing(FakeGitHub):
    """Refuses a review comment on a given path, as GitHub does for a line it cannot place."""

    def __init__(self, routes_, refuse=()):
        super().__init__(routes_)
        self.refuse = refuse

    def post(self, path, body):
        if path.endswith("/comments") and body.get("path") in self.refuse:
            raise GitHubError(422, path)
        super().post(path, body)


def publish(tmp_path, findings, files=None, line_comments=None, comments=None, dry_run=False):
    pr, files = pr_and_files(files)
    gh = FakeGitHub(routes(pr, files=files, line_comments=line_comments, comments=comments))
    app = FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=findings)), POLICY, dry_run)
    return gh, app


def line_posts(client):
    return [w[2] for w in client.writes if w[1] == "pulls/7/comments"]


def summary_of(client):
    return next(w[2]["body"] for w in client.writes if w[1] == "issues/7/comments")


def test_inline_comments_and_the_summary_are_posted_by_the_app_only(tmp_path):
    gh, app = publish(tmp_path, [ASSESSMENT, finding(11), finding(12, text="Second: also here.")])
    assert [(p["line"], p["side"]) for p in line_posts(app)] == [(11, "RIGHT"), (12, "RIGHT")]
    assert gh.writes == []
    summary = summary_of(app)
    assert "2 findings commented on the changed lines" in summary
    assert "Possible Bug: the tag moves" not in summary and "Assessment: low risk" in summary


def test_a_finding_that_cannot_anchor_stays_in_the_summary(tmp_path):
    _, app = publish(tmp_path, [ASSESSMENT, finding(500, text="Late: off the diff."), finding(11)])
    assert [p["line"] for p in line_posts(app)] == [11]
    summary = summary_of(app)
    assert "Late: off the diff" in summary and "1 finding commented" in summary


def test_a_rerun_with_the_same_findings_adds_nothing(tmp_path):
    first = publish(tmp_path, [ASSESSMENT, finding(11)])[1]
    existing = [{"body": line_posts(first)[0]["body"], "user": BOT, "path": FILE["filename"], "line": 11}]
    _, again = publish(tmp_path, [ASSESSMENT, finding(11)], line_comments=existing)
    assert line_posts(again) == []
    assert "1 finding commented" in summary_of(again)  # still accounted for in the summary


def test_a_reworded_finding_and_a_rebase_do_not_pile_up(tmp_path):
    first = publish(tmp_path, [ASSESSMENT, finding(11)])[1]
    existing = [{"body": line_posts(first)[0]["body"], "user": BOT, "path": FILE["filename"], "line": 31}]
    shifted = dict(FILE, patch=PATCH.replace("@@ -10,4 +10,5 @@", "@@ -30,4 +30,5 @@"))
    _, again = publish(tmp_path, [ASSESSMENT, finding(31, text="Different words, same code.")], files=[shifted],
                       line_comments=existing)
    assert line_posts(again) == []


def test_someone_elses_copy_of_the_marker_does_not_hide_a_finding(tmp_path):
    first = publish(tmp_path, [ASSESSMENT, finding(11)])[1]
    forged = [{"body": line_posts(first)[0]["body"], "user": {"login": "someone"}}]
    _, again = publish(tmp_path, [ASSESSMENT, finding(11)], line_comments=forged)
    assert len(line_posts(again)) == 1


def test_a_refused_line_does_not_fail_the_job_and_the_finding_moves_to_the_summary(tmp_path):
    pr, files = pr_and_files([FILE, dict(FILE, filename=OTHER)])
    app = Refusing({}, refuse=(OTHER,))
    gh = FakeGitHub(routes(pr, files=files))
    findings = [ASSESSMENT, finding(11, path=OTHER, text="Refused: x."), finding(11)]
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=findings)), POLICY, False)
    assert [p["path"] for p in line_posts(app)] == [FILE["filename"]]
    summary = summary_of(app)
    assert "Refused: x" in summary and "1 finding commented" in summary


def test_a_failure_listing_the_comments_still_posts_the_summary(tmp_path):
    pr, files = pr_and_files()
    gh = FakeGitHub(dict(routes(pr, files=files), **{"pulls/7/comments": GitHubError(500, "pulls/7/comments")}))
    app = FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=[ASSESSMENT, finding(11)])), POLICY, False)
    assert line_posts(app) == []
    assert "Possible Bug: the tag moves" in summary_of(app)


def test_a_moved_head_posts_nothing(tmp_path):
    pr, files = pr_and_files()
    moved = dict(pr, head=dict(pr["head"], sha="d" * 40))
    gh = FakeGitHub(dict(routes(pr, files=files), **{"pulls/7": moved}))
    app = FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=[ASSESSMENT, finding(11)])), POLICY, False)
    assert app.writes == []


def test_a_dry_run_and_an_unavailable_review_post_nothing(tmp_path):
    gh, app = publish(tmp_path, [ASSESSMENT, finding(11)], dry_run=True)
    assert app.writes == [] and gh.writes == []
    pr, files = pr_and_files()
    gh, app = FakeGitHub(routes(pr, files=files)), FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, review.unavailable("timeout")), POLICY, False)
    assert app.writes == []


def test_the_inline_text_is_the_scrubbed_text_with_nothing_clickable(tmp_path):
    hostile = "see https://evil.example/x @josh #12 <img src=x> ghp_" + "a" * 36
    row = review.result_from_review(review_with(issue_content=hostile, start_line=11))["findings"][1]
    _, app = publish(tmp_path, [ASSESSMENT, row])
    posted = line_posts(app)
    body = posted[0]["body"] if posted else "withheld"
    assert "evil.example" not in body and "@josh" not in body and "<img" not in body and "ghp_" not in body


def test_the_summary_alone_is_unchanged_when_nothing_is_inline():
    result = dict(LOW, findings=[{"file": "f", "text": "Bug: x."}])
    assert fold.advisory(result, SHA) == fold.advisory(result, SHA, ())
    assert "commented on the changed lines" not in fold.advisory(result, SHA)


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x1c", "\x85", " ", " ", "\r"])
def test_odd_line_separators_inside_a_line_do_not_shift_the_numbers(separator):
    patch = f"@@ -1,2 +1,3 @@\n a\n+b{separator}c\n+d"
    assert inline.visible(patch) == {1: "a", 2: f"b{separator}c", 3: "d"}


def test_a_trailing_newline_does_not_add_a_line():
    assert inline.visible("@@ -1,2 +1,2 @@\n a\n+b\n") == {1: "a", 2: "b"}


def test_identical_lines_in_different_blocks_are_different_spots():
    patch = "@@ -1,4 +1,4 @@\n a:\n+  enabled: true\n b:\n+  enabled: true"
    file = dict(FILE, patch=patch)
    first = inline.anchors([ASSESSMENT, finding(2)], [file])[0]["key"]
    second = inline.anchors([ASSESSMENT, finding(4)], [file])[0]["key"]
    assert first != second
    both = inline.anchors([ASSESSMENT, finding(2), finding(4)], [file])
    assert len(both) == 2
