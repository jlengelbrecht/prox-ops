from pathlib import Path

import pytest

from classify import NEEDS, PASS, glob_match, identity, load_policy, parse_packages, policy_stage

POLICY = load_policy(Path(__file__).resolve().parents[1] / "policy.yaml")
REPO = {"full_name": "o/r"}


def table(*rows, header="| Package | Update | Change |"):
    return "\n".join(["This PR contains the following updates:", "", header, "|---|---|---|", *rows])


def make_pr(body, labels=("update/patch",), title="fix(container): update app", login="renovate[bot]"):
    return {"number": 1, "title": title, "body": body, "labels": [{"name": n} for n in labels],
            "user": {"login": login, "type": "Bot" if login.endswith("[bot]") else "User"},
            "head": {"repo": REPO, "sha": "a" * 40}, "base": {"repo": REPO, "ref": "main"}}


RENOVATE_COMMIT = [{"author": {"login": "renovate[bot]"}}]


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


def test_identity_accepts_renovate_only():
    assert identity(make_pr(""), RENOVATE_COMMIT, POLICY).verdict == PASS
    human = identity(make_pr("", login="someone"), RENOVATE_COMMIT, POLICY)
    assert human.verdict == NEEDS and "not on the steward allowlist" in human.reason


def test_identity_rejects_forks_other_bases_and_foreign_commits():
    fork = make_pr("")
    fork["head"]["repo"] = {"full_name": "fork/r"}
    assert identity(fork, RENOVATE_COMMIT, POLICY).verdict == NEEDS
    other_base = make_pr("")
    other_base["base"]["ref"] = "develop"
    assert identity(other_base, RENOVATE_COMMIT, POLICY).verdict == NEEDS
    mixed = identity(make_pr(""), RENOVATE_COMMIT + [{"author": {"login": "someone"}}], POLICY)
    assert mixed.verdict == NEEDS and "someone" in mixed.reason
    assert identity(make_pr(""), [], POLICY).verdict == NEEDS


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
