import json
import unittest
from pathlib import Path

from flagger_recovery.identity import AttributionRefused, _parse_tag, is_manual_rollback, resolve

FIXTURES = Path(__file__).parent / "fixtures"

def _load(name: str):
    """Load a fixture. ``replicasets.json``/``pods.json`` are the raw ``kubectl
    get ... -o json`` List shape (``{"items": [...]}``, matching what
    ``kube.ApiReader`` returns for multi-object GETs) — unwrap to the bare
    list callers expect. Single-object fixtures (canary, deployment, ...)
    have no ``items`` key and pass through unchanged."""
    with open(FIXTURES / name, encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("items", data) if isinstance(data, dict) else data

def _live_objects(**overrides):
    objects = {
        "canary": _load("canary.json"),
        "deployment": _load("deployment.json"),
        "candidate_pods": _load("pods.json"),
        "candidate_replicasets": _load("replicasets.json"),
        "helmrelease": _load("helmrelease.json"),
        "ocirepository": _load("ocirepository.json"),
        "kustomization": _load("kustomization.json"),
    }
    objects.update(overrides)
    return objects

class ResolveHappyPathTests(unittest.TestCase):
    def test_resolves_every_field_with_a_source(self):
        identity = resolve(**_live_objects(commit_message="feat(flagger-pilot): tune canary thresholds (#1300)"))

        self.assertEqual(identity.namespace, "flagger-pilot")
        self.assertEqual(identity.canary_name, "podinfo")
        self.assertEqual(identity.deployment_uid, "556b57bc-ee18-45bb-8d8d-9ccfb483e07d")
        self.assertEqual(identity.template_hash, "759f9fb7bd")
        self.assertEqual(identity.replicaset_hash, "675556c6fd")
        self.assertNotEqual(identity.template_hash, identity.replicaset_hash)
        self.assertEqual(len(identity.images), 1)
        image = identity.images[0]
        self.assertEqual(image.name, "app")
        self.assertEqual(image.repository, "ghcr.io/stefanprodan/podinfo")
        self.assertEqual(image.tag, "6.15.0")
        self.assertEqual(
            image.digest,
            "ghcr.io/stefanprodan/podinfo@sha256:ec73780a8425f59ea49f5bc8cdff0d598805a224fbaa1f86c67a244f250fa9da",
        )
        self.assertEqual(identity.chart_name, "app-template")
        self.assertEqual(identity.chart_version, "4.4.0")
        self.assertEqual(identity.oci_digest, "sha256:0fd5a5fdbf95f32758c1cb18b2031dacba7eb6f553abcb5cd7c37b93c7a5bd0d")
        self.assertEqual(identity.source_branch, "flagger-pilot")
        self.assertEqual(identity.source_sha, "99abc217d85ddcf310d1919f691f538c2a6c8082")
        self.assertEqual(identity.last_promoted_spec, "5b86bd6879")
        self.assertFalse(identity.is_promoted)
        self.assertEqual(identity.pr_number, 1300)

        expected_fields = {
            "namespace",
            "canary_name",
            "deployment_uid",
            "template_hash",
            "replicaset_hash",
            "images",
            "chart_name",
            "chart_version",
            "oci_digest",
            "source_branch",
            "source_sha",
            "last_promoted_spec",
            "is_promoted",
            "pr_number",
        }
        self.assertEqual(set(identity.sources), expected_fields)
        for field, source in identity.sources.items():
            self.assertTrue(source, f"{field} has an empty source")

    def test_pr_number_absent_when_commit_message_not_supplied(self):
        identity = resolve(**_live_objects())
        self.assertIsNone(identity.pr_number)
        self.assertEqual(identity.sources["pr_number"], "not supplied")

    def test_is_promoted_true_when_hash_equals_last_promoted_spec(self):
        canary = _load("canary.json")
        canary["status"]["lastAppliedSpec"] = canary["status"]["lastPromotedSpec"]
        identity = resolve(**_live_objects(canary=canary))
        self.assertTrue(identity.is_promoted)

    def test_selects_replicaset_by_deployment_revision_not_by_hash(self):
        # Several old ReplicaSets (revisions 8, 9, 10) sit alongside the current one
        # (revision 11) under the same Deployment; only the revision match may win, and
        # its pod-template-hash never equals Flagger's lastAppliedSpec.
        identity = resolve(**_live_objects())
        self.assertEqual(identity.template_hash, "759f9fb7bd")
        self.assertEqual(identity.replicaset_hash, "675556c6fd")
        self.assertNotEqual(identity.template_hash, identity.replicaset_hash)

    def test_dedupes_images_seen_on_multiple_pods(self):
        pods = _load("pods.json")
        second = json.loads(json.dumps(pods[0]))
        second["metadata"]["name"] = "podinfo-5b86bd6879-second"
        identity = resolve(**_live_objects(candidate_pods=pods + [second]))
        self.assertEqual(len(identity.images), 1)

    def test_images_are_sorted_regardless_of_input_order(self):
        pods = _load("pods.json")
        first = pods[0]
        second = json.loads(json.dumps(first))
        second["metadata"]["name"] = "podinfo-5b86bd6879-second"
        second["status"]["containerStatuses"][0]["name"] = "sidecar"
        second["status"]["containerStatuses"][0]["imageID"] = (
            "ghcr.io/stefanprodan/sidecar@sha256:" + "a" * 64
        )
        second["status"]["containerStatuses"][0]["image"] = "ghcr.io/stefanprodan/sidecar:1.0.0"

        forward = resolve(**_live_objects(candidate_pods=[first, second]))
        reversed_order = resolve(**_live_objects(candidate_pods=[second, first]))
        self.assertEqual(forward.images, reversed_order.images)
        self.assertEqual([image.name for image in forward.images], ["app", "sidecar"])

    def test_filters_candidate_pods_to_the_matching_replicaset_only(self):
        replicasets = _load("replicasets.json")
        pods = _load("pods.json")

        old_rs = next(rs for rs in replicasets if rs["metadata"]["name"] == "podinfo-74bf8ddcdd")
        stale_pod = json.loads(json.dumps(pods[0]))
        stale_pod["metadata"]["name"] = "podinfo-74bf8ddcdd-stale"
        stale_pod["metadata"]["ownerReferences"][0]["uid"] = old_rs["metadata"]["uid"]
        stale_pod["status"]["containerStatuses"][0]["imageID"] = (
            "ghcr.io/stefanprodan/podinfo@sha256:" + "f" * 64
        )

        identity = resolve(**_live_objects(candidate_pods=[stale_pod] + pods, candidate_replicasets=replicasets))

        self.assertEqual(len(identity.images), 1)
        self.assertEqual(identity.images[0].digest, pods[0]["status"]["containerStatuses"][0]["imageID"])

    def test_to_dict_from_dict_round_trip(self):
        identity = resolve(**_live_objects())
        restored = type(identity).from_dict(identity.to_dict())
        self.assertEqual(identity, restored)

class ResolveRefusalTests(unittest.TestCase):
    def test_refuses_with_no_candidate_pods(self):
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_pods=[]))

    def test_refuses_with_tag_only_image_id(self):
        pods = _load("pods.json")
        pods[0]["status"]["containerStatuses"][0]["imageID"] = "ghcr.io/stefanprodan/podinfo:6.15.0"
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_pods=pods))

    def test_refuses_with_empty_image_id(self):
        pods = _load("pods.json")
        pods[0]["status"]["containerStatuses"][0]["imageID"] = ""
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_pods=pods))

    def test_refuses_with_two_replicasets_sharing_the_current_revision(self):
        replicasets = _load("replicasets.json")
        candidate = next(rs for rs in replicasets if rs["metadata"]["name"] == "podinfo-675556c6fd")
        duplicate = json.loads(json.dumps(candidate))
        duplicate["metadata"]["uid"] = "duplicate-rs-uid"
        duplicate["metadata"]["name"] = "podinfo-675556c6fd-duplicate"
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_replicasets=replicasets + [duplicate]))

    def test_refuses_when_no_replicaset_has_the_current_revision(self):
        replicasets = [rs for rs in _load("replicasets.json") if rs["metadata"]["name"] != "podinfo-675556c6fd"]
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_replicasets=replicasets))

    def test_refuses_when_deployment_has_no_revision_annotation(self):
        deployment = _load("deployment.json")
        del deployment["metadata"]["annotations"]["deployment.kubernetes.io/revision"]
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(deployment=deployment))

    def test_refuses_with_no_last_applied_spec(self):
        canary = _load("canary.json")
        del canary["status"]["lastAppliedSpec"]
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(canary=canary))

    def test_refuses_with_blank_last_applied_spec(self):
        canary = _load("canary.json")
        canary["status"]["lastAppliedSpec"] = ""
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(canary=canary))

    def test_refuses_when_helmrelease_has_no_deployed_history(self):
        helmrelease = _load("helmrelease.json")
        for entry in helmrelease["status"]["history"]:
            entry["status"] = "superseded"
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(helmrelease=helmrelease))

    def test_refuses_when_ocirepository_has_no_artifact_digest(self):
        ocirepository = _load("ocirepository.json")
        del ocirepository["status"]["artifact"]["digest"]
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(ocirepository=ocirepository))

    def test_refuses_when_kustomization_revision_is_malformed(self):
        kustomization = _load("kustomization.json")
        kustomization["status"]["lastAppliedRevision"] = "not-a-revision"
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(kustomization=kustomization))

    def test_refuses_when_candidate_pod_has_no_container_statuses(self):
        pods = _load("pods.json")
        pods[0]["status"]["containerStatuses"] = []
        with self.assertRaises(AttributionRefused):
            resolve(**_live_objects(candidate_pods=pods))

