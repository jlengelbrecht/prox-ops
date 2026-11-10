"""Offline guards for the Hindsight 0.9.2 gateway consumer cutover."""

from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "kubernetes/apps/ai/hermes-hindsight/app"


def documents(name):
    return list(yaml.safe_load_all((APP / name).read_text()))


class HindsightGatewayConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = documents("hindsight.yaml")[0]
        cls.values = cls.release["spec"]["values"]
        cls.api = cls.values["api"]
        cls.worker = cls.values["worker"]
        cls.secrets = {doc["metadata"]["name"]: doc for doc in documents("externalsecrets.yaml")}
        cls.gateway_secret = documents("externalsecret-hindsight-litellm.yaml")[0]
        cls.policies = {doc["metadata"]["name"]: doc for doc in documents("networkpolicy.yaml")}

    def test_operation_routes_and_shared_worker_env(self):
        api_env = self.api["env"]
        worker_env = self.worker["env"]
        routes = {
            "HINDSIGHT_API_LLM_MODEL": "hindsight-retain",
            "HINDSIGHT_API_RETAIN_LLM_MODEL": "hindsight-retain",
            "HINDSIGHT_API_REFLECT_LLM_MODEL": "hindsight-reflect",
            "HINDSIGHT_API_CONSOLIDATION_LLM_MODEL": "hindsight-reflect",
        }
        self.assertEqual({name: api_env[name] for name in routes}, routes)
        self.assertEqual(api_env["HINDSIGHT_API_LLM_PROVIDER"], "openai-responses")
        self.assertEqual(api_env["HINDSIGHT_API_LLM_BASE_URL"],
                         "http://litellm.ai.svc.cluster.local:4000/v1")
        for name in ("HINDSIGHT_API_LLM_PROVIDER", "HINDSIGHT_API_LLM_BASE_URL", *routes):
            self.assertEqual(worker_env[name], api_env[name], name)
        self.assertEqual(
            {key for key in worker_env if key.startswith("HINDSIGHT_API_") and key.endswith("LLM_MODEL")},
            {key for key in api_env if key.startswith("HINDSIGHT_API_") and key.endswith("LLM_MODEL")},
        )

    def test_gateway_credential_is_dedicated_and_other_secret_sources_survive(self):
        secret = self.secrets["hindsight-secrets"]
        self.assertEqual(secret["spec"]["secretStoreRef"],
                         {"kind": "ClusterSecretStore", "name": "onepassword-connect"})
        self.assertEqual(secret["spec"]["target"],
                         {"name": "hindsight-secrets", "creationPolicy": "Owner"})
        refs = {item["secretKey"]: item["remoteRef"] for item in secret["spec"]["data"]}
        self.assertNotIn("HINDSIGHT_API_LLM_API_KEY", refs)
        self.assertEqual(refs["postgres-password"],
                         {"key": "postgres-cluster", "property": "HINDSIGHT_PASSWORD"})
        for name in ("HINDSIGHT_API_TENANT_API_KEY", "HINDSIGHT_CP_DATAPLANE_API_KEY"):
            self.assertEqual(refs[name], {"key": "hindsight-secrets", "property": "api-tenant-key"})
        self.assertEqual(len(refs), 3)
        self.assertEqual(self.gateway_secret["spec"]["data"], [{
            "secretKey": "credential",
            "remoteRef": {"key": "hindsight-litellm", "property": "credential"},
        }])
        self.assertEqual(self.gateway_secret["spec"]["target"],
                         {"name": "hindsight-litellm", "creationPolicy": "Owner"})
        self.assertEqual(self.gateway_secret["spec"]["secretStoreRef"],
                         {"kind": "ClusterSecretStore", "name": "onepassword-connect"})
        patches = self.release["spec"]["postRenderers"][0]["kustomize"]["patches"]
        for kind, name in (("Deployment", "hindsight-api"), ("StatefulSet", "hindsight-worker")):
            key_entries = [op["value"] for patch in patches
                           if patch["target"].get("kind") == kind and patch["target"].get("name") == name
                           for op in yaml.safe_load(patch["patch"])
                           if op["op"] == "add" and op["path"] == "/spec/template/spec/containers/0/env/-"
                           and op["value"]["name"] == "HINDSIGHT_API_LLM_API_KEY"]
            self.assertEqual(key_entries, [{"name": "HINDSIGHT_API_LLM_API_KEY", "valueFrom": {
                "secretKeyRef": {"name": "hindsight-litellm", "key": "credential", "optional": False}}}], name)
        self.assertIn("hindsight-litellm", self.values["podAnnotations"]["secret.reloader.stakater.com/reload"])

    def test_key_preparation_is_independent_of_consumer_switch(self):
        # Stage 1 publishes only the new ExternalSecret and its kustomization
        # entry. The baseline release still points at direct Codex then.
        baseline = yaml.safe_load(subprocess.check_output(
            ["git", "show", "HEAD:kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml"], cwd=ROOT))
        self.assertEqual(baseline["spec"]["values"]["api"]["env"]["HINDSIGHT_API_LLM_PROVIDER"],
                         "openai-codex")
        resources = documents("kustomization.yaml")[0]["resources"]
        self.assertIn("./externalsecret-hindsight-litellm.yaml", resources)

    def test_gateway_egress_is_pod_scoped_for_api_and_worker(self):
        for name in ("hindsight-api", "hindsight-worker"):
            with self.subTest(policy=name):
                rules = self.policies[name]["spec"]["egress"]
                gateway = [rule for rule in rules if any(
                    endpoint.get("matchLabels", {}).get("app.kubernetes.io/name") == "litellm"
                    for endpoint in rule.get("toEndpoints", [])
                )]
                self.assertEqual(gateway, [{
                    "toEndpoints": [{"matchLabels": {
                        "io.kubernetes.pod.namespace": "ai", "app.kubernetes.io/name": "litellm"}}],
                    "toPorts": [{"ports": [{"port": "4000", "protocol": "TCP"}]}],
                }])
                self.assertFalse(any("toCIDR" in rule or "toCIDRSet" in rule for rule in rules))
                self.assertTrue(any(rule.get("toFQDNs") == [
                    {"matchName": "chatgpt.com"}, {"matchName": "auth.openai.com"}]
                    for rule in rules))

    def test_runtime_and_data_safeguards_remain(self):
        env = self.api["env"]
        for name, value in {
            "HINDSIGHT_API_LLM_MAX_CONCURRENT": "2",
            "HINDSIGHT_API_LLM_TIMEOUT": "30",
            "HINDSIGHT_API_LLM_MAX_RETRIES": "1",
            "HINDSIGHT_API_ENABLE_AUTO_CONSOLIDATION": "false",
            "HF_HUB_OFFLINE": "1",
        }.items():
            self.assertEqual(env[name], value, name)
        self.assertEqual(self.api["replicaCount"], 1)
        self.assertEqual(self.worker["replicaCount"], 0)
        self.assertEqual(self.values["postgresql"]["enabled"], False)
        self.assertEqual(self.values["postgresql"]["external"]["database"], "hindsight")
        self.assertEqual(self.api["persistence"]["modelCache"]["enabled"], False)
        self.assertEqual(self.api["extraVolumes"], self.worker["extraVolumes"])
        self.assertEqual(self.api["extraVolumeMounts"], self.worker["extraVolumeMounts"])
        self.assertEqual(self.api["extraVolumes"][0]["persistentVolumeClaim"]["claimName"],
                         "hindsight-codex-auth")
        self.assertEqual(env["CODEX_HOME"], "/home/hindsight/codex-auth")
        patches = self.release["spec"]["postRenderers"][0]["kustomize"]["patches"]
        worker_patch = next(item["patch"] for item in patches
                            if item["target"].get("name") == "hindsight-worker")
        operations = yaml.safe_load(worker_patch)
        self.assertIn({"op": "add", "path": "/spec/replicas", "value": 0}, operations)
        self.assertIn({"op": "test", "path": "/spec/template/spec/containers/0/env/40",
                       "value": {"name": "HINDSIGHT_API_WORKER_ID", "value": "hindsight-api"}},
                      operations)
        self.assertIn({"op": "remove", "path": "/spec/template/spec/containers/0/env/40"},
                      operations)


if __name__ == "__main__":
    unittest.main()
