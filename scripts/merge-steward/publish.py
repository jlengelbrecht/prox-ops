"""Publish a verdict: the merge-steward check run, the sticky comment and steward/* labels.

The check run and the comment are written with the GitHub App token, so their source stays
the same app and only that app can leave an approval record. Labels carry no trust and use
the workflow token, whose events do not start other workflows.
"""

import sys
from datetime import datetime
from urllib.parse import quote

from classify import HOLD, NEEDS, PASS
from fold import ADVISORY_MARKER, MARKER
from ghapi import GitHubError
from review import UNAVAILABLE_LABEL

CHECK_NAME = "merge-steward"
VERDICT_LABELS = {PASS: "steward/pass", NEEDS: "steward/needs-human", HOLD: "steward/hold"}
LABEL_COLORS = {"steward/pass": "0e8a16", "steward/needs-human": "d93f0b", "steward/hold": "fbca04"}
# Author of the sticky comment before the app took over that write.
LEGACY_COMMENT_AUTHOR = "github-actions[bot]"


def _when(stamp):
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def newest_label_event(timeline, label):
    """The newest `labeled` event for the label in the PR timeline, or None."""
    applied = [e for e in timeline if e.get("event") == "labeled" and (e.get("label") or {}).get("name") == label]
    return applied[-1] if applied else None


def labeller_problem(gh, login):
    """None when login is a repository admin, else why the label does not count.

    A 404 means the actor is not a collaborator at all (App actors such as renovate[bot]),
    so it counts as "not admin". Any other API error raises, and the caller fails closed.
    """
    if not login:
        return "the labeller is unknown"
    try:
        perm = gh.get(f"collaborators/{quote(login)}/permission")
    except GitHubError as err:
        if err.status == 404:
            return f"{login} is not a collaborator on this repository"
        raise
    if (perm or {}).get("permission") == "admin":
        return None
    return f"{login} is not a repository admin"


def label_postdates_head(timeline, event):
    """True when the label event is newer than every commit and force push in the timeline.

    Commit events carry git dates, force pushes a server time. A commit without a date
    counts as newer, so the check fails closed.
    """
    labelled = _when(event["created_at"])
    for item in timeline:
        if item.get("event") == "committed":
            stamp = (item.get("committer") or {}).get("date")
        elif item.get("event") == "head_ref_force_pushed":
            stamp = item.get("created_at")
        else:
            continue
        if not stamp or _when(stamp) >= labelled:
            return False
    return True


def remove_label(gh, number, label):
    try:
        gh.delete(f"issues/{number}/labels/{quote(label, safe='')}")
    except GitHubError as err:
        if err.status != 404:
            raise


def check_payload(verdict, reason, text, detail=None):
    payload = {"name": CHECK_NAME, "output": {"title": reason[:250], "summary": text[:60000]}}
    if detail:
        payload["output"]["text"] = detail[:60000]
    if verdict == HOLD:
        payload["status"] = "in_progress"
    else:
        # Never neutral or skipped: GitHub treats both as success for a required check.
        payload.update(status="completed", conclusion="success" if verdict == PASS else "failure")
    return payload


def publish_check(app_gh, sha, verdict, reason, text, detail=None):
    """Create a new check run per evaluation; GitHub uses the latest run of a name.

    detail goes to output.text, where the steward keeps its render record.
    """
    app_gh.post("check-runs", dict(check_payload(verdict, reason, text, detail), head_sha=sha))


def set_labels(gh, number, current, verdict):
    """Leave only the label for verdict; a verdict of None clears them all."""
    want = VERDICT_LABELS.get(verdict)
    for label in VERDICT_LABELS.values():
        if label != want and label in current:
            remove_label(gh, number, label)
    if want and want not in current:
        try:
            gh.post("labels", {"name": want, "color": LABEL_COLORS[want], "description": "Merge steward verdict"})
        except GitHubError as err:
            if err.status != 422:  # 422: the label already exists
                raise
        gh.post(f"issues/{number}/labels", {"labels": [want]})


def set_unavailable_label(gh, number, current, unavailable):
    """Show that a PR's verdict was reached without its AI review."""
    if not unavailable:
        if UNAVAILABLE_LABEL in current:
            remove_label(gh, number, UNAVAILABLE_LABEL)
    elif UNAVAILABLE_LABEL not in current:
        try:
            gh.post("labels", {"name": UNAVAILABLE_LABEL, "color": "d4c5f9", "description": "Merge steward: no AI review"})
        except GitHubError as err:
            if err.status != 422:  # 422: the label already exists
                raise
        gh.post(f"issues/{number}/labels", {"labels": [UNAVAILABLE_LABEL]})


def upsert_advisory(gh, number, body):
    """Advisory comments carry no trust and use the workflow token."""
    mine = [c for c in gh.paginate(f"issues/{number}/comments")
            if c["body"].startswith(ADVISORY_MARKER) and (c.get("user") or {}).get("login") == LEGACY_COMMENT_AUTHOR]
    upsert_comment(gh, number, mine[0] if mine else None, body)


def find_comments(gh, number, policy):
    """Return (sticky, stale): the app's sticky comment, and the marker comments to remove.

    The comment belongs to the app by id, which GitHub sets, not by login. Stale ones are
    those the workflow token wrote and duplicates from racing runs. Only the app's comment
    counts for an approval record, so an old record has to be applied again.
    """
    comments = [c for c in gh.paginate(f"issues/{number}/comments") if c["body"].startswith(MARKER)]
    mine = [c for c in comments
            if (c.get("performed_via_github_app") or {}).get("id") == policy["steward_app_id"]]
    legacy = [c for c in comments if (c.get("user") or {}).get("login") == LEGACY_COMMENT_AUTHOR]
    return (mine[0] if mine else None), mine[1:] + legacy


def delete_comments(gh, comments):
    """Best effort: a stale comment is ignored from now on, so a refusal does not stop the run."""
    for comment in comments:
        try:
            gh.delete(f"issues/comments/{comment['id']}")
        except GitHubError as err:
            print(f"could not delete comment {comment['id']}: {err}", file=sys.stderr)


def upsert_comment(gh, number, existing, body):
    if existing is None:
        gh.post(f"issues/{number}/comments", {"body": body})
    elif existing["body"] != body:
        gh.patch(f"issues/comments/{existing['id']}", {"body": body})
