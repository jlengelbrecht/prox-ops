"""Inline comments for an advisory review: which findings can sit on a changed line, and how
a rerun recognises the comments it already posted.

PR-Agent returns start_line and end_line with each key issue, and review.py keeps them as a
claim. A model can be off by a few lines or name a line the PR did not touch, and GitHub
refuses a review comment on a line that is not in the diff. So every claim is checked here
against the patch GitHub returns for the file, and a finding that does not fit stays in the
summary comment.

Pure code: nothing here calls GitHub.
"""

import hashlib
import re

import fold

INLINE_MARKER = "<!-- merge-steward-inline key={key} -->"
_MARKER = re.compile(r"^<!-- merge-steward-inline key=(?P<key>[0-9a-f]{24}) -->$", re.MULTILINE)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
# Most inline comments one review may add, and the longest range one may cover.
MAX_INLINE, MAX_SPAN = 3, 8


def visible(patch):
    """Line number to text for the lines of a file's patch that show on the head side.

    Added lines and context lines can take a comment; removed lines have no head number.
    """
    lines, number = {}, None
    # Split on newline only, as git does: str.splitlines also splits on \x0b, \x85,   and
    # others, which a file line can contain and which would shift every number after it.
    for raw in (patch or "").removesuffix("\n").split("\n"):
        hunk = _HUNK.match(raw)
        if hunk:
            number = int(hunk.group("new"))
        elif number is None or raw.startswith("\\"):
            continue
        elif raw == "" or raw.startswith(("+", " ")):  # an empty line is blank context with its space trimmed
            lines[number] = raw[1:]
            number += 1
    return lines


def key(path, texts, occurrence=0):
    """Identity of a comment: the file and the code it sits on, not the model's wording.

    A rerun words the same finding differently and a rebase moves it to another line number,
    but the code under it stays, so this is what stops a second comment on the same spot.
    occurrence counts the identical lines above it in the diff, so the same text in another
    block (enabled: true) is another spot.
    """
    material = "\0".join([path, str(occurrence)] + [t.strip() for t in texts])
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def anchors(findings, files):
    """The findings that can sit on a changed line, as dicts with path, line, start_line and key.

    The first finding is the overall assessment and never goes inline. A range is kept only
    when every line in it is in the diff and it is short; otherwise the first line stands
    alone if that one is in the diff, and the finding goes to the summary if not.
    """
    patches = {f["filename"]: visible(f.get("patch")) for f in files if f.get("status") != "removed"}
    found, seen = [], set()
    for index, row in enumerate(findings):
        shown = patches.get(row.get("file")) if index and row.get("start") else None
        if not shown:
            continue
        start, end = row["start"], row.get("end") or row["start"]
        if not (start in shown and start <= end <= start + MAX_SPAN and all(n in shown for n in range(start, end + 1))):
            end = start
        if start not in shown:
            continue
        above = sum(1 for n, text in shown.items() if n < start and text.strip() == shown[start].strip())
        digest = key(row["file"], [shown[n] for n in range(start, end + 1)], above)
        if digest in seen:
            continue
        seen.add(digest)
        found.append({"index": index, "path": row["file"], "start": start, "end": end, "key": digest})
    return found[:MAX_INLINE]


def payload(anchor, text, sha):
    """The body for POST pulls/N/comments. Right side only: the line numbers are head numbers."""
    body = {"commit_id": sha, "path": anchor["path"], "line": anchor["end"], "side": "RIGHT",
            "body": "\n".join([fold.clean(text, 1000), "",
                               "Advisory AI review. It does not affect any check.",
                               INLINE_MARKER.format(key=anchor["key"])])}
    if anchor["start"] != anchor["end"]:
        body.update(start_line=anchor["start"], start_side="RIGHT")
    return body


# How close, in lines, an earlier steward comment must be for a new finding to count as the same one.
NEAR = 5
_FOOTER = "Advisory AI review. It does not affect any check."
_STOP = frozenset("that this with from have will would should could there their which when then than into onto "
                  "does doesn about also only each other such these those been being were what where while "
                  "before after because still more most make made need needs must".split())


def _words(text):
    """The distinctive words of a finding: lower case, four letters or more, no filler."""
    return {w for w in re.findall(r"[a-z0-9]{4,}", text.lower()) if w not in _STOP}


def _header(text):
    """The short label before the first colon ("Runner Prerequisite"), lower case, or ''."""
    head, sep, _ = text.partition(":")
    return " ".join(head.lower().split()) if sep and len(head) <= 40 else ""


def similar(a, b):
    """True when two findings read as the same point, without comparing their text exactly.

    The model rewords a finding on every run, so the wording cannot be the test. The same label
    before the colon counts, and so does sharing at least half of the shorter finding's distinctive
    words. Two findings about different things near each other share neither.
    """
    wa, wb = _words(a), _words(b)
    shared = len(wa & wb)
    if _header(a) and _header(a) == _header(b) and shared >= 2:
        return True  # a label alone is not enough: "Bug:" heads many unrelated findings
    return len(min(wa, wb, key=len)) >= 3 and shared / len(min(wa, wb, key=len)) >= 0.5


def steward_inline(comments, login):
    """The root inline comments the steward app left, each with where it sits now and its text.

    GitHub fills in performed_via_github_app on issue comments but not on review comments, so the
    author is the bot user's login, which GitHub sets and nobody else can have (a person cannot
    take a login ending in [bot]). A reply, or a comment without the marker, is not one of ours.
    line is None once the code the comment sat on has changed.
    """
    found = []
    for comment in comments:
        user = comment.get("user") or {}
        marker = _MARKER.search(comment.get("body") or "")
        if user.get("login") != login or user.get("type") != "Bot" or comment.get("in_reply_to_id") or not marker:
            continue
        line = comment.get("line") if type(comment.get("line")) is int else None
        start = comment.get("start_line") if type(comment.get("start_line")) is int else line
        text = (comment.get("body") or "").split(_FOOTER)[0].strip()
        found.append({"id": comment.get("id"), "path": comment.get("path"), "line": line, "start": start,
                      "key": marker.group("key"), "text": text})
    return found


def is_repeat(anchor, text, earlier, resolved=frozenset()):
    """True when a finding is one the steward has already raised on this PR.

    Either the same code under the same key, or an unresolved comment of ours on the same file
    within NEAR lines whose text reads as the same point. A comment on code that has since
    changed (line None) or in a resolved thread does not count for nearness; a resolved thread
    still counts for an identical key, so a settled point is not raised again on the same code.
    """
    for old in earlier:
        if old["path"] != anchor["path"]:
            continue
        if old["key"] == anchor["key"]:
            return True
        if old["line"] is None or old.get("id") in resolved:
            continue
        gap = max(anchor["start"] - old["line"], old["start"] - anchor["end"], 0)
        if gap <= NEAR and similar(text, old["text"]):
            return True
    return False
