import json
import unittest
from pathlib import Path

from flagger_recovery.identity import resolve
from flagger_recovery.kube import CandidateReader, LiveCanary

FIXTURES = Path(__file__).parent / "fixtures"

def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)

class FakeApiReader:
    """Answers the seven GETs ``CandidateReader`` makes, by path suffix, and
    records every path so the test can assert no other request is issued."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def get(self, path: str, *, params=None):
        self.paths.append(path)
        if "/canaries/" in path:
            return _load("canary.json")
        if "/deployments/" in path:
            return _load("deployment.json")
        if path.endswith("/replicasets"):
            return _load("replicasets.json")
        if path.endswith("/pods"):
            return _load("pods.json")
        if "/helmreleases/" in path:
            return _load("helmrelease.json")
        if "/ocirepositories/" in path:
            return _load("ocirepository.json")
        if "/kustomizations/" in path:
            return _load("kustomization.json")
        raise AssertionError(f"unexpected request: {path}")

class CandidateReaderTests(unittest.TestCase):
    def test_read_returns_exactly_the_kwargs_resolve_takes(self):
        reader = CandidateReader(FakeApiReader())
        identity = resolve(**reader.read("flagger-pilot", "podinfo"))

        self.assertEqual(identity.namespace, "flagger-pilot")
        self.assertEqual(identity.canary_name, "podinfo")
        self.assertEqual(identity.template_hash, "759f9fb7bd")

    def test_reads_the_expected_paths_and_nothing_else(self):
        api = FakeApiReader()
        CandidateReader(api).read("flagger-pilot", "podinfo")

        self.assertEqual(
            api.paths,
            [
                "/apis/flagger.app/v1beta1/namespaces/flagger-pilot/canaries/podinfo",
                "/apis/apps/v1/namespaces/flagger-pilot/deployments/podinfo",
                "/apis/apps/v1/namespaces/flagger-pilot/replicasets",
                "/api/v1/namespaces/flagger-pilot/pods",
                "/apis/helm.toolkit.fluxcd.io/v2/namespaces/flagger-pilot/helmreleases/podinfo",
                "/apis/source.toolkit.fluxcd.io/v1/namespaces/flagger-pilot/ocirepositories/podinfo",
                "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flagger-system/kustomizations/flagger-pilot-app",
            ],
        )

    def test_the_flux_kustomization_is_configurable(self):
        api = FakeApiReader()
        CandidateReader(api, kustomization_namespace="ns", kustomization_name="app").read(
            "flagger-pilot", "podinfo"
        )
        self.assertIn("/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/ns/kustomizations/app", api.paths)

    def test_names_from_a_payload_cannot_escape_the_url_path(self):
        # The canary name reaches this from an unauthenticated-until-checked
        # webhook body, so path traversal has to be encoded away, not trusted.
        api = FakeApiReader()
        CandidateReader(api).read("flagger-pilot", "../../secrets")
        self.assertEqual(
            api.paths[0], "/apis/flagger.app/v1beta1/namespaces/flagger-pilot/canaries/..%2F..%2Fsecrets"
        )
        self.assertTrue(all("/../" not in path for path in api.paths))

class DeploymentApiReader(FakeApiReader):
    """``FakeApiReader`` with every Deployment's status overridden; the fixture
    carries no generation, so one is added for the observed check."""

    def __init__(self, **status):
        super().__init__()
        self._status = status

    def get(self, path, *, params=None):
        obj = super().get(path, params=params)
        if "/deployments/" in path:
            obj["metadata"]["generation"] = 12
            obj["status"] = {**obj["status"], "observedGeneration": 12, **self._status}
        return obj

class LiveCanaryTests(unittest.TestCase):
    """The live ``decide.LiveState``: Flagger's own hashes, gated on the
    Deployment behind each one having observed — and, for the primary, still
    serving — the spec Flagger recorded."""

    def test_it_reports_the_hashes_the_canary_tracks(self):
        api = FakeApiReader()
        live = LiveCanary(api, "flagger-pilot", "podinfo")
        self.assertEqual(live.canary_status()["phase"], "Progressing")
        self.assertEqual(live.deployment_template_hash(), "759f9fb7bd")
        self.assertEqual(live.primary_template_hash(), "5b86bd6879")
        self.assertIsNone(live.functional_check(), "no prober is wired into the receiver")
        self.assertIn("/apis/apps/v1/namespaces/flagger-pilot/deployments/podinfo-primary", api.paths)
        self.assertEqual(len(api.paths), 3, "one snapshot per decision, not one read per question")

    def test_a_primary_that_is_not_serving_its_spec_reports_no_hash(self):
        for label, status in (("degraded", {"readyReplicas": 0}), ("surging", {"replicas": 2}),
                              ("unobserved", {"observedGeneration": 11})):
            with self.subTest(label):
                live = LiveCanary(DeploymentApiReader(**status), "flagger-pilot", "podinfo")
                self.assertIsNone(live.primary_template_hash())
                # Only the unobserved case reaches the target: Flagger scales it
                # to zero after every rollout, so replica counts say nothing.
                self.assertEqual(live.deployment_template_hash(), None if label == "unobserved" else "759f9fb7bd")

    def test_a_canary_name_from_a_payload_cannot_escape_the_url_path(self):
        api = FakeApiReader()
        LiveCanary(api, "flagger-pilot", "../../secrets").canary_status()
        self.assertIn("canaries/..%2F..%2Fsecrets", api.paths[0])
