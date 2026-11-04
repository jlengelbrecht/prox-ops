"""Checks stage: read every check run and commit status on the PR head commit.

Required or not, any failing check gets needs-human: GitHub auto-merge only waits for
required checks, so a red path-filtered check (for example `verify`) would otherwise merge.

The checks in policy.yaml `required_checks` must also exist, come from the named app and
conclude success. A missing one holds the verdict until `required_checks_deadline_minutes`
after the head commit, then gives needs-human. No set of checks, however small, passes
without them.
"""

from classify import HOLD, NEEDS, PASS, Stage

OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def _required(check_runs, policy, age_minutes):
    """Return (failed, pending, missing) for policy["required_checks"]."""
    failed, pending, missing = [], [], []
    for want in policy["required_checks"]:
        runs = [r for r in check_runs if r["name"] == want["name"] and (r.get("app") or {}).get("id") == want["app_id"]]
        if not runs:
            missing.append(want["name"])
        elif any(r["status"] != "completed" for r in runs):
            pending.append(want["name"])
        elif any(r["conclusion"] != "success" for r in runs):
            failed.append(f"{want['name']} ({', '.join(sorted({str(r['conclusion']) for r in runs}))})")
    if missing and age_minutes > policy["required_checks_deadline_minutes"]:
        failed += [f"{name} (never reported)" for name in missing]
        missing = []
    return failed, pending, missing


def evaluate(check_runs, statuses, policy, age_minutes=0):
    """Return (Stage, pending) where pending lists checks that have not finished or appeared."""
    ignore = set(policy["ignore_checks"])
    failed, pending, soaking = [], [], False
    for status in statuses:
        context, state = status["context"], status["state"]
        if state == "success" or context in ignore:
            continue
        if context == policy["soak_status"] and state == "pending":
            soaking = True
        elif state == "pending":
            pending.append(context)
        else:
            failed.append(f"{context} ({state})")
    for run in check_runs:
        if run["name"] in ignore:
            continue
        if run["status"] != "completed":
            pending.append(run["name"])
        elif run["conclusion"] not in OK_CONCLUSIONS:
            failed.append(f"{run['name']} ({run['conclusion']})")
    req_failed, req_pending, missing = _required(check_runs, policy, age_minutes)
    failed = sorted(set(failed) | set(req_failed))
    pending = sorted(set(pending) | set(req_pending) | set(missing))
    if failed:
        return Stage("checks", NEEDS, "failing check: " + ", ".join(failed)), pending
    if soaking:
        return Stage("checks", HOLD, f"{policy['soak_status']} is pending (release still soaking)"), pending
    if missing:
        return Stage("checks", HOLD, "required check not reported yet: " + ", ".join(missing)), pending
    if pending:
        return Stage("checks", HOLD, "waiting for: " + ", ".join(pending)), pending
    if not check_runs:  # unreachable while required_checks is non-empty; kept as a floor
        return Stage("checks", NEEDS, "no check runs on the head commit"), pending
    return Stage("checks", PASS, f"{len(check_runs)} check runs and {len(statuses)} statuses are green"), pending
