"""The steward policy must cover every Renovate rule that turns automerge off."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

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


@needs_node
def test_release_exceptions_mirror_a_renovate_automerge_rule():
    rules = json5(ROOT / ".github/renovate.json5")["packageRules"]
    assert POLICY["release_exceptions"], "found no release exceptions; the parser is broken"
    for exception in POLICY["release_exceptions"]:
        package = exception["package"]
        mirrored = [r for r in rules if package in r.get("matchPackageNames", []) and r.get("automerge") is True]
        assert len(mirrored) == 1, f"{package} needs exactly one Renovate automerge rule"
        assert mirrored[0]["matchFileNames"] == [exception["file"]]
        assert mirrored[0]["allowedVersions"] == f"/{exception['tag_pattern']}/"
        assert mirrored[0]["ignoreUnstable"] is False  # prereleases are part of the exception
        manual = [r for r in manual_rules() if package in r.get("matchPackageNames", [])]
        assert not manual, f"{package} has an exception here and a manual rule in Renovate"


def test_release_exceptions_name_the_real_object():
    for exception in POLICY["release_exceptions"]:
        doc = yaml.safe_load((ROOT / exception["file"]).read_text())
        target = exception["oci_repository"]
        assert doc["kind"] == "OCIRepository" and doc["spec"]["url"] == target["url"]
        assert (doc["metadata"]["namespace"], doc["metadata"]["name"]) == (target["namespace"], target["name"])
        assert target["url"] == f"oci://{exception['package']}"
        assert not any(glob_match(g, exception["file"]) for g in POLICY["protected_paths"])


def test_no_manual_package_rule_shadows_a_release_exception():
    for exception in POLICY["release_exceptions"]:
        for rule in POLICY["manual_packages"]:
            assert not any(name_match(p, exception["package"]) for p in rule["match"]), rule["reason"]


def test_regex_matchers_catch_every_image_name():
    rules = {r["reason"]: r for r in POLICY["manual_packages"]}
    for name in ("docker.io/nousresearch/hermes-agent", "hermes-agent", "ghcr.io/vectorize-io/hindsight-api",
                 "hindsight", "ghcr.io/paperclipai/paperclip", "n8nio/n8n", "docker.io/n8nio/n8n"):
        assert any(any(name_match(p, name) for p in r["match"]) for r in rules.values()), name
