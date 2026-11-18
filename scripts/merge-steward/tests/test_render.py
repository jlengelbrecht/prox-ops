import json
from pathlib import Path

import pytest

import render
from classify import NEEDS, PASS, load_policy

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def recorded(name):
    fixture = json.loads((FIXTURES / name).read_text())
    return render.classify(fixture["changes"])[0]


def test_recorded_rook_ceph_minor_flags_the_crds():
    stage = recorded("pr-1837-rook-ceph-minor.json")
    assert stage.verdict == NEEDS and stage.reason.startswith("CRD: CustomResourceDefinition")


def test_recorded_uptime_kuma_minor_is_clean():
    stage = recorded("pr-1822-uptime-kuma-minor.json")
    assert stage.verdict == PASS and "Deployment" in stage.reason


def test_recorded_tetragon_shows_version_skew():
    stage = recorded("pr-1817-tetragon-skew.json")
    assert stage.verdict == NEEDS and stage.reason.startswith("version skew: DaemonSet kube-system/tetragon")


def obj_diff(kind, before, after, name="app", release=None, api="v1"):
    """One changed object the way render_pairs.py records it."""
    return [{"apiVersion": api, "kind": kind, "namespace": "ns", "name": name, "release": release,
             "before": before, "after": after}]


def pod(image, kind="Deployment", version=None, **spec):
    meta = {"name": "app", "labels": {"app.kubernetes.io/version": version}} if version else {"name": "app"}
    return {"kind": kind, "metadata": meta,
            "spec": dict(spec, template={"spec": {"containers": [{"name": "app", "image": image}]}})}


def verdict(kind, before, after):
    return render.classify(obj_diff(kind, before, after))[0]


def test_minor_image_bump_passes():
    assert verdict("Deployment", pod("app:1.2.0"), pod("app:1.3.0")).verdict == PASS


def test_image_major_in_a_minor_pr_is_flagged():
    assert verdict("Deployment", pod("app:1.9.0"), pod("app:2.0.0")).reason.startswith("major")


def test_removed_object_is_flagged():
    assert verdict("Service", {"kind": "Service", "metadata": {"name": "app"}}, None).reason.startswith("removed")


def test_statefulset_outside_the_template_is_flagged():
    before = pod("db:1.0.0", "StatefulSet", serviceName="db", volumeClaimTemplates=[{"size": "1Gi"}])
    after = pod("db:1.0.1", "StatefulSet", serviceName="db", volumeClaimTemplates=[{"size": "2Gi"}])
    assert verdict("StatefulSet", before, after).reason.startswith("StatefulSet")
    image_only = pod("db:1.0.1", "StatefulSet", serviceName="db", volumeClaimTemplates=[{"size": "1Gi"}])
    assert verdict("StatefulSet", before, image_only).verdict == PASS


@pytest.mark.parametrize("kind", ["PersistentVolumeClaim", "ClusterRole", "CiliumNetworkPolicy", "HTTPRoute", "Namespace"])
def test_sensitive_kinds_are_flagged(kind):
    assert verdict(kind, {"kind": kind, "a": 1}, {"kind": kind, "a": 2}).verdict == NEEDS


def test_job_template_change_is_flagged():
    assert "Job template" in verdict("Job", pod("j:1.0.0", "Job"), pod("j:1.0.1", "Job")).reason


def test_helmrelease_chart_major_is_flagged():
    hr = lambda v: {"kind": "HelmRelease", "spec": {"chart": {"spec": {"version": v}}}}  # noqa: E731
    assert verdict("HelmRelease", hr("4.6.2"), hr("5.0.0")).reason.startswith("major")
    assert verdict("HelmRelease", hr("4.6.2"), hr("4.7.0")).verdict == PASS


def test_skew_needs_the_label_to_match_the_old_image():
    assert verdict("Deployment", pod("a:1.6.1", version="1.6.0"), pod("a:1.8.0", version="1.6.0")).reason.startswith("version skew")
    assert verdict("Deployment", pod("a:2.4.0", version="1.23.17"), pod("a:2.5.8", version="1.23.17")).verdict == PASS
    assert verdict("Deployment", pod("a:1.6.1", version="1.6.1"), pod("a:1.8.0", version="1.8.0")).verdict == PASS


