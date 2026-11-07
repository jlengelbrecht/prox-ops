"""The two steward steps of the AI review job, on either side of PR-Agent.

gate runs first, on a GitHub-hosted runner, and says whether a rebase left the diff of an
earlier review unchanged, so that no steward runner is queued for nothing. prepare decides
whether the PR gets a review and writes the context file. collect reads what PR-Agent wrote
and leaves one filtered result file for the evaluate job. None of them sees the model key.

Environment: GITHUB_REPOSITORY, GITHUB_TOKEN (read-only).
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import classify
import render
import review
import steward
from ghapi import GitHub, GitHubError


NO_RENDER = "render: not run, because the PR changes nothing under kubernetes/ or tests/flux-local/"
# The classifier's allowlist was written for dependency bumps. On any other PR it still lists
# what the change does to the rendered cluster, which is the point, but a flag is not a defect.
ADVISORY_NOTE = ("note: this PR is not a dependency update. A 'flagged' line names a kind of cluster change "
                 "(added or removed object, RBAC, network policy, Job template...) to weigh, not a defect.")


def render_lines(gh, sha, render_dir, policy, advisory=False):
    """Classifier output: this run's artifact, else the record from an earlier evaluation.

    An artifact for another commit, or none at all, gives no lines. A render that was skipped
    because the PR does not touch the cluster says so, so the model does not guess.
    """
    lines = [_plain(line) for line in _render_lines(gh, sha, render_dir, policy)]
    return ([ADVISORY_NOTE] + lines if lines and advisory and lines != [NO_RENDER] else lines)


# Object kinds and names come from the PR's manifests. Only plain characters reach the model as
# classifier output, on one line each, so a name cannot pose as another line or as an instruction.
_UNPLAIN = re.compile(r"[^A-Za-z0-9._:/ ()+,=-]")
MAX_RENDER_BYTES = 20_000_000
TOO_LARGE = "render: the output was too large to summarize"


def _plain(line):
    return " ".join(_UNPLAIN.sub("?", str(line)).split())[:200]


def _render_lines(gh, sha, render_dir, policy):
    exceptions = policy["release_exceptions"]
    if render_dir:
        sizes = [(Path(render_dir) / f"{n}.json") for n in ("helmrelease", "kustomization")]
        if any(f.is_file() and f.stat().st_size > MAX_RENDER_BYTES for f in sizes):
            return [TOO_LARGE]
        try:
            meta = json.loads((Path(render_dir) / "meta.json").read_text())
        except (OSError, ValueError):
            meta = {}
        if isinstance(meta, dict) and meta.get("head_sha") == sha and meta.get("skipped") is True:
            return [NO_RENDER]
    stage = render.from_artifact(render_dir, sha, exceptions) if render_dir else steward.recorded_render(gh, sha, policy)
    if stage is None:
        return []
    return [f"verdict: {stage.verdict}", f"reason: {stage.reason}"] + (
        render.object_lines(render_dir, exceptions) if render_dir else [])


def upstream_notes(gh, pr, policy):
    """Upstream release text for a Renovate PR with no notes of its own, or None.

    A failed lookup only leaves the context saying nothing was found. The review goes on.
    """
    if review.mode(pr, policy) != review.VERDICT or review.release_notes(pr.get("body")):
        return None
    found = [dict(t, tag="", body="") for t in review.upstream_targets(pr.get("body"), policy)]
    try:
        for target in found:
            for tag in target["tags"]:
                try:
                    release = gh.get(f"{gh.api}/repos/{target['repo']}/releases/tags/{quote(tag)}")
                except GitHubError as err:
                    if err.status != 404:
                        raise
                    continue
                if release.get("tag_name") == tag and isinstance(release.get("body"), str):
                    target.update(tag=tag, body=release["body"])
                    break
    except Exception as err:  # the type only: the message can quote a repository name
        print(f"upstream release lookup stopped: {type(err).__name__}", file=sys.stderr)
    return found


def gate(gh, args, policy):
    """True when this head can stand on an earlier review. Any doubt is False: a new review."""
    pr = gh.get(f"pulls/{args.pr}")
    if pr["state"] != "open" or pr["head"]["sha"] != args.sha or review.mode(pr, policy) != review.VERDICT:
        return False
    return steward.reused_review(gh, pr, gh.paginate(f"pulls/{args.pr}/files"), policy) is not None


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
        lines = render_lines(gh, sha, args.render_dir, policy, review.mode(pr, policy) == review.ADVISORY)
        upstream = upstream_notes(gh, pr, policy)
        (out / review.CONTEXT_FILE).write_text(review.context(pr, lines, text, policy, upstream))
        print(json.dumps({"context": review.context_stats(pr, lines, text, upstream)}))
        # PR-Agent drops an empty context file without a word.
        assert (out / review.CONTEXT_FILE).stat().st_size > 0
    model = policy["ai"]["model"]  # exactly the validated keys; run, sha and url come last
    return {**model, "run": "true" if run else "false", "sha": sha, "url": pr["html_url"]}


def collect(args):
    """Turn PR-Agent's exit status and output into result.json for this commit."""
    out = Path(args.out)
    result = review.unavailable(args.status) if args.status != "ok" else None
    answer = {}
    if result is None:
        try:
            raw = json.loads((out / review.REVIEW_FILE).read_text())
            answer = review.shape(raw)
            result = review.result_from_review(raw.get("review") if isinstance(raw, dict) else None)
        except FileNotFoundError:
            result = review.unavailable("no-output")
        except (OSError, ValueError, RecursionError):
            result = review.unavailable("unparseable")
    (out / review.RESULT_FILE).write_text(json.dumps(dict(result, head_sha=args.sha, model=args.model)))
    print(json.dumps({"sha": args.sha[:12], "available": result["available"], "risk": result.get("risk"),
                      "recommendation": result.get("recommendation"), "reason": result.get("reason"),
                      "reasons_given": len(result.get("findings") or []), "answer": answer}))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("step", choices=["gate", "prepare", "collect"])
    parser.add_argument("--out", help="the review directory")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--sha", default="", help="the commit under review")
    parser.add_argument("--render-dir", help="this run's render artifact")
    parser.add_argument("--status", default="failed", help="what happened to PR-Agent")
    parser.add_argument("--model", default="", help="the model alias PR-Agent used")
    args = parser.parse_args(argv)
    if args.step != "gate" and not args.out:
        parser.error("--out is required")
    if args.step == "collect":
        collect(args)
        return 0
    policy = classify.load_policy(steward.POLICY)
    gh = GitHub(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPOSITORY"])
    if args.step == "gate":
        try:
            outputs = {"reuse": "true" if gate(gh, args, policy) else "false"}
        except Exception as err:  # never blocks a review; prints no response body
            print(f"gate failed: {type(err).__name__}", file=sys.stderr)
            outputs = {"reuse": "false"}
        print(json.dumps(outputs))
    else:
        outputs = prepare(gh, args, policy)
    with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as handle:
        handle.writelines(f"{key}={value}\n" for key, value in outputs.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())
