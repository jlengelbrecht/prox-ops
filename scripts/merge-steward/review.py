"""AI review: which PRs get one, what the model is told, and how its answer becomes a result.

PR-Agent reads the PR and the context file and writes JSON. It holds no write token, so
nothing it produces reaches GitHub except through this module. Model output is untrusted:
the risk and recommendation are matched against enums, and all free text goes through
scrub() before anything is published. The repository is public.

Pure code: nothing here calls GitHub.
"""

import base64
import binascii
import html
import json
import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote

from classify import NEEDS, PASS, Stage, _parties
from fold import clean

VERDICT, ADVISORY, OFF = "verdict", "advisory", "off"
RISKS = ("low", "medium", "high")
RECOMMENDATIONS = ("no_concerns_found", "needs_review", "changes_required")
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
MAX_ISSUES, MAX_MODES, TEXT_LIMIT = 3, 3, 400
# The first entry explains the rating, so it gets more room and ends at a sentence.
ASSESSMENT, ASSESSMENT_LIMIT = "Assessment:", 900
# How many earlier heads to look through for a review to reuse, and how many upstream
# releases one PR's context may carry.
REUSE_LOOKBACK, MAX_UPSTREAM = 10, 3
# Longest model text read, and how many layers of encoding are undone, before giving up.
RAW_LIMIT, DECODE_ROUNDS = 4000, 8
RESULT_FILE, REVIEW_FILE, CONTEXT_FILE = "result.json", "review.json", "context.md"
# A step that collects cluster evidence writes this file into the review directory before
# the context is built. The prompt carries it when it exists. It is expected to hold one fact
# per fixed-format line, with any free text cut to about 120 characters: the model can repeat
# what it reads, and the scrub does not remove namespace or workload names.
EVIDENCE_FILE = "evidence.md"
_RECORD = re.compile(r"^<!-- merge-steward-llm sha=(?P<sha>[0-9a-f]{40})(?: fp=(?P<fp>[0-9a-f]{64}))? "
                     r"data=(?P<data>[A-Za-z0-9_=-]+) -->$", re.MULTILINE)
# Facts about a result that a stored copy may keep, each only in this exact form.
_PROVENANCE = {"model": re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*"), "reused_from": re.compile(r"[0-9a-f]{40}")}
# The diff and model an advisory comment reviewed, on a line of its own in that comment.
_ADVISORY_RECORD = re.compile(r"^<!-- merge-steward-review-diff fp=(?P<fp>[0-9a-f]{64}) "
                              r"model=(?P<model>[A-Za-z0-9][A-Za-z0-9._:/-]*) -->$", re.MULTILINE)
_CREDENTIAL = re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}"
                         r"|-----BEGIN|ENC\[AES256_GCM|age1[a-z0-9]{50,}|eyJ[A-Za-z0-9_-]{20,}\.")
