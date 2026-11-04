import copy
import json
from pathlib import Path

import pytest
import yaml

from classify import (NEEDS, PASS, glob_match, identity, is_renovate_push, load_policy, out_of_scope,
                      parse_packages, policy_stage)

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
REPO = {"full_name": "o/r"}


def table(*rows, header="| Package | Update | Change |"):
    return "\n".join(["This PR contains the following updates:", "", header, "|---|---|---|", *rows])


def make_pr(body, labels=("update/patch",), title="fix(container): update app", login="renovate[bot]"):
    return {"number": 1, "title": title, "body": body, "labels": [{"name": n} for n in labels],
            "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "head": {"repo": REPO, "sha": "a" * 40}, "base": {"repo": REPO, "ref": "main"}}


# Real commits from the API: a Renovate commit, a GitHub update-branch merge on a Renovate
# branch (its author is whoever asked for it), and a server-side rebase (Renovate stays the
# author, the person becomes the committer, and the signature is gone).
REAL = json.loads((Path(__file__).parent / "fixtures" / "commits.json").read_text())
RENOVATE = REAL["renovate_commit_pr1876"]
MERGE = REAL["update_branch_merge_pr1691"]
REBASE = REAL["server_side_rebase_pr1813"]
RENOVATE_COMMIT = [RENOVATE]


def commit(base=RENOVATE, **changes):
    """A copy of a real commit with top-level fields replaced."""
    return {**copy.deepcopy(base), **changes}


def unsigned(base=RENOVATE):
    changed = copy.deepcopy(base)
    changed["commit"]["verification"] = {"verified": False, "reason": "unsigned"}
    return changed


def test_parse_packages_handles_the_three_renovate_table_shapes():
    plain = parse_packages(table("| louislam/uptime-kuma | minor | `2.4.0` → `2.5.8` |"))
    assert plain == [{"name": "louislam/uptime-kuma", "update": "minor", "current": "2.4.0"}]
    typed = parse_packages(table("| [external-secrets](https://x) ([source](https://y)) | HelmChart | minor | `1.1.1` → `1.3.2` |",
                                 header="| Package | Type | Update | Change |"))
    assert typed[0]["name"] == "external-secrets" and typed[0]["update"] == "minor"
    no_update = parse_packages(table("| [anyio](https://x) | `==4.15.1` → `==4.16.0` | age | conf |",
                                     header="| Package | Change | Age | Confidence |"))
    assert no_update == [{"name": "anyio", "update": None, "current": "4.15.1"}]


def test_parse_packages_ignores_tables_after_the_first():
    body = table("| app | patch | `1.0.0` → `1.0.1` |") + "\n\n### Release Notes\n| Package | Update | Change |\n|-|-|-|\n| evil | major | x |"
    assert [p["name"] for p in parse_packages(body)] == ["app"]


@pytest.mark.parametrize("pattern,path,expected", [
    ("talos/**", "talos/talenv.yaml", True),
    ("**/*.sops.yaml", "kubernetes/apps/a/secret.sops.yaml", True),
    ("**/*.sops.yaml", "secret.sops.yaml", True),
    ("kubernetes/apps/network/**", "kubernetes/apps/networking/x.yaml", False),
    (".github/workflows/**", ".github/workflows/a.yaml", True),
])
def test_glob_match(pattern, path, expected):
    assert glob_match(pattern, path) is expected


def test_identity_accepts_a_real_renovate_commit():
    assert identity(make_pr(""), RENOVATE_COMMIT, POLICY).verdict == PASS


def test_identity_refuses_any_other_author_it_is_asked_about():
    human = identity(make_pr("", login="someone"), RENOVATE_COMMIT, POLICY)
    assert human.verdict == NEEDS and "not the Renovate app" in human.reason


def set_author(pr, login, kind):
    pr["user"] = {"login": login, "type": kind}
    return pr


@pytest.mark.parametrize("login,kind", [
    ("someone", "User"),
    ("renovate", "User"),  # a real account name that looks like the bot
    ("renovate-bot", "User"),
    ("renovatebot", "User"),
])
def test_same_repo_pr_by_a_person_is_out_of_scope(login, kind):
    assert out_of_scope(set_author(make_pr(""), login, kind), POLICY) is True


@pytest.mark.parametrize("login,kind", [
    ("homebot-0[bot]", "Bot"),  # #1809: a retired app's n8n minor with auto-merge enabled
    ("dependabot[bot]", "Bot"),
    ("renovate-bot[bot]", "Bot"),
    ("someone", "Mannequin"),
    ("some-org", "Organization"),
])
def test_anything_but_a_person_is_needs_human(login, kind):
    pr = set_author(make_pr(""), login, kind)
    assert out_of_scope(pr, POLICY) is False
    result = identity(pr, RENOVATE_COMMIT, POLICY)
    assert result.verdict == NEEDS and "not the Renovate app" in result.reason


def test_exact_renovate_pr_is_in_scope():
    assert out_of_scope(make_pr(""), POLICY) is False


@pytest.mark.parametrize("login,kind", [("renovate[bot]", "User"), ("Renovate[bot]", "Bot"), ("RENOVATE[BOT]", "User")])
def test_renovate_lookalike_logins_are_never_out_of_scope(login, kind):
    pr = set_author(make_pr(""), login, kind)
    assert out_of_scope(pr, POLICY) is False
    assert identity(pr, RENOVATE_COMMIT, POLICY).verdict == NEEDS


@pytest.mark.parametrize("login,kind", [("renovate[bot]", "Bot"), ("someone", "User"), ("dependabot[bot]", "Bot"),
                                        ("renovate", "User")])
def test_fork_pr_is_needs_human_for_any_author(login, kind):
    pr = set_author(make_pr(""), login, kind)
    pr["head"]["repo"] = {"full_name": "fork/r"}
    pr["head"]["ref"] = "renovate/looks-real"
    assert out_of_scope(pr, POLICY) is False
    result = identity(pr, RENOVATE_COMMIT, POLICY)
    assert result.verdict == NEEDS and "not in this repository" in result.reason


def test_renovate_text_in_the_title_body_or_branch_changes_nothing():
    pr = set_author(make_pr("renovate[bot]", title="chore: renovate[bot]"), "someone", "User")
    pr["head"]["ref"] = "renovate/lookalike"
    assert out_of_scope(pr, POLICY) is True
    assert identity(pr, RENOVATE_COMMIT, POLICY).verdict == NEEDS


@pytest.mark.parametrize("change,reason", [
    (lambda p: p.pop("user"), "author is unreadable"),
    (lambda p: p.update(user=None), "author is unreadable"),
    (lambda p: p.update(user={"login": "", "type": "User"}), "author is unreadable"),
    (lambda p: p.update(user={"login": "someone"}), "author is unreadable"),
    (lambda p: p["head"].update(repo=None), "head repository is unreadable"),  # a deleted fork
    (lambda p: p["head"].update(repo={}), "head repository is unreadable"),
    (lambda p: p["base"].update(repo=None), "head repository is unreadable"),
])
def test_unreadable_author_or_repo_fails_closed(change, reason):
    pr = make_pr("")
    pr["head"], pr["base"] = dict(pr["head"]), dict(pr["base"])
    change(pr)
    assert out_of_scope(pr, POLICY) is False
    result = identity(pr, RENOVATE_COMMIT, POLICY)
    assert result.verdict == NEEDS and reason in result.reason


def test_update_branch_merge_passes_identity():
    assert identity(make_pr(""), [RENOVATE, MERGE], POLICY).verdict == PASS
    assert identity(make_pr(""), [RENOVATE, MERGE, MERGE], POLICY).verdict == PASS


def test_renovate_rebase_looks_like_any_renovate_commit():
    rebased = commit(sha="d" * 40, parents=[{"sha": "e" * 40}])
    assert identity(make_pr(""), [rebased], POLICY).verdict == PASS
    assert is_renovate_push(rebased, POLICY)


def test_server_side_rebase_reaches_the_diff_check_but_is_not_a_renovate_push():
    assert identity(make_pr(""), [REBASE], POLICY).verdict == PASS
    assert not is_renovate_push(REBASE, POLICY)


@pytest.mark.parametrize("changed", [unsigned(), commit(committer={"login": "renovate[bot]", "type": "Bot"}),
                                     commit(MERGE), commit(author={"login": "someone", "type": "User"}),
                                     commit(author=None), commit(committer=None), {"sha": "f" * 40}])
def test_only_a_genuine_commit_counts_as_a_renovate_push(changed):
    assert not is_renovate_push(changed, POLICY)


def test_commit_by_a_person_through_the_web_ui_is_flagged():
    # GitHub signs it and web-flow commits it, exactly like Renovate's, but the author is the person.
    web_edit = commit(author={"login": "someone", "type": "User"})
    result = identity(make_pr(""), [RENOVATE, web_edit], POLICY)
    assert result.verdict == NEEDS and "someone" in result.reason


def test_a_merge_made_locally_and_pushed_is_flagged():
    result = identity(make_pr(""), [RENOVATE, unsigned(MERGE)], POLICY)
    assert result.verdict == NEEDS and "jlengelbrecht" in result.reason
    with_git_committer = commit(MERGE, committer={"login": "jlengelbrecht", "type": "User"})
    assert identity(make_pr(""), [RENOVATE, with_git_committer], POLICY).verdict == NEEDS


def test_octopus_merge_and_unreadable_commit_data_are_flagged():
    octopus = commit(MERGE, parents=[{"sha": "a"}, {"sha": "b"}, {"sha": "c"}])
    assert identity(make_pr(""), [RENOVATE, octopus], POLICY).verdict == NEEDS
    for broken in (commit(author=None), commit(parents=None), {"sha": "f" * 40}):
        assert identity(make_pr(""), [broken], POLICY).verdict == NEEDS


def test_branch_must_hold_exactly_one_renovate_commit():
    assert "2 Renovate commits" in identity(make_pr(""), [RENOVATE, RENOVATE], POLICY).reason
    assert "0 Renovate commits" in identity(make_pr(""), [MERGE], POLICY).reason
    assert "0 Renovate commits" in identity(make_pr(""), [], POLICY).reason


def test_identity_rejects_forks_other_bases_and_foreign_commits():
    fork = make_pr("")
    fork["head"]["repo"] = {"full_name": "fork/r"}
    assert identity(fork, RENOVATE_COMMIT, POLICY).verdict == NEEDS
    other_base = make_pr("")
    other_base["base"]["ref"] = "develop"
    assert identity(other_base, RENOVATE_COMMIT, POLICY).verdict == NEEDS
    mixed = identity(make_pr(""), RENOVATE_COMMIT + [{"author": {"login": "someone"}}], POLICY)
    assert mixed.verdict == NEEDS and "someone" in mixed.reason


def stage(rows, labels=("update/patch",), files=("kubernetes/apps/observability/uptime-kuma/app/helmrelease.yaml",),
          jobs=(), title="fix(container): update app"):
    return policy_stage(make_pr(table(*rows), labels, title), list(files), list(jobs), POLICY)


def test_tier_one_patch_passes():
    result = stage(["| louislam/uptime-kuma | patch | `2.4.0` → `2.4.1` |"])
    assert result.verdict == PASS and "uptime-kuma" in result.reason


def test_major_is_needs_human():
    result = stage(["| louislam/uptime-kuma | major | `2.4.0` → `3.0.0` |"], labels=("update/major",))
    assert result.verdict == NEEDS and "major" in result.reason


def test_update_type_falls_back_to_the_label():
    assert stage(["| [anyio](https://x) | `==4.15.1` → `==5.0.0` | a | c |"], labels=("update/major",)).verdict == NEEDS


def test_protected_path_and_job_manifest_are_needs_human():
    rows = ["| app | patch | `1.0.0` → `1.0.1` |"]
    assert "protected path" in stage(rows, files=["talos/talenv.yaml"]).reason
    assert "protected path" in stage(rows, files=["kubernetes/apps/a/app/secret.sops.yaml"]).reason
    assert "Job manifest" in stage(rows, jobs=["kubernetes/apps/a/job.yaml"]).reason


def test_manual_package_respects_update_types():
    assert stage(["| docker.io/mariadb | minor | `11.4.8` → `11.8.9` |"], labels=("update/minor",)).verdict == NEEDS
    assert stage(["| docker.io/mariadb | patch | `11.4.8` → `11.4.9` |"]).verdict == PASS
    assert stage(["| ghcr.io/paperclipai/paperclip | patch | `1.0.0` → `1.0.1` |"]).verdict == NEEDS


def test_zero_x_minor_and_breaking_markers():
    assert "0.x" in stage(["| app | minor | `0.3.1` → `0.4.0` |"], labels=("update/minor",)).reason
    assert stage(["| app | patch | `1.0.0` → `1.0.1` |"], labels=("update/patch", "breaking")).verdict == NEEDS
    assert stage(["| app | minor | `1.0.0` → `1.1.0` |"], title="feat(container)!: update app").verdict == NEEDS


def test_grouped_pr_worst_package_wins():
    rows = ["| app-a | patch | `1.0.0` → `1.0.1` |", "| docker.io/1password/op | minor | `2.38.1` → `2.41.1` |",
            "| [external-secrets](https://x) | minor | `1.1.1` → `1.3.2` |"]
    result = stage(rows, labels=("update/minor",))
    assert result.verdict == NEEDS and "external-secrets" in result.reason


def test_unclassifiable_is_needs_human():
    assert "no Renovate package table" in policy_stage(make_pr("no table"), [], [], POLICY).reason
    assert stage(["| app | `1.0.0` → `1.0.1` | x |"], labels=()).verdict == NEEDS
    assert stage(["| app | replacement | `1.0.0` → `1.0.1` |"]).verdict == NEEDS


def test_version_is_not_read_out_of_a_digest():
    digest = parse_packages(table("| app | digest | `abc1234` → `def5678` |"))
    assert digest[0]["current"] is None
    assert parse_packages(table("| app | minor | `v1.2.3` → `v1.3.0` |"))[0]["current"] == "1.2.3"
    assert parse_packages(table("| app | minor | `sha256:9f86d0` → `1.0.0` |"))[0]["current"] is None


def test_minor_without_a_readable_current_version_is_needs_human():
    result = stage(["| app | minor | `latest` → `1.3.0` |"], labels=("update/minor",))
    assert result.verdict == NEEDS and "current version" in result.reason


def test_policy_loader_requires_required_checks(tmp_path):
    import yaml
    base = yaml.safe_load((Path(__file__).resolve().parents[1] / "policy.yaml").read_text())
    for bad in ([], [{"name": "x"}], [{"name": "x", "app_id": "15368"}]):
        path = tmp_path / "policy.yaml"
        path.write_text(yaml.safe_dump(dict(base, required_checks=bad)))
        with pytest.raises(ValueError):
            load_policy(path)


def test_policy_loader_rejects_unknown_versions(tmp_path):
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 2\n")
    with pytest.raises(ValueError):
        load_policy(bad)


SPEKTR = "ghcr.io/jlengelbrecht/agent-router-base"
SPEKTR_FILE = "kubernetes/apps/ai/agent-router/app/ocirepository.yaml"
# The shape of #1880, which Renovate marks as breaking because it is a 0.x minor.
SPEKTR_LABELS = ("area/kubernetes", "update/minor", "renovate", "spektr", "breaking")
SPEKTR_TITLE = "feat(container)!: Update ghcr.io/jlengelbrecht/agent-router-base ( v0.8.3 → v0.9.0-rc.2 )"


def release(package=SPEKTR, update="minor", change="`v0.8.3` → `v0.9.0-rc.2`", files=(SPEKTR_FILE,),
            labels=SPEKTR_LABELS, title=SPEKTR_TITLE, extra_rows=()):
    row = f"| [{package}](https://redirect.github.com/jlengelbrecht/ai-control-plane) | {update} | {change} |"
    return stage([row, *extra_rows], labels=labels, files=files, title=title)


def test_spektr_release_like_1880_passes():
    result = release()
    assert result.verdict == PASS and result.reason.startswith("release exception")


@pytest.mark.parametrize("update,change,label", [
    ("patch", "`v0.8.2` → `v0.8.3-rc.1`", "update/patch"),
    ("patch", "`v0.8.3-rc.1` → `v0.8.3-rc.2`", "update/patch"),
    ("patch", "`v0.8.3-rc.2` → `v0.8.3`", "update/patch"),
    ("digest", "`f75620a` → `297ed18`", "update/digest"),
])
def test_other_spektr_updates_pass(update, change, label):
    assert release(update=update, change=change, labels=("renovate", label)).verdict == PASS


def test_the_same_bump_on_another_package_is_needs_human():
    result = release(package="ghcr.io/jlengelbrecht/other-base")
    assert result.verdict == NEEDS and "breaking" in result.reason
    unmarked = release(package="ghcr.io/jlengelbrecht/other-base", labels=("update/minor",), title="feat: bump")
    assert unmarked.verdict == NEEDS and "0.x minor" in unmarked.reason


def test_the_exception_needs_that_exact_file():
    assert release(files=("kubernetes/apps/ai/other/app/ocirepository.yaml",)).verdict == NEEDS


def test_anything_else_in_the_pr_gets_the_normal_verdict():
    assert release(files=(SPEKTR_FILE, "kubernetes/apps/ai/other/app/helmrelease.yaml")).verdict == NEEDS
    assert release(extra_rows=("| other/app | patch | `1.0.0` → `1.0.1` |",)).verdict == NEEDS
    assert release(files=(SPEKTR_FILE, ".github/workflows/x.yaml")).reason.startswith("changes protected path")


def test_exception_does_not_cover_a_major():
    result = release(update="major", change="`v0.9.0` → `v1.0.0`", labels=("update/major", "breaking"))
    assert result.verdict == NEEDS and "breaking" in result.reason
    assert release(update="major", change="`v0.9.0` → `v1.0.0`", labels=("update/major",), title="x").verdict == NEEDS


@pytest.mark.parametrize("change", [
    lambda rules: rules[0].update(update_types=["major"]),
    lambda rules: rules[0].pop("file"),
    lambda rules: rules[0]["oci_repository"].pop("url"),
    lambda rules: rules[0].update(tag_pattern="("),
])
def test_a_malformed_release_exception_is_refused(tmp_path, change):
    import re
    policy = yaml.safe_load((Path(__file__).resolve().parents[1] / "policy.yaml").read_text())
    change(policy["release_exceptions"])
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy))
    with pytest.raises((ValueError, re.error)):
        load_policy(path)
