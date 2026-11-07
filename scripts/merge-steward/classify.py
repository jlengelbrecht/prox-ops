"""Policy loading plus the identity and policy stages of the merge steward.

Every function here is pure: it takes PR data that the caller already fetched and
returns Stage results. Nothing here calls GitHub.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

PASS, HOLD, NEEDS = "pass", "hold", "needs-human"
OUT_OF_SCOPE = "not a Renovate PR, out of scope"
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
    for key in ("authors", "protected_paths", "manual_packages", "pass_update_types", "ignore_checks",
                "release_exceptions"):
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
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*\[bot\]", str(policy.get("steward_app_login") or "")):
        raise ValueError("policy key steward_app_login must be the app's bot login, ending in [bot]")
    if not isinstance(policy.get("commit_signer"), str) or not policy["commit_signer"]:
        raise ValueError("policy key commit_signer must be a login")
    deadline = policy.get("required_checks_deadline_minutes")
    if not isinstance(deadline, int) or deadline <= 0:
        raise ValueError("policy key required_checks_deadline_minutes must be a positive integer")
    for rule in policy["release_exceptions"]:
        target = rule.get("oci_repository") or {}
        if not (rule.get("package") and rule.get("file") and all(target.get(k) for k in ("namespace", "name", "url"))):
            raise ValueError("a release exception needs a package, a file and an oci_repository")
        if not rule.get("update_types") or not set(rule["update_types"]) <= set(policy["pass_update_types"]):
            raise ValueError("a release exception may only name update types that can pass")
        re.compile(rule["tag_pattern"])
    _check_ai(policy.get("ai"))
    for rule in policy["manual_packages"]:
        if rule["update_types"] == "all":
            rule["update_types"] = list(ALL_TYPES)
    return policy


def _check_ai(ai):
    """An unknown value stops the steward rather than giving the AI an unintended role."""
    if not isinstance(ai, dict) or ai.get("verdict_authority") != "lower_only":
        raise ValueError("policy key ai.verdict_authority must be lower_only")
    lowers = ai.get("needs_human_on") or {}
    risks, recommendations = set(lowers.get("risks") or []), set(lowers.get("recommendations") or [])
    if not ("high" in risks <= {"low", "medium", "high"}
            and "changes_required" in recommendations <= {"no_concerns_found", "needs_review", "changes_required"}):
        raise ValueError("policy key ai.needs_human_on must keep high and changes_required")
    model = ai.get("model") or {}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", str(model.get("alias") or "")):
        raise ValueError("policy key ai.model.alias must be a model name")
    numbers = ("context_tokens", "max_prompt_tokens", "call_timeout_seconds", "review_timeout_seconds")
    if set(model) != {"alias", *numbers}:
        raise ValueError("policy key ai.model must have exactly alias and the four limits")
    if not all(type(model[k]) is int and model[k] > 0 for k in numbers):
        raise ValueError("policy key ai.model needs positive token limits and timeouts")
    if model["max_prompt_tokens"] >= model["context_tokens"] or model["call_timeout_seconds"] > model["review_timeout_seconds"]:
        raise ValueError("policy key ai.model has limits that cannot work together")
    scope = ai.get("review_scope") or {}
    if scope.get("renovate") not in ("verdict", "advisory", "off") or scope.get("same_repo") not in ("advisory", "off"):
        raise ValueError("policy key ai.review_scope has an unknown value")
    limits = ai.get("context_chars") or {}
    if not all(isinstance(limits.get(k), int) and limits[k] > 0 for k in ("render", "evidence", "release_notes")):
        raise ValueError("policy key ai.context_chars needs render, evidence and release_notes")
    upstream = ai.get("upstream_releases")
    if not isinstance(upstream, dict) or not all(_upstream_entry(v) for v in upstream.values()):
        raise ValueError("policy key ai.upstream_releases maps a package to a repo and an optional tag with {version}")


def _upstream_entry(entry):
    return (isinstance(entry, dict) and set(entry) <= {"repo", "tag"}
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9._-]*", str(entry.get("repo") or ""))
            and ("tag" not in entry or re.fullmatch(r"[A-Za-z0-9._/-]*\{version\}[A-Za-z0-9._-]*", str(entry["tag"]))))


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


def _parties(pr):
    """Author login and type, head repo and base repo, from the API's PR object; None when missing."""
    user = pr.get("user") or {}
    head = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    return user.get("login"), user.get("type"), head, base


def out_of_scope(pr, policy):
    """True only for a readable, same-repo PR opened by a person.

    Anything else goes to identity(): an unreadable author or head repo, every fork, every
    Bot (a retired or other dependency app can hold auto-merge), and a login that differs
    from Renovate's only by case.
    """
    login, kind, head, base = _parties(pr)
    if not (login and head and base) or kind != "User" or head != base:
        return False
    return login.casefold() not in {a.casefold() for a in policy["authors"]}


