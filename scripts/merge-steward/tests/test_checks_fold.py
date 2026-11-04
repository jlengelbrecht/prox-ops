from pathlib import Path

from checks import evaluate
from classify import HOLD, NEEDS, PASS, Stage, load_policy
from fold import MARKER, comment, fold, summary

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")


def run(name, status="completed", conclusion="success", app=15368):
    return {"name": name, "status": status, "conclusion": conclusion, "app": {"id": app}}


REQUIRED = [run(c["name"], app=c["app_id"]) for c in POLICY["required_checks"]]


def test_green_checks_pass_and_own_runs_are_ignored():
    runs = REQUIRED + [run("Flux Local Diff", conclusion="skipped"),
            run("merge-steward", conclusion="failure"), run("merge-steward evaluate", status="in_progress")]
    stage, pending = evaluate(runs, [{"context": "x", "state": "success"}], POLICY)
    assert stage.verdict == PASS and pending == []


def test_soak_status_pending_holds():
    stage, _ = evaluate(REQUIRED, [{"context": "renovate/stability-days", "state": "pending"}], POLICY)
    assert stage.verdict == HOLD and "soaking" in stage.reason


def test_failing_non_required_check_is_needs_human():
    stage, _ = evaluate(REQUIRED + [run("verify", conclusion="failure")], [], POLICY)
    assert stage.verdict == NEEDS and "verify" in stage.reason
    stage, _ = evaluate([], [{"context": "ci/x", "state": "error"}], POLICY)
    assert stage.verdict == NEEDS


def test_running_checks_hold_and_are_reported_as_pending():
    stage, pending = evaluate(REQUIRED + [run("Flux Local Test", status="queued")], [], POLICY)
    assert stage.verdict == HOLD and pending == ["Flux Local Test"]


def test_required_checks_must_exist_be_green_and_come_from_their_app():
    deadline = POLICY["required_checks_deadline_minutes"]
    stage, pending = evaluate(REQUIRED[1:], [], POLICY, age_minutes=5)
    assert stage.verdict == HOLD and REQUIRED[0]["name"] in stage.reason and REQUIRED[0]["name"] in pending
    stage, _ = evaluate(REQUIRED[1:], [], POLICY, age_minutes=deadline + 1)
    assert stage.verdict == NEEDS and "never reported" in stage.reason
    foreign = [dict(REQUIRED[0], app={"id": 1})] + REQUIRED[1:]
    assert evaluate(foreign, [], POLICY)[0].verdict == HOLD


def test_empty_check_set_never_passes():
    assert evaluate([], [], POLICY)[0].verdict == HOLD
    assert evaluate([], [], POLICY, age_minutes=10**6)[0].verdict == NEEDS
    assert evaluate([], [{"context": "x", "state": "success"}], POLICY)[0].verdict != PASS


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


def test_required_check_across_suites_success_and_skipped_passes():
    # Multi-Layer Bug Detection appears in two suites; a skipped twin must not block.
    runs = REQUIRED + [dict(REQUIRED[0], conclusion="skipped"), dict(REQUIRED[1], conclusion="neutral")]
    assert evaluate(runs, [], POLICY)[0].verdict == PASS


def test_required_check_across_suites_success_and_failure_fails():
    for bad in ("failure", "cancelled", "timed_out", "action_required", "stale"):
        stage, _ = evaluate(REQUIRED + [dict(REQUIRED[0], conclusion=bad)], [], POLICY)
        assert stage.verdict == NEEDS and REQUIRED[0]["name"] in stage.reason, bad


def test_required_check_skipped_only_holds_then_needs_human():
    runs = [dict(REQUIRED[0], conclusion="skipped"), dict(REQUIRED[0], conclusion="skipped")] + REQUIRED[1:]
    stage, pending = evaluate(runs, [], POLICY, age_minutes=5)
    assert stage.verdict == HOLD and REQUIRED[0]["name"] in pending
    stage, _ = evaluate(runs, [], POLICY, age_minutes=POLICY["required_checks_deadline_minutes"] + 1)
    assert stage.verdict == NEEDS and "never reported" in stage.reason


def test_required_check_still_running_in_another_suite_holds():
    runs = REQUIRED + [dict(REQUIRED[0], status="in_progress", conclusion=None)]
    assert evaluate(runs, [], POLICY)[0].verdict == HOLD