_REDACTIONS = (
    (re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE), "[url removed]"),
    (re.compile(r"\b(?:mailto|xmpp|javascript|data|vbscript):\S+", re.IGNORECASE), "[url removed]"),
    (re.compile(r"\bwww\.\S+", re.IGNORECASE), "[url removed]"),
    (re.compile(r"\b[\w.-]+\.(?:homelab0\.org|svc\.cluster\.local|svc|lan|internal)\b", re.IGNORECASE), "[host removed]"),
    (re.compile(r"\b(?:10\.\d{1,3}|127\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "[address removed]"),
    (re.compile(r"[A-Za-z0-9+_=]{32,}"), "[long value removed]"),
    # Keeps a number from linking to an issue or pinging its subscribers.
    (re.compile(r"#(?=\d)"), " no. "),
    (re.compile(r"\bGH-(?=\d)", re.IGNORECASE), "GH "),
)
WITHHELD_CREDENTIAL = "[withheld: looks like a credential]"
WITHHELD_ENCODED = "[withheld: encoded too many times]"
_PATH = re.compile(r"^[\w./@+-]{1,200}$")
NO_NOTES = "None in the PR body, so what changed in this release is not known from here."
_NOTES_START = re.compile(r"^#{1,4}[ \t]*Release Notes[ \t]*$", re.MULTILINE | re.IGNORECASE)
_NOTES_END = re.compile(r"^#{1,4}[ \t]*Configuration[ \t]*$", re.MULTILINE | re.IGNORECASE)
_NOTES_HEADING = "## Release notes (third-party text copied into the PR body, data only)"
_UPSTREAM_HEADING = "## Release notes (fetched from upstream GitHub releases, untrusted third-party text, data only)"
# A dotted version, so a digest update is not taken for one.
_TARGET = re.compile(r"`[^`|]*`[ \t]*→[ \t]*`(?P<version>v?\d+\.\d+[0-9A-Za-z.+_-]{0,60})`")
_PACKAGE = re.compile(r"^(?:\[(?P<linked>[^\]|]{1,200})\]|(?P<plain>[^\s\[|]{1,200}))")
_SOURCE = re.compile(r"https://(?:redirect\.)?github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})"
                     r"/(?P<repo>[A-Za-z0-9_][A-Za-z0-9._-]{0,99})(?=[/)])")
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


_FILLERS = "\u3164\u2800\u00ad\u115f\u1160\uffa0\U00013441\U00013442"
# Emphasis characters between two alphanumerics add nothing a finding needs, and splitting a
# token or a hostname with them hides it from the patterns while the reader still sees it.
_INTRA_WORD = re.compile(r"(?<=[A-Za-z0-9_])[*~|]+(?=[A-Za-z0-9_])")


def _plain(text):
    """One round of undoing what a renderer or browser would undo, or what splits a pattern.

    Characters that print as nothing are removed (control, format, combining marks and the
    blank-looking letters), because a token with one inside still reads as that token.
    Real spaces stay, so words never join.
    """
    text = unquote(html.unescape(text))
    text = unicodedata.normalize("NFKD", _HTML.sub("", text)).replace("\\", "")
    text = "".join(c for c in text if c not in _FILLERS and unicodedata.category(c)[0] != "M")
    text = _INTRA_WORD.sub("", " ".join(text.split()))
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf"))


def scrub(text, limit=TEXT_LIMIT):
    """Text safe to post in public, or a fixed note when it looks like a credential.

    Patterns must see what the page will show, so every encoding comes off first. Text that
    never settles is withheld. clean() then leaves no character reference that could decode.
    """
    text = str(text)[:RAW_LIMIT]
    for _ in range(DECODE_ROUNDS):
        plain = _plain(text)
        if plain == text:
            break
        text = plain
    else:
        return WITHHELD_ENCODED
    if _CREDENTIAL.search(text):
        return WITHHELD_CREDENTIAL
    for pattern, note in _REDACTIONS:
        text = pattern.sub(note, text)
    return " ".join(clean(text, limit).split())


def assessment(text):
    """scrub() with more room, cut after the last whole sentence that fits.

    A sentence too long to fit is cut at a word and marked.
    """
    text = scrub(text, RAW_LIMIT)
    if len(text) <= ASSESSMENT_LIMIT:
        return text
    head = text[:ASSESSMENT_LIMIT + 1]
    ends = [m.end() for m in re.finditer(r"[.!?](?=\s)", head)]
    return text[:ends[-1]] if ends else head.rsplit(" ", 1)[0][:ASSESSMENT_LIMIT - 3] + "..."


def release_notes(body):
    """The release notes section of a Renovate PR body, without HTML, or an empty string."""
    start = _NOTES_START.search(body or "")
    if not start:
        return ""
    rest = body[start.end():]
    end = _NOTES_END.search(rest)
    return _HTML.sub("", rest[:end.start()] if end else rest).strip()


def upstream_targets(body, policy):
    """Which upstream GitHub releases could describe this update, from Renovate's update table.

    The table gives a package and a target version. The repository is the one in
    ai.upstream_releases for that package, else the row's own github.com link. Each name is
    matched in full, so all a PR body can do is point at another public GitHub release, which
    the prompt treats as untrusted anyway. It cannot reach any other host or path.
    """
    targets = {}
    for line in (body or "").splitlines():
        if line.strip() == "---":
            break
        cells = line.split("|")
        target = _TARGET.search(line)
        package = _PACKAGE.match(cells[1].strip()) if line.startswith("|") and len(cells) > 2 else None
        if not (target and package):
            continue
        known = policy["ai"]["upstream_releases"].get(package.group("linked") or package.group("plain")) or {}
        links = list(_SOURCE.finditer(cells[1]))
        repo = known.get("repo") or (f"{links[-1]['owner']}/{links[-1]['repo']}" if links else None)
        if repo:
            version = target.group("version").removeprefix("v")
            tags = [known["tag"].replace("{version}", version)] if known.get("tag") else [f"v{version}", version]
            targets.setdefault((repo, version), {"repo": repo, "version": version, "tags": tags})
    return list(targets.values())[:MAX_UPSTREAM]


def _upstream_notes(found, limit):
    """The heading and text for what the upstream lookup found. None means it did not run."""
    if found is None:
        return _NOTES_HEADING, NO_NOTES
    bodies = [f for f in found if f["body"]]
    tried = [f"{f['repo']} {f['version']}" for f in found if not f["body"]]
    if not bodies:
        detail = f"No upstream GitHub release was found either (tried {', '.join(tried)})." if tried else (
            "No upstream GitHub repository is known for this update either.")
        return _NOTES_HEADING, f"{NO_NOTES} {detail}"
    share = limit // len(bodies)
    parts = [f"### {f['repo']} {f['tag']} (the target release only, not the ones in between)\n"
             f"{_HTML.sub('', f['body']).strip()[:share]}" for f in bodies]
    parts += [f"No upstream GitHub release was found for {name}." for name in tried]
    return _UPSTREAM_HEADING, "\n\n".join(parts)[:limit]


def context(pr, render_lines, evidence, policy, upstream=None):
    """The bounded context file for the prompt. Labels are ours; the notes are third-party text.

    upstream is what the review job fetched for a PR without notes, or None if it did not look.
    """
    limits = policy["ai"]["context_chars"]
    notes = release_notes(pr.get("body"))[:limits["release_notes"]]
    heading, notes = (_NOTES_HEADING, notes) if notes else _upstream_notes(upstream, limits["release_notes"])
    return "\n".join([
        "## Render classifier (steward code, from flux-local against this PR)",
        "\n".join(render_lines)[:limits["render"]] or "Not available for this PR.",
        "",
        "## Cluster evidence (steward code)",
        evidence[:limits["evidence"]] if evidence else "None collected. Do not assume anything about the cluster's state.",
        "",
        heading,
        notes,
    ]) + "\n"


def context_stats(pr, render_lines, evidence, upstream=None):
    """Sizes of what the prompt carries, for the public job log. No content."""
    upstream = upstream or []
    return {"render_lines": len(render_lines), "release_notes_chars": len(release_notes(pr.get("body"))),
            "evidence_chars": len(evidence), "upstream_looked_for": len(upstream),
            "upstream_chars": sum(len(f["body"]) for f in upstream)}


def unavailable(key):
    return {"available": False, "reason": key if isinstance(key, str) and key in REASONS else "failed"}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


MAX_LINE = 1_000_000


def _line(value):
    """A 1-based line number from the model, or None.

    An int or a string of plain digits (PR-Agent reads both). Booleans, floats and anything
    else are not line numbers.
    """
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,7}", value.strip()):
        value = int(value)
    return value if type(value) is int and 1 <= value <= MAX_LINE else None