class ParseTagTests(unittest.TestCase):
    def test_plain_tag(self):
        self.assertEqual(_parse_tag("ghcr.io/stefanprodan/podinfo:6.15.0"), "6.15.0")

    def test_digest_only_reference_has_no_tag(self):
        self.assertIsNone(
            _parse_tag("ghcr.io/stefanprodan/podinfo@sha256:" + "a" * 64)
        )

    def test_tag_and_digest_reference_ignores_digest(self):
        self.assertEqual(
            _parse_tag("ghcr.io/stefanprodan/podinfo:6.15.0@sha256:" + "a" * 64),
            "6.15.0",
        )

    def test_registry_port_is_not_mistaken_for_a_tag(self):
        self.assertIsNone(_parse_tag("registry:5000/repo"))

    def test_registry_port_with_tag(self):
        self.assertEqual(_parse_tag("registry:5000/repo:1.2.3"), "1.2.3")

    def test_registry_port_with_digest_has_no_tag(self):
        self.assertIsNone(
            _parse_tag("registry:5000/repo@sha256:" + "b" * 64)
        )

    def test_no_tag_or_digest(self):
        self.assertIsNone(_parse_tag("ghcr.io/stefanprodan/podinfo"))

class ManualRollbackTests(unittest.TestCase):
    def test_true_when_new_hash_equals_last_promoted_spec(self):
        status = {"lastAppliedSpec": "abc123", "lastPromotedSpec": "abc123"}
        self.assertTrue(is_manual_rollback(status, "abc123"))

    def test_false_when_new_hash_differs_from_last_promoted_spec(self):
        status = {"lastAppliedSpec": "def456", "lastPromotedSpec": "abc123"}
        self.assertFalse(is_manual_rollback(status, "def456"))

    def test_false_when_never_promoted(self):
        status = {}
        self.assertFalse(is_manual_rollback(status, "abc123"))

if __name__ == "__main__":
    unittest.main()