@pytest.mark.parametrize("side", ["kind: Deployment", ["kind"], 1, True])
def test_a_change_without_whole_objects_fails_closed(tmp_path, side):
    for before, after in ((side, pod("app:1.2.1")), (pod("app:1.2.0"), side)):
        report = obj_diff("Deployment", before, after)
        with pytest.raises(ValueError):
            render.classify(report)
        assert render.from_artifact(write_artifact(tmp_path, changes=report), "a" * 40).verdict == NEEDS


@pytest.mark.parametrize("change", [
    "Deployment", None, 1, ["kind", "Deployment"], {},
    {"kind": "Deployment", "name": "app", "before": None},  # no after
    {"kind": "Deployment", "name": "app", "after": None},  # no before
    {"kind": "Deployment", "before": {}, "after": {}},  # no name
    {"name": "app", "before": {}, "after": {}},  # no kind
    {"kind": "Deployment", "name": "app", "before": {}, "after": {}, "release": 1},
])
def test_a_malformed_change_fails_closed(tmp_path, change):
    with pytest.raises(Exception):
        render.classify([change])
    assert render.from_artifact(write_artifact(tmp_path, changes=[change]), "a" * 40).verdict == NEEDS
    assert render.object_lines(tmp_path) == []


def test_an_object_two_producers_render_is_flagged_even_for_an_allowlisted_change():
    change = obj_diff("Deployment", pod("app:1.2.0"), pod("app:1.2.1"))[0]
    assert render.classify([change])[0].verdict == PASS
    stage = render.classify([dict(change, ambiguous=True)])[0]
    assert stage.verdict == NEEDS and stage.reason.startswith("changed an object two producers render: Deployment ns/app")


def test_added_object_is_flagged():
    assert verdict("Service", None, {"kind": "Service", "metadata": {"name": "app"}}).reason.startswith("added")


def test_changed_unknown_kind_is_flagged():
    before = {"kind": "Widget", "metadata": {"name": "app"}, "spec": {"size": 1}}
    stage = verdict("Widget", before, dict(before, spec={"size": 2}))
    assert stage.verdict == NEEDS and stage.reason.startswith("changed spec.size")


@pytest.mark.parametrize("field,value", [
    ("hostNetwork", True), ("hostPID", True), ("serviceAccountName", "admin"),
    ("volumes", [{"name": "root", "hostPath": {"path": "/"}}]),
])
def test_privileged_pod_spec_change_is_flagged(field, value):
    before = pod("app:1.2.0")
    after = pod("app:1.2.1")
    after["spec"]["template"]["spec"][field] = value
    stage = verdict("Deployment", before, after)
    assert stage.verdict == NEEDS and stage.reason.split(":")[0] in ("privileged pod spec", "service account")


