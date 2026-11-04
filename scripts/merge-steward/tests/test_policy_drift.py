"""The steward policy must cover every Renovate rule that turns automerge off."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from classify import ALL_TYPES, glob_match, load_policy

ROOT = Path(__file__).resolve().parents[3]
POLICY = load_policy(ROOT / "scripts/merge-steward/policy.yaml")
# Patterns Renovate turns off and then opts back in for one update type, or handles in code.
EXEMPT = {
    "ghcr.io/jlengelbrecht/groschen": "only main digest updates are enabled, and those automerge",
}


def json5(path):
    script = 'process.stdout.write(JSON.stringify(require("vm").runInNewContext("("+process.argv[1]+")",{},{timeout:1000})))'
    return json.loads(subprocess.check_output(["node", "-e", script, path.read_text()], text=True))


def manual_rules():
    rules = []
    for path in (".github/renovate/autoMerge.json5", ".github/renovate.json5"):
        rules += [r for r in json5(ROOT / path)["packageRules"]
                  if r.get("automerge") is False and r.get("enabled") is not False and "matchCurrentVersion" not in r]
    return rules


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to read the Renovate config")
def test_every_manual_renovate_rule_is_covered():
    rules = manual_rules()
    assert rules, "found no manual Renovate rules; the parser is broken"
    gaps = []
    for rule in rules:
        types = rule.get("matchUpdateTypes", ALL_TYPES)
        if rule.get("matchFileNames"):
            # File-scoped rules: every file pattern must sit under a protected path.
            for pattern in rule["matchFileNames"]:
                probe = pattern.replace("**", "x/y").replace("*", "x")
                if not any(glob_match(g, probe) for g in POLICY["protected_paths"]):
                    gaps.append(f"path {pattern}")
            continue
        for name in rule.get("matchPackageNames", []):
            if name.startswith("!") or name in EXEMPT:
                continue
            covered = [r for r in POLICY["manual_packages"] if name in r["match"]]
            if not covered or not set(types) <= set(covered[0]["update_types"]):
                gaps.append(f"package {name} {types}")
    assert not gaps, "policy.yaml misses Renovate manual rules: " + "; ".join(gaps)
