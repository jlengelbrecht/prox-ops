"""Offline boundaries for Nyctr's model credentials and registration egress."""
import copy
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/security/nyctr/app'
HR = yaml.safe_load((APP / 'helmrelease.yaml').read_text())
VALUES = HR['spec']['values']
ARCHIVE = os.environ.get('NYCTR_CHART_ARCHIVE')
HELM = os.environ.get('HELM', 'helm')


def model_boundary(test, values, secrets):
    assistant = values['assistant']
    test.assertEqual(assistant, {
        'hosts': ['https://ai-api.homelab0.org'],
        'secrets': [{'envName': 'NYCTR_MODEL_SECRET_LITELLM',
                     'existingSecret': 'nyctr-model', 'key': 'api-key'}]})
    model = next(s for s in secrets if s['metadata']['name'] == 'nyctr-model')['spec']
    test.assertEqual(model, {
        'refreshInterval': '1h',
        'secretStoreRef': {'kind': 'ClusterSecretStore', 'name': 'onepassword-connect'},
        'target': {'name': 'nyctr-model', 'creationPolicy': 'Owner'},
        'data': [{'secretKey': 'api-key',
                  'remoteRef': {'key': 'nyctr-model', 'property': 'api-key'}}]})
    refs = {v['existingSecret'] for v in values['connectors']['secrets']}
    refs.update((values['database']['existingSecret'],
                 values['auth']['oidc']['existingSecret']))
    test.assertNotIn('nyctr-model', refs)


def policy_boundary(test, policy):
    spec = policy['spec']
    test.assertEqual(set(spec), {'endpointSelector', 'egress'})
    test.assertEqual(spec['endpointSelector']['matchLabels'], {
        'app.kubernetes.io/name': 'nyctr', 'app.kubernetes.io/instance': 'nyctr'})
    test.assertEqual(len(spec['egress']), 5)
    for rule in spec['egress']:
        destinations = set(rule) - {'toPorts'}
        test.assertIn(destinations, ({'toEndpoints'}, {'toFQDNs'}))
        if 'toFQDNs' in rule:
            test.assertTrue(all(set(f) == {'matchName'} for f in rule['toFQDNs']))
            test.assertTrue(all('*' not in f['matchName'] for f in rule['toFQDNs']))
            test.assertEqual(rule['toPorts'], [{'ports': [{'port': '443', 'protocol': 'TCP'}]}])


def permits(policy, *, address, port, protocol='TCP', labels=None, dns=None):
    """Evaluate our bounded L3/L4 subset with fabricated DNS/pod metadata.

    This models additive IP permissions, including shared IPs, not a dataplane
    test or a TLS hostname filter. policy_boundary rejects unsupported shapes.
    """
    for rule in policy['spec']['egress']:
        endpoint = any(all((labels or {}).get(k) == v for k, v in s['matchLabels'].items())
                       for s in rule.get('toEndpoints', []))
        fqdn = any(address in (dns or {}).get(s['matchName'], [])
                   for s in rule.get('toFQDNs', []))
        ports = [p for group in rule['toPorts'] for p in group['ports']]
        if (endpoint or fqdn) and {'port': str(port), 'protocol': protocol} in ports:
            return True
    return False


class SelfServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        built = subprocess.check_output(['kubectl', 'kustomize', str(APP)], text=True, timeout=30)
        cls.docs = list(yaml.safe_load_all(built))
        cls.policy = next(d for d in cls.docs if d['kind'] == 'CiliumNetworkPolicy')
        cls.secrets = [d for d in cls.docs if d['kind'] == 'ExternalSecret']

    def test_model_and_provider_isolation(self):
        model_boundary(self, VALUES, self.secrets)
        self.assertEqual(VALUES['providerWrites'], ['nextdns'])
        self.assertEqual({d['metadata']['name'] for d in self.secrets},
                         {'nyctr-db', 'nyctr-oidc', 'nyctr-connectors', 'nyctr-model'})
        self.assertFalse(any(d['kind'] == 'Secret' for d in self.docs))

    def test_reject_connector_key_or_gateway_master_key_reuse(self):
        values = copy.deepcopy(VALUES)
        values['assistant']['secrets'][0]['existingSecret'] = 'nyctr-connectors'
        with self.assertRaises(AssertionError):
            model_boundary(self, values, self.secrets)
        secrets = copy.deepcopy(self.secrets)
        model = next(s for s in secrets if s['metadata']['name'] == 'nyctr-model')
        model['spec']['data'][0]['remoteRef'] = {'key': 'litellm', 'property': 'master-key'}
        with self.assertRaises(AssertionError):
            model_boundary(self, VALUES, secrets)

    def test_exact_reviewed_registry_snapshot(self):
        self.assertEqual([d['metadata']['name'] for d in self.docs
                          if d['kind'] in {'CiliumNetworkPolicy', 'NetworkPolicy',
                                           'CiliumClusterwideNetworkPolicy'}], ['nyctr-egress'])
        policy_boundary(self, self.policy)
        rules = self.policy['spec']['egress']
        self.assertEqual(rules[3]['toFQDNs'],
                         [{'matchName': 'api.nextdns.io'}, {'matchName': 'api.ui.com'}])
        names = [f['matchName'] for f in rules[4]['toFQDNs']]
        self.assertEqual(names[0], 'data.iana.org')
        self.assertEqual(len(names), 378)
        self.assertEqual(names[1:], sorted(set(names[1:])))
        self.assertEqual(hashlib.sha256(('\n'.join(names[1:]) + '\n').encode()).hexdigest(),
                         '82cc878ed6636176f17b78a4fe2d4c32a990edbd88d1aed9f85a2c9d848085b1')

    def test_registry_and_source_paths_and_denials(self):
        dns = {name: [f'192.0.2.{i}'] for i, name in enumerate((
            'data.iana.org', 'rdap.verisign.com', 'rdap.publicinterestregistry.org',
            'api.nextdns.io', 'api.ui.com'), 1)}
        for name, addresses in dns.items():
            with self.subTest(name=name):
                self.assertTrue(permits(self.policy, address=addresses[0], port=443, dns=dns))
                for port in (80, 4000, 8443):
                    self.assertFalse(permits(self.policy, address=addresses[0], port=port, dns=dns))
                self.assertFalse(permits(self.policy, address=addresses[0], port=443,
                                         protocol='UDP', dns=dns))
        for address in ('192.0.2.99', '169.254.169.254', '127.0.0.1', '10.0.0.1'):
            self.assertFalse(permits(self.policy, address=address, port=443, dns=dns))
        # The name alone grants nothing before a DNS answer; shared IPs are
        # admitted regardless of an unrelated TLS name. Document that limit.
        self.assertFalse(permits(self.policy, address='192.0.2.2', port=443))
        dns['unrelated.example'] = dns['rdap.verisign.com']
        self.assertTrue(permits(self.policy, address=dns['unrelated.example'][0], port=443, dns=dns))

    def test_internal_gateway_database_and_dns_boundaries(self):
        fixtures = [({'io.kubernetes.pod.namespace': 'network',
                      'gateway.envoyproxy.io/owning-gateway-name': 'envoy-internal'}, [443, 10443]),
                    ({'io.kubernetes.pod.namespace': 'database', 'cnpg.io/cluster': 'postgres'}, [5432]),
                    ({'io.kubernetes.pod.namespace': 'kube-system', 'k8s-app': 'kube-dns'}, [53])]
        for labels, ports in fixtures:
            for port in (53, 443, 5432, 10443, 4000):
                self.assertEqual(permits(self.policy, address='192.0.2.50', port=port, labels=labels),
                                 port in ports)
            other_namespace = dict(labels, **{'io.kubernetes.pod.namespace': 'other'})
            self.assertFalse(permits(self.policy, address='192.0.2.50', port=ports[0], labels=other_namespace))
        dns_rule = self.policy['spec']['egress'][0]
        self.assertEqual(dns_rule['toPorts'][0]['rules'], {'dns': [{'matchPattern': '*'}]})
        self.assertTrue(permits(self.policy, address='192.0.2.50', port=53,
                                protocol='UDP', labels=fixtures[2][0]))
        for labels in ({'io.kubernetes.pod.namespace': 'network',
                        'gateway.envoyproxy.io/owning-gateway-name': 'envoy-external'},
                       {'io.kubernetes.pod.namespace': 'ai', 'app.kubernetes.io/name': 'litellm'}):
            for port in (443, 10443, 4000):
                self.assertFalse(permits(self.policy, address='192.0.2.50', port=port, labels=labels))

    def test_reject_world_cidr_and_fqdn_wildcard_mutations(self):
        for key, value in [('toEntities', ['world']), ('toCIDR', ['0.0.0.0/0']),
                           ('toFQDNs', [{'matchPattern': '*'}])]:
            policy = copy.deepcopy(self.policy)
            policy['spec']['egress'][-1] = {
                key: value, 'toPorts': [{'ports': [{'port': '443', 'protocol': 'TCP'}]}]}
            with self.subTest(key=key), self.assertRaises(AssertionError):
                policy_boundary(self, policy)


