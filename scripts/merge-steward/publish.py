"""Publish a verdict: the merge-steward check run, the sticky comment and steward/* labels.

The check run is written with the GitHub App token so its source stays the same app
wherever the steward runs. Labels and the comment use the workflow token, whose events
do not start other workflows.
"""

from urllib.parse import quote

from classify import HOLD, NEEDS, PASS
from fold import MARKER
from ghapi import GitHubError

CHECK_NAME = "merge-steward"
VERDICT_LABELS = {PASS: "steward/pass", NEEDS: "steward/needs-human", HOLD: "steward/hold"}
LABEL_COLORS = {"steward/pass": "0e8a16", "steward/needs-human": "d93f0b", "steward/hold": "fbca04"}
COMMENT_AUTHOR = "github-actions[bot]"


def verify_override(gh, number, label):
    """Return (admin_login, ignored_login) for the most recent application of the label."""
    events = gh.paginate(f"issues/{number}/events")
    applied = [e for e in events if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    if not applied:
        return None, None
    login = (applied[-1].get("actor") or {}).get("login")
    if not login:
        return None, "unknown"
    perm = gh.get(f"collaborators/{quote(login)}/permission")
    if (perm or {}).get("permission") == "admin":
        return login, None
    return None, login


def remove_label(gh, number, label):
    try:
        gh.delete(f"issues/{number}/labels/{quote(label, safe='')}")
    except GitHubError as err:
        if err.status != 404:
            raise


def check_payload(verdict, reason, text):
    payload = {"name": CHECK_NAME, "output": {"title": reason[:250], "summary": text[:60000]}}
    if verdict == HOLD:
        payload["status"] = "in_progress"
    else:
        # Never neutral or skipped: GitHub treats both as success for a required check.
        payload.update(status="completed", conclusion="success" if verdict == PASS else "failure")
    return payload


def publish_check(app_gh, sha, verdict, reason, text):
    """Create a new check run per evaluation; GitHub uses the latest run of a name."""
    app_gh.post("check-runs", dict(check_payload(verdict, reason, text), head_sha=sha))


def set_labels(gh, number, current, verdict):
    want = VERDICT_LABELS[verdict]
    for label in VERDICT_LABELS.values():
        if label != want and label in current:
            remove_label(gh, number, label)
    if want not in current:
        try:
            gh.post("labels", {"name": want, "color": LABEL_COLORS[want], "description": "Merge steward verdict"})
        except GitHubError as err:
            if err.status != 422:  # 422: the label already exists
                raise
        gh.post(f"issues/{number}/labels", {"labels": [want]})


def find_comment(gh, number):
    """The steward's sticky comment: written by the workflow token and starting with the marker."""
    comments = gh.paginate(f"issues/{number}/comments")
    mine = [c for c in comments if c["user"]["login"] == COMMENT_AUTHOR and c["body"].startswith(MARKER)]
    return mine[0] if mine else None


def upsert_comment(gh, number, existing, body):
    if existing is None:
        gh.post(f"issues/{number}/comments", {"body": body})
    elif existing["body"] != body:
        gh.patch(f"issues/comments/{existing['id']}", {"body": body})
