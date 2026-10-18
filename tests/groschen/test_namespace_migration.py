"""Guard the namespace preparation's transfer and isolation boundaries."""

import base64
import copy
import json
import os
import pathlib
import subprocess
import tempfile
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
KUSTOMIZE = "/home/devbox/.local/share/mise/installs/aqua-kubernetes-sigs-kustomize/5.7.1/kustomize"
SOURCE = ROOT / "kubernetes/apps/groschen-test"
SOURCE_GRANT = SOURCE / "groschen/transfer-grant"
TARGET = ROOT / "kubernetes/apps/groschen"
TARGET_INFRA = TARGET / "infra"
TRANSFER = TARGET / "transfer"
NAMES = {
    "groschen-key": "groschen_secret_key",
    "groschen-owner-credentials": "password",
    "groschen-member-credentials": "password",
}


def render(path):
    output = subprocess.check_output([KUSTOMIZE, "build", str(path)], text=True)
    return [doc for doc in yaml.safe_load_all(output) if doc]


def named(docs, kind, name):
    matches = [doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {kind}/{name}, found {len(matches)}")
    return matches[0]


class NamespaceMigrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = render(SOURCE)
        cls.source_infra = render(SOURCE / "groschen/infra")
        cls.source_grant = render(SOURCE_GRANT)
        cls.target = render(TARGET)
        cls.target_infra = render(TARGET_INFRA)
        cls.transfer = render(TRANSFER)

    def test_source_read_grant_is_three_named_secrets_only(self):
        role = named(self.source_grant, "Role", "groschen-secret-transfer-source")
        self.assertEqual(role["metadata"]["namespace"], "groschen-test")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""], "resources": ["secrets"],
            "resourceNames": list(NAMES), "verbs": ["get"],
        }])
        binding = named(self.source_grant, "RoleBinding", "groschen-secret-transfer-source")
        self.assertEqual(binding["metadata"]["namespace"], "groschen-test")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
            "name": "groschen-secret-transfer-source",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": "groschen-secret-transfer",
            "namespace": "groschen",
        }])
        self.assertEqual({d["kind"] for d in self.source_grant},
                         {"Role", "RoleBinding"})
        self.assertFalse({"Role", "RoleBinding"}.intersection(d["kind"] for d in self.source_infra))

    def test_target_write_grant_and_token_are_transfer_only(self):
        role = named(self.transfer, "Role", "groschen-secret-transfer-target")
        self.assertEqual(role["metadata"]["namespace"], "groschen")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""], "resources": ["secrets"],
             "resourceNames": list(NAMES), "verbs": ["get"]},
            {"apiGroups": [""], "resources": ["secrets"], "verbs": ["create"]},
        ])
        binding = named(self.transfer, "RoleBinding", "groschen-secret-transfer-target")
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": "groschen-secret-transfer", "namespace": "groschen",
        }])
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        account = named(self.transfer, "ServiceAccount", "groschen-secret-transfer")
        self.assertFalse(account["automountServiceAccountToken"])
        job = named(self.transfer, "Job", "groschen-secret-transfer")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], account["metadata"]["name"])
        self.assertTrue(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        container = pod["containers"][0]
        self.assertIn("@sha256:", container["image"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])

    def test_create_only_copy_fails_on_mismatch_and_never_generates(self):
        job = named(self.transfer, "Job", "groschen-secret-transfer")
        script = job["spec"]["template"]["spec"]["containers"][0]["args"][0]
        subprocess.run(["/bin/sh", "-n", "-c", script], check=True)
        for name, key in NAMES.items():
            self.assertIn(f"{name}:{key}", script)
        self.assertIn("kubectl create -f -", script)
        self.assertIn('source" != "$target', script)
        self.assertIn('.type == "Opaque" and .immutable == true', script)
        self.assertIn('keys == [$key]', script)
        self.assertIn("immutable:true", script)
        for forbidden in ("kubectl apply", "kubectl replace", "kubectl patch", "kubectl delete",
                          "randBytes", "--from-literal", "set -x"):
            self.assertNotIn(forbidden, script)
        self.assertEqual({d["kind"] for d in self.transfer},
                         {"Job", "ServiceAccount", "Role", "RoleBinding"})

    def test_target_is_default_denied_and_transfer_has_only_api_egress(self):
        deny = named(self.target_infra, "NetworkPolicy", "groschen-default-deny")
        self.assertEqual(deny["metadata"]["namespace"], "groschen")
        self.assertEqual(deny["spec"], {
            "podSelector": {}, "policyTypes": ["Ingress", "Egress"],
        })
        policy = named(self.target_infra, "CiliumNetworkPolicy", "groschen-secret-transfer")
        self.assertEqual(policy["metadata"]["namespace"], "groschen")
        pod_labels = named(self.transfer, "Job", "groschen-secret-transfer")["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(policy["spec"], {
            "endpointSelector": {"matchLabels": pod_labels},
            "enableDefaultDeny": {"ingress": True, "egress": True},
            "egress": [{"toEntities": ["kube-apiserver"]}],
        })
        self.assertEqual(len([d for d in self.target_infra if d["kind"] == "NetworkPolicy"]), 1)
        self.assertEqual(len([d for d in self.target_infra if d["kind"] == "CiliumNetworkPolicy"]), 1)
        self.assertFalse(any(d["kind"] == "CiliumNetworkPolicy" for d in self.transfer))

    def test_transfer_is_idempotent_and_rejects_existing_mismatch(self):
        script = named(self.transfer, "Job", "groschen-secret-transfer")["spec"]["template"]["spec"]["containers"][0]["args"][0]
        mock = '''#!/usr/bin/env python3
import json, os, sys
path = os.environ["TEST_STATE"]
with open(path) as stream:
    state = json.load(stream)
args = sys.argv[1:]
if args[0] == "get":
    name = args[2]
    namespace = args[args.index("-n") + 1]
    secret = state[namespace].get(name)
    if secret is None:
        sys.exit(1)
    if "-o" not in args:
        print(name)
    else:
        output = args[args.index("-o") + 1]
        if output == "json":
            print(json.dumps({"metadata": {"name": name}, **secret}))
        elif output == "jsonpath={.immutable}":
            print(str(secret.get("immutable", False)).lower())
        else:
            key = output.removeprefix("jsonpath={.data.").removesuffix("}")
            print(secret["data"].get(key, ""))
elif args[:3] == ["create", "-f", "-"]:
    secret = json.load(sys.stdin)
    namespace = secret["metadata"]["namespace"]
    name = secret["metadata"]["name"]
    if name in state[namespace]:
        sys.exit(1)
    state[namespace][name] = {key: secret[key] for key in ("type", "immutable", "data")}
    state["creates"] += 1
    with open(path, "w") as stream:
        json.dump(state, stream)
else:
    sys.exit(2)
'''
        with tempfile.TemporaryDirectory() as directory:
            stub = pathlib.Path(directory) / "kubectl"
            stub.write_text(mock)
            stub.chmod(0o755)
            state_file = pathlib.Path(directory) / "state.json"
            state = {
                "groschen-test": {
                    name: {"type": "Opaque", "immutable": True, "data": {
                        key: base64.b64encode(name.encode()).decode()}}
                    for name, key in NAMES.items()},
                "groschen": {}, "creates": 0,
            }
            state_file.write_text(json.dumps(state))
            env = {**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"],
                   "TEST_STATE": str(state_file)}

            def run():
                return subprocess.run(["/bin/sh", "-ec", script], env=env,
                                      text=True, capture_output=True)

            self.assertEqual(run().returncode, 0)
            self.assertEqual(run().returncode, 0)
            state = json.loads(state_file.read_text())
            self.assertEqual(state["creates"], 3)
            self.assertEqual(set(state["groschen"]), set(NAMES))
            self.assertTrue(all(secret["immutable"] for secret in state["groschen"].values()))
            self.assertTrue(all(secret["type"] == "Opaque" for secret in state["groschen"].values()))
            self.assertTrue(all(set(secret["data"]) == {NAMES[name]}
                                for name, secret in state["groschen"].items()))

            for change in ("different-value", "mutable", "extra-key", "wrong-type"):
                with self.subTest(change=change):
                    bad = copy.deepcopy(state)
                    key_secret = bad["groschen"]["groschen-key"]
                    if change == "different-value":
                        key_secret["data"]["groschen_secret_key"] = "different"
                    elif change == "mutable":
                        key_secret["immutable"] = False
                    elif change == "extra-key":
                        key_secret["data"]["unexpected"] = "extra"
                    else:
                        key_secret["type"] = "kubernetes.io/basic-auth"
                    state_file.write_text(json.dumps(bad))
                    result = run()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(json.loads(state_file.read_text()), bad)
                    self.assertNotIn(key_secret["data"]["groschen_secret_key"],
                                     result.stdout + result.stderr)

    def test_namespace_is_restricted_and_not_pruned(self):
        ns = named(self.target_infra, "Namespace", "groschen")
        self.assertEqual(ns["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"],
                         "disabled")
        for mode in ("enforce", "audit", "warn"):
            self.assertEqual(ns["metadata"]["labels"][f"pod-security.kubernetes.io/{mode}"],
                             "restricted")
        ks = named(self.target, "Kustomization", "groschen-secret-transfer")
        infra_ks = named(self.target, "Kustomization", "groschen-namespace")
        source_ks = named(self.source, "Kustomization", "groschen-infra")
        grant_ks = named(self.source, "Kustomization", "groschen-source-transfer-grant")
        self.assertEqual(ks["apiVersion"], "kustomize.toolkit.fluxcd.io/v1")
        self.assertEqual(set(ks["spec"]), set(source_ks["spec"]))
        self.assertEqual({d["name"] for d in ks["spec"]["dependsOn"]},
                         {"groschen-namespace", "external-secrets-operator", "groschen-source-transfer-grant"})
        self.assertEqual(grant_ks["spec"]["path"],
                         "./kubernetes/apps/groschen-test/groschen/transfer-grant")
        self.assertEqual(grant_ks["spec"]["dependsOn"],
                         [{"name": "groschen-infra", "namespace": "flux-system"}])
        self.assertTrue(grant_ks["spec"]["prune"])
        self.assertTrue(grant_ks["spec"]["wait"])
        self.assertEqual(infra_ks["spec"]["path"], "./kubernetes/apps/groschen/infra")
        self.assertFalse(infra_ks["spec"]["prune"])
        self.assertTrue(infra_ks["spec"]["wait"])
        self.assertEqual(ks["spec"]["targetNamespace"], "groschen")
        self.assertFalse(ks["spec"]["prune"])
        self.assertTrue(ks["spec"]["wait"])

    def test_stale_source_infra_ready_cannot_start_transfer(self):
        transfer = named(self.target, "Kustomization", "groschen-secret-transfer")
        dependencies = {item["name"] for item in transfer["spec"]["dependsOn"]}
        old_ready = {"groschen-infra", "external-secrets-operator", "groschen-namespace"}
        self.assertEqual(dependencies - old_ready, {"groschen-source-transfer-grant"})
        self.assertFalse(dependencies <= old_ready)
        self.assertTrue(dependencies <= old_ready | {"groschen-source-transfer-grant"})

    def test_no_new_app_and_old_app_remains_active(self):
        forbidden = {"Deployment", "StatefulSet", "DaemonSet", "CronJob", "HTTPRoute", "Service"}
        self.assertFalse(forbidden.intersection(d["kind"] for d in self.target + self.target_infra + self.transfer))
        self.assertEqual({d["kind"] for d in self.source if d["kind"] == "Namespace"},
                         {"Namespace"})
        self.assertIn("groschen-app", {d["metadata"]["name"] for d in self.source})
        self.assertIn("groschen-admin", {d["metadata"]["name"] for d in self.source})
        self.assertEqual(named(self.source, "Namespace", "groschen-test")["metadata"]["name"],
                         "groschen-test")


if __name__ == "__main__":
    unittest.main()
