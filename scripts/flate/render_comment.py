"""Write the pull request diff comment from two full flate renders.

Input is `flate build all -o json` for the base and for the PR merge commit. The objects
are paired by scripts/merge-steward/render_pairs.py, the same pairing the merge steward
classifies, so the comment and the steward look at the same set of changes. Both sides are
rendered in full: an empty result means the two renders are equal, not that a tool found
nothing to compare.

Output is plain text for a ```diff block. Objects are printed in a YAML-like layout that
is meant for reading, not for parsing. CRD bodies are left out. Secrets are never
rendered.

Standard library only. Anything it cannot read is an error, never an empty diff.
"""

import argparse
import difflib
import importlib.util
import json
import pathlib
import re
import sys

PAIRS = pathlib.Path(__file__).resolve().parents[1] / "merge-steward" / "render_pairs.py"
# One object can be a whole dashboard. Cut each object's diff so one of them cannot push
# every other change out of the comment.
OBJECT_LINES = 120
CONTEXT = 3
_FENCE = re.compile(r"`{3,}|~{3,}")


def _pairs():
    spec = importlib.util.spec_from_file_location("render_pairs", PAIRS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _scalar(value):
    return json.dumps(value, ensure_ascii=False)


def text(value, indent=0):
    """The lines of value in a YAML-like layout. Mappings are sorted by key."""
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return [pad + "{}"]
        lines = []
        for key in sorted(value, key=str):
            lines.extend(_entry(pad + _key(key) + ":", value[key], indent + 1))
        return lines
    if isinstance(value, list):
        if not value:
            return [pad + "[]"]
        lines = []
        for item in value:
            lines.extend(_entry(pad + "-", item, indent + 1))
        return lines
    return [pad + _scalar(value)]


def _key(key):
    key = str(key)
    return key if key and all(c.isalnum() or c in "._/-" for c in key) else _scalar(key)


def _entry(head, value, indent):
    """head is `key:` or `-`; value goes beside it when it fits on one line."""
    if isinstance(value, str) and "\n" in value:
        pad = "  " * indent
        return [head + " |"] + [pad + line if line else "" for line in value.split("\n")]
    if isinstance(value, (dict, list)) and value:
        return [head] + text(value, indent)
    return [head + " " + text(value)[0]]


def _safe(line):
    """One printable line that cannot close the code block it is shown in."""
    line = "".join(c if c == "\t" or c.isprintable() else "?" for c in line)
    return _FENCE.sub(lambda run: " ".join(run.group()[0] * 3), line)


def _name(change):
    where = change["namespace"] + "/" if change["namespace"] else ""
    return f'{change["kind"]} {where}{change["name"]}'


def _status(change):
    if change["before"] is None:
        return "added"
    if change["after"] is None:
        return "removed"
    return "changed"


def _object_diff(change):
    before = [] if change["before"] is None else text(change["before"])
    after = [] if change["after"] is None else text(change["after"])
    if change["kind"] in _pairs().BODYLESS:
        return ["  (CRD body not shown)"]
    lines = [
        line for line in difflib.unified_diff(before, after, lineterm="", n=CONTEXT)
        if not line.startswith(("--- ", "+++ "))
    ]
    if len(lines) > OBJECT_LINES:
        more = len(lines) - OBJECT_LINES
        lines = lines[:OBJECT_LINES] + [f"  ({more} more lines not shown)"]
    return lines


def comment(result):
    """The comment text for the output of render_pairs.pair()."""
    changes = result["changes"]
    if not changes:
        return f'No rendered changes. Both renders hold {result["head"]} objects.\n'
    counts = {"added": 0, "removed": 0, "changed": 0}
    for change in changes:
        counts[_status(change)] += 1
    out = [
        f'{len(changes)} rendered objects differ: {counts["changed"]} changed, '
        f'{counts["added"]} added, {counts["removed"]} removed. '
        f'Base holds {result["base"]} objects, this change {result["head"]}.',
        "",
    ]
    for change in changes:
        note = " (two producers render this object)" if change.get("ambiguous") else ""
        out.append(f"=== {_name(change)} ({_status(change)}){note}")
        out.extend(_object_diff(change))
        out.append("")
    return "\n".join(_safe(line) for line in out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base", help="flate build all -o json of the base")
    parser.add_argument("head", help="flate build all -o json of the PR merge commit")
    args = parser.parse_args(argv)
    try:
        renders = []
        for path in (args.base, args.head):
            with open(path, encoding="utf-8") as handle:
                renders.append(json.load(handle))
        # Release attribution is the steward's concern; the comment does not use it.
        result = comment(_pairs().pair(renders[0], renders[1], [], []))
    except Exception as err:  # the caller reports a failed render
        print(f"render comment failed ({type(err).__name__})", file=sys.stderr)
        return 1
    sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
