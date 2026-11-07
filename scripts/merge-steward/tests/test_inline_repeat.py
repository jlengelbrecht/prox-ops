"""The same inline finding must not be posted again when the model rewords it or moves its range."""

import json
from pathlib import Path

import pytest

import inline
import publish
import steward
from ghapi import GitHubError
from test_inline import ASSESSMENT, BOT, FILE, PATCH, Refusing, finding, line_posts, summary_of
from test_review import LOW, artifact
from test_steward import POLICY, FakeGitHub, fresh_head, renovate_pr, routes  # noqa: F401

LIVE = json.loads((Path(__file__).parent / "fixtures" / "repeated_inline_comments_pr1927.json").read_text())
PATH = ".github/workflows/merge-steward-ask.yaml"


def anchor(start, end, path=PATH, key="k"):
    return {"index": 1, "path": path, "start": start, "end": end, "key": key}


def old(line, start, text, key="o", path=PATH, id=1):
    return {"id": id, "path": path, "line": line, "start": start, "key": key, "text": text}


# The five comments the steward actually posted on #1927 in five pushes (keys differ, ranges differ,
# wording differs). Each one after the first must read as a repeat of the ones before it.

def test_each_of_the_five_live_comments_is_a_repeat_of_the_earlier_ones():
    earlier = []
    for n, row in enumerate(LIVE):
        new = anchor(row["start_line"], row["line"], key=f"live{n}")
        if n == 0:
            assert not inline.is_repeat(new, row["text"], earlier)
        else:
            assert inline.is_repeat(new, row["text"], earlier), (n, row["text"][:60])
        earlier.append(old(row["line"], row["start_line"], row["text"], key=f"live{n}", id=row["id"]))


def test_the_live_wordings_are_not_identical_and_the_keys_were_not_stable():
    assert len({row["text"] for row in LIVE}) == 5
    assert len({(row["start_line"], row["line"]) for row in LIVE}) == 4  # 70-77, 74-77, 70-75, 74-77, 70-74


def test_the_live_wordings_read_as_one_point_even_without_the_label():
    texts = [row["text"].split(":", 1)[1] for row in LIVE]
    for a in texts[1:]:
        assert inline.similar(a, texts[0])


# What is not a repeat.

RUNNER = LIVE[0]["text"]


def test_a_different_finding_on_the_same_lines_is_posted():
    other = "Missing Timeout: the publish job has no timeout-minutes, so a stuck app token step can hold a runner for six hours."
    assert not inline.is_repeat(anchor(70, 77), other, [old(77, 70, RUNNER)])


def test_a_different_finding_with_a_different_label_and_few_shared_words_is_posted():
    other = "Cache Key: the dependency cache key ignores the lock file, so stale wheels survive an upgrade."
    assert not inline.is_repeat(anchor(75, 76), other, [old(77, 70, RUNNER)])


def test_the_same_point_far_away_is_posted():
    assert not inline.is_repeat(anchor(120, 121), RUNNER, [old(77, 70, RUNNER)])


@pytest.mark.parametrize("start,end,expected", [(78, 80, True), (82, 83, True), (83, 84, False), (60, 65, True), (55, 64, False),
                                               (70, 77, True), (1, 2, False)])
def test_nearness_is_a_gap_of_at_most_five_lines(start, end, expected):
    assert inline.is_repeat(anchor(start, end), RUNNER, [old(77, 70, RUNNER)]) is expected


def test_another_file_is_never_a_repeat():
    assert not inline.is_repeat(anchor(70, 77, path="scripts/other.py"), RUNNER, [old(77, 70, RUNNER)])


def test_a_comment_on_code_that_has_changed_does_not_count_for_nearness():
    assert not inline.is_repeat(anchor(70, 77), RUNNER, [old(None, None, RUNNER)])


def test_a_resolved_thread_does_not_count_for_nearness_but_the_same_key_still_does():
    resolved = {1}
    assert not inline.is_repeat(anchor(70, 77), RUNNER, [old(77, 70, RUNNER, id=1)], resolved)
    assert inline.is_repeat(anchor(70, 77, key="o"), RUNNER, [old(77, 70, RUNNER, key="o", id=1)], resolved)


def test_the_same_key_is_a_repeat_whatever_the_text_or_position():
    assert inline.is_repeat(anchor(3, 3, key="o"), "Anything else entirely.", [old(None, None, RUNNER, key="o")])


