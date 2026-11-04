import difflib
import json
from pathlib import Path

import pytest
import yaml

import render
from classify import NEEDS, PASS

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


def test_label_churn_passes_but_new_labels_do_not():
    a = {"kind": "Service", "metadata": {"name": "s", "labels": {"helm.sh/chart": "x-1.0.0"}}}
    b = {"kind": "Service", "metadata": {"name": "s", "labels": {"helm.sh/chart": "x-1.1.0"}}}
    assert verdict("Service", a, b).verdict == PASS
    c = {"kind": "Service", "metadata": {"name": "s", "labels": {"helm.sh/chart": "x-1.0.0", "new": "y"}}}
    assert verdict("Service", a, c).verdict == NEEDS


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