@unittest.skipUnless(ARCHIVE, 'Set NYCTR_CHART_ARCHIVE to verify the published chart')
class PublishedChartTests(unittest.TestCase):
    def test_schema_lint_render_postrender_and_secret_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = root / 'values.yaml'
            values.write_text(yaml.safe_dump(VALUES))
            subprocess.run([HELM, 'lint', ARCHIVE, '-f', str(values), '--strict'], check=True,
                           capture_output=True, text=True, timeout=30)
            rendered = subprocess.check_output(
                [HELM, 'template', 'nyctr', ARCHIVE, '-n', 'security', '-f', str(values)],
                text=True, timeout=30)
            (root / 'resources.yaml').write_text(rendered)
            (root / 'kustomization.yaml').write_text(yaml.safe_dump({
                'resources': ['resources.yaml'],
                'patches': HR['spec']['postRenderers'][0]['kustomize']['patches']}))
            built = subprocess.check_output(['kubectl', 'kustomize', directory], text=True, timeout=30)
        docs = list(yaml.safe_load_all(built))
        pod = next(d for d in docs if d['kind'] == 'Deployment')['spec']['template']['spec']
        env = {e['name']: e for e in pod['containers'][0]['env']}
        self.assertEqual(len(env), len(pod['containers'][0]['env']))
        self.assertEqual(env['NYCTR_MODEL_SECRET_LITELLM'], {
            'name': 'NYCTR_MODEL_SECRET_LITELLM',
            'valueFrom': {'secretKeyRef': {'name': 'nyctr-model', 'key': 'api-key'}}})
        self.assertEqual(env['NYCTR_MODEL_HOSTS']['value'], 'https://ai-api.homelab0.org')
        self.assertEqual(env['NYCTR_PROVIDER_WRITES']['value'], 'nextdns')
        self.assertFalse({'NYCTR_AI_BASE_URL', 'NYCTR_AI_MODEL', 'NYCTR_AI_API_KEY'} & env.keys())
        self.assertEqual(pod['dnsConfig'], {'options': [{'name': 'ndots', 'value': '1'}]})
        self.assertEqual(pod['containers'][0]['image'], VALUES['image']['repository'] + '@' + VALUES['image']['digest'])
        self.assertFalse(any(d['kind'] == 'Secret' for d in docs))
        network = next(d for d in docs if d['kind'] == 'NetworkPolicy')['spec']
        self.assertEqual(network['policyTypes'], ['Ingress'])
        self.assertEqual(network['ingress'][0]['from'], VALUES['networkPolicy']['ingressFrom'])


if __name__ == '__main__':
    unittest.main()