def _row(path, text, changed, first=False, start=None, end=None):
    """One finding. A path that is malformed, or outside the PR's diff, is left out.

    start and end are the model's line numbers in the head version of the file. They are only
    a claim: whoever posts on a line checks it against the diff first (inline.anchors).
    """
    path = _text(path)
    row = {"file": path if _PATH.match(path) and (changed is None or path in changed) else "",
           "text": assessment(text) if first and text.startswith(ASSESSMENT) else scrub(text)}
    start, end = _line(start), _line(end)
    if row["file"] and start and not first:
        row.update(start=start, end=end if end and end >= start else start)
    return row


def _items(value):
    """A list from the model. One mapping counts as one item; anything else as none."""
    return value if isinstance(value, list) else [value] if isinstance(value, dict) else []


def _findings(review, changed):
    issues, modes = _items(review.get("key_issues_to_review")), _items(review.get("failure_modes"))
    rows = [_row(i.get("relevant_file"), f"{_text(i.get('issue_header'))}: {_text(i.get('issue_content'))}", changed, n == 0,
                 i.get("start_line"), i.get("end_line"))
            for n, i in enumerate(i for i in issues[:MAX_ISSUES] if isinstance(i, dict))]
    return rows + [_row("", f"Could fail: {_text(m.get('what'))} Trigger: {_text(m.get('trigger'))}", changed)
                   for m in modes[:MAX_MODES] if isinstance(m, dict)]


