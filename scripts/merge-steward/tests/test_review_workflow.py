"""What the review workflows may touch: the key, the token, PR code and the queue."""

import json
import re
from pathlib import Path

import yaml

from test_steward import POLICY

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def workflows():
    return {name: yaml.safe_load((WORKFLOWS / name).read_text())
            for name in ("merge-steward.yaml", "merge-steward-review.yaml")}


def test_the_key_reaches_one_step_and_the_two_review_calls_only():
    outer, inner = workflows()["merge-steward.yaml"], workflows()["merge-steward-review.yaml"]
    assert re.findall(r"MERGE_STEWARD_LLM_KEY", (WORKFLOWS / "merge-steward.yaml").read_text()).__len__() == 4
    callers = {n for n, j in outer["jobs"].items() if "MERGE_STEWARD_LLM_KEY" in json.dumps(j)}
    assert callers == {"review", "advisory-review"}
    steps = [s for s in inner["jobs"]["review"]["steps"] if "MERGE_STEWARD_LLM_KEY" in json.dumps(s)]
    assert [s.get("name") for s in steps] == ["Run PR-Agent"] and "secrets.MERGE_STEWARD_LLM_KEY" in steps[0]["env"]["OPENAI__KEY"]
    assert "inherit" not in json.dumps(outer["jobs"]["review"].get("secrets"))


def test_the_review_job_cannot_write_and_never_sees_pr_code():
    job = workflows()["merge-steward-review.yaml"]["jobs"]["review"]
    assert set(job["permissions"].values()) == {"read"} and job["runs-on"] == "gha-prox-ops-steward"
    checkout = next(s for s in job["steps"] if "actions/checkout" in s.get("uses", ""))
    assert "default_branch" in checkout["with"]["ref"] and checkout["with"]["persist-credentials"] is False
    assert not any("head.sha" in str(s.get("with")) or "pull/" in str(s.get("with")) for s in job["steps"])
    agent = next(s for s in job["steps"] if s.get("name") == "Run PR-Agent")
    assert agent["env"]["CONFIG__PUBLISH_OUTPUT"] == "false" and agent["env"]["CONFIG__USE_REPO_SETTINGS_FILE"] == "false"
    assert agent["env"]["OPENAI__API_BASE"].startswith("http://litellm.ai.svc.cluster.local:4000")
    upload = next(s for s in job["steps"] if "upload-artifact" in s.get("uses", ""))
    assert upload["with"]["path"].endswith("/result.json")


def test_reviews_are_cancelled_per_pr_and_gated_by_the_variable():
    outer = workflows()["merge-steward.yaml"]["jobs"]
    inner = workflows()["merge-steward-review.yaml"]["jobs"]["review"]
    assert "inputs.pr" in inner["concurrency"]["group"] and inner["concurrency"]["cancel-in-progress"] is True
    for name in ("review", "advisory-review"):
        assert "vars.MERGE_STEWARD_AI == 'true'" in outer[name]["if"]
    assert "head.repo.fork == false" in outer["advisory-review"]["if"]
    assert "head.repo.full_name == github.repository" in outer["advisory-review"]["if"]
    assert "head.repo.full_name == github.repository" in outer["review"]["if"]


def test_review_jobs_are_ignored_by_the_checks_stage():
    for name in ("merge-steward review / pr-agent", "merge-steward advisory review / pr-agent", "merge-steward advisory"):
        assert name in POLICY["ignore_checks"]
