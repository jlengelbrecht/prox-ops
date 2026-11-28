import os
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/mcp/onepassword-connect-ai-access/app'


class FilterContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release = yaml.safe_load((APP / 'helmrelease.yaml').read_text())
        cls.config = yaml.safe_load((APP / 'envoy-config.yaml').read_text())

    def test_filter_is_fail_closed(self):
        hcm = self.config['data']['envoy.yaml']
        envoy = yaml.safe_load(hcm)
        listener = envoy['static_resources']['listeners'][0]
        self.assertEqual(listener['address']['socket_address']['port_value'], 8443)
        manager = listener['filter_chains'][0]['filters'][0]['typed_config']
        self.assertTrue(manager['normalize_path'])
        self.assertEqual(manager['path_with_escaped_slashes_action'], 'REJECT_REQUEST')
        routes = manager['route_config']['virtual_hosts'][0]['routes']
        self.assertEqual(routes[-1]['direct_response']['status'], 403)
        allowed = [(r['match'].get('path') or r['match'].get('safe_regex', {}).get('regex'),
                    r['match']['headers']) for r in routes[:-1]]
        for route in routes[:-1]:
            self.assertEqual(route['route'], {'cluster': 'connect'})
            self.assertEqual(route['match']['headers'][0]['name'], ':method')
            self.assertEqual(len(route['match']['headers']), 1)
        allowed = [(path, headers[0]['string_match']['exact']) for path, headers in allowed]
        self.assertEqual(allowed, [('/v1/vaults', 'GET'),
                                   ('/v1/vaults/[a-z0-9]{26}/items', 'GET'),
                                   ('/v1/vaults/[a-z0-9]{26}/items', 'POST'),
                                   ('/v1/vaults/[a-z0-9]{26}/items/[a-z0-9]{26}', 'GET')])
        log = manager['access_log'][0]['typed_config']['log_format']['json_format']
        self.assertEqual(log, {'operation': '%ROUTE_NAME%', 'status': '%RESPONSE_CODE%',
                               'duration_ms': '%DURATION%'})
        removed = manager['route_config']['virtual_hosts'][0]['request_headers_to_remove']
        self.assertIn('x-http-method-override', removed)
        self.assertIn('x-original-url', removed)
        self.assertIn('x-envoy-original-path', removed)
        for method, path in [('DELETE', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26),
                             ('PUT', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26),
                             ('PATCH', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26),
                             ('POST', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26),
                             ('GET', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26 + '/extra'),
                             ('GET', '/v1/vaults/' + 'a'*26 + '/items/' + 'b'*26 + '/')]:
            self.assertFalse(any((match == method and
                                  (route_path == path if route_path == '/v1/vaults'
                                   else re.fullmatch(route_path, path)))
                                 for route_path, match in allowed))

    def test_chart_ports_and_rollout(self):
        path = pathlib.Path(os.environ['FILTER_RENDERED_HELM'])
        resources = {r['kind']: r for r in yaml.safe_load_all(path.read_text())
                     if r and r['metadata']['name'] == 'onepassword-connect'}
        deployment = resources['Deployment']['spec']
        pod = deployment['template']
        service = resources['Service']['spec']
        connects = {c['name']: c for c in pod['spec']['containers']}
        self.assertEqual(deployment['strategy']['type'], 'Recreate')
        self.assertEqual(service['selector'], {'app': 'onepassword-connect'})
        ports = {port['name']: port for port in service['ports']}
        self.assertEqual(ports['connect-api-https']['port'], 8443)
        self.assertEqual(ports['connect-api-https']['targetPort'], 8443)
        self.assertEqual(connects['envoy']['ports'][0]['containerPort'], 8443)
        self.assertEqual(connects['envoy']['readinessProbe']['tcpSocket']['port'], 'filter-https')
        self.assertEqual(connects['connect-api']['readinessProbe']['httpGet']['port'], 9443)
        self.assertEqual(
            next(env['value'] for env in connects['connect-api']['env'] if env['name'] == 'OP_HTTPS_PORT'),
            '9443',
        )
        policy = yaml.safe_load((APP / 'networkpolicy.yaml').read_text())
        for key, value in policy['spec']['endpointSelector']['matchLabels'].items():
            self.assertEqual(pod['metadata']['labels'].get(key), value)

    def test_network_only_allows_filter(self):
        policy = yaml.safe_load((APP / 'networkpolicy.yaml').read_text())
        ports = [p['port'] for rule in policy['spec']['ingress']
                 for group in rule['toPorts'] for p in group['ports']]
        self.assertEqual(ports, ['8443'])

    def test_open_connector_proxy_requires_runtime_auth(self):
        connector = ROOT / 'kubernetes/apps/mcp/open-connector/app'
        deployment = yaml.safe_load((connector / 'deployment.yaml').read_text())
        container = deployment['spec']['template']['spec']['containers'][0]
        env = {entry['name']: entry.get('value') for entry in container['env']}
        self.assertNotIn('envFrom', container)
        self.assertNotIn('OOMOL_CONNECT_RUNTIME_TOKEN', env)
        self.assertEqual(env['OOMOL_CONNECT_ALLOWED_PROXIES'], 'one_password')
        self.assertEqual(env['OOMOL_CONNECT_OAUTH_ENABLED'], 'true')
        self.assertEqual(env['OOMOL_CONNECT_OAUTH_AUTH_MODE'], 'mixed')
        self.assertEqual(env['OOMOL_CONNECT_OAUTH_ALLOW_DCR'], 'false')
        self.assertEqual(env['OOMOL_CONNECT_OAUTH_ALLOW_CIMD'], 'false')
        route = yaml.safe_load((connector / 'httproute-ai-access.yaml').read_text())
        self.assertEqual(route['spec']['rules'][0]['matches'], [
            {'method': 'POST', 'path': {'type': 'Exact', 'value': '/v1/proxy/one_password'}},
        ])


if __name__ == '__main__':
    unittest.main()