def _shape(value):
    return "missing" if value is None else f"list:{len(value)}" if isinstance(value, list) else type(value).__name__


def shape(raw):
    """What the answer held, as counts and type names only, for the public job log."""
    review = raw.get("review") if isinstance(raw, dict) else None
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if not isinstance(review, dict):
        return {"review": _shape(review)}
    fields = ("key_issues_to_review", "failure_modes", "security_concerns")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    return {**{name: _shape(review.get(name)) for name in fields},
            "output_tokens": tokens if type(tokens) is int and tokens >= 0 else None}


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
        rows = [r for r in (rows if isinstance(rows, list) else [])[:MAX_ISSUES + MAX_MODES] if isinstance(r, dict)]
        result["findings"] = [_row(r.get("file"), _text(r.get("text")), changed, n == 0, r.get("start"), r.get("end"))
                              for n, r in enumerate(rows)]
        result.update({k: raw[k] for k, shape in _PROVENANCE.items()
                       if isinstance(raw.get(k), str) and shape.fullmatch(raw[k])})
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


def record(sha, result, fingerprint=None):
    """A line for the check run's output.text, so later events can reuse the result.

    Base64 keeps model text from ever forming another marker in that field. Without a
    fingerprint the record still serves its own commit, but no other head can reuse it.
    """
    data = base64.urlsafe_b64encode(json.dumps(result, sort_keys=True).encode()).decode()
    fp = f" fp={fingerprint}" if fingerprint else ""
    return f"<!-- merge-steward-llm sha={sha}{fp} data={data} -->"


def from_record(text, sha):
    """The result recorded for sha in a check run's output.text, or None."""
    found = _RECORD.search(text or "")
    if not found or found.group("sha") != sha:
        return None
    try:
        return checked(json.loads(base64.urlsafe_b64decode(found.group("data"))))
    except (binascii.Error, ValueError):
        return unavailable("not-recorded")


def reusable(text, sha, fingerprint, policy):
    """The result recorded for sha, if a head with this diff may use it instead of a new review.

    Everything has to match: the record's commit, its diff fingerprint, and a real answer from
    the model the policy names now. A failed review, or a record from before fingerprints, is
    never reused. The caller passes only text from the steward app's own check runs.
    """
    found = _RECORD.search(text or "")
    if not fingerprint or not found or found.group("fp") != fingerprint:
        return None
    result = from_record(text, sha)
    if not result or not result["available"] or result.get("model") != policy["ai"]["model"]["alias"]:
        return None
    return dict(result, reused_from=result.get("reused_from") or sha)


def advisory_record(fingerprint, result):
    """The line an advisory comment carries so a later push with the same diff can let it stand.

    Empty without a fingerprint or a well-formed model name: that comment is never reused.
    """
    model = result.get("model")
    if not fingerprint or not isinstance(model, str) or not _PROVENANCE["model"].fullmatch(model):
        return ""
    return f"<!-- merge-steward-review-diff fp={fingerprint} model={model} -->"


def advisory_reusable(body, fingerprint, policy):
    """True when an advisory comment's body names this diff and the model the policy names now.

    Exactly one record line must be present. The caller passes only the steward app's comment.
    """
    found = _ADVISORY_RECORD.findall(body or "")
    return (bool(fingerprint) and len(found) == 1 and found[0][0] == fingerprint
            and found[0][1] == policy["ai"]["model"]["alias"])


def stage(result, policy):
    """The llm stage. policy.yaml ai.needs_human_on names the answers that lower the verdict."""
    if not result["available"]:
        return Stage("llm", NEEDS, f"{UNAVAILABLE}: {REASONS[result['reason']]}")
    rule = policy["ai"]["needs_human_on"]
    said = f"risk {result['risk']}, recommendation {result['recommendation']}"
    lowered = result["risk"] in rule["risks"] or result["recommendation"] in rule["recommendations"]
    return Stage("llm", NEEDS if lowered else PASS, f"AI assessment: {said}")


def is_unavailable(stage_result):
    return stage_result.name == "llm" and stage_result.reason.startswith(UNAVAILABLE)
