"""Checks stage: read every check run and commit status on the PR head commit.

Required or not, any failing check gets needs-human: GitHub auto-merge only waits for
required checks, so a red path-filtered check (for example `verify`) would otherwise merge.
"""

from classify import HOLD, NEEDS, PASS, Stage

OK_CONCLUSIONS = {"success", "neutral", "skipped"}


def evaluate(check_runs, statuses, policy):
    """Return (Stage, pending) where pending lists checks that have not finished."""
    ignore = set(policy["ignore_checks"])
    failed, pending, soaking = [], [], False
    for status in statuses:
        context, state = status["context"], status["state"]
        if state == "success":
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
    if failed:
        return Stage("checks", NEEDS, "failing check: " + ", ".join(sorted(failed))), pending
    if soaking:
        return Stage("checks", HOLD, f"{policy['soak_status']} is pending (release still soaking)"), pending
    if pending:
        return Stage("checks", HOLD, "waiting for: " + ", ".join(sorted(set(pending)))), pending
    return Stage("checks", PASS, f"{len(check_runs)} check runs and {len(statuses)} statuses are green"), pending
