"""Policy loading plus the identity and policy stages of the merge steward.

Every function here is pure: it takes PR data that the caller already fetched and
returns Stage results. Nothing here calls GitHub.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PASS, HOLD, NEEDS = "pass", "hold", "needs-human"
ALL_TYPES = ["digest", "pinDigest", "pin", "patch", "minor", "major", "replacement", "rollback", "bump"]
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
# A version number standing on its own: not part of a digest, hash or longer word.
_VERSION = re.compile(r"(?<![0-9A-Za-z])v?(\d+(?:\.\d+)*)(?![0-9A-Za-z])")


@dataclass
class Stage:
    name: str
    verdict: str
    reason: str


def load_policy(path):
    policy = yaml.safe_load(Path(path).read_text())
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise ValueError("unsupported policy file")
    for key in ("authors", "protected_paths", "manual_packages", "pass_update_types", "ignore_checks"):
        if not isinstance(policy.get(key), list):
            raise ValueError(f"policy key {key} must be a list")
    required = policy.get("required_checks")
    if not isinstance(required, list) or not required:
        raise ValueError("policy key required_checks must be a non-empty list")
    for check in required:
        if not isinstance(check, dict) or not check.get("name") or not isinstance(check.get("app_id"), int):
            raise ValueError("each required check needs a name and an integer app_id")
    if not isinstance(policy.get("steward_app_id"), int):
        raise ValueError("policy key steward_app_id must be an integer")
    deadline = policy.get("required_checks_deadline_minutes")
    if not isinstance(deadline, int) or deadline <= 0:
        raise ValueError("policy key required_checks_deadline_minutes must be a positive integer")
    for rule in policy["manual_packages"]:
        if rule["update_types"] == "all":
            rule["update_types"] = list(ALL_TYPES)
    return policy


def glob_match(pattern, path):
    """Match a path against a glob where ** spans directories and * stays in one."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.fullmatch(out, path) is not None


def name_match(pattern, name):
    """Renovate package-name syntax: "/regex/" searches, anything else is exact."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], name) is not None
    return pattern == name


def parse_packages(body):
    """Read Renovate's package table (the first table in the PR body).

    Returns a list of {name, update, current}; update and current may be None.
    Release notes come after this table and are never parsed.
    """
    lines = (body or "").splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("| Package")), None)
    if start is None:
        return []
    header = [c.strip().lower() for c in lines[start].strip().strip("|").split("|")]
    rows = []
    for line in lines[start + 2:]:
        if not line.strip().startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        row = dict(zip(header, cells))
        link = _LINK.search(row.get("package", ""))
        name = (link.group(1) if link else row.get("package", "")).strip("` ")
        change = row.get("change", "").split("→")[0]
        version = _VERSION.search(change)
        rows.append({"name": name, "update": row.get("update") or None,
                     "current": version.group(1) if version else None})
    return rows


def identity(pr, commits, policy):
    author = pr["user"]["login"]
    if author not in policy["authors"] or pr["user"].get("type") != "Bot":
        return Stage("identity", NEEDS, f"author {author} is not on the steward allowlist")
    if pr["head"]["repo"] is None or pr["head"]["repo"]["full_name"] != pr["base"]["repo"]["full_name"]:
        return Stage("identity", NEEDS, "head branch is not in this repository")
    if pr["base"]["ref"] != policy["base_branch"]:
        return Stage("identity", NEEDS, f"base branch is not {policy['base_branch']}")
    others = sorted({(c.get("author") or {}).get("login") or "unknown" for c in commits} - set(policy["authors"]))
    if others or not commits:
        return Stage("identity", NEEDS, "branch has commits from other authors: " + ", ".join(others or ["none"]))
    return Stage("identity", PASS, f"opened by {author}")


def _label_update_type(labels):
    order = ["major", "minor", "patch", "pin", "pinDigest", "digest"]
    found = [t for t in order if f"update/{t}" in labels]
    return found[0] if found else None


def policy_stage(pr, files, job_files, policy):
    """Hard policy: protected paths, Job manifests, breaking markers, then per package."""
    for path in files:
        hit = next((g for g in policy["protected_paths"] if glob_match(g, path)), None)
        if hit:
            return Stage("policy", NEEDS, f"changes protected path {path}")
    if job_files:
        return Stage("policy", NEEDS, f"changes a Job manifest: {job_files[0]}")
    labels = {l["name"] for l in pr["labels"]}
    if "breaking" in labels or "!:" in pr["title"]:
        return Stage("policy", NEEDS, "Renovate marked the update as breaking")
    packages = parse_packages(pr.get("body"))
    if not packages:
        return Stage("policy", NEEDS, "no Renovate package table in the PR body")
    fallback = _label_update_type(labels)
    for pkg in packages:
        kind = pkg["update"] or fallback
        if not pkg["name"] or not kind:
            return Stage("policy", NEEDS, f"cannot classify package {pkg['name'] or '?'}")
        if kind not in policy["pass_update_types"]:
            return Stage("policy", NEEDS, f"{pkg['name']} is a {kind} update")
        if kind == "minor" and not pkg["current"]:
            return Stage("policy", NEEDS, f"{pkg['name']} minor: cannot read the current version")
        if kind == "minor" and pkg["current"].split(".")[0] == "0":
            return Stage("policy", NEEDS, f"{pkg['name']} is a 0.x minor update")
        for rule in policy["manual_packages"]:
            if kind in rule["update_types"] and any(name_match(p, pkg["name"]) for p in rule["match"]):
                return Stage("policy", NEEDS, f"{pkg['name']} {kind}: {rule['reason']}")
    names = ", ".join(f"{p['name']} ({p['update'] or fallback})" for p in packages)
    return Stage("policy", PASS, f"default tier: {names}")
