"""Reusing a review across a rebase, upstream release notes, and the Assessment entry's length."""

import json

import pytest

import classify
import fold
import review
import review_job
import steward
from classify import PASS
from ghapi import GitHubError
from test_review import LOW, artifact, evaluate
from test_steward import FILES, OWN_PUSHES, POLICY, REQUIRED, SHA, FakeGitHub, fresh_head, recorded, renovate_pr, routes  # noqa: F401

ALIAS = POLICY["ai"]["model"]["alias"]
OLD, OLDER = "a" * 40, "b" * 40
REVIEWED = dict(LOW, model=ALIAS)
FP = steward.fingerprint(FILES)


def earlier_run(text, app=5615799):
    return {"id": 1, "name": "merge-steward", "app": {"id": app}, "output": {"text": text}}


def rebased(old_runs, activity=None, **kw):
    """Routes for the head after a rebase, with old_runs on the commit before it."""
    data = routes(renovate_pr(), runs=REQUIRED + [recorded()], activity=activity or (
        OWN_PUSHES + [{"activity_type": "force_push", "after": OLD, "timestamp": "2026-11-04T10:00:00Z",
                       "actor": {"login": "renovate[bot]"}}]), **kw)
    data[f"commits/{OLD}/check-runs"] = {"check_runs": old_runs}
    return data


def reused(old_runs, pr=None, **kw):
    pr = pr or renovate_pr()
    return steward.reused_review(FakeGitHub(rebased(old_runs, **kw)), pr, kw.get("files") or FILES, POLICY)


def test_a_rebase_with_the_same_diff_reuses_the_review():
    result = reused([earlier_run(review.record(OLD, REVIEWED, FP))])
    assert result["risk"] == "low" and result["reused_from"] == OLD


def test_a_reused_review_stands_in_the_verdict_and_can_be_reused_again():
    data = rebased([earlier_run(review.record(OLD, REVIEWED, FP))])
    gh, app = FakeGitHub(data), FakeGitHub({})
    assert steward.evaluate(gh, app, renovate_pr(), POLICY, 0, False, None, None, True, None) == PASS
    check = next(w[2] for w in app.writes if w[1] == "check-runs")
    assert f"commit {OLD[:12]} has the same diff" in check["output"]["summary"]
    # The next rebase sees this head's record, and still names the review that was really run.
    again = review.reusable(check["output"]["text"], SHA, FP, POLICY)
    assert again["model"] == ALIAS and again["reused_from"] == OLD


def test_a_rebase_with_a_high_rating_keeps_it():
    data = rebased([earlier_run(review.record(OLD, dict(REVIEWED, risk="high"), FP))])
    assert steward.evaluate(FakeGitHub(data), FakeGitHub({}), renovate_pr(), POLICY, 0, False, None, None, True, None) == "needs-human"


@pytest.mark.parametrize("text", [
    review.record(OLD, REVIEWED, "f" * 64),                   # another diff
    review.record(OLD, dict(REVIEWED, model="other"), FP),    # another model
    review.record(OLD, LOW, FP),                              # no model named
    review.record(OLD, REVIEWED),                             # a record from before fingerprints
    review.record(OLD, review.unavailable("timeout"), FP),    # a failed review
    review.record(SHA, REVIEWED, FP),                         # names another commit
    "<!-- merge-steward-llm sha=" + OLD + " fp=" + FP + " data=!!! -->",
    f"prefix <!-- merge-steward-llm sha={OLD} fp={FP} data=e30= -->",
    "",
    None,
])
def test_any_doubt_about_the_record_is_a_new_review(text):
    assert reused([earlier_run(text)]) is None


def test_only_the_stewards_app_can_supply_the_record():
    assert reused([earlier_run(review.record(OLD, REVIEWED, FP), app=1)]) is None


