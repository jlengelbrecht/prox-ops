"""Pairing two full renders into the objects that differ."""

import copy
import json

import pytest

import render
import render_pairs
from classify import NEEDS, PASS

RELEASE = {"helm.toolkit.fluxcd.io/namespace": "media", "helm.toolkit.fluxcd.io/name": "app"}


def deployment(image="app:1.2.0", name="app", namespace="media", labels=None, template=None):
    meta = {"name": name, "namespace": namespace}
    if labels is not None:
        meta["labels"] = labels
    pods = {"spec": {"containers": [{"name": "app", "image": image}]}}
    if template is not None:
        pods["metadata"] = template
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta, "spec": {"template": pods}}


def service(name="app", namespace="media", port=80):
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace},
            "spec": {"ports": [{"port": port}]}}


def crd(name="widgets.example.org", versions=("v1",)):
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition", "metadata": {"name": name},
            "spec": {"versions": [{"name": v} for v in versions]}}


def pair(base, head, base_helm=None, head_helm=None):
    """pair() strips its input in place, so every call gets its own copy.

    Unless a test says otherwise, every object counts as HelmRelease output."""
    renders = [base, head, base if base_helm is None else base_helm, head if head_helm is None else head_helm]
    return render_pairs.pair(*(copy.deepcopy(docs) for docs in renders))


def changes(base, head, base_helm=None, head_helm=None):
    return pair(base, head, base_helm, head_helm)["changes"]


def test_identical_renders_have_no_changes_whatever_their_order():
    docs = [deployment(), service(), crd()]
    assert pair(docs, docs) == {"base": 3, "head": 3, "changes": []}
    assert pair(docs, docs[::-1]) == {"base": 3, "head": 3, "changes": []}


def test_a_changed_object_is_carried_whole_on_both_sides():
    before, after = deployment("app:1.2.0"), deployment("app:1.2.1")
    assert pair([before, service()], [service(), after]) == {"base": 2, "head": 2, "changes": [{
        "apiVersion": "apps/v1", "kind": "Deployment", "namespace": "media", "name": "app", "release": None,
        "before": before, "after": after}]}


def test_added_and_removed_objects_have_one_side():
    [added] = changes([service()], [service(), service("new")])
    assert (added["name"], added["before"], added["after"]) == ("new", None, service("new"))
    [removed] = changes([service(), service("old")], [service()])
    assert (removed["name"], removed["before"], removed["after"]) == ("old", service("old"), None)
    assert pair([service(), service("old")], [service()])["base"] == 2


def test_a_cluster_scoped_object_has_no_namespace():
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "r"}, "rules": []}
    [change] = changes([service()], [service(), role])
    assert change["namespace"] is None and change["kind"] == "ClusterRole"
    assert render.classify([change])[0].reason == "added: ClusterRole -/r"


def test_objects_are_told_apart_by_group_kind_namespace_and_name():
    other_kind = dict(service(), kind="Endpoints")
    other_group = dict(service(), apiVersion="example.org/v1")
    found = changes([service()], [service(), service(namespace="other"), other_kind, other_group])
    assert sorted((c["apiVersion"], c["kind"], c["namespace"]) for c in found) == [
        ("example.org/v1", "Service", "media"), ("v1", "Endpoints", "media"), ("v1", "Service", "other")]
    assert all(c["before"] is None for c in found)


def test_a_new_api_version_of_the_same_group_is_one_changed_object():
    before = dict(deployment(), apiVersion="apps/v1beta1")
    [change] = changes([before], [deployment()])
    assert change["apiVersion"] == "apps/v1" and change["before"]["apiVersion"] == "apps/v1beta1"
    assert render.classify([change])[0].reason.startswith("changed apiVersion")


def test_changes_come_out_in_a_fixed_order():
    base = [service("b"), service("a"), deployment(), service("c", namespace="alpha")]
    head = [copy.deepcopy(doc) for doc in reversed(base)]
    for doc in head:
        doc["metadata"]["annotations"] = {"touched": "yes"}
    found = [(c["kind"], c["namespace"], c["name"]) for c in changes(base, head)]
    assert found == [("Service", "alpha", "c"), ("Service", "media", "a"), ("Service", "media", "b"),
                     ("Deployment", "media", "app")]
    assert found == [(c["kind"], c["namespace"], c["name"]) for c in changes(base[::-1], head[::-1])]


def churn(chart, checksum, version="1.2.0", secret="s1"):
    labels = {"helm.sh/chart": chart, "app.kubernetes.io/version": version}
    annotations = {"checksum/config": checksum, "checksum/secret": secret}
    doc = deployment(labels=dict(labels), template={"labels": dict(labels), "annotations": dict(annotations)})
    doc["metadata"]["annotations"] = dict(annotations)
    return doc


