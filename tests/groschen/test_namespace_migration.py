"""Guard the two-commit Groschen cutover and retained identity."""

import copy
import pathlib
import subprocess
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
KUSTOMIZE = "/home/devbox/.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize"
FLUX = pathlib.Path.home() / ".local/share/mise/installs/aqua-fluxcd-flux2/2.7.2/flux"
SOURCE = ROOT / "kubernetes/apps/groschen-test"
TARGET = ROOT / "kubernetes/apps/groschen"
ACTIVE = (TARGET / "app/ks.yaml").exists()
EXPECTED_PHASE = "start"
COPIED = {"groschen-key", "groschen-owner-credentials", "groschen-member-credentials"}


def render(path):
    output = subprocess.check_output([KUSTOMIZE, "build", str(path)], text=True)
    return [doc for doc in yaml.safe_load_all(output) if doc]


def named(docs, kind, name):
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {kind}/{name}, found {len(matches)}")
    return matches[0]


def assert_tls_only_pod_delta(test, old_pod, new_pod):
    """The target may add only certificate verification and its public CA mount."""
    old_pod = copy.deepcopy(old_pod)
    new_pod = copy.deepcopy(new_pod)
    test.assertEqual(len(old_pod["containers"]), len(new_pod["containers"]))
    for old_container, new_container in zip(old_pod["containers"], new_pod["containers"]):
        test.assertEqual(new_container["image"], old_container["image"])
        old_dsn = next(item["value"] for item in old_container["env"]
                       if item["name"] == "DATABASE_URL")
        new_dsn = next(item for item in new_container["env"]
                       if item["name"] == "DATABASE_URL")
        test.assertEqual(old_dsn.count("sslmode=disable"), 1)
        test.assertEqual(new_dsn["value"], old_dsn.replace(
            "sslmode=disable", "sslmode=verify-full sslrootcert=/tls/ca.crt"))
        new_dsn["value"] = old_dsn
        ca_mount = {"name": "postgres-ca", "mountPath": "/tls", "readOnly": True}
        test.assertEqual(new_container["volumeMounts"].count(ca_mount), 1)
        new_container["volumeMounts"].remove(ca_mount)
    ca_volume = {"name": "postgres-ca", "secret": {
        "secretName": "groschen-postgres-ca-public", "defaultMode": 0o444,
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }}
    test.assertEqual(new_pod["volumes"].count(ca_volume), 1)
    new_pod["volumes"].remove(ca_volume)
    test.assertEqual(new_pod, old_pod)


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

    def test_cluster_apps_root_builds_with_one_app_owner(self):
        output = subprocess.check_output([
            str(FLUX), "build", "kustomization", "cluster-apps",
            "--path", "kubernetes/apps",
            "--kustomization-file", "kubernetes/flux/cluster/ks.yaml",
            "--dry-run",
        ], cwd=ROOT, text=True)
        docs = [doc for doc in yaml.safe_load_all(output) if doc]
        identity = lambda doc: (doc["apiVersion"], doc["kind"],
                                doc["metadata"].get("namespace", ""), doc["metadata"]["name"])
        self.assertEqual(len(docs), len({identity(doc) for doc in docs}))
        owners = [doc for doc in docs
                  if doc["kind"] == "Kustomization"
                  and doc["apiVersion"].startswith("kustomize.toolkit.fluxcd.io/")
                  and doc["metadata"].get("namespace") == "flux-system"
                  and doc["metadata"]["name"] == "groschen-app"]
        self.assertEqual(len(owners), 1)
        self.assertEqual(owners[0]["spec"]["path"],
                         "./kubernetes/apps/groschen/app" if ACTIVE else
                         "./kubernetes/apps/groschen-test/groschen/app")
        self.assertEqual(owners[0]["spec"]["targetNamespace"],
                         "groschen" if ACTIVE else "groschen-test")

    def test_stop_resources_are_retired_without_pruning_old_identity(self):
        self.assertEqual(ACTIVE, EXPECTED_PHASE == "start")
        for docs in (self.old_app, self.source_grant):
            self.assertEqual([d["kind"] for d in docs], ["ConfigMap"])
        self.assertFalse({"Deployment", "HTTPRoute", "Service", "Job", "Role", "RoleBinding", "ServiceAccount"} &
                         {d["kind"] for d in self.old_app + self.source_grant + self.transfer})
        source_owners = ["groschen-source-transfer-grant"] + ([] if ACTIVE else ["groschen-app"])
        for name in source_owners:
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
        self.assertFalse({"Job", "Role", "RoleBinding"} &
                         {d["kind"] for d in self.target_infra + self.transfer})
        self.assertEqual({d["metadata"]["name"] for d in self.target_infra
                          if d["kind"] == "ServiceAccount"},
                         {"groschen-postgres-ca-reader"})
        self.assertFalse(any(d["kind"] == "ServiceAccount" for d in self.transfer))

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
        self.assertRegex(next(iter(images)),
                         r"\Aghcr\.io/jlengelbrecht/groschen:main@sha256:[0-9a-f]{64}\Z")
        for name, deployment in deployments.items():
            old_deployment = next(d for d in old if d["metadata"]["name"] == name)
            self.assertEqual(deployment["metadata"]["annotations"], {
                "secret.reloader.stakater.com/reload":
                    "groschen-db-credentials,groschen-postgres-ca-public"})
            self.assertEqual(old_deployment["metadata"]["annotations"], {
                "secret.reloader.stakater.com/reload": "groschen-db-credentials"})
            assert_tls_only_pod_delta(self, old_deployment["spec"]["template"]["spec"],
                                      deployment["spec"]["template"]["spec"])
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
        db_policies = list(yaml.safe_load_all((ROOT / "kubernetes/apps/database/cluster/app/networkpolicy.yaml").read_text()))
        ingress = named(db_policies, "CiliumNetworkPolicy", "postgres-cluster")["spec"]["ingress"][0]
        allowed = {(match["matchLabels"].get("io.kubernetes.pod.namespace"),
                    match["matchLabels"].get("app.kubernetes.io/name"))
                   for match in ingress["fromEndpoints"]}
        self.assertTrue({("groschen", "groschen-api"), ("groschen", "groschen-worker")} <= allowed)
        self.assertEqual(ingress["toPorts"], [{"ports": [{"port": "5432", "protocol": "TCP"}]}])

    def test_paired_renovate_digest_fixture_keeps_tls_delta_valid(self):
        old = named(list(yaml.safe_load_all(
            (SOURCE / "groschen/app/deployment.yaml").read_text())),
            "Deployment", "groschen-api")
        new = named(render(TARGET / "app"), "Deployment", "groschen-api")
        old_pod = copy.deepcopy(old["spec"]["template"]["spec"])
        new_pod = copy.deepcopy(new["spec"]["template"]["spec"])
        changed_digest = "ghcr.io/jlengelbrecht/groschen:main@sha256:" + "a" * 64
        old_pod["containers"][0]["image"] = changed_digest
        new_pod["containers"][0]["image"] = changed_digest
        assert_tls_only_pod_delta(self, old_pod, new_pod)


if __name__ == "__main__":
    unittest.main()
