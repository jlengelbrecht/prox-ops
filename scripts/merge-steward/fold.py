"""Fold stage results into one verdict and render the check summary and PR comment.

The default verdict is needs-human. Pass needs every expected stage to pass, so a stage
can only ever lower the result. That includes the AI stage.
"""

import re

from classify import HOLD, NEEDS, PASS

MARKER = "<!-- merge-steward -->"
ADVISORY_MARKER = "<!-- merge-steward-review -->"
EXPECTED = ("identity", "commits", "policy", "checks", "render")
ICON = {PASS: "pass", HOLD: "hold", NEEDS: "needs-human"}
# Reasons carry package names, paths and check names taken from the PR. Strip markdown and
# HTML control characters and @, and neutralise character references, so nothing renders as
# a link, image, mention or table cell.
_UNSAFE = re.compile(r"[`~<>@!\[\]|\\\r\n]")
# GitHub decodes character references when it renders, so a `&` left alone could bring back
# anything stripped here (&#64; is @). A `&amp;` this function wrote stays as it is.
_AMPERSAND = re.compile(r"&(?!amp;)")


def clean(text, limit=200):
    return _AMPERSAND.sub("&amp;", _UNSAFE.sub(" ", str(text))[:limit])


def fold(stages, override=None):
    """Return (verdict, reason). override is the login of a verified admin, or None."""
    if override:
        return PASS, f"human-approved by {override}"
    for stage in stages:
        if stage.verdict not in (PASS, HOLD, NEEDS):
            return NEEDS, f"{stage.name}: unknown result"
    for stage in stages:
        if stage.verdict == NEEDS:
            return NEEDS, f"{stage.name}: {stage.reason}"
    for stage in stages:
        if stage.verdict == HOLD:
            return HOLD, f"{stage.name}: {stage.reason}"
    seen = {s.name for s in stages}
    missing = [name for name in EXPECTED if name not in seen]
    if missing:
        return NEEDS, "stage did not run: " + ", ".join(missing)
    return PASS, "all stages passed"


def ai_lines(result):
    """The AI's assessment as markdown. Its text was scrubbed by review.py."""
    if not result or not result.get("available"):
        return []
    lines = [f"**AI assessment:** risk {clean(result['risk'])}, recommendation {clean(result['recommendation'])}"]
    for row in result["findings"]:
        where = f"{clean(row['file'], 150)}: " if row["file"] else ""
        lines.append(f"- {where}{clean(row['text'], 320)}")
    return lines if len(lines) > 1 else lines + ["- No findings given."]


def summary(stages, verdict, reason, ai=None):
    lines = [f"**Verdict:** {ICON[verdict]} ({clean(reason)})", "", "| Stage | Result | Reason |", "|---|---|---|"]
    for stage in stages:
        lines.append(f"| {stage.name} | {ICON.get(stage.verdict, 'unknown')} | {clean(stage.reason)} |")
    extra = ai_lines(ai)
    return "\n".join(lines + ([""] + extra if extra else []))


def comment(stages, verdict, reason, sha, override_label, ai=None):
    body = [MARKER, f"### Merge steward: {ICON[verdict]}", "", summary(stages, verdict, reason, ai), "",
            f"Evaluated commit `{sha[:12]}`. The verdict comes from deterministic rules in "
            "`scripts/merge-steward/policy.yaml` on the default branch. The AI assessment can only lower it."]
    if verdict != PASS:
        body += ["", f"To merge anyway, a repository admin adds the `{override_label}` label. "
                 "The steward checks the labeller's permission and ignores the label otherwise."]
    return "\n".join(body)


def advisory(result, sha):
    """The comment for a PR whose review changes no verdict."""
    return "\n".join([ADVISORY_MARKER, "### AI review (advisory)", ""] + ai_lines(result) + [
        "", f"Reviewed commit `{sha[:12]}`. This comment does not affect any check."])
