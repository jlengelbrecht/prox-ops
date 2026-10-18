"""Guard the two-commit Groschen cutover and retained identity."""

import pathlib
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
KUSTOMIZE = "/home/devbox/.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize"
SOURCE = ROOT / "kubernetes/apps/groschen-test"
TARGET = ROOT / "kubernetes/apps/groschen"
ACTIVE = (TARGET / "app/ks.yaml").exists()
EXPECTED_PHASE = "stop"
COPIED = {"groschen-key", "groschen-owner-credentials", "groschen-member-credentials"}


def render(path):
    output = subprocess.check_output([KUSTOMIZE, "build", str(path)], text=True)
    return [doc for doc in yaml.safe_load_all(output) if doc]


def named(docs, kind, name):
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {kind}/{name}, found {len(matches)}")
    return matches[0]


class NamespaceCutoverTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = render(SOURCE)
        cls.source_infra = render(SOURCE / "groschen/infra")
        cls.old_app = render(SOURCE / "groschen/app")
        cls.old_admin = render(SOURCE / "groschen/admin")
        cls.source_grant = render(SOURCE / "groschen/transfer-grant")
        cls.target = render(TARGET)
        cls.target_infra = render(TARGET / "infra")
        cls.transfer = render(TARGET / "transfer")

    def test_stop_resources_are_retired_without_pruning_old_identity(self):
        self.assertEqual(ACTIVE, EXPECTED_PHASE == "start")
        for docs in (self.old_app, self.source_grant, self.transfer):
            self.assertEqual([d["kind"] for d in docs], ["ConfigMap"])
        self.assertFalse({"Deployment", "HTTPRoute", "Service", "Job", "Role", "RoleBinding", "ServiceAccount"} &
                         {d["kind"] for d in self.old_app + self.source_grant + self.transfer})
        for name in ("groschen-app", "groschen-source-transfer-grant"):
            self.assertTrue(named(self.source, "Kustomization", name)["spec"]["prune"])
        self.assertTrue(named(self.target, "Kustomization", "groschen-secret-transfer")["spec"]["prune"])
        self.assertTrue(named(self.target, "Kustomization", "groschen-namespace")["spec"]["prune"])
        self.assertEqual([d["kind"] for d in self.old_admin], ["Job"])
        source_secrets = [d for d in self.source_infra if d["kind"] == "ExternalSecret"]
        self.assertEqual(COPIED, {d["metadata"]["name"] for d in source_secrets
                                  if d["spec"].get("refreshPolicy") == "CreatedOnce"})
        for secret in source_secrets:
            if secret["metadata"]["name"] in COPIED:
                self.assertEqual(secret["spec"]["target"]["creationPolicy"], "Orphan")
                self.assertTrue(secret["spec"]["target"]["immutable"])
        self.assertEqual(named(self.source, "Namespace", "groschen-test")["metadata"]["name"], "groschen-test")
        self.assertEqual(named(self.target_infra, "Namespace", "groschen")["metadata"]["annotations"]
                         ["kustomize.toolkit.fluxcd.io/prune"], "disabled")
        self.assertFalse({"Job", "Role", "RoleBinding", "ServiceAccount"} &
                         {d["kind"] for d in self.target_infra + self.transfer})

    def test_start_reuses_existing_credentials_and_runtime(self):
        if not ACTIVE:
            self.assertFalse(any(d["kind"] in {"Deployment", "HTTPRoute", "Service"} for d in self.target))
            return
        app_ks = named(self.target, "Kustomization", "groschen-app")
        self.assertEqual(app_ks["spec"]["path"], "./kubernetes/apps/groschen/app")
        self.assertEqual(app_ks["spec"]["targetNamespace"], "groschen")
        self.assertTrue(app_ks["spec"]["prune"])
        app = render(TARGET / "app")
        deployments = {d["metadata"]["name"]: d for d in app if d["kind"] == "Deployment"}
        self.assertEqual(set(deployments), {"groschen-api", "groschen-worker"})
        self.assertEqual({d["metadata"]["namespace"] for d in app}, {"groschen"})
        images = {d["spec"]["template"]["spec"]["containers"][0]["image"] for d in deployments.values()}
        self.assertEqual(len(images), 1)
        old = list(yaml.safe_load_all((SOURCE / "groschen/app/deployment.yaml").read_text()))
        self.assertEqual(images, {old[0]["spec"]["template"]["spec"]["containers"][0]["image"]})
        for name, deployment in deployments.items():
            pod = deployment["spec"]["template"]["spec"]
            old_pod = next(d for d in old if d["metadata"]["name"] == name)["spec"]["template"]["spec"]
            self.assertEqual(pod, old_pod)
        route = named(app, "HTTPRoute", "groschen")
        self.assertEqual(route["spec"]["hostnames"], ["groshen.homelab0.org"])
        self.assertEqual(route["spec"]["parentRefs"][0]["name"], "envoy-internal")
        self.assertEqual(route["spec"]["rules"][0]["backendRefs"][0]["name"], "groschen-api")
        self.assertEqual(named(app, "Service", "groschen-api")["spec"]["selector"],
                         {"app.kubernetes.io/name": "groschen-api"})
        es = named(self.target_infra, "ExternalSecret", "groschen-db-credentials")
        self.assertEqual({item["remoteRef"]["property"] for item in es["spec"]["data"]},
                         {"GROSCHEN_USER", "GROSCHEN_PASSWORD"})
        self.assertEqual(es["spec"]["target"]["template"]["data"],
                         named(self.source_infra, "ExternalSecret", "groschen-db-credentials")
                         ["spec"]["target"]["template"]["data"])
        self.assertEqual(named(self.target_infra, "ExternalSecret", "groschen-ghcr")["spec"],
                         named(self.source_infra, "ExternalSecret", "groschen-ghcr")["spec"])
        self.assertFalse(COPIED & {d["metadata"]["name"] for d in self.target_infra
                                   if d["kind"] == "ExternalSecret"})
        self.assertFalse(any(d["kind"] == "Job" for d in app))


if __name__ == "__main__":
    unittest.main()
