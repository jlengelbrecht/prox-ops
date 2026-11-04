"""The steward policy must cover every Renovate rule that turns automerge off."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from classify import ALL_TYPES, glob_match, load_policy, name_match

ROOT = Path(__file__).resolve().parents[3]
POLICY = load_policy(ROOT / "scripts/merge-steward/policy.yaml")
# Patterns Renovate turns off and then opts back in for one update type, or handles in code.
EXEMPT = {
    "ghcr.io/jlengelbrecht/groschen": "only main digest updates are enabled, and those automerge",
}
# Matcher keys this test understands. Datasource, manager and current-value matchers only
# narrow a rule, so ignoring them makes the steward stricter, never looser. A new matcher
# key (matchDepNames, excludePackageNames, ...) fails the test until it is handled here.
KNOWN_MATCHERS = {"matchPackageNames", "matchUpdateTypes", "matchFileNames", "matchDatasources",
                  "matchManagers", "matchCurrentVersion", "matchCurrentValue"}


def json5(path):
    script = 'process.stdout.write(JSON.stringify(require("vm").runInNewContext("("+process.argv[1]+")",{},{timeout:1000})))'
    return json.loads(subprocess.check_output(["node", "-e", script, path.read_text()], text=True))


def manual_rules():
    rules = []
    for path in (".github/renovate/autoMerge.json5", ".github/renovate.json5"):
        rules += [r for r in json5(ROOT / path)["packageRules"]
                  if r.get("automerge") is False and r.get("enabled") is not False]
    return rules


def covered(name, types):
    """A Renovate pattern is covered when a policy rule lists it verbatim or, for an exact
    name, a policy pattern matches it; that rule must hold every one of the update types."""
    for rule in POLICY["manual_packages"]:
        hit = name in rule["match"] or (not name.startswith("/") and any(name_match(p, name) for p in rule["match"]))
        if hit and set(types) <= set(rule["update_types"]):
            return True
    return False


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to read the Renovate config")


@needs_node
def test_manual_rules_use_only_known_matchers():
    unknown = sorted({k for r in manual_rules() for k in r
                      if (k.startswith(("match", "exclude"))) and k not in KNOWN_MATCHERS})
    assert not unknown, "Renovate manual rules use matchers the drift test does not handle: " + ", ".join(unknown)


@needs_node
def test_name_less_manual_rules_never_cover_a_pass_type():
    """A rule with no package or file matcher applies to every package: its update types must
    already be needs-human by update type alone. 0.x minors are handled by their own rule."""
    for rule in manual_rules():
        if rule.get("matchPackageNames") or rule.get("matchFileNames") or "matchCurrentVersion" in rule:
            continue
        types = set(rule.get("matchUpdateTypes", ALL_TYPES))
        assert not types & set(POLICY["pass_update_types"]), f"name-less manual rule passes in the steward: {rule}"


@needs_node
def test_every_manual_renovate_rule_is_covered():
    rules = [r for r in manual_rules() if "matchCurrentVersion" not in r]
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
            if not covered(name, types):
                gaps.append(f"package {name} {types}")
    assert not gaps, "policy.yaml misses Renovate manual rules: " + "; ".join(gaps)


def test_regex_matchers_catch_every_image_name():
    rules = {r["reason"]: r for r in POLICY["manual_packages"]}
    for name in ("docker.io/nousresearch/hermes-agent", "hermes-agent", "ghcr.io/vectorize-io/hindsight-api",
                 "hindsight", "ghcr.io/paperclipai/paperclip", "n8nio/n8n", "docker.io/n8nio/n8n"):
        assert any(any(name_match(p, name) for p in r["match"]) for r in rules.values()), name
