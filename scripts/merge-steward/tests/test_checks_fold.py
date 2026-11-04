from pathlib import Path

from checks import evaluate
from classify import HOLD, NEEDS, PASS, Stage, load_policy
from fold import MARKER, comment, fold, summary

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


def run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def test_green_checks_pass_and_own_runs_are_ignored():
    runs = [run("Flux Local Success"), run("Labeler", conclusion="skipped"),
            run("merge-steward", conclusion="failure"), run("merge-steward evaluate", status="in_progress")]
    stage, pending = evaluate(runs, [{"context": "x", "state": "success"}], POLICY)
    assert stage.verdict == PASS and pending == []


def test_soak_status_pending_holds():
    stage, _ = evaluate([run("Flux Local Success")], [{"context": "renovate/stability-days", "state": "pending"}], POLICY)
    assert stage.verdict == HOLD and "soaking" in stage.reason


def test_failing_non_required_check_is_needs_human():
    stage, _ = evaluate([run("Flux Local Success"), run("verify", conclusion="failure")], [], POLICY)
    assert stage.verdict == NEEDS and "verify" in stage.reason
    stage, _ = evaluate([], [{"context": "ci/x", "state": "error"}], POLICY)
    assert stage.verdict == NEEDS


def test_running_checks_hold_and_are_reported_as_pending():
    stage, pending = evaluate([run("Flux Local Test", status="queued")], [], POLICY)
    assert stage.verdict == HOLD and pending == ["Flux Local Test"]


def test_fold_defaults_to_needs_human():
    assert fold([])[0] == NEEDS
    assert fold([Stage("identity", PASS, ""), Stage("policy", PASS, "")])[0] == NEEDS
    assert fold([Stage("identity", "maybe", "")])[0] == NEEDS


def test_fold_order_needs_human_beats_hold_beats_pass():
    stages = [Stage("identity", PASS, ""), Stage("policy", NEEDS, "major"), Stage("checks", HOLD, "soak")]
    assert fold(stages) == (NEEDS, "policy: major")
    stages[1] = Stage("policy", PASS, "")
    assert fold(stages)[0] == HOLD
    stages[2] = Stage("checks", PASS, "")
    assert fold(stages)[0] == PASS


def test_override_passes_with_the_admin_named():
    assert fold([Stage("identity", NEEDS, "x")], override="admin") == (PASS, "human-approved by admin")


def test_comment_is_sticky_short_and_explains_the_override():
    stages = [Stage("identity", PASS, "ok"), Stage("policy", NEEDS, "major")]
    body = comment(stages, NEEDS, "policy: major", "f" * 40, "steward/human-approved")
    assert body.startswith(MARKER) and len(body.splitlines()) <= 40
    assert "steward/human-approved" in body


def test_untrusted_reasons_cannot_inject_markdown():
    hostile = "@someone ![x](https://e/i.png) <img> | col\n## head `code`"
    text = summary([Stage("policy", NEEDS, hostile)], NEEDS, hostile)
    for bad in ("@", "![", "<img", "`", "| col", "\n## head"):
        assert bad not in text
