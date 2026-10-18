"""Guard the rendered Groschen egress policy surface."""

import copy
import pathlib
import subprocess
import unittest

import yaml


ROOT = pathlib.Path(__file__).resolve().parents[2]
INFRA = ROOT / "kubernetes/apps/groschen-test/groschen/infra"
KUBECTL = "/home/devbox/.local/share/mise/installs/aqua-kubernetes-kubectl/1.34.0/kubectl"
CLIENTS = {"groschen-api", "groschen-worker", "groschen-admin"}


def selected(selector, app):
    labels = {"app.kubernetes.io/name": app}
    if any(labels.get(key) != value for key, value in selector.get("matchLabels", {}).items()):
        return False
    for expr in selector.get("matchExpressions", []):
        if expr["operator"] != "In" or labels.get(expr["key"]) not in expr["values"]:
            return False
    return True


def assert_network_policy_boundaries(test, policies):
    """Check every selected NetworkPolicy's full rules and policy types."""
    by_name = {policy["metadata"]["name"]: policy["spec"] for policy in policies}
    test.assertEqual(len(by_name), len(policies))
    test.assertEqual(set(by_name), {"groschen-default-deny", "groschen-clients", "groschen-api"})

    test.assertEqual(by_name["groschen-default-deny"], {
        "podSelector": {}, "policyTypes": ["Ingress", "Egress"]})
    test.assertEqual(by_name["groschen-clients"], {
        "podSelector": {"matchExpressions": [{
            "key": "app.kubernetes.io/name", "operator": "In", "values": [
                "groschen-api", "groschen-worker", "groschen-admin"]}]},
        "policyTypes": ["Egress"],
        "egress": [{
            "to": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "database"}},
                "podSelector": {"matchLabels": {"cnpg.io/cluster": "postgres"}},
            }],
            "ports": [{"protocol": "TCP", "port": 5432}],
        }],
    })
    test.assertEqual(by_name["groschen-api"], {
        "podSelector": {"matchLabels": {"app.kubernetes.io/name": "groschen-api"}},
        "policyTypes": ["Ingress"],
        "ingress": [{
            "from": [{
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "network"}},
                "podSelector": {"matchLabels": {
                    "gateway.envoyproxy.io/owning-gateway-name": "envoy-internal",
                    "gateway.envoyproxy.io/owning-gateway-namespace": "network"}},
            }],
            "ports": [{"protocol": "TCP", "port": 8080}],
        }],
    })


class PlaidEgressTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        output = subprocess.check_output([KUBECTL, "kustomize", str(INFRA)], text=True)
        cls.docs = [doc for doc in yaml.safe_load_all(output) if doc]
        cls.cnps = [doc for doc in cls.docs if doc["kind"] == "CiliumNetworkPolicy"]
        cls.nps = [doc for doc in cls.docs if doc["kind"] == "NetworkPolicy"]

    def test_policies_render_in_groschen_namespace(self):
        for policy in self.cnps + self.nps:
            with self.subTest(name=policy["metadata"]["name"]):
                self.assertEqual(policy["metadata"]["namespace"], "groschen-test")
                if policy["kind"] == "CiliumNetworkPolicy":
                    self.assertNotIn("ingress", policy["spec"])

    def test_sandbox_https_is_exact_and_only_for_api_worker(self):
        fqdn_rules = [(policy, rule) for policy in self.cnps
                      for rule in policy["spec"].get("egress", []) if "toFQDNs" in rule]
        self.assertEqual(len(fqdn_rules), 1)
        policy, rule = fqdn_rules[0]
        self.assertEqual(rule["toFQDNs"], [{"matchName": "sandbox.plaid.com"}])
        self.assertEqual(rule["toPorts"], [{"ports": [{"port": "443", "protocol": "TCP"}]}])
        selector = policy["spec"]["endpointSelector"]
        for app in CLIENTS | {"unrelated"}:
            with self.subTest(app=app):
                self.assertEqual(selected(selector, app), app in {"groschen-api", "groschen-worker"})

    def test_dns_proxy_is_scoped_to_coredns_for_all_clients(self):
        dns_rules = [(policy, rule) for policy in self.cnps
                     for rule in policy["spec"].get("egress", []) if "toEndpoints" in rule]
        self.assertEqual(len(dns_rules), 1)
        policy, rule = dns_rules[0]
        self.assertEqual(rule["toEndpoints"], [{"matchLabels": {
            "io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}])
        self.assertEqual({(port["port"], port["protocol"])
                          for port in rule["toPorts"][0]["ports"]},
                         {("53", "TCP"), ("53", "UDP")})
        self.assertEqual(rule["toPorts"][0]["rules"], {"dns": [{"matchPattern": "*"}]})
        for app in CLIENTS | {"unrelated"}:
            with self.subTest(app=app):
                self.assertEqual(selected(policy["spec"]["endpointSelector"], app), app in CLIENTS)

    def test_no_parallel_dns_bypass_or_broad_egress(self):
        self.assertEqual({p["metadata"]["name"] for p in self.cnps},
                         {"groschen-client-dns", "groschen-plaid-sandbox"})
        self.assertEqual(sum(len(p["spec"].get("egress", [])) for p in self.cnps), 2)
        for policy in self.cnps:
            for rule in policy["spec"].get("egress", []):
                self.assertFalse({"toEntities", "toCIDR", "toCIDRSet", "toServices"} & rule.keys())
        assert_network_policy_boundaries(self, self.nps)

    def test_rejects_broad_api_egress_mutation(self):
        policies = copy.deepcopy(self.nps)
        api = next(p for p in policies if p["metadata"]["name"] == "groschen-api")
        api["spec"]["policyTypes"].append("Egress")
        api["spec"]["egress"] = [{
            "to": [{"ipBlock": {"cidr": "0.0.0.0/0"}}],
            "ports": [{"protocol": "TCP", "port": 443}],
        }]
        with self.assertRaises(AssertionError):
            assert_network_policy_boundaries(self, policies)

    def test_rejects_extra_api_ingress_source_mutation(self):
        policies = copy.deepcopy(self.nps)
        api = next(p for p in policies if p["metadata"]["name"] == "groschen-api")
        api["spec"]["ingress"][0]["from"].append({"namespaceSelector": {}})
        with self.assertRaises(AssertionError):
            assert_network_policy_boundaries(self, policies)


if __name__ == "__main__":
    unittest.main()