def test_two_findings_in_one_run_are_checked_against_each_other_too():
    first = anchor(70, 72, key="a")
    second = anchor(73, 74, key="b")
    earlier = [old(72, 70, RUNNER, key="a", id=None)]
    assert inline.is_repeat(second, LIVE[1]["text"], earlier)


# The words.

def test_similar_uses_the_label_or_half_the_shorter_findings_words():
    assert inline.similar("Runner Prerequisite: the guard change must be merged first.",
                          "Runner Prerequisite: confirm the runner guard change is applied before enabling.")
    assert not inline.similar("Bug: the timeout is missing from the publish job.", "Bug: the cache key ignores the lock file.")
    assert not inline.similar("Security: tokens are echoed into the log.", "Security: the workflow allows any fork to run.")
    assert inline.similar("Guard: the runner guard change must be merged first.", "Order: merge the runner guard change before enabling.")
    assert not inline.similar("Guard: the runner guard change must be merged first.", "Cache: the lock file is ignored by the cache key.")
    assert not inline.similar("", "Anything: text.") and not inline.similar("A: b", "C: d")


# Who wrote a comment.

def test_only_the_steward_bot_users_root_comments_count():
    marker = inline.INLINE_MARKER.format(key="a" * 24)
    good = {"id": 1, "body": f"x\n{marker}", "user": BOT, "path": "p", "line": 4, "start_line": 2}
    bad = [dict(good, user={"login": "someone", "type": "User"}), dict(good, user={"login": BOT["login"], "type": "User"}),
           dict(good, in_reply_to_id=9), dict(good, body="no marker"), dict(good, performed_via_github_app={"id": 5615799}, user=None)]
    found = inline.steward_inline([good] + bad, BOT["login"])
    assert [(c["id"], c["line"], c["start"]) for c in found] == [(1, 4, 2)]
    assert inline.steward_inline([dict(good, line=None, start_line=None)], BOT["login"])[0]["line"] is None


def test_the_live_api_shape_is_recognised_without_performed_via_github_app():
    digest = "0123456789ab" * 2
    marker = inline.INLINE_MARKER.format(key=digest)
    live = {"id": 4499853907, "body": LIVE[0]["text"] + "\n\nAdvisory AI review. It does not affect any check.\n" + marker,
            "user": {"login": "homebot-bouncer[bot]", "type": "Bot"}, "performed_via_github_app": None,
            "path": PATH, "line": 77, "start_line": 70}
    (found,) = inline.steward_inline([live], POLICY["steward_app_login"])
    assert found["key"] == digest and found["text"] == LIVE[0]["text"]


def test_the_policy_names_the_bot_login_and_the_loader_requires_it(tmp_path):
    import yaml

    from classify import load_policy

    assert POLICY["steward_app_login"] == "homebot-bouncer[bot]"
    raw = yaml.safe_load((Path(__file__).resolve().parents[1] / "policy.yaml").read_text())
    for bad in (None, "", "homebot-bouncer", "bot", 5, "a b[bot]"):
        broken = dict(raw, steward_app_login=bad) if bad is not None else {k: v for k, v in raw.items() if k != "steward_app_login"}
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(broken))
        with pytest.raises(ValueError):
            load_policy(path)


# Through post_advisory, against an in-memory GitHub.

TEXT = "Runner Prerequisite: the guard change must be merged and applied first."
PATCH_LONG = "@@ -1,12 +1,12 @@\n" + "\n".join(f"+line {n} value" for n in range(1, 13))
LONG = {"filename": "kubernetes/apps/a/long.yaml", "status": "modified", "sha": "2" * 40, "patch": PATCH_LONG}


def existing_comment(start, end, text=TEXT, key="a" * 24, id=900):
    body = f"{text}\n\nAdvisory AI review. It does not affect any check.\n{inline.INLINE_MARKER.format(key=key)}"
    return {"id": id, "body": body, "user": BOT, "path": LONG["filename"], "line": end, "start_line": start}


def run(tmp_path, findings, comments, threads=None):
    pr = renovate_pr(login="someone")
    table = routes(pr, files=[LONG], line_comments=comments)
    gh = FakeGitHub(table)
    gh.request = lambda method, path, body=None: (threads() if callable(threads) else threads, "")
    app = FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=findings)), POLICY, False)
    return gh, app