def test_container_privilege_change_is_flagged():
    before, after = pod("app:1.2.0"), pod("app:1.2.1")
    after["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"privileged": True}
    assert verdict("Deployment", before, after).reason.startswith("privileged pod spec")
    caps = pod("app:1.2.1")
    caps["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"capabilities": {"add": ["SYS_ADMIN"]}}
    before["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"capabilities": {"add": []}}
    assert verdict("Deployment", before, caps).reason.startswith("privileged pod spec")


def test_other_pod_spec_and_replica_changes_are_flagged():
    before, after = pod("app:1.2.0"), pod("app:1.2.0")
    after["spec"]["template"]["spec"]["containers"][0]["args"] = ["--debug"]
    assert verdict("Deployment", before, after).verdict == NEEDS
    assert verdict("Deployment", pod("app:1.2.0", replicas=1), pod("app:1.2.0", replicas=3)).reason.startswith("replicas")


def test_webhook_added_or_changed_is_flagged():
    hook = {"kind": "ValidatingWebhookConfiguration", "metadata": {"name": "v"}, "webhooks": [{"name": "a"}]}
    assert verdict("ValidatingWebhookConfiguration", None, hook).verdict == NEEDS
    changed = dict(hook, webhooks=[{"name": "a", "failurePolicy": "Ignore"}])
    assert verdict("ValidatingWebhookConfiguration", hook, changed).reason.startswith("webhook")


def test_cnpg_image_name_change_is_flagged():
    cluster = lambda image: {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",  # noqa: E731
                             "metadata": {"name": "pg"}, "spec": {"imageName": image}}
    stage = verdict("Cluster", cluster("ghcr.io/cloudnative-pg/postgresql:17.5"), cluster("ghcr.io/cloudnative-pg/postgresql:17.6"))
    assert stage.reason.startswith("CNPG cluster")


@pytest.mark.parametrize("kind", ["ExternalSecret", "CiliumClusterwideNetworkPolicy", "ServiceAccount", "Service"])
def test_other_kinds_are_flagged(kind):
    assert verdict(kind, {"kind": kind, "spec": {"a": 1}}, {"kind": kind, "spec": {"a": 2}}).verdict == NEEDS


def test_allowlisted_changes_pass():
    cm = lambda v: {"kind": "ConfigMap", "metadata": {"name": "c", "labels": {"app.kubernetes.io/version": v}},  # noqa: E731
                    "data": {"x": v}}
    assert verdict("ConfigMap", cm("1.0.0"), cm("1.1.0")).verdict == PASS
    oci = lambda t: {"kind": "OCIRepository", "spec": {"ref": {"tag": t}}}  # noqa: E731
    assert verdict("OCIRepository", oci("1.2.0"), oci("1.3.0")).verdict == PASS
    assert verdict("OCIRepository", oci("1.2.0"), oci("2.0.0")).reason.startswith("major")
    assert verdict("Deployment", pod("app:1.2.0@sha256:aa"), pod("app:1.2.0@sha256:bb")).verdict == PASS


def test_only_version_labels_may_churn():
    svc = lambda labels, ann=None: {"kind": "Service", "metadata": {"name": "s", "labels": labels,  # noqa: E731
                                                                     "annotations": ann or {}}}
    assert verdict("Service", svc({"helm.sh/chart": "x-1.0.0", "app.kubernetes.io/version": "1.0"}),
                   svc({"helm.sh/chart": "x-1.1.0", "app.kubernetes.io/version": "1.1"})).verdict == PASS
    assert verdict("Service", svc({"helm.sh/chart": "x-1.0.0"}), svc({"helm.sh/chart": "x-1.0.0", "new": "y"})).verdict == NEEDS
    assert verdict("Service", svc({"app": "a"}), svc({"app": "b"})).verdict == NEEDS


def test_external_dns_hostname_change_is_flagged():
    svc = lambda host: {"kind": "Service", "metadata": {"name": "s", "annotations": {  # noqa: E731
        "external-dns.alpha.kubernetes.io/hostname": host}}}
    assert verdict("Service", svc("a.example.org"), svc("b.example.org")).verdict == NEEDS


def test_lbipam_ip_change_is_flagged():
    svc = lambda ip: {"kind": "Service", "metadata": {"name": "s", "annotations": {"lbipam.cilium.io/ips": ip}}}  # noqa: E731
    assert verdict("Service", svc("192.0.2.10"), svc("192.0.2.11")).verdict == NEEDS


def test_pod_label_rename_is_flagged():
    before, after = pod("app:1.2.0"), pod("app:1.2.0")
    before["spec"]["template"]["metadata"] = {"labels": {"app.kubernetes.io/name": "app", "app.kubernetes.io/version": "1.2.0"}}
    after["spec"]["template"]["metadata"] = {"labels": {"app.kubernetes.io/instance": "app", "app.kubernetes.io/version": "1.2.0"}}
    assert verdict("Deployment", before, after).verdict == NEEDS
    after["spec"]["template"]["metadata"] = {"labels": {"app.kubernetes.io/name": "app", "app.kubernetes.io/version": "1.2.1"}}
    assert verdict("Deployment", before, after).verdict == PASS
    after["spec"]["template"]["metadata"] = {"labels": {"app.kubernetes.io/name": "app2", "app.kubernetes.io/version": "1.2.0"}}
    assert verdict("Deployment", before, after).verdict == NEEDS


def test_pod_template_annotations_never_pass():
    before, after = pod("app:1.2.0"), pod("app:1.2.0")
    before["spec"]["template"]["metadata"] = {"annotations": {"checksum/secret": "a"}}
    after["spec"]["template"]["metadata"] = {"annotations": {"checksum/secret": "b"}}
    assert verdict("Deployment", before, after).verdict == NEEDS


def test_digest_rules():
    assert verdict("Deployment", pod("app:1.2.0@sha256:aa"), pod("app:1.2.0@sha256:bb")).verdict == PASS
    assert verdict("Deployment", pod("app:1.2.0"), pod("app:1.2.0@sha256:bb")).verdict == PASS
    assert verdict("Deployment", pod("app:1.2.0@sha256:aa"), pod("app:1.2.0")).reason.startswith("image")
    assert verdict("Deployment", pod("app:1.2.0@sha256:aa"), pod("app:1.2.1")).reason.startswith("image")
    assert verdict("Deployment", pod("app:1.2.0@sha256:aa"), pod("app:1.2.1@sha256:bb")).verdict == PASS


def test_image_repository_change_and_zero_x_minor_are_flagged():
    assert verdict("Deployment", pod("a/app:1.2.0"), pod("b/app:1.2.1")).reason.startswith("image")
    assert verdict("Deployment", pod("app:0.3.0"), pod("app:0.4.0")).reason.startswith("major")
    assert verdict("Deployment", pod("app:latest"), pod("app:main")).reason.startswith("version")


def test_helmrelease_values_pass_only_when_the_release_was_rendered():
    hr = lambda tag: {"kind": "HelmRelease", "metadata": {"name": "app"}, "spec": {"values": {"tag": tag}}}  # noqa: E731
    values = obj_diff("HelmRelease", hr("1.0.0"), hr("1.0.1"))
    assert render.classify(values)[0].reason.startswith("values")
    # The release counts as rendered when a changed object names it as its HelmRelease.
    output = lambda release: obj_diff("Deployment", pod("app:1.0.0"), pod("app:1.0.1"), "app-web", release)  # noqa: E731
    assert render.classify(output(["ns", "app"]) + values)[0].verdict == PASS
    assert render.classify(values, output(["ns", "app"]))[0].verdict == PASS
    # A changed object from no release, or from another one, says nothing about these values.
    for release in (None, ["ns", "other"], ["other", "app"], ["app", "ns"]):
        assert render.classify(output(release) + values)[0].reason.startswith("values"), release
    # What the release rendered is still classified object by object.
    major = obj_diff("Deployment", pod("app:1.0.0"), pod("app:2.0.0"), "app-web", ["ns", "app"])
    assert render.classify(major + values)[0].reason.startswith("major")


def write_artifact(tmp_path, head="a" * 40, failed=0, changes=(), counts=(3, 3)):
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": head, "failed": failed}))
    result = {"base": counts[0], "head": counts[1], "changes": list(changes)}
    (tmp_path / "changes.json").write_text(json.dumps(result))
    return tmp_path


def test_no_changes_after_a_clean_run_mean_no_rendered_change(tmp_path):
    directory = write_artifact(tmp_path)
    stage = render.from_artifact(directory, "a" * 40)
    assert stage.verdict == PASS and stage.reason == "no rendered changes"
    # An empty file is not an empty list of changes.
    for text in ("", "\n", "[]", "{}", "null"):
        (directory / "changes.json").write_text(text)
        assert render.from_artifact(directory, "a" * 40).verdict == NEEDS, text
    # A missing result is not an empty one, and a failed run never counts as clean.
    (directory / "changes.json").unlink()
    assert render.from_artifact(directory, "a" * 40).verdict == NEEDS
    assert render.from_artifact(write_artifact(tmp_path, failed=1), "a" * 40).verdict == NEEDS


def test_artifact_changes_reach_the_verdict(tmp_path):
    bump = obj_diff("Deployment", pod("app:1.2.0"), pod("app:1.2.1"))
    stage = render.from_artifact(write_artifact(tmp_path, changes=bump), "a" * 40)
    assert stage.verdict == PASS and stage.reason == "1 Deployment"
    added = obj_diff("Service", None, {"kind": "Service", "metadata": {"name": "app"}})
    stage = render.from_artifact(write_artifact(tmp_path, changes=bump + added), "a" * 40)
    assert stage.verdict == NEEDS and stage.reason.startswith("added: Service ns/app")
    assert render.object_lines(tmp_path) == ["flagged: added: Service ns/app", "changed: 1 Deployment", "changed: 1 Service"]


@pytest.mark.parametrize("result", [
    [], "changes", None, 3, {},
    {"base": 3, "head": 3}, {"base": 3, "head": 3, "changes": None}, {"base": 3, "head": 3, "changes": {}},
    {"base": 3, "head": 3, "changes": "[]"},
    {"changes": []}, {"base": 3, "changes": []}, {"head": 3, "changes": []},
    {"base": 0, "head": 3, "changes": []}, {"base": 3, "head": 0, "changes": []}, {"base": 0, "head": 0, "changes": []},
    {"base": -1, "head": 3, "changes": []}, {"base": "3", "head": 3, "changes": []},
    {"base": 3.0, "head": 3, "changes": []}, {"base": True, "head": True, "changes": []},
    {"base": None, "head": 3, "changes": []}, {"base": [3], "head": 3, "changes": []},
])
def test_a_result_that_does_not_record_a_render_of_both_sides_is_rejected(tmp_path, result):
    write_artifact(tmp_path)
    (tmp_path / "changes.json").write_text(json.dumps(result))
    with pytest.raises(ValueError):
        render._load(tmp_path)
    stage = render.from_artifact(tmp_path, "a" * 40)
    assert stage.verdict == NEEDS and stage.reason.startswith("render failed")
    assert render.object_lines(tmp_path) == []


def test_artifact_is_bound_to_the_head_and_fails_closed(tmp_path):
    assert render.from_artifact(write_artifact(tmp_path), "a" * 40).verdict == PASS
    assert render.from_artifact(tmp_path, "b" * 40) is None
    assert render.from_artifact(write_artifact(tmp_path, failed=1), "a" * 40).verdict == NEEDS
    assert render.from_artifact(tmp_path / "missing", "a" * 40).reason == "render evidence unavailable"
    for text in ("not json", "{"):
        write_artifact(tmp_path)
        (tmp_path / "changes.json").write_text(text)
        assert render.from_artifact(tmp_path, "a" * 40).verdict == NEEDS
    # The files of the retired render are not read: without changes.json there is no result.
    write_artifact(tmp_path)
    (tmp_path / "changes.json").unlink()
    (tmp_path / "helmrelease.json").write_text("[]")
    (tmp_path / "kustomization.json").write_text("[]")
    assert render.from_artifact(tmp_path, "a" * 40).verdict == NEEDS
    write_artifact(tmp_path)
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": "a" * 40}))
    assert render.from_artifact(tmp_path, "a" * 40).verdict == NEEDS  # no exit status recorded


def test_cli_fails_closed(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    render.main([str(bad)])
    render.main(["--failed", str(bad)])
    bad.write_text("[]")  # a bare list is not a render result
    render.main([str(bad)])
    bad.write_text(json.dumps({"base": 3, "head": 3}))
    render.main([str(bad)])
    render.main([str(tmp_path / "absent.json")])
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["verdict"] for r in results] == [NEEDS] * 5
    assert all(r["reason"].startswith("render failed") for r in results)


def test_cli_classifies_a_render_result(tmp_path, capsys):
    good = tmp_path / "changes.json"
    good.write_text(json.dumps({"base": 3, "head": 3, "changes": obj_diff("Deployment", pod("app:1.2.0"), pod("app:1.2.1"))}))
    render.main([str(good)])
    assert json.loads(capsys.readouterr().out) == {"verdict": PASS, "reason": "1 Deployment", "counts": {"Deployment": 1}}
    good.write_text(json.dumps({"base": 3, "head": 3, "changes": obj_diff("Role", {"rules": []}, {"rules": [{}]})}))
    render.main([str(good)])
    assert json.loads(capsys.readouterr().out)["verdict"] == NEEDS


EXCEPTIONS = load_policy(Path(render.__file__).with_name("policy.yaml"))["release_exceptions"]
PR_1880 = json.loads((FIXTURES / "pr-1880-agent-router-base.json").read_text())
OLD_DIGEST, NEW_DIGEST = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def router(tag, digest, name="agent-router-base", namespace="ai", url="oci://ghcr.io/jlengelbrecht/agent-router-base"):
    return {"kind": "OCIRepository", "metadata": {"name": name, "namespace": namespace},
            "spec": {"url": url, "ref": {"tag": tag, "digest": digest}}}


def release_verdict(before, after):
    return render.classify(obj_diff("OCIRepository", before, after), exceptions=EXCEPTIONS)[0]


def test_real_render_of_1880_passes_with_the_exception_and_is_flagged_without_it():
    assert render.classify(PR_1880, exceptions=EXCEPTIONS)[0].verdict == PASS
    plain = render.classify(PR_1880)[0]
    assert plain.verdict == NEEDS and plain.reason.startswith("major")


def test_the_same_real_bump_on_another_package_is_flagged():
    for section, field, new in (("metadata", "name", "other-base"),
                                ("spec", "url", "oci://ghcr.io/jlengelbrecht/other-base"),
                                ("metadata", "namespace", "other")):
        other = json.loads(json.dumps(PR_1880))
        [change] = other
        for side in ("before", "after"):
            assert change[side][section][field] != new
            change[side][section][field] = new
        assert render.classify(other, exceptions=EXCEPTIONS)[0].verdict == NEEDS, new


@pytest.mark.parametrize("old,new", [
    ("v0.8.3", "v0.9.0-rc.2"), ("v0.8.2", "v0.8.3-rc.1"), ("v0.8.3-rc.1", "v0.8.3-rc.2"), ("v0.8.3-rc.2", "v0.8.3"),
])
def test_excepted_release_tags_pass(old, new):
    assert release_verdict(router(old, OLD_DIGEST), router(new, NEW_DIGEST)).verdict == PASS


def test_digest_only_rebuild_of_a_tag_passes():
    assert release_verdict(router("v0.8.3", OLD_DIGEST), router("v0.8.3", NEW_DIGEST)).verdict == PASS


@pytest.mark.parametrize("old,new,reason", [
    ("v0.9.0", "v1.0.0", "major"),
    ("v0.9.0", "v0.8.3", "version"),  # a rollback needs a person
    ("v0.9.5", "v0.9.2", "version"),
    ("v0.9.0", "v0.9.0-rc.1", "version"),
    ("v0.8.3", "0.9.0", "version"),  # outside the release tag format
    ("v0.8.3", "latest", "version"),
    ("v0.8.3", "v0.9.0-", "version"),
])
def test_other_tags_on_the_excepted_object_are_flagged(old, new, reason):
    assert release_verdict(router(old, OLD_DIGEST), router(new, NEW_DIGEST)).reason.startswith(reason)


def test_digest_must_stay_a_digest_on_the_excepted_object():
    after = router("v0.9.0", "sha256:short")
    assert release_verdict(router("v0.8.3", OLD_DIGEST), after).reason.startswith("changed spec.ref.digest")
    dropped = router("v0.9.0", NEW_DIGEST)
    del dropped["spec"]["ref"]["digest"]
    assert release_verdict(router("v0.8.3", OLD_DIGEST), dropped).verdict == NEEDS


def test_other_fields_on_the_excepted_object_are_flagged():
    moved = router("v0.9.0", NEW_DIGEST, url="oci://ghcr.io/evil/agent-router-base")
    assert release_verdict(router("v0.8.3", OLD_DIGEST), moved).verdict == NEEDS
    extra = router("v0.9.0", NEW_DIGEST)
    extra["spec"]["secretRef"] = {"name": "other"}
    assert release_verdict(router("v0.8.3", OLD_DIGEST), extra).reason.startswith("changed spec.secretRef")


def test_other_objects_in_the_same_render_get_the_normal_verdict():
    report = PR_1880 + obj_diff("Deployment", pod("app:1.2.0"), pod("app:2.0.0"))
    assert render.classify(report, exceptions=EXCEPTIONS)[0].verdict == NEEDS
