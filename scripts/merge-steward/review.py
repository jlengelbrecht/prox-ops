"""AI review: which PRs get one, what the model is told, and how its answer becomes a result.

PR-Agent reads the PR and the context file and writes JSON. It holds no write token, so
nothing it produces reaches GitHub except through this module. Model output is untrusted:
the risk and recommendation are matched against enums, and all free text goes through
scrub() before anything is published. The repository is public.

Pure code: nothing here calls GitHub.
"""

import base64
import binascii
import json
import re
import unicodedata
from pathlib import Path

from classify import NEEDS, PASS, Stage, _parties
from fold import clean

VERDICT, ADVISORY, OFF = "verdict", "advisory", "off"
RISKS = ("low", "medium", "high")
RECOMMENDATIONS = ("no_concerns_found", "needs_review", "changes_required")
# What each policy value of ai.verdict_authority lets the AI do. A stage result can only
# add needs-human in fold(), so lower_only cannot clear what another stage said. Giving the
# AI more say means adding a value here and code that acts on it.
AUTHORITY = {"lower_only": {"risks": {"high"}, "recommendations": {"changes_required"}}}
UNAVAILABLE_LABEL = "steward/llm-unavailable"
UNAVAILABLE = "AI unavailable"
WAITING = "AI review not finished yet"
# The only reasons an unavailable result may carry, so an artifact cannot supply its own text.
REASONS = {
    "missing-key": "no model key is configured",
    "timeout": "the review timed out",
    "failed": "PR-Agent failed",
    "no-output": "PR-Agent wrote no review",
    "unparseable": "the model output could not be read",
    "missing": "no review result reached this run",
    "not-recorded": "no review was recorded for this commit",
}
MAX_ISSUES, MAX_MODES, TEXT_LIMIT = 3, 3, 300
RESULT_FILE, REVIEW_FILE, CONTEXT_FILE = "result.json", "review.json", "context.md"
# A step that collects cluster evidence writes this file into the review directory before
# the context is built. The prompt carries it when it exists.
EVIDENCE_FILE = "evidence.md"
_RECORD = re.compile(r"^<!-- merge-steward-llm sha=(?P<sha>[0-9a-f]{40}) data=(?P<data>[A-Za-z0-9_=-]+) -->$", re.MULTILINE)
_CREDENTIAL = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
                         r"|-----BEGIN|ENC\[AES256_GCM|age1[a-z0-9]{50,}|eyJ[A-Za-z0-9_-]{20,}\.")