def test_the_newest_record_decides_and_a_failed_review_is_not_skipped_over():
    stale = {"activity_type": "force_push", "after": OLDER, "timestamp": "2026-11-05T00:00:00Z", "actor": {}}
    new = {"activity_type": "force_push", "after": OLD, "timestamp": "2026-11-05T01:00:00Z", "actor": {}}
    data = rebased([earlier_run(review.record(OLD, review.unavailable("timeout"), FP))], activity=[stale, new])
    data[f"commits/{OLDER}/check-runs"] = {"check_runs": [earlier_run(review.record(OLDER, REVIEWED, FP))]}
    assert steward.reused_review(FakeGitHub(data), renovate_pr(), FILES, POLICY) is None
    data[f"commits/{OLD}/check-runs"] = {"check_runs": []}  # superseded before any evaluation
    assert steward.reused_review(FakeGitHub(data), renovate_pr(), FILES, POLICY)["reused_from"] == OLDER


def test_only_recent_heads_are_searched():
    pushes = [{"activity_type": "force_push", "after": f"{n:040x}", "timestamp": f"2026-11-05T{n:02d}:00:00Z", "actor": {}}
              for n in range(30)]
    data = rebased([], activity=pushes)
    data.update({f"commits/{p['after']}/check-runs": {"check_runs": []} for p in pushes})
    gh = FakeGitHub(data)
    assert steward.reused_review(gh, renovate_pr(), FILES, POLICY) is None
    assert sum("check-runs" in path for path in gh.reads) == review.REUSE_LOOKBACK


def test_a_diff_that_may_be_incomplete_is_never_matched():
    text = review.record(OLD, REVIEWED, FP)
    assert reused([earlier_run(text)], pr=dict(renovate_pr(), changed_files=2)) is None
    bare = [{"filename": "a.yaml", "status": "modified"}]
    assert steward.diff_fingerprint(dict(renovate_pr(), changed_files=1), bare) is None


def test_an_unreadable_old_commit_or_log_is_a_new_review():
    data = rebased([])
    data[f"commits/{OLD}/check-runs"] = GitHubError(404, "gone")
    assert steward.reused_review(FakeGitHub(data), renovate_pr(), FILES, POLICY) is None
    data["activity"] = GitHubError(500, "down")
    assert steward.reused_review(FakeGitHub(data), renovate_pr(), FILES, POLICY) is None


def test_the_record_written_for_a_new_review_can_be_reused(tmp_path):
    (tmp_path / "review.json").write_text(json.dumps({"review": {"risk_level": "low", "merge_recommendation": "no_concerns_found"}}))
    review_job.collect(type("Args", (), dict(out=str(tmp_path), status="ok", sha=SHA, model=ALIAS)))
    _, check, _, _ = evaluate(renovate_pr(), str(tmp_path))
    assert review.reusable(check["output"]["text"], SHA, FP, POLICY)["reused_from"] == SHA


def test_a_result_with_a_forged_provenance_field_loses_it():
    result = review.checked(dict(LOW, model="x y", reused_from="not-a-sha"))
    assert "model" not in result and "reused_from" not in result


def gate_args(sha=SHA):
    return type("Args", (), dict(pr=7, sha=sha))


def test_the_gate_answers_only_for_the_current_head_of_an_open_renovate_pr():
    data = rebased([earlier_run(review.record(OLD, REVIEWED, FP))])
    assert review_job.gate(FakeGitHub(data), gate_args(), POLICY) is True
    assert review_job.gate(FakeGitHub(data), gate_args("d" * 40), POLICY) is False
    data["pulls/7"] = dict(renovate_pr(), state="closed")
    assert review_job.gate(FakeGitHub(data), gate_args(), POLICY) is False
    data["pulls/7"] = renovate_pr(login="someone")
    assert review_job.gate(FakeGitHub(data), gate_args(), POLICY) is False


def test_a_gate_that_crashes_says_review(monkeypatch, tmp_path):
    outputs = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setattr(review_job, "gate", lambda *a: 1 / 0)
    assert review_job.main(["gate", "--pr", "7", "--sha", SHA]) == 0
    assert outputs.read_text() == "reuse=false\n"