def test_chart_and_config_checksum_churn_is_not_a_change():
    assert changes([churn("app-1.0.0", "aaa")], [churn("app-1.1.0", "bbb")]) == []
    # The same keys appearing or going away are not a change either.
    assert changes([deployment(labels={}, template={})], [churn("app-1.1.0", "bbb")]) != []
    bare = churn("app-1.0.0", "aaa")
    for meta in (bare["metadata"], bare["spec"]["template"]["metadata"]):
        del meta["labels"]["helm.sh/chart"], meta["annotations"]["checksum/config"]
    assert changes([bare], [churn("app-1.1.0", "bbb")]) == []


def test_the_version_label_and_other_checksums_are_still_a_change():
    base = churn("app-1.0.0", "aaa")
    [change] = changes([base], [churn("app-1.1.0", "bbb", version="1.3.0")])
    assert change["after"]["metadata"]["labels"] == {"app.kubernetes.io/version": "1.3.0"}
    assert change["before"]["spec"]["template"]["metadata"]["labels"] == {"app.kubernetes.io/version": "1.2.0"}
    # The stripped keys are gone from what the classifier reads, on both sides.
    assert "helm.sh/chart" not in json.dumps(change) and "checksum/config" not in json.dumps(change)
    [change] = changes([base], [churn("app-1.0.0", "aaa", secret="s2")])
    assert change["after"]["spec"]["template"]["metadata"]["annotations"] == {"checksum/secret": "s2"}
    assert render.classify([change])[0].verdict == NEEDS


def test_only_object_and_pod_template_metadata_are_stripped():
    cronjob = lambda chart: {  # noqa: E731
        "apiVersion": "batch/v1", "kind": "CronJob", "metadata": {"name": "c", "namespace": "media"},
        "spec": {"jobTemplate": {"metadata": {"labels": {"helm.sh/chart": chart}}}}}
    assert len(changes([cronjob("a-1")], [cronjob("a-2")])) == 1
    config = lambda value: {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "media"},  # noqa: E731
                            "data": {"helm.sh/chart": value, "checksum/config": value}}
    [change] = changes([config("a")], [config("b")])
    assert change["after"]["data"] == {"helm.sh/chart": "b", "checksum/config": "b"}


def test_odd_metadata_shapes_do_not_break_stripping():
    odd = deployment(labels=["helm.sh/chart"], template="checksum/config")
    odd["metadata"]["annotations"] = None
    assert changes([odd], [odd]) == []
    assert changes([dict(service(), spec="text")], [dict(service(), spec=None)]) != []


def test_release_names_the_helmrelease_that_rendered_the_object():
    labels = dict(RELEASE, **{"app.kubernetes.io/name": "app"})
    [change] = changes([deployment("app:1.2.0", labels=labels)], [deployment("app:1.2.1", labels=labels)])
    assert change["release"] == ["media", "app"]
    # A removed object is attributed by the side it exists on.
    [removed] = changes([deployment(labels=labels), service()], [service()])
    assert removed["release"] == ["media", "app"] and removed["after"] is None
    [added] = changes([service()], [deployment(labels=labels), service()])
    assert added["release"] == ["media", "app"]


@pytest.mark.parametrize("labels", [
    None, {}, {"helm.toolkit.fluxcd.io/name": "app"}, {"helm.toolkit.fluxcd.io/namespace": "media"},
    {"helm.toolkit.fluxcd.io/name": 1, "helm.toolkit.fluxcd.io/namespace": "media"},
    {"helm.toolkit.fluxcd.io/name": "app", "helm.toolkit.fluxcd.io/namespace": None},
    {"kustomize.toolkit.fluxcd.io/name": "app", "kustomize.toolkit.fluxcd.io/namespace": "media"},
    ["helm.toolkit.fluxcd.io/name"],
])
def test_an_object_without_both_release_labels_has_no_release(labels):
    [change] = changes([deployment("app:1.2.0", labels=labels)], [deployment("app:1.2.1", labels=labels)])
    assert change["release"] is None


