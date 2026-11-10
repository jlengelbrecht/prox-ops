"""Offline contract for prepared Hindsight operation routes; no provider calls."""

import hashlib
import json
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[3]
CONFIGMAP = ROOT / "kubernetes/apps/ai/litellm/app/configmap.yaml"
BACKEND = "automation-chatgpt-responses"
OPERATIONS = ("hindsight-retain", "hindsight-reflect")
TIERS = ("SIMPLE", "MEDIUM", "COMPLEX", "REASONING")

# Canonical parsed values before this addition. These catch changes to existing
# aliases and proxy policy without depending on Git or YAML comment formatting.
EXISTING_MODELS = {
    "gpt-4": "2e8212804106de8d3de815ebd68cb49c584b5b36ce7fbcdc0cede9ea6da43694",
    "gpt-3.5-turbo": "03287cbc20d142e263b6002ea4766ece0d7c01c453f9524dc3889e8f9921b465",
    "anthropic/*": "d300d155ed8689f91b756818d0d291ecddf79f1391d8bb6c616fea10b3dbca70",
    "merge-steward-review": "d5609def8430fbda725817a699872ecccdd7d56d7a1e39cda446361ab8f4875e",
    "chatgpt-gpt-6-luna": "82407c2212cb2565256f831ad15502181850ed70458cf129daddc1d4ccf8510c",
    "chatgpt-gpt-6-sol": "3562f9e0af3323aff063b5cca00d350b4c22b0bdda2a28f5926fec6d62fc54d7",
    "chatgpt-gpt-6.1-sol": "49e615e3f87d1ff670896962b512581cca5bb66b16f5b5f48096ee0eec51fccb",
    "smart-router": "faeccba44de1d29600e4a54b0f5b946b2d88d3a6c17183caa3e8124465db5e46",
    "project-chatgpt-responses": "f0f3d0ac971855a8fa80f5500a98c96c762fd7ae3fa570886146b8629ff39be1",
    "project-claude-native": "1cf770905b8784293cf9de335cb0a19003742aa52f24daa4462bdb2574f32d00",
    "project-router": "f22493c577ac6f53932f786a83e156bbe0aae54d47e0fcbb7bf379649067c657",
    "dub-text": "989f66778c9f331482471dc6c68eb26fc94097243a7aee405b928f3cc844a3d1",
}
EXISTING_SETTINGS = {
    "router_settings": "266ab498cbfa7bf99840061fdca8950dc9b229702e7eb3f3003232ba289658db",
    "general_settings": "5a68cea07eb029c10267bd873c3659a352a5fa0b4d2e44f4e93fa2adc23394e7",
    "litellm_settings": "97d1d04234c42f101662cc9a1d8167df26fc6ae8073929091f91c243289bbeba",
    "guardrails": "6665cf83066600df5a623edd544f40f7eaa7e60f0bf0db87d7f9757f980ad1a1",
    "mcp_servers": "6460d43023e34af4eb3121a2d55a3cf4bf43a873fec0a942c1275103cf1a2587",
}


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class HindsightRouterConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        outer = yaml.safe_load(CONFIGMAP.read_text())
        assert outer["kind"] == "ConfigMap" and outer["metadata"]["name"] == "litellm-config"
        cls.config = yaml.safe_load(outer["data"]["config.yaml"])
        entries = cls.config["model_list"]
        cls.models = {entry["model_name"]: entry for entry in entries}
        assert len(cls.models) == len(entries), "duplicate model_name"

    def test_operation_tiers_have_only_the_qualified_subscription_backend(self):
        allowlist = {BACKEND}
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                entry = self.models[operation]
                params = entry["litellm_params"]
                self.assertEqual(params["model"], "auto_router/complexity_router")
                route = params["complexity_router_config"]
                self.assertEqual(route["classifier_type"], "heuristic")
                self.assertIs(route["adaptive"], False)
                self.assertEqual(set(route["tiers"]), set(TIERS))
                for tier in TIERS:
                    target = route["tiers"][tier]
                    self.assertIn(target, allowlist)
                    self.assertIn(target, self.models)
                    self.assertEqual(self.models[target]["litellm_params"]["model"],
                                     "chatgpt/gpt-6.1-sol")

    def test_backend_floor_responses_and_failure_boundary(self):
        backend = self.models[BACKEND]
        self.assertEqual(backend["model_info"]["mode"], "responses")
        self.assertEqual(backend["litellm_params"], {
            "model": "chatgpt/gpt-6.1-sol", "reasoning_effort": "high",
            "timeout": 300, "max_retries": 0,
        })
        self.assertEqual(self.config["router_settings"]["num_retries"], 0)
        self.assertNotIn("fallbacks", self.config["router_settings"])
        self.assertNotIn("fallbacks", self.config["litellm_settings"])
        for name in (*OPERATIONS, BACKEND):
            with self.subTest(name=name):
                entry = self.models[name]
                self.assertNotIn("fallbacks", entry)
                for forbidden in ("fallbacks", "api_key", "api_base"):
                    self.assertNotIn(forbidden, entry["litellm_params"])

    def test_existing_aliases_and_proxy_settings_are_unchanged(self):
        self.assertEqual(set(self.models), set(EXISTING_MODELS) | set(OPERATIONS) | {BACKEND})
        for name, expected in EXISTING_MODELS.items():
            with self.subTest(model=name):
                self.assertEqual(digest(self.models[name]), expected)
        for name, expected in EXISTING_SETTINGS.items():
            with self.subTest(setting=name):
                self.assertEqual(digest(self.config[name]), expected)


if __name__ == "__main__":
    unittest.main()
