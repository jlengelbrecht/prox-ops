"""What the review jobs may touch: the key, the token, PR code and the queue."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from test_steward import POLICY

GITHUB = Path(__file__).resolve().parents[3] / ".github"
WORKFLOWS = sorted(GITHUB.glob("workflows/*.y*ml"))
ACTION_FILE = GITHUB / "actions" / "merge-steward-review" / "action.yaml"
ACTION = yaml.safe_load(ACTION_FILE.read_text())
STEWARD = yaml.safe_load((GITHUB / "workflows" / "merge-steward.yaml").read_text())["jobs"]
REVIEW_JOBS = ("review", "advisory-review")
LOCAL_ACTIONS = {
    "./.github/actions/merge-steward-review": ACTION,
    "./.github/actions/merge-steward-ask": yaml.safe_load((GITHUB / "actions" / "merge-steward-ask" / "action.yaml").read_text()),
}
AGENT = next(s for s in ACTION["runs"]["steps"] if s.get("name") == "Run PR-Agent")


def runner_names(job):
    """The labels a job asks for, whichever YAML form it uses."""
    runs_on = job.get("runs-on")
    if isinstance(runs_on, dict):
        runs_on = runs_on.get("labels", runs_on.get("group", ""))
    return [str(x) for x in (runs_on if isinstance(runs_on, list) else [runs_on])]


def steward_runner_jobs():
    """Every job in any workflow that could land on the steward runner.

    A runner chosen by an expression cannot be read here, so it counts as this runner.
    """
    for path in WORKFLOWS:
        for name, job in (yaml.safe_load(path.read_text()).get("jobs") or {}).items():
            if "runs-on" in job and any("gha-prox-ops-steward" in r or "${{" in r for r in runner_names(job)):
                yield f"{path.name}:{name}", job


def all_steps(job):
    steps = list(job["steps"])
    local = [s["uses"] for s in steps if s.get("uses", "").startswith("./")]
    assert set(local) <= set(LOCAL_ACTIONS), "add new local actions to this scan"
    return steps + [step for name in local for step in LOCAL_ACTIONS[name]["runs"]["steps"]]


JOBS = dict(steward_runner_jobs())


def test_the_review_jobs_are_on_the_steward_runner():
    assert {f"merge-steward.yaml:{n}" for n in REVIEW_JOBS} <= set(JOBS)


def test_every_way_of_naming_the_runner_is_scanned():
    for form in (["self-hosted", "gha-prox-ops-steward"], {"labels": ["gha-prox-ops-steward"]}, {"group": "gha-prox-ops-steward"}):
        assert any("gha-prox-ops-steward" in r for r in runner_names({"runs-on": form}))


@pytest.mark.parametrize("name", JOBS)
def test_a_steward_runner_job_cannot_write_or_touch_pr_code(name):
    job = JOBS[name]
    assert job["permissions"] and set(job["permissions"].values()) == {"read"}
    for step in all_steps(job):
        text = json.dumps(step.get("with") or {}) + str(step.get("run") or "")
        assert not re.search(r"pull/|head\.sha|head\.ref|head_ref|merge_commit_sha|refs/pull", text), step.get("name")
        assert "${{" not in str(step.get("run") or ""), step.get("name")
        if "actions/checkout" in step.get("uses", ""):
            assert step["with"]["persist-credentials"] is False
            assert "default_branch" in step["with"]["ref"] and "repository" not in step["with"]


def test_the_key_is_referenced_once_per_review_job_and_used_in_one_step():
    named = re.compile(r"""secrets\s*(?:\.|\[\s*['"])\s*merge_steward_llm_key""", re.IGNORECASE)
    counts = {p.name: len(named.findall(p.read_text())) for p in WORKFLOWS}
    assert {k: v for k, v in counts.items() if v} == {"merge-steward.yaml": 2, "merge-steward-ask.yaml": 1}
    for name, job in JOBS.items():
        assert not re.search(r"toJSON\(\s*secrets|secrets:\s*inherit", yaml.safe_dump(job), re.IGNORECASE), name
    assert "secrets.MERGE_STEWARD_LLM_KEY" not in ACTION_FILE.read_text()
    assert ACTION_FILE.read_text().count("inputs.llm-key") == 1
    assert AGENT["env"]["OPENAI__KEY"] == "${{ inputs.llm-key }}"
    assert not any("inherit" in str(j.get("secrets")) for j in STEWARD.values())


def test_the_review_environment_is_not_the_one_with_the_app_key():
    for name, job in JOBS.items():
        assert job.get("environment") == "merge-steward-review", name
    assert STEWARD["evaluate"]["environment"] == "merge-steward"


def test_pr_agent_writes_nothing_and_reads_no_repository_settings():
    env = AGENT["env"]
    assert env["LOG_LEVEL"] == "INFO"  # the load is logged at INFO; the log never leaves the runner
    assert env["CONFIG__PUBLISH_OUTPUT"] == "false" and env["CONFIG__USE_REPO_SETTINGS_FILE"] == "false"
    assert env["OPENAI__API_BASE"].startswith("http://litellm.ai.svc.cluster.local:4000")
    upload = next(s for s in ACTION["runs"]["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["path"].endswith("/result.json")


def test_the_review_directory_is_inside_the_workspace_and_not_hidden():
    # PR-Agent drops a context file outside GITHUB_WORKSPACE, and upload-artifact skips hidden files.
    steps = {s.get("name"): s for s in ACTION["runs"]["steps"]}
    for name in ("Decide and build the context", "Run PR-Agent", "Collect the result"):
        assert "$GITHUB_WORKSPACE/steward-review" in steps[name]["run"]
    assert '"$out/context.md"' in AGENT["run"]
    assert "RUNNER_TEMP/review" not in ACTION_FILE.read_text() and "/.review" not in ACTION_FILE.read_text()
    assert "github.workspace" in steps["Upload the result"]["with"]["path"]


LOADED = "INFO | pr_agent.algo.artifacts:inject_artifact_context:320 - Injected artifact context into tools: ['pr_reviewer']"


@pytest.mark.parametrize("key,exit_code,log,status", [
    ("k", 0, LOADED, "ok"),
    ("k", 0, "reviewed", "failed"),
    ("k", 0, "PR text: Injected artifact context into tools", "failed"),
    ("k", 0, LOADED + "\nWARNING Artifact size budget exhausted before reading 'x'", "failed"),
    ("k", 0, "WARNING Artifact file not found or path rejected: 'x'", "failed"),
    ("k", 0, "WARNING Failed to process artifacts: bad", "failed"),
    ("k", 0, "", "failed"),
    ("k", 1, LOADED, "failed"),
    ("k", 124, LOADED, "timeout"),
    ("k", 124, "", "timeout"),
    ("", 0, "", "missing-key"),
])
def test_the_step_reports_what_happened_to_pr_agent(tmp_path, key, exit_code, log, status):
    """Runs the step's own shell with a stand-in pr-agent."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "pr-agent"
    stub.write_text(f"#!/bin/sh\necho \"{log}\"\nexit {exit_code}\n")
    stub.chmod(0o755)
    (tmp_path / "steward-review").mkdir()
    output = tmp_path / "output"
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "OPENAI__KEY": key, "PR_URL": "u", "RUNNER_TEMP": str(tmp_path),
           "GITHUB_WORKSPACE": str(tmp_path), "GITHUB_OUTPUT": str(output), "REVIEW_TIMEOUT": "5"}
    subprocess.run(["bash", "-c", AGENT["run"]], env=env, check=True, capture_output=True)
    assert output.read_text().strip() == f"status={status}"


def test_reviews_are_cancelled_per_pr_and_gated_by_the_variable():
    for name in REVIEW_JOBS:
        job = STEWARD[name]
        assert "number" in job["concurrency"]["group"] and job["concurrency"]["cancel-in-progress"] is True
        assert "vars.MERGE_STEWARD_AI == 'true'" in job["if"]
        assert "head.repo.full_name == github.repository" in job["if"]
    advisory = STEWARD["advisory-review"]["if"]
    assert "head.repo.fork == false" in advisory and "pull_request.draft == false" in advisory


def test_the_model_is_set_only_in_the_policy():
    model = POLICY["ai"]["model"]
    env = AGENT["env"]
    assert env["CONFIG__MODEL"] == "openai/${{ steps.prepare.outputs.alias }}"
    for key, setting in (("CONFIG__CUSTOM_MODEL_MAX_TOKENS", "context_tokens"), ("CONFIG__MAX_MODEL_TOKENS", "max_prompt_tokens"),
                         ("CONFIG__AI_TIMEOUT", "call_timeout_seconds"), ("REVIEW_TIMEOUT", "review_timeout_seconds")):
        assert env[key] == f"${{{{ steps.prepare.outputs.{setting} }}}}" and setting in model
    text = ACTION_FILE.read_text() + "".join(p.read_text() for p in WORKFLOWS if "merge-steward" in p.name)
    # The alias is also the name of the composite action, its artifact and the environment, so
    # look for it where a model name would go rather than anywhere in the text.
    assert f"openai/{model['alias']}" not in text and f"MODEL: {model['alias']}" not in text
    for name in REVIEW_JOBS:
        assert model["review_timeout_seconds"] + 120 <= STEWARD[name]["timeout-minutes"] * 60


def test_the_prompt_requires_a_reason_for_every_rating():
    text = AGENT["env"]["PR_REVIEWER__EXTRA_INSTRUCTIONS"]
    assert "never empty" in text and '"Assessment"' in text and "why that gives your risk_level" in text


def test_the_reuse_check_holds_no_secret_and_only_a_failure_free_output_skips_the_review():
    gate = STEWARD["review-gate"]
    assert gate["runs-on"] == "ubuntu-latest" and "environment" not in gate
    assert set(gate["permissions"].values()) == {"read"} and "secrets." not in yaml.safe_dump(gate)
    assert "github.event.action == 'synchronize'" in gate["if"]
    assert gate["permissions"].keys() >= {"contents", "pull-requests", "checks"}
    for step in gate["steps"]:
        if "actions/checkout" in step.get("uses", ""):
            assert "default_branch" in step["with"]["ref"] and step["with"]["persist-credentials"] is False
    step = next(s for s in gate["steps"] if s.get("id") == "gate")
    assert step["continue-on-error"] is True and "${{" not in step["run"]
    review = STEWARD["review"]
    assert "review-gate" in review["needs"]
    assert "!cancelled()" in review["if"] and "needs.review-gate.outputs.reuse != 'true'" in review["if"]


def test_the_advisory_reuse_check_holds_no_secret_and_only_its_answer_skips_the_advisory_review():
    gate = STEWARD["advisory-gate"]
    assert gate["runs-on"] == "ubuntu-latest" and "environment" not in gate
    assert set(gate["permissions"].values()) == {"read"} and "secrets." not in yaml.safe_dump(gate)
    condition = " ".join(gate["if"].split())
    for part in ("github.event.action == 'synchronize'", "head.repo.fork == false",
                 "head.repo.full_name == github.repository", "user.login != 'renovate[bot]'"):
        assert part in condition
    for step in gate["steps"]:
        if "actions/checkout" in step.get("uses", ""):
            assert "default_branch" in step["with"]["ref"] and step["with"]["persist-credentials"] is False
    step = next(s for s in gate["steps"] if s.get("id") == "gate")
    assert step["continue-on-error"] is True and "${{" not in step["run"] and "advisory-gate" in step["run"]
    for name in ("advisory-render", "advisory-review"):
        job = STEWARD[name]
        assert "advisory-gate" in job["needs"]
        assert job["if"].startswith("!cancelled()") and "needs.advisory-gate.outputs.reuse != 'true'" in job["if"]
    assert STEWARD["advisory-publish"]["if"] == "needs.advisory-review.result == 'success'"


def test_review_jobs_are_ignored_by_the_checks_stage():
    names = {j["name"] for k, j in STEWARD.items()
             if k in REVIEW_JOBS + ("advisory-publish", "review-gate", "advisory-gate", "advisory-render")}
    assert names <= set(POLICY["ignore_checks"])


def test_the_advisory_comments_come_from_the_app_through_the_steward_environment():
    job = STEWARD["advisory-publish"]
    assert job["environment"] == "merge-steward"
    assert set(job["permissions"].values()) == {"read"}  # the workflow token only reads
    token = next(s for s in job["steps"] if s.get("id") == "app-token")
    assert token["uses"].startswith("actions/create-github-app-token@")
    assert {k for k in token["with"] if k.startswith("permission-")} == {"permission-pull-requests"}
    assert token["with"]["permission-pull-requests"] == "write"
    post = next(s for s in job["steps"] if s.get("name") == "Post the advisory review")
    assert post["env"]["STEWARD_APP_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "${{" not in post["run"] and "--advisory-only" in post["run"]
    # No PR code: only default-branch steward code is checked out.
    for step in job["steps"]:
        if "actions/checkout" in step.get("uses", ""):
            assert "default_branch" in step["with"]["ref"] and step["with"]["persist-credentials"] is False


def _evaluate_if():
    return " ".join(STEWARD["evaluate"]["if"].split())


def test_status_and_check_suite_events_are_narrowed_to_renovate_branches():
    cond = _evaluate_if()
    assert "github.event.context == 'renovate/stability-days'" in cond
    # A status lists at most 10 branches. A full list cannot prove anything, so it still runs.
    assert "github.event.branches == null" in cond  # no list in the payload: do not filter
    assert "github.event.branches[9] != null" in cond
    assert "contains(join(github.event.branches.*.name, ' '), 'renovate/')" in cond
    assert "contains(join(github.event.check_suite.pull_requests.*.head.ref, ' '), 'renovate/')" in cond
    assert "github.event.check_suite.app.id != 5615799" in cond  # the steward's own suites


def test_the_events_that_post_the_required_check_are_not_narrowed():
    cond = _evaluate_if()
    assert "github.event_name == 'schedule'" in cond and "github.event_name == 'workflow_dispatch'" in cond
    assert "github.event_name == 'pull_request_target'" in cond
    for kind in ("opened", "synchronize", "reopened"):
        assert kind in cond
    # Neither filter applies to those: the renovate/ prefix appears only after a status or suite term.
    before_status = cond.split("github.event_name == 'status'")[0]
    assert "renovate/" not in before_status
    assert cond.rstrip(")").endswith("github.event_name == 'workflow_dispatch'")
    assert STEWARD["evaluate"]["concurrency"]["cancel-in-progress"] is False


def test_the_branch_filter_matches_the_renovate_branch_prefix():
    config = (GITHUB / "renovate.json5").read_text() + "".join(p.read_text() for p in (GITHUB / "renovate").glob("*.json5"))
    assert "branchPrefix" not in config  # the default prefix is renovate/