def test_release_labels_let_the_classifier_tie_values_to_what_they_rendered():
    release = lambda tag: {  # noqa: E731
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": "app", "namespace": "media"}, "spec": {"values": {"image": {"tag": tag}}}}
    workload = lambda tag, labels: deployment(f"app:{tag}", "app-web", labels=labels)  # noqa: E731
    found = changes([release("1.2.0"), workload("1.2.0", RELEASE)], [release("1.2.1"), workload("1.2.1", RELEASE)])
    assert render.classify(found)[0].verdict == PASS
    # The same values change with no changed object from that release is not understood.
    assert render.classify(changes([release("1.2.0"), workload("1.2.0", RELEASE)],
                                   [release("1.2.1"), workload("1.2.0", RELEASE)]))[0].reason.startswith("values")
    found = changes([release("1.2.0"), workload("1.2.0", {})], [release("1.2.1"), workload("1.2.1", {})])
    assert render.classify(found)[0].reason.startswith("values")


def test_unchanged_duplicates_are_not_a_change():
    twice = [service(), service(), deployment()]
    assert changes(twice, twice[::-1]) == []
    different = [service(port=80), service(port=81)]
    assert changes(different, different[::-1]) == []


def test_a_changed_copy_of_a_duplicated_object_is_ambiguous():
    [change] = changes([service(port=80), service(port=81)], [service(port=80), service(port=82)])
    assert change["ambiguous"] is True and (change["kind"], change["name"]) == ("Service", "app")
    assert render.classify([change])[0].reason.startswith("changed an object two producers render: Service media/app")
    # A second producer appearing or going away is ambiguous too, even when its copy is the same.
    for base, head in (([service()], [service(), service()]), ([service(), service()], [service()]),
                       ([service()], [service(), service(port=81)])):
        [change] = changes(base, head)
        assert change["ambiguous"] is True
        assert render.classify([change])[0].verdict == NEEDS


def test_an_allowlisted_change_to_a_duplicated_object_does_not_pass():
    base = [deployment("app:1.2.0"), deployment("app:1.2.0")]
    head = [deployment("app:1.2.1"), deployment("app:1.2.1")]
    assert render.classify(changes(base[:1], head[:1]))[0].verdict == PASS
    assert render.classify(changes(base, head))[0].verdict == NEEDS


def test_an_ordinary_change_is_not_marked_ambiguous():
    [change] = changes([service(port=80)], [service(port=81)])
    assert "ambiguous" not in change


def test_crd_bodies_are_emptied_and_the_change_stays_visible():
    [changed] = changes([crd(versions=("v1",))], [crd(versions=("v1", "v2"))])
    assert (changed["before"], changed["after"]) == ({}, {})
    assert changed["apiVersion"] == "apiextensions.k8s.io/v1" and changed["namespace"] is None
    [added] = changes([service()], [service(), crd()])
    assert (added["before"], added["after"]) == (None, {})
    [removed] = changes([service(), crd()], [service()])
    assert (removed["before"], removed["after"]) == ({}, None)
    for change in (changed, added, removed):
        assert render.classify([change])[0].reason == "CRD: CustomResourceDefinition -/widgets.example.org"
    assert changes([crd(), service()], [service(), crd()]) == []


GOOD = [service()]


@pytest.mark.parametrize("docs", [
    None, {}, "text", 3, [], {"kind": "Service"},
    [1], ["text"], [None], [[service()]],
    [{"kind": "Service", "apiVersion": "v1"}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": None}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": ["name"]}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": {}}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": {"name": ""}}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": {"name": 7}}],
    [{"kind": "Service", "apiVersion": "v1", "metadata": {"name": "app", "namespace": 7}}],
    [{"apiVersion": "v1", "metadata": {"name": "app"}}],
    [{"kind": "Service", "metadata": {"name": "app"}}],
    [{"kind": ["Service"], "apiVersion": "v1", "metadata": {"name": "app"}}],
    [service(), {"kind": "Service"}],
])
def test_input_that_is_not_a_render_raises(docs):
    for base, head in ((docs, GOOD), (GOOD, docs), (docs, docs)):
        with pytest.raises(ValueError):
            pair(base, head)


def write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return str(path)


@pytest.mark.parametrize("args", [[], ["one"], ["one", "two"], ["one", "two", "three"], ["1", "2", "3", "4", "5"]])
def test_main_wants_exactly_four_files(args, capsys):
    assert render_pairs.main(args) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("usage:")


@pytest.mark.parametrize("content", ["", "not json", "{", [], {}, None, [{"kind": "Service"}], {"items": [service()]}])
def test_main_fails_on_input_it_cannot_read_and_prints_no_result(tmp_path, capsys, content):
    good, bad = write(tmp_path, "good.json", GOOD), write(tmp_path, "bad.json", content)
    absent = str(tmp_path / "absent.json")
    for args in ([bad, good, good, good], [good, bad, good, good], [good, absent, good, good], [absent, good, good, good]):
        assert render_pairs.main(args) == 1
        out = capsys.readouterr()
        assert out.out == "" and out.err.startswith("render pairing failed (")


