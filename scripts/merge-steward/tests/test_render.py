import difflib
import json
from pathlib import Path

import pytest
import yaml

import render
from classify import NEEDS, PASS, load_policy

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def recorded(name):
    fixture = json.loads((FIXTURES / name).read_text())
    return render.classify(fixture["reports"]["helmrelease"], fixture["reports"]["kustomization"])[0]


def test_recorded_rook_ceph_minor_flags_the_crds():
    stage = recorded("pr-1837-rook-ceph-minor.json")
    assert stage.verdict == NEEDS and stage.reason.startswith("CRD: CustomResourceDefinition")


def test_recorded_uptime_kuma_minor_is_clean():
    stage = recorded("pr-1822-uptime-kuma-minor.json")
    assert stage.verdict == PASS and "Deployment" in stage.reason


def test_recorded_tetragon_shows_version_skew():
    stage = recorded("pr-1817-tetragon-skew.json")
    assert stage.verdict == NEEDS and stage.reason.startswith("version skew: DaemonSet kube-system/tetragon")


def obj_diff(kind, before, after, name="app"):
    """Build an object diff the way flux-local does with --unified 100000."""
    dump = lambda d: ("---\n" + yaml.safe_dump(d, sort_keys=False)).splitlines() if d else []  # noqa: E731
    lines = list(difflib.unified_diff(dump(before), dump(after), "a", "b", n=100000, lineterm=""))
    return [{"diffs": [{"kind": kind, "name": name, "namespace": "ns", "diff": "\n\n".join(lines[:3]) + "\n" + "\n".join(lines[3:])}]}]


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


def test_partial_diff_fails_closed():
    report = [{"diffs": [{"kind": "Deployment", "name": "a", "diff": "@@ -40,7 +40,7 @@\n-x: 1\n+x: 2"}]}]
    with pytest.raises(ValueError):
        render.classify(report)


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
    output = [dict(r, kind="HelmRelease", name="app", namespace="ns") for r in obj_diff("Deployment", pod("app:1.0.0"), pod("app:1.0.1"))]
    assert render.classify(output, values)[0].verdict == PASS


def write_artifact(tmp_path, head="a" * 40, failed=0, reports=([], [])):
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": head, "failed": failed}))
    for name, report in zip(("helmrelease", "kustomization"), reports):
        (tmp_path / f"{name}.json").write_text(json.dumps(report))
    return tmp_path


def test_empty_reports_after_a_clean_run_mean_no_rendered_change(tmp_path):
    directory = write_artifact(tmp_path)
    (directory / "helmrelease.json").write_text("")
    (directory / "kustomization.json").write_text("\n")
    assert render.from_artifact(directory, "a" * 40).verdict == PASS
    # A missing report is not an empty one, and a failed run never counts as clean.
    (directory / "helmrelease.json").unlink()
    assert render.from_artifact(directory, "a" * 40).verdict == NEEDS
    (directory / "helmrelease.json").write_text("")
    assert render.from_artifact(write_artifact(tmp_path, failed=1), "a" * 40).verdict == NEEDS


def test_artifact_is_bound_to_the_head_and_fails_closed(tmp_path):
    assert render.from_artifact(write_artifact(tmp_path), "a" * 40).verdict == PASS
    assert render.from_artifact(tmp_path, "b" * 40) is None
    assert render.from_artifact(write_artifact(tmp_path, failed=1), "a" * 40).verdict == NEEDS
    assert render.from_artifact(tmp_path / "missing", "a" * 40).reason == "render evidence unavailable"
    (tmp_path / "kustomization.json").write_text("not json")
    write_artifact(tmp_path, reports=([], []))
    (tmp_path / "helmrelease.json").write_text("{")
    assert render.from_artifact(tmp_path, "a" * 40).verdict == NEEDS


def test_cli_fails_closed(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    render.main([str(bad)])
    render.main(["--failed", str(bad)])
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["verdict"] for r in results] == [NEEDS, NEEDS]
    assert all(r["reason"].startswith("render failed") for r in results)


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
    for old, new in (("name: agent-router-base", "name: other-base"),
                     ("oci://ghcr.io/jlengelbrecht/agent-router-base", "oci://ghcr.io/jlengelbrecht/other-base"),
                     ("namespace: ai", "namespace: other")):
        other = json.loads(json.dumps(PR_1880).replace(old, new))
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
