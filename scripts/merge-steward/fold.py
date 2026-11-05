"""Fold stage results into one verdict and render the check summary and PR comment.

The default verdict is needs-human. Pass needs every expected stage to pass.
"""

import re

from classify import HOLD, NEEDS, PASS

MARKER = "<!-- merge-steward -->"
EXPECTED = ("identity", "commits", "policy", "checks", "render")
ICON = {PASS: "pass", HOLD: "hold", NEEDS: "needs-human"}
# Reasons carry package names, paths and check names taken from the PR. Strip markdown and
# HTML control characters and @ so nothing renders as a link, image, mention or table cell.
_UNSAFE = re.compile(r"[`<>@!\[\]|\r\n]")


def clean(text, limit=200):
    return _UNSAFE.sub(" ", str(text))[:limit]


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


def summary(stages, verdict, reason):
    lines = [f"**Verdict:** {ICON[verdict]} ({clean(reason)})", "", "| Stage | Result | Reason |", "|---|---|---|"]
    for stage in stages:
        lines.append(f"| {stage.name} | {ICON.get(stage.verdict, 'unknown')} | {clean(stage.reason)} |")
    return "\n".join(lines)


def comment(stages, verdict, reason, sha, override_label):
    body = [MARKER, f"### Merge steward: {ICON[verdict]}", "", summary(stages, verdict, reason), "",
            f"Evaluated commit `{sha[:12]}`. The verdict comes from deterministic rules in "
            "`scripts/merge-steward/policy.yaml` on the default branch."]
    if verdict != PASS:
        body += ["", f"To merge anyway, a repository admin adds the `{override_label}` label. "
                 "The steward checks the labeller's permission and ignores the label otherwise."]
    return "\n".join(body)