def test_main_does_not_put_render_content_in_its_error(tmp_path, capsys):
    bad = write(tmp_path, "bad.json", [{"kind": "Service", "metadata": {"name": "leaked-name"}, "data": "leaked-value"}])
    good = write(tmp_path, "good.json", GOOD)
    assert render_pairs.main([good, bad, good, good]) == 1
    assert "leaked" not in capsys.readouterr().err


def test_main_writes_a_result_the_classifier_accepts(tmp_path, capsys):
    base = write(tmp_path, "base.json", [service(), deployment("app:1.2.0")])
    head = write(tmp_path, "head.json", [deployment("app:1.2.1"), service()])
    assert render_pairs.main([base, head, base, head]) == 0
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert out.err == "" and (result["base"], result["head"]) == (2, 2)
    assert [(c["kind"], c["name"]) for c in result["changes"]] == [("Deployment", "app")]
    (tmp_path / "changes.json").write_text(out.out)
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": "a" * 40, "failed": 0}))
    stage = render.from_artifact(tmp_path, "a" * 40)
    assert stage.verdict == PASS and stage.reason == "1 Deployment"


def test_main_reads_its_arguments_from_the_command_line(tmp_path, capsys, monkeypatch):
    same = write(tmp_path, "same.json", [service(), crd()])
    monkeypatch.setattr(render_pairs.sys, "argv", ["render_pairs.py", same, same, same, same])
    assert render_pairs.main() == 0
    assert json.loads(capsys.readouterr().out) == {"base": 2, "head": 2, "changes": []}
    (tmp_path / "changes.json").write_text(json.dumps({"base": 2, "head": 2, "changes": []}))
    (tmp_path / "meta.json").write_text(json.dumps({"head_sha": "a" * 40, "failed": 0}))
    assert render.from_artifact(tmp_path, "a" * 40).reason == "no rendered changes"


def values_change(helm):
    """A HelmRelease values change beside a ConfigMap that carries that release's labels."""
    release = lambda tag: {  # noqa: E731
        "apiVersion": "helm.toolkit.fluxcd.io/v2", "kind": "HelmRelease",
        "metadata": {"name": "app", "namespace": "media"}, "spec": {"values": {"tag": tag}}}
    config = lambda value: {  # noqa: E731
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "notes", "namespace": "media", "labels": dict(RELEASE)}, "data": {"a": value}}
    base, head = [release("1.2.0"), config("1")], [release("1.2.1"), config("2")]
    return changes(base, head, [config("1")] if helm else [], [config("2")] if helm else [])


def test_release_labels_count_only_on_an_object_in_the_helmrelease_render():
    # Rendered by the release: the labels are the tool's, and the values change is understood.
    found = values_change(helm=True)
    assert [c["release"] for c in found if c["kind"] == "ConfigMap"] == [["media", "app"]]
    assert render.classify(found)[0].verdict == PASS
    # The same labels written into a manifest claim nothing, so the values change stays unexplained.
    found = values_change(helm=False)
    assert [c["release"] for c in found if c["kind"] == "ConfigMap"] == [None]
    stage = render.classify(found)[0]
    assert stage.verdict == NEEDS and stage.reason.startswith("values")


def test_release_is_taken_from_the_side_that_was_rendered_by_the_release():
    labelled = deployment("app:1.2.0", labels=dict(RELEASE))
    # An object the release stops rendering: only the base side is HelmRelease output.
    [removed] = changes([labelled, service()], [service()], [labelled], [])
    assert removed["release"] == ["media", "app"]
    # A hand-written copy on the head side with the same labels is not.
    changed = deployment("app:1.2.1", labels=dict(RELEASE))
    [change] = changes([labelled], [changed], [], [])
    assert change["release"] is None
    # Nor does the base side lend its release to an object the head no longer renders from it.
    [change] = changes([labelled], [changed], [labelled], [])
    assert change["release"] is None
    [change] = changes([labelled], [changed], [labelled], [changed])
    assert change["release"] == ["media", "app"]


@pytest.mark.parametrize("helm", [None, {}, "render", 5, [5], [{"kind": "Service"}], [{"metadata": {"name": "x"}}]])
def test_a_helmrelease_render_that_is_not_a_list_of_objects_raises(helm):
    with pytest.raises(ValueError):
        render_pairs.pair([service()], [service()], helm, [])
    with pytest.raises(ValueError):
        render_pairs.pair([service()], [service()], [], helm)


def test_an_empty_helmrelease_render_is_allowed():
    assert render_pairs.pair([service()], [service()], [], []) == {"base": 1, "head": 1, "changes": []}