TABLE = """This PR contains the following updates:

| Package | Type | Update | Change |
|---|---|---|---|
| [external-dns](https://redirect.github.com/kubernetes-sigs/external-dns) | HelmChart | minor | `1.21.1` → `1.23.0` |
| [ghcr.io/coredns/charts/coredns](https://coredns.io) ([source](https://redirect.github.com/coredns/coredns)) | HelmChart | minor | `1.46.2` → `1.50.1` |
| [docker.io/mariadb](https://hub.docker.com/_/mariadb) ([source](https://redirect.github.com/MariaDB/mariadb-docker)) | | minor | `11.4.8` → `11.8.9` |
| ghcr.io/jlengelbrecht/obsidian-mcp | | digest | `fe17d0d` → `a533e03` |
| [hashicorp/setup-terraform](https://redirect.github.com/hashicorp/setup-terraform) | action | pinDigest |  → `b9cd54a` |

---

| [late](https://github.com/evil/repo) | | | `1.0.0` → `2.0.0` |
"""


class Releases(FakeGitHub):
    api = "https://api.github.com"


def test_targets_come_from_the_policy_map_then_the_table_and_skip_digests():
    targets = review.upstream_targets(TABLE, POLICY)
    assert targets == [
        {"repo": "kubernetes-sigs/external-dns", "version": "1.23.0", "tags": ["external-dns-helm-chart-1.23.0"]},
        {"repo": "coredns/helm", "version": "1.50.1", "tags": ["coredns-1.50.1"]},
        {"repo": "MariaDB/mariadb-docker", "version": "11.8.9", "tags": ["v11.8.9", "11.8.9"]},
    ]


@pytest.mark.parametrize("package", [
    "[x](https://evil.example/owner/repo)",
    "[x](https://github.com.evil.example/owner/repo)",
    "[x](https://redirect.github.com/../../user)",
    "[x](http://github.com/owner/repo)",
    "[x](https://api.github.com/owner/repo)",
    "[x](https://evil.example/?https://github.com)",
    "plain-name-with-no-link",
])
def test_pr_text_cannot_name_anything_but_a_github_repo(package):
    assert review.upstream_targets(f"| {package} | minor | `1.0.0` → `2.0.0` |", POLICY) == []


def test_a_version_cannot_leave_the_tag_path():
    row = "| [x](https://github.com/o/r) | minor | `1.0.0` → `2.0.0/../../../user` |"
    assert review.upstream_targets(row, POLICY) == []


def test_a_row_with_the_wrong_shape_is_skipped():
    assert review.upstream_targets("| | minor | `1.0.0` → `2.0.0` |\nnot a table `1.0.0` → `2.0.0`", POLICY) == []
    assert review.upstream_targets(None, POLICY) == []


def tetragon_pr(login="renovate[bot]"):
    return dict(renovate_pr(login=login), body="| quay.io/cilium/tetragon | minor | `v1.6.1` → `v1.8.0` |\n\n---\n")


URL = "https://api.github.com/repos/cilium/tetragon/releases/tags/"


def test_notes_are_fetched_from_github_only_and_labelled_as_untrusted():
    gh = Releases({URL + "v1.8.0": {"tag_name": "v1.8.0", "body": "<!-- x -->Removes the old flag." + "n" * 50000}})
    found = review_job.upstream_notes(gh, tetragon_pr(), POLICY)
    assert gh.reads == [URL + "v1.8.0"]
    text = review.context(tetragon_pr(), [], "", POLICY, found)
    limit = POLICY["ai"]["context_chars"]["release_notes"]
    assert "untrusted third-party text" in text and "### cilium/tetragon v1.8.0" in text and "<!--" not in text
    assert "Removes the old flag." in text and "not the ones in between" in text and text.count("n" * 10) * 10 <= limit
    assert review.NO_NOTES not in text
    assert review.context_stats(tetragon_pr(), [], "", found)["upstream_chars"] > 50000


def test_several_releases_share_the_bound():
    found = [{"repo": f"o/r{n}", "version": "1.0", "tag": "v1.0", "tags": [], "body": str(n) * 50000} for n in range(3)]
    limit = POLICY["ai"]["context_chars"]["release_notes"]
    text = review.context(tetragon_pr(), [], "", POLICY, found)
    assert all(str(n) * 1000 in text for n in range(3)) and len(text) < limit + 500


def test_notes_in_the_pr_body_are_not_looked_up():
    pr = dict(tetragon_pr(), body=tetragon_pr()["body"] + "### Release Notes\nFixed.")
    gh = Releases({})
    assert review_job.upstream_notes(gh, pr, POLICY) is None and gh.reads == []
    assert "Fixed." in review.context(pr, [], "", POLICY, None)