def long_finding(start, end, text):
    return {"file": LONG["filename"], "text": text, "start": start, "end": end}


def graphql(resolved_ids=()):
    nodes = [{"isResolved": True, "comments": {"nodes": [{"databaseId": i}]}} for i in resolved_ids]
    return {"data": {"repository": {"pullRequest": {"reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                                      "nodes": nodes}}}}}


A0 = {"file": LONG["filename"], "text": "Assessment: low risk."}


def test_a_reworded_finding_on_a_moved_range_is_not_posted_again(tmp_path):
    _, app = run(tmp_path, [A0, long_finding(5, 8, "Runner Prerequisite: please confirm the runner guard change is applied.")],
                 [existing_comment(3, 6)], graphql())
    assert line_posts(app) == []
    assert "1 finding commented" in summary_of(app)


def test_a_different_finding_nearby_is_still_posted(tmp_path):
    _, app = run(tmp_path, [A0, long_finding(5, 6, "Timeout: the job has no timeout, so a stuck step holds a runner for hours.")],
                 [existing_comment(3, 6)], graphql())
    assert len(line_posts(app)) == 1


def test_the_same_point_far_from_the_old_comment_is_posted(tmp_path):
    _, app = run(tmp_path, [A0, long_finding(12, 12, TEXT + " Again.")], [existing_comment(1, 2)], graphql())
    assert len(line_posts(app)) == 1


def test_a_resolved_thread_does_not_hold_back_a_new_finding_nearby(tmp_path):
    _, app = run(tmp_path, [A0, long_finding(5, 6, TEXT)], [existing_comment(3, 6, id=900)], graphql([900]))
    assert len(line_posts(app)) == 1


def test_if_threads_cannot_be_read_resolved_ones_count_as_open_and_nothing_fails(tmp_path, capsys):
    def boom():
        raise GitHubError(502, "graphql")

    _, app = run(tmp_path, [A0, long_finding(5, 6, TEXT)], [existing_comment(3, 6)], boom)
    assert line_posts(app) == [] and "could not read review threads" in capsys.readouterr().err
    assert "1 finding commented" in summary_of(app)


def test_no_thread_lookup_when_the_steward_has_not_commented_yet(tmp_path):
    calls = []
    pr = renovate_pr(login="someone")
    gh = FakeGitHub(routes(pr, files=[LONG], line_comments=[]))
    gh.request = lambda *a, **k: calls.append(a) or (graphql(), "")
    app = FakeGitHub({})
    steward.post_advisory(gh, app, pr, artifact(tmp_path, dict(LOW, findings=[A0, long_finding(5, 6, TEXT)])), POLICY, False)
    assert calls == [] and len(line_posts(app)) == 1


def test_two_near_identical_findings_in_one_run_post_one_comment(tmp_path):
    findings = [A0, long_finding(3, 4, TEXT), long_finding(5, 6, "Runner Prerequisite: merge the guard change before enabling this.")]
    _, app = run(tmp_path, findings, [], graphql())
    assert len(line_posts(app)) == 1
    assert "2 findings commented" in summary_of(app)


def test_the_limit_of_three_per_run_still_holds(tmp_path):
    findings = [A0] + [long_finding(n, n, f"Point {n}: unrelated topic number {n} about {w} entirely.")
                       for n, w in ((1, "alpha"), (4, "bravo"), (8, "charlie"), (12, "delta"))]
    _, app = run(tmp_path, findings, [], graphql())
    assert len(line_posts(app)) <= inline.MAX_INLINE


def test_a_person_pasting_our_marker_does_not_hide_a_finding(tmp_path):
    fake = existing_comment(3, 6)
    fake["user"] = {"login": "someone", "type": "User"}
    _, app = run(tmp_path, [A0, long_finding(5, 6, TEXT)], [fake], graphql())
    assert len(line_posts(app)) == 1


def test_graphql_goes_to_the_api_host_as_a_post_with_the_pull_request_number():
    seen = {}

    class Gh:
        repo, api = "o/r", "https://api.github.com"

        def request(self, method, path, body=None):
            seen.update(method=method, path=path, body=body)
            return graphql([7, 8]), ""

    assert publish.resolved_comment_ids(Gh(), 12) == {7, 8}
    assert seen["method"] == "POST" and seen["path"] == "https://api.github.com/graphql"
    assert seen["body"]["variables"] == {"owner": "o", "name": "r", "number": 12, "after": None}
