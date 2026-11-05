"""The two steward steps of the AI review job, on either side of PR-Agent.

prepare decides whether the PR gets a review and writes the context file. collect reads
what PR-Agent wrote and leaves one filtered result file for the evaluate job. Neither one
sees the model key.

Environment: GITHUB_REPOSITORY, GITHUB_TOKEN (read-only).
"""

import argparse
import json
import os
import sys
from pathlib import Path

import classify
import render
import review
import steward
from ghapi import GitHub


def render_lines(gh, sha, render_dir, policy):
    """Classifier output: this run's artifact, else the record from an earlier evaluation."""
    exceptions = policy["release_exceptions"]
    stage = render.from_artifact(render_dir, sha, exceptions) if render_dir else steward.recorded_render(gh, sha, policy)
    if stage is None:
        return []
    return [f"verdict: {stage.verdict}", f"reason: {stage.reason}"] + (
        render.object_lines(render_dir, exceptions) if render_dir else [])


def prepare(gh, args, policy):
    """Write the context file. Returns the outputs for the workflow."""
    pr = gh.get(f"pulls/{args.pr}")
    sha, out = pr["head"]["sha"], Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # A closed PR or a newer push: nobody is waiting for this review.
    run = pr["state"] == "open" and review.mode(pr, policy) != review.OFF and args.sha in ("", sha)
    if run:
        evidence = out / review.EVIDENCE_FILE
        text = evidence.read_text(errors="replace") if evidence.is_file() else ""
        lines = render_lines(gh, sha, args.render_dir, policy)
        (out / review.CONTEXT_FILE).write_text(review.context(pr, lines, text, policy))
    return {"run": "true" if run else "false", "sha": sha}


def collect(args):
    """Turn PR-Agent's exit status and output into result.json for this commit."""
    out = Path(args.out)
    result = review.unavailable(args.status) if args.status != "ok" else None
    if result is None:
        try:
            raw = json.loads((out / review.REVIEW_FILE).read_text())
            result = review.result_from_review(raw.get("review") if isinstance(raw, dict) else None)
        except FileNotFoundError:
            result = review.unavailable("no-output")
        except (OSError, ValueError):
            result = review.unavailable("unparseable")
    (out / review.RESULT_FILE).write_text(json.dumps(dict(result, head_sha=args.sha)))
    print(json.dumps({"sha": args.sha[:12], "available": result["available"], "risk": result.get("risk"),
                      "recommendation": result.get("recommendation"), "reason": result.get("reason")}))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["prepare", "collect"])
    parser.add_argument("--out", required=True, help="the review directory")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--sha", default="", help="the commit under review")
    parser.add_argument("--render-dir", help="this run's render artifact")
    parser.add_argument("--status", default="failed", help="what happened to PR-Agent")
    args = parser.parse_args(argv)
    if args.step == "collect":
        collect(args)
        return 0
    policy = classify.load_policy(steward.POLICY)
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    outputs = prepare(gh, args, policy)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