def test_a_pr_outside_the_verdict_scope_is_not_looked_up():
    gh = Releases({})
    assert review_job.upstream_notes(gh, tetragon_pr(login="someone"), POLICY) is None and gh.reads == []


def test_the_context_says_when_nothing_was_found():
    missing = review.context(tetragon_pr(), [], "", POLICY, review_job.upstream_notes(Releases({}), tetragon_pr(), POLICY))
    assert review.NO_NOTES in missing and "tried cilium/tetragon 1.8.0" in missing
    unknown = review.context(renovate_pr(), [], "", POLICY, [])
    assert review.NO_NOTES in unknown and "No upstream GitHub repository is known" in unknown
    assert review.context(renovate_pr(), [], "", POLICY).count("upstream") == 0


@pytest.mark.parametrize("answer", [GitHubError(403, "rate limit"), OSError("no route"), {"tag_name": "other", "body": "x"},
                                    {"tag_name": "v1.8.0", "body": None}, []])
def test_a_failed_lookup_never_stops_the_review(answer):
    found = review_job.upstream_notes(Releases({URL + "v1.8.0": answer}), tetragon_pr(), POLICY)
    assert [f["body"] for f in found] == [""]
    assert "tried cilium/tetragon 1.8.0" in review.context(tetragon_pr(), [], "", POLICY, found)


@pytest.mark.parametrize("entry", [{"repo": "a"}, {"repo": "../a/b"}, {"repo": "a/b", "tag": "v1"}, {"repo": "a/b", "tag": "x y{version}"},
                                   {"repo": "a/b", "url": "https://evil.example"}, "a/b"])
def test_the_policy_map_is_validated(entry):
    assert not classify._upstream_entry(entry)


def issue(content, header="Assessment"):
    return {"risk_level": "low", "merge_recommendation": "no_concerns_found", "key_issues_to_review": [
        {"relevant_file": FILES[0]["filename"], "issue_header": header, "issue_content": content}]}


def sentences(n):
    return " ".join(f"Sentence number {i} says something about the change." for i in range(n))


def test_the_assessment_keeps_more_room_and_ends_at_a_sentence():
    text = review.result_from_review(issue(sentences(40)))["findings"][0]["text"]
    assert 400 < len(text) <= review.ASSESSMENT_LIMIT and text.endswith(".")
    assert sentences(10) in text and text.count("Sentence number") == text.count("change.")


def test_a_short_assessment_is_unchanged():
    assert review.result_from_review(issue("Bumps v1.2.3 to v1.2.4."))["findings"][0]["text"] == "Assessment: Bumps v1.2.3 to v1.2.4."


def test_one_endless_sentence_is_cut_at_a_word_and_marked():
    text = review.result_from_review(issue("word " * 400))["findings"][0]["text"]
    assert len(text) <= review.ASSESSMENT_LIMIT and text.endswith("word...")


def test_only_the_first_entry_gets_the_room():
    other = review.result_from_review(issue(sentences(40), header="Risk"))["findings"][0]["text"]
    assert len(other) == review.TEXT_LIMIT
    two = issue(sentences(40))
    two["key_issues_to_review"].append(dict(two["key_issues_to_review"][0]))
    rows = review.result_from_review(two)["findings"]
    assert len(rows[0]["text"]) > review.TEXT_LIMIT >= len(rows[1]["text"])


def test_the_assessment_survives_the_record_and_the_comment_whole():
    result = review.result_from_review(issue(sentences(40)))
    stored = review.from_record(review.record(SHA, result), SHA)
    assert stored["findings"] == result["findings"]
    assert result["findings"][0]["text"] in "\n".join(fold.ai_lines(result))


def test_the_longer_limit_still_scrubs():
    secret = "ghp_" + "a" * 30
    assert review.result_from_review(issue(f"Leaks {secret}."))["findings"][0]["text"] == review.WITHHELD_CREDENTIAL
    text = review.result_from_review(issue(f"{'Fine. ' * 20}See https://evil.example/x and @josh."))["findings"][0]["text"]
    assert "evil.example" not in text and "@" not in text
