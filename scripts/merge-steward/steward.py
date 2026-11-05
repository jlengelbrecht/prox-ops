"""Merge steward entry point: evaluate one PR, the PRs on a commit, or sweep all open ones.

Environment: GITHUB_REPOSITORY, GITHUB_TOKEN (reads, labels) and STEWARD_APP_TOKEN (check
run, comment). --dry-run prints the verdict and writes nothing.
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
import render
import review
from classify import HOLD, NEEDS, OUT_OF_SCOPE, PASS, Stage
from ghapi import GitHub, GitHubError

POLICY = Path(__file__).with_name("policy.yaml")
# kind: Job, quoted or not, with or without a trailing comment, at any indent (a list item too).
_JOB_KIND = re.compile(r"""^[ \t]*(?:-[ \t]+)?kind:[ \t]*(["']?)Job\1[ \t]*(?:#.*)?$""", re.MULTILINE)
_APPROVAL = re.compile(r"^<!-- merge-steward-approval login=(?P<login>[A-Za-z0-9-]+) event=(?P<event>[0-9]+) "
                       r"fp=(?P<fp>[0-9a-f]{64}) -->$", re.MULTILINE)
# The render result, kept in the app-written check run so later events can reuse it.
_RENDER = re.compile(r"^<!-- merge-steward-render sha=(?P<sha>[0-9a-f]{40}) verdict=(?P<verdict>pass|hold|needs-human) "
                     r"reason=(?P<reason>[^\n]*?) -->$", re.MULTILINE)
RENDER_WAITING = "render not evaluated yet"
# Activity entries that put a commit on a branch.
PUSH_TYPES = ("push", "force_push", "branch_creation")


class Superseded(Exception):
    """The PR head moved while this run waited; a newer run owns the verdict."""


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


def head_blobs(files):
    """Filename to blob at the head of the diff. Unlike the fingerprint, this is position-bound."""
    return {f["filename"]: f.get("sha") for f in files}


def steward_runs(gh, sha, policy):
    """The merge-steward check runs on sha written by the steward's app, newest first."""
    runs = gh.paginate(f"commits/{sha}/check-runs?check_name={publish.CHECK_NAME}&filter=all", key="check_runs")
    mine = [r for r in runs if r.get("name") == publish.CHECK_NAME
            and (r.get("app") or {}).get("id") == policy["steward_app_id"]]
    return sorted(mine, key=lambda r: r["id"], reverse=True)


def recorded_render(gh, sha, policy):
    """The render stage recorded for sha in an earlier merge-steward check run, or None.

    Only runs written by the steward's app count; the newest one with a record wins.
    """
    for run in steward_runs(gh, sha, policy):
        found = _RENDER.search((run.get("output") or {}).get("text") or "")
        if found and found.group("sha") == sha:
            return Stage("render", found.group("verdict"), found.group("reason"))
    return None


def render_record(sha, stage):
    return f"<!-- merge-steward-render sha={sha} verdict={stage.verdict} reason={fold.clean(stage.reason)} -->"


def render_stage(gh, pr, policy, render_dir, age_minutes):
    """From this run's render artifact (PR events), else from the recorded result.

    No evidence yet is hold until the checks deadline, then needs-human.
    """
    sha = pr["head"]["sha"]
    exceptions = policy["release_exceptions"]
    stage = render.from_artifact(render_dir, sha, exceptions) if render_dir else recorded_render(gh, sha, policy)
    if stage is not None:
        return stage
    if age_minutes > policy["required_checks_deadline_minutes"]:
        return Stage("render", NEEDS, "render evidence unavailable")
    return Stage("render", HOLD, RENDER_WAITING)


def recorded_review(gh, sha, policy):
    """The AI result recorded for sha in an earlier merge-steward check run, or None."""
    for run in steward_runs(gh, sha, policy):
        result = review.from_record((run.get("output") or {}).get("text"), sha)
        if result is not None:
            return result
    return None


def llm_stage(gh, pr, files, policy, review_dir, age_minutes):
    """Return (Stage, result to show and record, or None).

    From this run's review artifact (PR events), else from the recorded result. No result
    yet is hold until the checks deadline, then needs-human, because a queued review can
    take a while.
    """
    sha = pr["head"]["sha"]
    changed = {f["filename"] for f in files}
    result = review.from_artifact(review_dir, sha, changed) if review_dir else recorded_review(gh, sha, policy)
    if result is not None:
        return review.stage(result, policy), result
    if age_minutes > policy["required_checks_deadline_minutes"]:
        return review.stage(review.unavailable("not-recorded"), policy), None
    return Stage("llm", HOLD, review.WAITING), None


def post_advisory(gh, pr, review_dir, dry_run):
    """Post a review that changes no verdict. A failed review stays quiet."""
    number, sha = pr["number"], pr["head"]["sha"]
    try:
        files = gh.paginate(f"pulls/{number}/files")
        result = review.from_artifact(review_dir, sha, {f["filename"] for f in files})
        if not result or not result["available"]:
            print(f"PR #{number}: no advisory review to post", file=sys.stderr)
        elif not dry_run:
            publish.upsert_advisory(gh, number, fold.advisory(result, sha))
    except GitHubError as err:  # advisory: the PR's verdict must not depend on this comment
        print(f"PR #{number}: advisory comment failed: {err}", file=sys.stderr)


def commit_stage(gh, pr, files, policy, age_minutes):
    """The branch must carry exactly what Renovate last pushed.

    The activity log names each pusher, which no commit metadata can forge. An update-branch
    merge, a server-side rebase and a hand-resolved conflict all sit on top of Renovate's
    push, so what separates them is whether the content still equals that push. The patch
    alone says nothing about where in a file it applies, so each changed file's head blob
    must match too. A base-branch change to a file Renovate touched also trips this, which
    is safe: a Renovate rebase clears it.
    """
    ref = quote(f"refs/heads/{pr['head']['ref']}", safe="")
    pushes = [a for a in gh.paginate(f"activity?ref={ref}") if a.get("activity_type") in PUSH_TYPES]
    head = next((a for a in pushes if a.get("after") == pr["head"]["sha"]), None)
    if head is None:
        if age_minutes > policy["required_checks_deadline_minutes"]:
            return Stage("commits", NEEDS, "no record of who pushed this head")
        return Stage("commits", HOLD, "waiting for the push to reach the activity log")
    own = [a for a in pushes if (a.get("actor") or {}).get("login") in policy["authors"]]
    if not own:
        return Stage("commits", NEEDS, "Renovate never pushed to this branch")
    try:
        pushed = gh.get(f"commits/{max(own, key=lambda a: a.get('timestamp') or '')['after']}")
    except GitHubError as err:
        if err.status != 404:
            raise
        return Stage("commits", NEEDS, "the last push by Renovate can no longer be read")
    if not classify.is_renovate_push(pushed, policy):
        return Stage("commits", NEEDS, "the last push by Renovate is not a commit signed for Renovate")
    blobs = head_blobs(files)
    if None in blobs.values() or blobs != head_blobs(pushed["files"]) or fingerprint(pushed["files"]) != fingerprint(files):
        pusher = (head.get("actor") or {}).get("login") or "unknown"
        return Stage("commits", NEEDS, f"the diff differs from Renovate's last push; head pushed by {pusher}")
    return Stage("commits", PASS, "the diff equals Renovate's last push")


def stages_for(gh, pr, files, policy, wait_seconds, render_dir=None):
    number, sha = pr["number"], pr["head"]["sha"]
    commits = gh.paginate(f"pulls/{number}/commits")
    stages = [classify.identity(pr, commits, policy)]
    if stages[0].verdict == NEEDS:
        return stages
    if len(commits) != pr["commits"] or len(files) != pr["changed_files"]:
        return stages + [Stage("policy", NEEDS, "PR is too large to read in full")]
    stages.append(commit_stage(gh, pr, files, policy, head_age_minutes(commits, pr)))
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
        if gh.get(f"pulls/{number}")["head"]["sha"] != sha:
            raise Superseded()
    stages.append(stage)
    stages.append(render_stage(gh, pr, policy, render_dir, head_age_minutes(commits, pr)))
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


def publish_out_of_scope(gh, app_gh, pr, policy, dry_run):
    """Post success without judging the PR, and clear what an earlier verdict left behind.

    A check run belongs to the commit, so another open PR on this head that the steward does
    judge keeps its verdict. An existing out-of-scope result is not posted again.
    """
    number, sha = pr["number"], pr["head"]["sha"]
    print(json.dumps({"pr": number, "sha": sha[:12], "verdict": PASS, "reason": OUT_OF_SCOPE}))
    if dry_run:
        return PASS
    if gh.get(f"pulls/{number}")["head"]["sha"] != sha:
        print(f"PR #{number}: head moved during evaluation; leaving it to the newer run", file=sys.stderr)
        return None
    shared = [p for p in gh.get(f"commits/{sha}/pulls") if p["number"] != number and p.get("state") == "open"]
    if not all(classify.out_of_scope(p, policy) for p in shared):
        print(f"PR #{number}: another open PR shares this head; leaving the check to it", file=sys.stderr)
        return None
    newest = next(iter(steward_runs(gh, sha, policy)), {})
    if (newest.get("output") or {}).get("title") != OUT_OF_SCOPE or newest.get("conclusion") != "success":
        stage = Stage("scope", PASS, OUT_OF_SCOPE)
        publish.publish_check(app_gh, sha, PASS, OUT_OF_SCOPE, fold.summary([stage], PASS, OUT_OF_SCOPE))
    publish.set_labels(gh, number, {l["name"] for l in pr["labels"]}, None)
    comment, stale = publish.find_comments(gh, number, policy)
    publish.delete_comments(app_gh, stale + ([comment] if comment else []))
    return PASS


def evaluate(gh, app_gh, pr, policy, wait_seconds, dry_run, labeled_sha=None, render_dir=None,
             ai_enabled=False, review_dir=None):
    ai_mode = review.mode(pr, policy) if ai_enabled else review.OFF
    if ai_mode == review.ADVISORY and review_dir:
        post_advisory(gh, pr, review_dir, dry_run)
    if classify.out_of_scope(pr, policy):
        return publish_out_of_scope(gh, app_gh, pr, policy, dry_run)
    number, sha = pr["number"], pr["head"]["sha"]
    labels = {l["name"] for l in pr["labels"]}
    label = policy["override_label"]
    comment, stale = publish.find_comments(gh, number, policy)
    previous = _APPROVAL.search(comment["body"]) if comment else None
    approval = previous.group(0) if previous else None  # kept if evaluation errors out
    stages, ai_result = [], None
    # Set here, apart from the stage below: with AI on, a verdict needs the llm stage to exist.
    expected = fold.EXPECTED + (("llm",) if ai_mode == review.VERDICT else ())
    try:
        files = gh.paginate(f"pulls/{number}/files")
        stages = stages_for(gh, pr, files, policy, wait_seconds, render_dir)
        if ai_mode == review.VERDICT:
            # After the other stages and whatever they said: every PR in scope gets an assessment.
            age = head_age_minutes(gh.paginate(f"pulls/{number}/commits"), pr)
            stage, ai_result = llm_stage(gh, pr, files, policy, review_dir, age)
            stages.append(stage)
        override = None
        if label in labels:
            override, approval, problem = check_override(gh, pr, files, comment, policy, labeled_sha, dry_run)
            if problem:
                labels.discard(label)
                stages.append(problem)
        else:
            approval = None
        verdict, reason = fold.fold(stages, override, expected)
    except Superseded:
        print(f"PR #{number}: head moved while waiting; leaving it to the newer run", file=sys.stderr)
        return None
    except Exception as err:  # fail closed on any error, without echoing response bodies
        verdict, reason = NEEDS, f"steward error ({type(err).__name__}); a human must review"
        detail = str(err) if isinstance(err, GitHubError) else type(err).__name__
        print(f"PR #{number}: evaluation error: {detail}", file=sys.stderr)
    # Keep the render result (from this run's artifact or an earlier record) with the check run.
    # A hold (no render yet, or a stale merge ref) is never recorded, so later events
    # keep waiting for a real render instead of reusing it.
    rendered = next((s for s in stages if s.name == "render" and s.verdict != HOLD), None)
    waiting = any(s.reason in (RENDER_WAITING, review.WAITING) for s in stages)
    print(json.dumps({"pr": number, "sha": sha[:12], "verdict": verdict, "reason": fold.clean(reason)}))
    if dry_run:
        return verdict
    if waiting and verdict == HOLD:
        # The PR event's run has not classified the render yet; it publishes when it does.
        print(f"PR #{number}: {RENDER_WAITING}; leaving the verdict to the pull request run", file=sys.stderr)
        return None
    if gh.get(f"pulls/{number}")["head"]["sha"] != sha:
        print(f"PR #{number}: head moved during evaluation; leaving it to the newer run", file=sys.stderr)
        return None
    records = ([render_record(sha, rendered)] if rendered else []) + ([review.record(sha, ai_result)] if ai_result else [])
    publish.publish_check(app_gh, sha, verdict, fold.clean(reason, 250), fold.summary(stages, verdict, reason, ai_result),
                          "\n".join(records) or None)
    publish.set_labels(gh, number, labels, verdict)
    publish.set_unavailable_label(gh, number, labels, any(review.is_unavailable(s) for s in stages))
    body = fold.comment(stages, verdict, reason, sha, label, ai_result) + (f"\n{approval}" if approval else "")
    publish.upsert_comment(app_gh, number, comment, body)
    publish.delete_comments(app_gh, stale)
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pr", type=int)
    target.add_argument("--sha")
    target.add_argument("--sweep", action="store_true")
    parser.add_argument("--wait", type=int, default=0, help="seconds to wait for running checks")
    parser.add_argument("--labeled-sha", help="head commit when the override label was applied")
    parser.add_argument("--event-sha", help="head commit of the triggering PR event; skip the PR if it moved")
    parser.add_argument("--render-dir", help="this run's render artifact (pull request events)")
    parser.add_argument("--review-dir", help="this run's AI review result (pull request events)")
    parser.add_argument("--advisory-only", action="store_true", help="post the review in --review-dir and stop")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    policy = classify.load_policy(POLICY)
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = GitHub(os.environ["GITHUB_TOKEN"], repo)
    if args.advisory_only:
        pr = gh.get(f"pulls/{args.pr}")
        if pr["state"] == "open" and review.mode(pr, policy) == review.ADVISORY:
            post_advisory(gh, pr, args.review_dir, args.dry_run)
        return 0
    app_gh = None if args.dry_run else GitHub(os.environ["STEWARD_APP_TOKEN"], repo)
    # Off until the variable says otherwise: with AI on, a PR without a review is needs-human.
    ai_enabled = os.environ.get("MERGE_STEWARD_AI") == "true"
    if args.pr:
        prs = [gh.get(f"pulls/{args.pr}")]
    elif args.sha:
        prs = [p for p in gh.get(f"commits/{args.sha}/pulls") if p["head"]["sha"] == args.sha]
        prs = [gh.get(f"pulls/{p['number']}") for p in prs]
    else:
        prs = [gh.get(f"pulls/{p['number']}") for p in gh.paginate("pulls?state=open")
               if (p.get("user") or {}).get("login") in policy["authors"]]
    failures = 0
    for pr in prs:
        if pr["state"] != "open":
            continue
        if args.event_sha and pr["head"]["sha"] != args.event_sha:
            print(f"PR #{pr['number']}: superseded by a newer push; leaving it to that run", file=sys.stderr)
            continue
        try:
            evaluate(gh, app_gh, pr, policy, args.wait, args.dry_run, args.labeled_sha, args.render_dir,
                     ai_enabled, args.review_dir)
        except Exception as err:  # publishing failed; the check stays missing, which blocks merge
            failures += 1
            detail = str(err) if isinstance(err, GitHubError) else type(err).__name__
            print(f"PR #{pr['number']}: publish error: {detail}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
