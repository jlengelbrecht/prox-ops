"""Merge steward entry point: evaluate one PR, the PRs on a commit, or sweep all open ones.

Environment: GITHUB_REPOSITORY, GITHUB_TOKEN (reads, labels, comment) and
STEWARD_APP_TOKEN (check run). --dry-run prints the verdict and writes nothing.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import checks
import classify
import fold
import publish
from classify import NEEDS, Stage
from ghapi import GitHub, GitHubError

POLICY = Path(__file__).with_name("policy.yaml")
# kind: Job, quoted or not, with or without a trailing comment, at any indent (a list item too).
_JOB_KIND = re.compile(r"""^[ \t]*(?:-[ \t]+)?kind:[ \t]*(["']?)Job\1[ \t]*(?:#.*)?$""", re.MULTILINE)
_APPROVAL = re.compile(r"^<!-- merge-steward-approval login=(?P<login>[A-Za-z0-9-]+) event=(?P<event>[0-9]+) "
                       r"fp=(?P<fp>[0-9a-f]{64}) -->$", re.MULTILINE)


def job_manifests(gh, files, sha):
    """Changed YAML files at the head commit that declare kind: Job (immutable pod template).

    A manifest the API will not return inline (over 1 MB) counts as a hit: fail closed.
    """
    hits = []
    for f in files:
        path = f["filename"]
        if f["status"] != "removed" and path.startswith("kubernetes/") and path.endswith((".yaml", ".yml")):
            raw = gh.get(f"contents/{quote(path)}?ref={sha}")
            if raw.get("encoding") != "base64" or not raw.get("content"):
                hits.append(path)
            elif _JOB_KIND.search(base64.b64decode(raw["content"]).decode("utf-8", "replace")):
                hits.append(path)
    return hits


def fingerprint(files):
    """Hash of the PR's changes, stable across a clean rebase (hunk headers dropped)."""
    rows = []
    for f in sorted(files, key=lambda f: f["filename"]):
        patch = "\n".join(l for l in (f.get("patch") or "").splitlines() if not l.startswith("@@"))
        # GitHub omits the patch for binary and very large files; the blob sha still changes.
        rows.append([f["filename"], f.get("previous_filename"), f["status"], patch, None if patch else f.get("sha")])
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


def stages_for(gh, pr, files, policy, wait_seconds):
    number, sha = pr["number"], pr["head"]["sha"]
    commits = gh.paginate(f"pulls/{number}/commits")
    stages = [classify.identity(pr, commits, policy)]
    if stages[0].verdict == NEEDS:
        return stages
    if len(commits) != pr["commits"] or len(files) != pr["changed_files"]:
        return stages + [Stage("policy", NEEDS, "PR is too large to read in full")]
    paths = [f["filename"] for f in files] + [f["previous_filename"] for f in files if f.get("previous_filename")]
    stages.append(classify.policy_stage(pr, paths, job_manifests(gh, files, sha), policy))
    deadline, stable = time.monotonic() + wait_seconds, 0
    while True:
        runs = gh.paginate(f"commits/{sha}/check-runs?filter=latest", key="check_runs")
        statuses = gh.paginate(f"commits/{sha}/status", key="statuses")
        stage, pending = checks.evaluate(runs, statuses, policy, head_age_minutes(commits, pr))
        # Two clean polls in a row, so a job that starts after another finishes is not missed.
        stable = stable + 1 if not pending else 0
        if stable >= 2 or (stable and wait_seconds == 0) or stage.verdict == NEEDS or time.monotonic() > deadline:
            break
        time.sleep(30)
    stages.append(stage)
    return stages


def head_age_minutes(commits, pr, now=None):
    """Minutes since the head commit was made (its committer date), else since the PR opened."""
    stamp = ((commits[-1].get("commit") or {}).get("committer") or {}).get("date") if commits else None
    stamp = stamp or pr.get("created_at")
    if not stamp:
        return 0
    now = now or datetime.now(timezone.utc)
    return (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds() / 60


def check_override(gh, pr, files, comment, policy, labeled_sha, dry_run):
    """Return (admin_login or None, approval marker or None, Stage or None).

    The PR timeline anchors the approval: the newest application of the label must come
    from an admin and post-date every commit and force push. The run for that labeled
    event (its head unchanged) records a fingerprint of the diff in the steward's comment,
    bound to that label event.
    Later runs need the same fingerprint, so a clean rebase keeps the approval and any
    change to the diff drops it, whatever dates the commits carry.
    """
    number, label = pr["number"], policy["override_label"]
    timeline = gh.paginate(f"issues/{number}/timeline")
    event = publish.newest_label_event(timeline, label)
    problem = None
    if event is None:
        problem = f"{label} has no labelling event in the PR timeline"
    else:
        login = (event.get("actor") or {}).get("login")
        why = publish.labeller_problem(gh, login)
        if why:
            problem = f"{label} ignored: {why}"
        else:
            fp, event_id = fingerprint(files), str(event["id"])
            recorded = _APPROVAL.search(comment["body"]) if comment else None
            marker = f"<!-- merge-steward-approval login={login} event={event_id} fp={fp} -->"
            if recorded and recorded.group("event") == event_id:
                if recorded.group("fp") == fp:
                    return recorded.group("login"), recorded.group(0), None
            elif not labeled_sha:
                # Only the run for the labeled event records an approval; commit dates are
                # set by the pusher, so other runs re-verify an existing record only.
                problem = f"no recorded approval yet for {label}; re-apply it to approve this version"
            elif labeled_sha == pr["head"]["sha"] and publish.label_postdates_head(timeline, event):
                return login, marker, None
            problem = problem or f"the PR changed after {label} was applied; re-apply it to approve this version"
    if not dry_run:
        publish.remove_label(gh, number, label)
    return None, None, Stage("override", NEEDS, problem)


def evaluate(gh, app_gh, pr, policy, wait_seconds, dry_run, labeled_sha=None):
    number, sha = pr["number"], pr["head"]["sha"]
    labels = {l["name"] for l in pr["labels"]}
    label = policy["override_label"]
    comment = publish.find_comment(gh, number)
    previous = _APPROVAL.search(comment["body"]) if comment else None
    approval = previous.group(0) if previous else None  # kept if evaluation errors out
    try:
        files = gh.paginate(f"pulls/{number}/files")
        stages = stages_for(gh, pr, files, policy, wait_seconds)
        override = None
        if label in labels:
            override, approval, problem = check_override(gh, pr, files, comment, policy, labeled_sha, dry_run)
            if problem:
                labels.discard(label)
                stages.append(problem)
        else:
            approval = None
        verdict, reason = fold.fold(stages, override)
    except Exception as err:  # fail closed on any error, without echoing response bodies
        stages, verdict, reason = [], NEEDS, f"steward error ({type(err).__name__}); a human must review"
        detail = str(err) if isinstance(err, GitHubError) else type(err).__name__
        print(f"PR #{number}: evaluation error: {detail}", file=sys.stderr)
    print(json.dumps({"pr": number, "sha": sha[:12], "verdict": verdict, "reason": fold.clean(reason)}))
    if dry_run:
        return verdict
    if gh.get(f"pulls/{number}")["head"]["sha"] != sha:
        print(f"PR #{number}: head moved during evaluation; leaving it to the newer run", file=sys.stderr)
        return None
    publish.publish_check(app_gh, sha, verdict, fold.clean(reason, 250), fold.summary(stages, verdict, reason))
    publish.set_labels(gh, number, labels, verdict)
    body = fold.comment(stages, verdict, reason, sha, label) + (f"\n{approval}" if approval else "")
    publish.upsert_comment(gh, number, comment, body)
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--sha")
    target.add_argument("--sweep", action="store_true")
    parser.add_argument("--wait", type=int, default=0, help="seconds to wait for running checks")
    parser.add_argument("--labeled-sha", help="head commit when the override label was applied")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    policy = classify.load_policy(POLICY)
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = GitHub(os.environ["GITHUB_TOKEN"], repo)
    app_gh = None if args.dry_run else GitHub(os.environ["STEWARD_APP_TOKEN"], repo)
    if args.pr:
        prs = [gh.get(f"pulls/{args.pr}")]
    elif args.sha:
        prs = [p for p in gh.get(f"commits/{args.sha}/pulls") if p["head"]["sha"] == args.sha]
        prs = [gh.get(f"pulls/{p['number']}") for p in prs]
    else:
        prs = [gh.get(f"pulls/{p['number']}") for p in gh.paginate("pulls?state=open")
               if p["user"]["login"] in policy["authors"]]
    failures = 0
    for pr in prs:
        if pr["state"] != "open":
            continue
        try:
            evaluate(gh, app_gh, pr, policy, args.wait, args.dry_run, args.labeled_sha)
        except Exception as err:  # publishing failed; the check stays missing, which blocks merge
            failures += 1
            detail = str(err) if isinstance(err, GitHubError) else type(err).__name__
            print(f"PR #{pr['number']}: publish error: {detail}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