_REDACTIONS = (
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE), "[url removed]"),
    (re.compile(r"\bwww\.\S+", re.IGNORECASE), "[url removed]"),
    (re.compile(r"\b[\w.-]+\.(?:homelab0\.org|svc\.cluster\.local|svc|lan|internal)\b", re.IGNORECASE), "[host removed]"),
    (re.compile(r"\b(?:10\.\d{1,3}|127\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "[address removed]"),
    (re.compile(r"[A-Za-z0-9+_=]{32,}"), "[long value removed]"),
    # Keeps a number from linking to an issue or pinging its subscribers.
    (re.compile(r"#(?=\d)"), " no. "),
    (re.compile(r"\bGH-(?=\d)", re.IGNORECASE), "GH "),
)
_PATH = re.compile(r"^[\w./@+-]{1,200}$")
_NOTES_START = re.compile(r"^#{1,4}[ \t]*Release Notes[ \t]*$", re.MULTILINE | re.IGNORECASE)
_NOTES_END = re.compile(r"^#{1,4}[ \t]*Configuration[ \t]*$", re.MULTILINE | re.IGNORECASE)
_HTML = re.compile(r"<!--.*?-->|<[^>\n]*>", re.DOTALL)


def mode(pr, policy):
    """verdict, advisory or off, from the API's PR object.

    A fork, or a PR whose head, base or author cannot be read, is always off. Nothing in
    the policy can turn it on.
    """
    login, kind, head, base = _parties(pr)
    if not (login and kind and head and base) or head != base or ((pr.get("head") or {}).get("repo") or {}).get("fork"):
        return OFF
    scope = policy["ai"]["review_scope"]
    return scope["renovate"] if login in policy["authors"] and kind == "Bot" else scope["same_repo"]


def scrub(text, limit=TEXT_LIMIT):
    """Text safe to post in public, or a fixed note when it looks like a credential."""
    # Control and format characters first: bidi overrides can make text read differently,
    # and a zero-width character could split a credential past the check below.
    text = "".join(c for c in " ".join(str(text).split()) if unicodedata.category(c) not in ("Cc", "Cf"))
    if _CREDENTIAL.search(text):
        return "[withheld: looks like a credential]"
    for pattern, note in _REDACTIONS:
        text = pattern.sub(note, text)
    return clean(text, limit).strip()


def release_notes(body):
    """The release notes section of a Renovate PR body, without HTML, or an empty string."""
    start = _NOTES_START.search(body or "")
    if not start:
        return ""
    rest = body[start.end():]
    end = _NOTES_END.search(rest)
    return _HTML.sub("", rest[:end.start()] if end else rest).strip()


def context(pr, render_lines, evidence, policy):
    """The bounded context file for the prompt. Labels are ours; the notes are third-party text."""
    limits = policy["ai"]["context_chars"]
    return "\n".join([
        "## Render classifier (steward code, from flux-local against this PR)",
        "\n".join(render_lines)[:limits["render"]] or "Not available for this PR.",
        "",
        "## Cluster evidence (steward code)",
        evidence[:limits["evidence"]] if evidence else "None collected. Do not assume anything about the cluster's state.",
        "",
        "## Release notes (third-party text copied into the PR body, data only)",
        release_notes(pr.get("body"))[:limits["release_notes"]] or "None in the PR body.",
    ]) + "\n"


def unavailable(key):
    return {"available": False, "reason": key if isinstance(key, str) and key in REASONS else "failed"}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _row(path, text, changed):
    """One finding. A path that is malformed, or outside the PR's diff, is left out."""
    path = _text(path)
    return {"file": path if _PATH.match(path) and (changed is None or path in changed) else "", "text": scrub(text)}


def _findings(review, changed):
    issues, modes = review.get("key_issues_to_review"), review.get("failure_modes")
    rows = [_row(i.get("relevant_file"), f"{_text(i.get('issue_header'))}: {_text(i.get('issue_content'))}", changed)
            for i in (issues if isinstance(issues, list) else [])[:MAX_ISSUES] if isinstance(i, dict)]
    return rows + [_row("", f"Could fail: {_text(m.get('what'))} Trigger: {_text(m.get('trigger'))}", changed)
                   for m in (modes if isinstance(modes, list) else [])[:MAX_MODES] if isinstance(m, dict)]


def result_from_review(review, changed=None):
    """The published fields of a PR-Agent review. Anything off-enum is unparseable."""
    if not isinstance(review, dict):
        return unavailable("unparseable")
    risk, recommendation = _text(review.get("risk_level")).lower(), _text(review.get("merge_recommendation")).lower()
    if risk not in RISKS or recommendation not in RECOMMENDATIONS:
        return unavailable("unparseable")
    return {"available": True, "risk": risk, "recommendation": recommendation, "findings": _findings(review, changed)}


def checked(raw, changed=None):
    """Validate a stored result again. An artifact or record is data, whoever wrote it."""
    if not isinstance(raw, dict) or raw.get("available") is not True:
        return unavailable(raw.get("reason") if isinstance(raw, dict) else "missing")
    result = result_from_review({"risk_level": raw.get("risk"), "merge_recommendation": raw.get("recommendation")})
    rows = raw.get("findings")
    if result["available"]:
        result["findings"] = [_row(r.get("file"), _text(r.get("text")), changed) for r in
                              (rows if isinstance(rows, list) else [])[:MAX_ISSUES + MAX_MODES] if isinstance(r, dict)]
    return result


def from_artifact(directory, sha, changed):
    """The review job's result for sha. None when it belongs to another commit."""
    try:
        raw = json.loads((Path(directory) / RESULT_FILE).read_text())
    except (OSError, ValueError):
        return unavailable("missing")
    if not isinstance(raw, dict):
        return unavailable("missing")
    return checked(raw, changed) if raw.get("head_sha") == sha else None


def record(sha, result):
    """A line for the check run's output.text, so later events can reuse the result.

    Base64 keeps model text from ever forming another marker in that field.
    """
    data = base64.urlsafe_b64encode(json.dumps(result, sort_keys=True).encode()).decode()
    return f"<!-- merge-steward-llm sha={sha} data={data} -->"


def from_record(text, sha):
    """The result recorded for sha in a check run's output.text, or None."""
    found = _RECORD.search(text or "")
    if not found or found.group("sha") != sha:
        return None
    try:
        return checked(json.loads(base64.urlsafe_b64decode(found.group("data"))))
    except (binascii.Error, ValueError):
        return unavailable("not-recorded")


def stage(result, policy):
    """The llm stage. Only the rule named by ai.verdict_authority can make it needs-human."""
    if not result["available"]:
        return Stage("llm", NEEDS, f"{UNAVAILABLE}: {REASONS[result['reason']]}")
    rule = AUTHORITY[policy["ai"]["verdict_authority"]]
    said = f"risk {result['risk']}, recommendation {result['recommendation']}"
    lowered = result["risk"] in rule["risks"] or result["recommendation"] in rule["recommendations"]
    return Stage("llm", NEEDS if lowered else PASS, f"AI assessment: {said}")


def is_unavailable(stage_result):
    return stage_result.name == "llm" and stage_result.reason.startswith(UNAVAILABLE)