def _signed(commit, policy):
    verification = (commit.get("commit") or {}).get("verification") or {}
    return ((commit.get("committer") or {}).get("login") == policy["commit_signer"]
            and verification.get("verified") is True)


def _renovate_authored(commit, policy):
    author = commit.get("author") or {}
    return author.get("login") in policy["authors"] and author.get("type") == "Bot"


def is_renovate_push(commit, policy):
    """True for a commit Renovate itself made: one parent, its author, and GitHub's signature.

    Renovate commits through the GitHub API, so web-flow is the committer. A plain git push
    can forge both names but not that signature.
    """
    return len(commit.get("parents") or []) == 1 and _renovate_authored(commit, policy) and _signed(commit, policy)


def commit_problem(commits, policy):
    """Why the branch is not one Renovate-authored commit plus GitHub merges, or None.

    A person's update-branch leaves one of two shapes. A merge: GitHub-signed, authored by
    that person. A rebase: Renovate still the author, that person the committer, no
    signature. Neither says anything about the content, and git can forge the second, so
    steward.commit_stage compares the branch with Renovate's last push.
    """
    own, others = [], set()
    for commit in commits:
        parents = len(commit.get("parents") or [])
        if parents == 1 and _renovate_authored(commit, policy):
            own.append(commit)
        elif not (parents == 2 and _signed(commit, policy)):
            others.add((commit.get("author") or {}).get("login") or "unknown")
    if others:
        return "branch has commits not made by Renovate: " + ", ".join(sorted(others))
    if len(own) != 1:
        return f"branch has {len(own)} Renovate commits, expected exactly one"
    return None


def identity(pr, commits, policy):
    login, kind, head, base = _parties(pr)
    if not login or not kind:
        return Stage("identity", NEEDS, "the PR author is unreadable")
    if not head or not base:
        return Stage("identity", NEEDS, "the head repository is unreadable")
    if head != base:
        return Stage("identity", NEEDS, "head branch is not in this repository")
    if login not in policy["authors"] or kind != "Bot":
        return Stage("identity", NEEDS, f"author {login} is not the Renovate app")
    if pr["base"]["ref"] != policy["base_branch"]:
        return Stage("identity", NEEDS, f"base branch is not {policy['base_branch']}")
    problem = commit_problem(commits, policy)
    if problem:
        return Stage("identity", NEEDS, problem)
    return Stage("identity", PASS, f"opened by {login}")


def _label_update_type(labels):
    order = ["major", "minor", "patch", "pin", "pinDigest", "digest"]
    found = [t for t in order if f"update/{t}" in labels]
    return found[0] if found else None


def release_exception(packages, files, policy):
    """The exception covering the whole PR, or None. Any other package or file in the PR rules it out."""
    if len(packages) != 1 or len(files) != 1:
        return None
    return next((rule for rule in policy["release_exceptions"]
                 if rule["package"] == packages[0]["name"] and rule["file"] == files[0]), None)


def policy_stage(pr, files, job_files, policy):
    """Hard policy: protected paths, Job manifests, breaking markers, then per package."""
    for path in files:
        hit = next((g for g in policy["protected_paths"] if glob_match(g, path)), None)
        if hit:
            return Stage("policy", NEEDS, f"changes protected path {path}")
    if job_files:
        return Stage("policy", NEEDS, f"changes a Job manifest: {job_files[0]}")
    labels = {l["name"] for l in pr["labels"]}
    packages = parse_packages(pr.get("body"))
    fallback = _label_update_type(labels)
    exception = release_exception(packages, files, policy)
    if exception and (packages[0]["update"] or fallback) not in exception["update_types"]:
        exception = None
    if not exception and ("breaking" in labels or "!:" in pr["title"]):
        return Stage("policy", NEEDS, "Renovate marked the update as breaking")
    if not packages:
        return Stage("policy", NEEDS, "no Renovate package table in the PR body")
    for pkg in packages:
        kind = pkg["update"] or fallback
        if not pkg["name"] or not kind:
            return Stage("policy", NEEDS, f"cannot classify package {pkg['name'] or '?'}")
        if kind not in policy["pass_update_types"]:
            return Stage("policy", NEEDS, f"{pkg['name']} is a {kind} update")
        if kind == "minor" and not pkg["current"]:
            return Stage("policy", NEEDS, f"{pkg['name']} minor: cannot read the current version")
        if kind == "minor" and pkg["current"].split(".")[0] == "0" and not exception:
            return Stage("policy", NEEDS, f"{pkg['name']} is a 0.x minor update")
        for rule in policy["manual_packages"]:
            if kind in rule["update_types"] and any(name_match(p, pkg["name"]) for p in rule["match"]):
                return Stage("policy", NEEDS, f"{pkg['name']} {kind}: {rule['reason']}")
    names = ", ".join(f"{p['name']} ({p['update'] or fallback})" for p in packages)
    return Stage("policy", PASS, f"{'release exception' if exception else 'default tier'}: {names}")
