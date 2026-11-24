"""Safety and exposure contract for the netboot.xyz PXE server manifests."""

import copy
import pathlib
import re
import struct
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/network/netbootxyz'
IMAGE = r'^ghcr\.io/netbootxyz/netbootxyz:\d+\.\d+\.\d+-nbxyz\d+@sha256:[0-9a-f]{64}$'
SCHEMATIC = '536b92328799fed89ad41d8ecb85ed3de23ab99ba7eded5ab6b6ef1197dc6b17'
CAPS = {'CHOWN', 'DAC_OVERRIDE', 'FOWNER', 'KILL', 'NET_BIND_SERVICE', 'SETGID', 'SETUID'}


def documents():
    docs = []
    for name in yaml.safe_load((APP / 'app/kustomization.yaml').read_text())['resources']:
        docs += [d for d in yaml.safe_load_all((APP / 'app' / name).read_text()) if d]
    return docs


GATEWAY = {'io.kubernetes.pod.namespace': 'network',
           'gateway.envoyproxy.io/owning-gateway-name': 'envoy-internal'}


def policy_problems(policy):
    """Boot ports open to LAN and cluster; editor port 3000 only from envoy-internal."""
    problems, boot, editor, boot_open, editor_routed = [], {('69', 'UDP'), ('80', 'TCP')}, \
        {('3000', 'TCP')}, False, False
    for rule in policy.get('ingress', []):
        # L7 rules, TLS or SNI on a toPorts entry filter traffic; flattening would hide them.
        problems += [f'toPorts entry with more than ports: {to}'
                     for to in rule.get('toPorts', []) if set(to) - {'ports'}]
        entries = [p for to in rule.get('toPorts', []) for p in to.get('ports', [])]
        # A range (endPort) or any other key can open more than (port, protocol) says.
        problems += [f'port entry other than port/protocol: {p}'
                     for p in entries if set(p) - {'port', 'protocol'}]
        ports = {(p['port'], p.get('protocol', 'ANY')) for p in entries}
        sources = {k: v for k, v in rule.items() if k != 'toPorts'}
        if not ports:
            problems.append(f'ingress without a port restriction from {sources}')
        elif ports <= boot:
            boot_open |= ports == boot and set(sources.get('fromEntities', [])) >= {'world', 'cluster'}
        elif ports == editor and sources == {'fromEndpoints': [{'matchLabels': GATEWAY}]}:
            editor_routed = True
        else:
            problems.append(f'unexpected ingress to {sorted(ports)} from {sources}')
    if not boot_open:
        problems.append('PXE clients must reach TFTP 69 and HTTP 80 without login')
    if not editor_routed:
        problems.append('the internal gateway must reach the editor on 3000')
    return problems


def nosniff_problems(route):
    """Every rule that reaches a backend must set X-Content-Type-Options: nosniff."""
    problems = []
    for index, rule in enumerate(route['spec'].get('rules', [])):
        if not rule.get('backendRefs'):
            continue
        headers = [header for flt in rule.get('filters', [])
                   if flt.get('type') == 'ResponseHeaderModifier'
                   for header in flt.get('responseHeaderModifier', {}).get('set', [])]
        if {'name': 'X-Content-Type-Options', 'value': 'nosniff'} not in headers:
            problems.append(f'rule {index} must set X-Content-Type-Options: nosniff')
    return problems


HOST = 'netboot.homelab0.org'
DENY = 'deny-authorization'
DENY_FILTER = [{'type': 'ExtensionRef', 'extensionRef': {
    'group': 'gateway.envoyproxy.io', 'kind': 'HTTPRouteFilter', 'name': 'netbootxyz-deny-authorization'}}]
PARENT = [{'name': 'envoy-internal', 'namespace': 'network', 'sectionName': 'https'}]
AUTHENTIK = [{'name': 'authentik-server', 'namespace': 'security', 'port': 80}]
# The only backend each route may reach. The outpost route has no forward auth
# (it would loop), so it must never point at the editor.
BACKENDS = {'netbootxyz': [{'name': 'netbootxyz-admin', 'port': 3000}], 'netbootxyz-outpost': AUTHENTIK}
TARGET = {'group': 'gateway.networking.k8s.io', 'kind': 'HTTPRoute', 'name': 'netbootxyz'}
CHECK_PATH = '/outpost.goauthentik.io/auth/envoy'
ROUTE_FIELDS = {'parentRefs', 'hostnames', 'rules'}
RULE_FIELDS = {'name', 'matches', 'filters', 'backendRefs'}


def prefix(value, *headers):
    match = {'path': {'type': 'PathPrefix', 'value': value}}
    return {**match, 'headers': list(headers)} if headers else match


# Security-sensitive header guards, pinned to the exact values the gateway gets.
# .+ means any non-empty Authorization value, with no length cap. (?i)websocket
# catches every casing of the Upgrade value. Both are RE2 (Go regexp) syntax
# that Envoy Gateway accepts.
AUTHORIZATION = {'name': 'Authorization', 'type': 'RegularExpression', 'value': '.+'}
SAME_ORIGIN = {'name': 'Origin', 'type': 'Exact', 'value': f'https://{HOST}'}
UPGRADE = {'name': 'Upgrade', 'type': 'RegularExpression', 'value': '(?i)websocket'}

# Every rule of both routes, in order, with its exact matches. This is the
# whole routing contract; nothing here re-implements Envoy matching.
#
# The order is right for Envoy Gateway 1.9 (internal/gatewayapi/sort.go: path
# type, then prefix length with '/' as 0, then header count, then Exact header
# count, then list order). With Authorization set, every path reaches a 403
# match before any backend: /outpost.goauthentik.io/ by its deny, /socket.io/
# with the exact Origin by the two-header deny (beats socket-io's one), any
# other /socket.io/ by the one-header deny (ties websocket-deny, listed first),
# and everything else by the / deny (beats the headerless editor). An offline
# egctl v1.9.3 translation of these manifests produced exactly that route
# order. Changing any entry here needs that check again, not only a test edit.
ROUTE_MATCHES = {
    'netbootxyz': [
        (DENY, [prefix('/socket.io/', AUTHORIZATION, SAME_ORIGIN),
                prefix('/socket.io/', AUTHORIZATION),
                prefix('/', AUTHORIZATION)]),
        ('socket-io', [prefix('/socket.io/', SAME_ORIGIN)]),
        ('websocket-deny', [prefix('/socket.io/', UPGRADE)]),
        ('editor', [prefix('/')]),
    ],
    'netbootxyz-outpost': [
        (DENY, [prefix('/outpost.goauthentik.io/', AUTHORIZATION)]),
        ('outpost', [prefix('/outpost.goauthentik.io/')]),
    ],
}


def rule_problems(route_name, rule):
    """A rule is a 403 deny, the backend-less websocket-deny, or a rule to this route's one backend."""
    if not isinstance(rule, dict):
        return [f'{route_name}: malformed rule {rule}']
    name, where, problems = rule.get('name'), f'{route_name}/{rule.get("name")}', []
    if set(rule) - RULE_FIELDS:
        problems.append(f'{where}: unsupported rule fields {sorted(set(rule) - RULE_FIELDS)}')
    if name == DENY:
        if 'backendRefs' in rule or rule.get('filters') != DENY_FILTER:
            problems.append(f'{route_name}: {DENY} must only return the 403 filter')
    elif name == 'websocket-deny':
        # Checked here, not through policy targets or ROUTE_MATCHES: a backend on this
        # rule would proxy a foreign-Origin upgrade with the user's session cookie.
        if route_name != 'netbootxyz' or set(rule) != {'name', 'matches'}:
            problems.append(f'{where}: must have no backend and no filters (the gateway answers 500)')
    else:
        if rule.get('backendRefs') != BACKENDS.get(route_name):
            problems.append(f'{where}: backend must be exactly {BACKENDS.get(route_name)}, '
                            f'got {rule.get("backendRefs")}')
        if any(not isinstance(f, dict) or f.get('type') != 'ResponseHeaderModifier'
               for f in rule.get('filters', [])):
            problems.append(f'{where}: backend rules may only set response headers')
    return problems


def route_problems(routes):
    """Both routes, on the internal HTTPS listener and this hostname only, with exactly ROUTE_MATCHES."""
    names = [route.get('metadata', {}).get('name') for route in routes]
    problems = [] if sorted(map(str, names)) == sorted(BACKENDS) else [
        f'HTTPRoutes must be exactly {sorted(BACKENDS)}, got {names}']
    for name, route in zip(names, routes):
        spec = route.get('spec') or {}
        if spec.get('parentRefs') != PARENT or spec.get('hostnames') != [HOST]:
            # On another host or listener the two routes' rules would no longer
            # be ranked together, so the order in ROUTE_MATCHES would not hold.
            problems.append(f'{name}: must attach only to {PARENT} for {HOST}, got '
                            f'{spec.get("parentRefs")} {spec.get("hostnames")}')
        if set(spec) - ROUTE_FIELDS:
            problems.append(f'{name}: unsupported route fields {sorted(set(spec) - ROUTE_FIELDS)}')
        rules = spec.get('rules')
        if not isinstance(rules, list) or not rules:
            problems.append(f'{name}: needs rules')
            continue
        got = [(rule.get('name'), rule.get('matches')) if isinstance(rule, dict) else (None, rule)
               for rule in rules]
        want = ROUTE_MATCHES.get(name, [])
        if [n for n, _ in got] != [n for n, _ in want]:
            problems.append(f'{name}: rules must be exactly {[n for n, _ in want]} in this order, '
                            f'got {[n for n, _ in got]}')
        # Exact equality: a method, queryParams, an extra deny-only prefix, a
        # length-capped or rewritten regex, or a missing match all fail here.
        expected = dict(want)
        problems += [f'{name}/{n}: matches must be exactly {expected[n]}, got {m}'
                     for n, m in got if isinstance(n, str) and n in expected and m != expected[n]]
        for rule in rules:
            problems += rule_problems(name, rule)
    return problems


def auth_policy_problems(docs, backend_rules):
    """One SecurityPolicy: Authentik ext_authz, fail-closed, on exactly the UI rules with a backend."""
    policies = [d for d in docs if d['kind'] == 'SecurityPolicy']
    if len(policies) != 1:
        return [f'exactly one SecurityPolicy must own forward auth, found {len(policies)}']
    spec, problems = policies[0].get('spec') or {}, []
    if set(spec) != {'targetRefs', 'extAuth'}:
        problems.append(f'the SecurityPolicy must set only targetRefs and extAuth, got {sorted(spec)}')
    refs = spec.get('targetRefs') if isinstance(spec.get('targetRefs'), list) else []
    if any(not isinstance(ref, dict) or 'sectionName' not in ref
           or {k: v for k, v in ref.items() if k != 'sectionName'} != TARGET for ref in refs):
        # A whole-route target would run forward auth ahead of the 403 rule.
        problems.append('forward auth must target UI rules by sectionName only')
    sections = [ref.get('sectionName') for ref in refs if isinstance(ref, dict)]
    if (None in backend_rules or set(sections) != set(backend_rules)
            or len(set(sections)) != len(sections) or len(sections) != len(backend_rules)):
        problems.append(f'forward auth targets {sections}, rules with a backend are {backend_rules}')
    ext = spec.get('extAuth') if isinstance(spec.get('extAuth'), dict) else {}
    http = ext.get('http') if isinstance(ext.get('http'), dict) else {}
    if 'grpc' in ext or http.get('backendRefs') != AUTHENTIK or http.get('path') != CHECK_PATH:
        # A target without this would attach a policy that authenticates nothing.
        problems.append(f'extAuth must call {AUTHENTIK} at {CHECK_PATH}')
    # Envoy Gateway's HTTP ext_authz does not forward Cookie by default, and the
    # Authentik session lives in a cookie: without it no one can sign in.
    to_ext = ext.get('headersToExtAuth')
    if not isinstance(to_ext, list) or 'cookie' not in to_ext:
        problems.append('extAuth.headersToExtAuth must include cookie (the Authentik session)')
    if ext.get('failOpen', False) is not False:
        problems.append('extAuth must fail closed (failOpen false or omitted)')
    return problems


def authorization_problems(docs):
    """Requests carrying Authorization get the 403 rule; forward auth covers every backend rule.

    Every problem is a finding, never an exception, so an extra, missing or
    malformed route or rule is reported by name.
    """
    routes = [d for d in docs if d['kind'] == 'HTTPRoute']
    problems = route_problems(routes)
    filters = [d for d in docs if d['kind'] == 'HTTPRouteFilter']
    if [(f['metadata']['name'], f['spec'].get('directResponse', {}).get('statusCode')) for f in filters] \
            != [('netbootxyz-deny-authorization', 403)]:
        problems.append('the deny filter must be a 403 direct response')
    ui = next((r for r in routes if r.get('metadata', {}).get('name') == 'netbootxyz'), {})
    rules = (ui.get('spec') or {}).get('rules')
    backend_rules = [rule.get('name') for rule in (rules if isinstance(rules, list) else [])
                     if isinstance(rule, dict) and rule.get('backendRefs')]
    return problems + auth_policy_problems(docs, backend_rules)


def topf_talos_version():
    return re.search(r'^talosVersion:\s*v(\S+)$', (ROOT / 'talos/topf.yaml').read_text(),
                     re.M).group(1)


class ManifestContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.docs = documents()
        cls.by_kind = {}
        for doc in cls.docs:
            cls.by_kind.setdefault(doc['kind'], []).append(doc)
        cls.deployment = cls.by_kind['Deployment'][0]
        cls.pod = cls.deployment['spec']['template']['spec']
        cls.container = cls.pod['containers'][0]
        cls.env = {e['name']: e['value'] for e in cls.container['env']}
        cls.data = cls.by_kind['ConfigMap'][0]['data']

    def test_flux_leaves_ipxe_variables_alone(self):
        # flate renders the enrolment; this checks iPXE ${...} reaches the pod unchanged.
        self.assertNotIn('postBuild', yaml.safe_load((APP / 'ks.yaml').read_text())['spec'])
        self.assertEqual(self.by_kind['ConfigMap'][0]['metadata']['annotations']
                         ['kustomize.toolkit.fluxcd.io/substitute'], 'disabled')

    def test_only_expected_kinds_and_no_secrets(self):
        self.assertEqual(sorted(self.by_kind), ['BackendTrafficPolicy', 'CiliumNetworkPolicy',
                                                'ConfigMap', 'Deployment', 'HTTPRoute',
                                                'HTTPRouteFilter', 'PersistentVolumeClaim',
                                                'SecurityPolicy', 'Service'])
        text = '\n'.join(p.read_text() for p in APP.rglob('*.yaml'))
        for marker in ('talos.config', 'PRIVATE KEY', 'secretKeyRef', 'secretRef', 'machine:'):
            self.assertNotIn(marker, text)
        self.assertNotRegex(text, r'(?i)\b(password|passwd|token|secret)\b\s*[:=]')

    def test_no_public_exposure(self):
        admin, lb = sorted(self.by_kind['Service'], key=lambda s: s['spec']['type'])
        self.assertEqual(lb['spec']['type'], 'LoadBalancer')
        self.assertEqual(lb['spec']['externalTrafficPolicy'], 'Cluster')
        self.assertEqual(lb['metadata']['annotations']['lbipam.cilium.io/ips'], '10.20.67.28')
        self.assertNotIn('network', lb['metadata'].get('labels', {}))
        self.assertEqual({(p['port'], p['protocol']) for p in lb['spec']['ports']},
                         {(69, 'UDP'), (8080, 'TCP')})
        self.assertEqual(admin['spec']['type'], 'ClusterIP')
        self.assertEqual([p['port'] for p in admin['spec']['ports']], [3000])

    def test_editor_rules_set_nosniff(self):
        """nosniff stops a cross-site <script> running the text/plain JSONP polling reply."""
        route = next(r for r in self.by_kind['HTTPRoute'] if r['metadata']['name'] == 'netbootxyz')
        self.assertEqual(nosniff_problems(route), [])
        # Negative control: dropping the header from the catch-all rule must fail.
        stripped = copy.deepcopy(route)
        modifier = stripped['spec']['rules'][-1]['filters'][0]['responseHeaderModifier']
        modifier['set'] = [h for h in modifier['set'] if h['name'] != 'X-Content-Type-Options']
        self.assertTrue(nosniff_problems(stripped))

    def test_authorization_header_never_reaches_forward_auth(self):
        """The outpost accepts Basic/Bearer whatever intercept_header_auth says (authentik
        2026.8.5 internal/outpost/proxyv2/application/auth.go checkAuth), so the gateway
        answers 403 first, and forward auth is attached only to rules with a backend."""
        self.assertEqual(authorization_problems(self.docs), [])

    def test_authorization_guard_weakening_is_rejected(self):
        def ui(docs):
            return next(d for d in docs if d['kind'] == 'HTTPRoute'
                        and d['metadata']['name'] == 'netbootxyz')['spec']['rules']

        def outpost(docs):
            return next(d for d in docs if d['kind'] == 'HTTPRoute'
                        and d['metadata']['name'] == 'netbootxyz-outpost')['spec']['rules']

        def policy(docs):
            return next(d for d in docs if d['kind'] == 'SecurityPolicy')['spec']

        def origin_match_dropped(docs):  # Origin rule (Exact) would outrank a regex-only deny
            del ui(docs)[0]['matches'][0]

        def socket_match_dropped(docs):
            del ui(docs)[0]['matches'][1]

        def root_match_dropped(docs):
            del ui(docs)[0]['matches'][2]

        def deny_after_websocket_deny(docs):
            rules = ui(docs)
            rules.insert(2, rules.pop(0))

        def empty_value_matches(docs):
            for m in ui(docs)[0]['matches']:
                m['headers'][0]['value'] = 'Basic .*'

        def outpost_deny_removed(docs):
            del outpost(docs)[0]

        def deny_given_backend(docs):
            ui(docs)[0]['backendRefs'] = [{'name': 'netbootxyz-admin', 'port': 3000}]

        def filter_allows(docs):
            next(d for d in docs if d['kind'] == 'HTTPRouteFilter')['spec']['directResponse'][
                'statusCode'] = 200

        def whole_route_target(docs):
            for ref in policy(docs)['targetRefs']:
                del ref['sectionName']

        def deny_targeted(docs):
            policy(docs)['targetRefs'].append({**policy(docs)['targetRefs'][0], 'sectionName': DENY})

        def editor_untargeted(docs):
            policy(docs)['targetRefs'] = [r for r in policy(docs)['targetRefs']
                                          if r['sectionName'] != 'editor']

        def new_backend_rule_untargeted(docs):
            rule = copy.deepcopy(ui(docs)[-1])
            rule['name'] = 'api'
            rule['matches'] = [{'path': {'type': 'PathPrefix', 'value': '/api/'}}]
            ui(docs).append(rule)

        def unnamed_rule(docs):
            del ui(docs)[1]['name']

        for change in (origin_match_dropped, socket_match_dropped, root_match_dropped,
                       deny_after_websocket_deny, empty_value_matches, outpost_deny_removed,
                       deny_given_backend, filter_allows, whole_route_target, deny_targeted,
                       editor_untargeted, new_backend_rule_untargeted, unnamed_rule):
            with self.subTest(change=change.__name__):
                docs = copy.deepcopy(self.docs)
                change(docs)
                self.assertTrue(authorization_problems(docs))

    def test_route_and_policy_shape_weakening_is_reported(self):
        """Each change below weakens the route or policy; each must fail for its own reason."""
        def route(docs, name):
            return next(d for d in docs if d['kind'] == 'HTTPRoute' and d['metadata']['name'] == name)

        def ui(docs):
            return route(docs, 'netbootxyz')['spec']['rules']

        def policy(docs):
            return next(d for d in docs if d['kind'] == 'SecurityPolicy')['spec']

        def method_limited_deny(docs):  # a POST would skip the deny and reach forward auth
            ui(docs)[0]['matches'][2]['method'] = 'GET'

        def query_limited_deny(docs):
            ui(docs)[0]['matches'][2]['queryParams'] = [{'type': 'Exact', 'name': 'a', 'value': 'b'}]

        def outpost_on_other_host(docs):  # the callback would fall to the editor catch-all
            route(docs, 'netbootxyz-outpost')['spec']['hostnames'] = ['other.homelab0.org']

        def outpost_on_other_listener(docs):
            route(docs, 'netbootxyz-outpost')['spec']['parentRefs'][0]['sectionName'] = 'http'

        def editor_on_external_gateway(docs):
            route(docs, 'netbootxyz')['spec']['parentRefs'][0]['name'] = 'envoy-external'

        def targeted_api_rule(docs):  # targeted correctly, but outranks the / deny match
            rule = copy.deepcopy(ui(docs)[-1])
            rule['name'], rule['matches'] = 'api', [{'path': {'type': 'PathPrefix', 'value': '/api/'}}]
            ui(docs).append(rule)
            policy(docs)['targetRefs'].append({**policy(docs)['targetRefs'][-1], 'sectionName': 'api'})

        def websocket_deny_proxies(docs):  # backend, nosniff and a target: all else stays green
            rule = next(r for r in ui(docs) if r['name'] == 'websocket-deny')
            rule['backendRefs'] = copy.deepcopy(ui(docs)[-1]['backendRefs'])
            rule['filters'] = copy.deepcopy(ui(docs)[-1]['filters'])
            policy(docs)['targetRefs'].append(
                {**policy(docs)['targetRefs'][-1], 'sectionName': 'websocket-deny'})

        def outpost_to_editor(docs):  # the outpost rule has no forward auth
            route(docs, 'netbootxyz-outpost')['spec']['rules'][-1]['backendRefs'] = [
                {'name': 'netbootxyz-admin', 'port': 3000}]

        def ext_auth_removed(docs):
            del policy(docs)['extAuth']

        def ext_auth_other_path(docs):
            policy(docs)['extAuth']['http']['path'] = '/'

        def ext_auth_fail_open(docs):
            policy(docs)['extAuth']['failOpen'] = True

        def rule_without_matches(docs):  # no matches means every path
            del ui(docs)[-1]['matches']

        def outpost_route_missing(docs):
            docs.remove(route(docs, 'netbootxyz-outpost'))

        def extra_route(docs):
            extra = copy.deepcopy(route(docs, 'netbootxyz'))
            extra['metadata']['name'] = 'netbootxyz-extra'
            docs.append(extra)

        def length_limited_deny(docs):  # a longer Authorization value would reach the editor
            ui(docs)[0]['matches'][2]['headers'][0]['value'] = '.{1,32}'

        def outpost_length_limited_deny(docs):
            route(docs, 'netbootxyz-outpost')['spec']['rules'][0]['matches'][0]['headers'][0][
                'value'] = '.{1,32}'

        def upgrade_lookahead(docs):  # Envoy Gateway's Go regexp rejects lookahead
            ui(docs)[2]['matches'][0]['headers'][0]['value'] = '(?i)(?=websocket)websocket'

        def upgrade_case_sensitive(docs):  # WEBSOCKET with a foreign Origin would reach the editor
            ui(docs)[2]['matches'][0]['headers'][0]['value'] = 'websocket'

        def origin_widened(docs):
            ui(docs)[1]['matches'][0]['headers'][0] = {
                'name': 'Origin', 'type': 'RegularExpression', 'value': r'https://.*\.homelab0\.org'}

        def deny_only_child_prefix(docs):  # /menus/custom would always get the 403
            ui(docs)[0]['matches'].append({'path': {'type': 'PathPrefix', 'value': '/menus/custom'}})

        def cookie_not_sent(docs):
            policy(docs)['extAuth']['headersToExtAuth'].remove('cookie')

        def headers_to_ext_auth_removed(docs):  # the gateway default omits Cookie
            del policy(docs)['extAuth']['headersToExtAuth']

        ui_rules = f"netbootxyz: rules must be exactly {[n for n, _ in ROUTE_MATCHES['netbootxyz']]}"
        for change, reason in (
                (method_limited_deny, f'netbootxyz/{DENY}: matches must be exactly'),
                (query_limited_deny, f'netbootxyz/{DENY}: matches must be exactly'),
                (outpost_on_other_host, 'netbootxyz-outpost: must attach only'),
                (outpost_on_other_listener, 'netbootxyz-outpost: must attach only'),
                (editor_on_external_gateway, 'netbootxyz: must attach only'),
                (targeted_api_rule, ui_rules),
                (websocket_deny_proxies, 'netbootxyz/websocket-deny: must have no backend'),
                (outpost_to_editor, 'netbootxyz-outpost/outpost: backend must be exactly'),
                (ext_auth_removed, 'extAuth must call'),
                (ext_auth_other_path, 'extAuth must call'),
                (ext_auth_fail_open, 'extAuth must fail closed'),
                (rule_without_matches, 'netbootxyz/editor: matches must be exactly'),
                (outpost_route_missing, 'HTTPRoutes must be exactly'),
                (extra_route, 'HTTPRoutes must be exactly'),
                (length_limited_deny, f'netbootxyz/{DENY}: matches must be exactly'),
                (outpost_length_limited_deny, f'netbootxyz-outpost/{DENY}: matches must be exactly'),
                (upgrade_lookahead, 'netbootxyz/websocket-deny: matches must be exactly'),
                (upgrade_case_sensitive, 'netbootxyz/websocket-deny: matches must be exactly'),
                (origin_widened, 'netbootxyz/socket-io: matches must be exactly'),
                (deny_only_child_prefix, f'netbootxyz/{DENY}: matches must be exactly'),
                (cookie_not_sent, 'headersToExtAuth must include cookie'),
                (headers_to_ext_auth_removed, 'headersToExtAuth must include cookie')):
            with self.subTest(change=change.__name__):
                docs = copy.deepcopy(self.docs)
                change(docs)
                problems = authorization_problems(docs)
                self.assertTrue(any(reason in p for p in problems), problems)

    def test_fail_closed_default_is_accepted(self):
        docs = copy.deepcopy(self.docs)
        del next(d for d in docs if d['kind'] == 'SecurityPolicy')['spec']['extAuth']['failOpen']
        self.assertEqual(authorization_problems(docs), [])

    def test_policy_admits_boot_ports_openly_and_editor_from_internal_gateway_only(self):
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        self.assertEqual(policy['endpointSelector']['matchLabels'],
                         self.deployment['spec']['selector']['matchLabels'])
        # No egress rules, so DNS and release downloads are not default-denied.
        self.assertEqual(set(policy), {'endpointSelector', 'ingress'})
        self.assertEqual(policy_problems(policy), [])

    def test_policy_rejects_editor_exposure(self):
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        editor = {'port': '3000', 'protocol': 'TCP'}
        for name, rule in [
            ('world', {'fromEntities': ['world'], 'toPorts': [{'ports': [editor]}]}),
            ('cluster', {'fromEntities': ['cluster'], 'toPorts': [{'ports': [editor]}]}),
            ('any pod', {'fromEndpoints': [{}], 'toPorts': [{'ports': [editor]}]}),
            ('external gateway', {'fromEndpoints': [{'matchLabels': {
                **GATEWAY, 'gateway.envoyproxy.io/owning-gateway-name': 'envoy-external'}}],
                'toPorts': [{'ports': [editor]}]}),
            ('all ports', {'fromEndpoints': [{'matchLabels': GATEWAY}]}),
            ('cidr', {'fromCIDR': ['10.20.65.0/24'], 'toPorts': [{'ports': [editor]}]}),
            ('range', {'fromEntities': ['world'], 'toPorts': [{'ports': [
                {'port': '80', 'protocol': 'TCP', 'endPort': 3000}]}]}),
        ]:
            with self.subTest(rule=name):
                mutated = {**policy, 'ingress': policy['ingress'] + [rule]}
                self.assertTrue(policy_problems(mutated))

    def test_policy_check_sees_per_port_restrictions(self):
        """toPorts entries are judged whole, not just by the ports they list."""
        policy = self.by_kind['CiliumNetworkPolicy'][0]['spec']
        editor = {'fromEndpoints': [{'matchLabels': GATEWAY}],
                  'toPorts': [{'ports': [{'port': '3000', 'protocol': 'TCP'}]}]}

        def split(http_rules=None):
            port80 = {'ports': [{'port': '80', 'protocol': 'TCP'}]}
            if http_rules:
                port80['rules'] = {'http': http_rules}
            return {**policy, 'ingress': [editor, {
                'fromEntities': ['world', 'cluster'],
                'toPorts': [{'ports': [{'port': '69', 'protocol': 'UDP'}]}, port80]}]}

        # Boot ports split across entries, with an HTTP path rule on 80: menus would be denied.
        self.assertTrue(policy_problems(split([{'path': '/health'}])))
        # The same split without restrictions is still an open boot rule, editor still routed.
        self.assertEqual(policy_problems(split()), [])
        # An HTTP rule on the editor port would filter routed UI traffic too.
        self.assertTrue(policy_problems({**split(), 'ingress': [
            {**editor, 'toPorts': [{**editor['toPorts'][0], 'rules': {'http': [{'path': '/'}]}}]},
            split()['ingress'][1]]}))

    def test_pinned_versions_and_single_port_tftp(self):
        # Pin shapes, not values, so Renovate bumps pass.
        self.assertRegex(self.container['image'], IMAGE)
        self.assertRegex(self.env['MENU_VERSION'], r'^\d+\.\d+\.\d+$')
        self.assertRegex(self.env['IPXE_SB_VERSION'], r'^v\d+\.\d+\.\d+$')
        self.assertEqual(self.env['TFTPD_OPTS'], '--tftp-single-port')

    def test_pod_has_no_host_access_and_minimal_capabilities(self):
        for key in ('hostNetwork', 'hostPID', 'hostIPC'):
            self.assertFalse(self.pod.get(key, False))
        for volume in self.pod['volumes']:
            self.assertNotIn('hostPath', volume)
        for port in self.container['ports']:
            self.assertNotIn('hostPort', port)
        context = self.container['securityContext']
        self.assertFalse(context['privileged'])
        self.assertFalse(context['allowPrivilegeEscalation'])
        self.assertEqual(context['capabilities']['drop'], ['ALL'])
        self.assertEqual(set(context['capabilities']['add']), CAPS)
        self.assertFalse(self.pod['automountServiceAccountToken'])

    def test_single_cpu_worker_writer_and_asset_readiness(self):
        self.assertEqual(self.deployment['spec']['replicas'], 1)
        self.assertEqual(self.deployment['spec']['strategy']['type'], 'Recreate')  # RWO volume
        terms = self.pod['affinity']['nodeAffinity']['requiredDuringSchedulingIgnoredDuringExecution']
        self.assertEqual({e['key'] for e in terms['nodeSelectorTerms'][0]['matchExpressions']},
                         {'node-role.kubernetes.io/control-plane', 'nvidia.com/gpu.present'})
        self.assertIn('memory', self.container['resources']['limits'])
        for claim in self.by_kind['PersistentVolumeClaim']:
            self.assertEqual(claim['spec']['storageClassName'], 'ceph-block-retain')
            self.assertEqual(claim['spec']['accessModes'], ['ReadWriteOnce'])
        self.assertEqual(self.container['readinessProbe']['exec']['command'][-1], '/netboot/ready.sh')
        self.assertEqual(self.container['command'], ['/bin/bash', '/netboot/entrypoint.sh'])
        for name in ('entrypoint.sh', 'ready.sh'):
            self.assertIn('. /netboot/lib.sh', self.data[name])
        self.assertIn('-o nbxyz -g nbxyz', self.data['entrypoint.sh'])  # dnsmasq --tftp-secure

    def test_config_and_assets_survive_restart_and_removal(self):
        claims = {c['metadata']['name']: c for c in self.by_kind['PersistentVolumeClaim']}
        self.assertEqual({n: c['spec']['resources']['requests']['storage'] for n, c in claims.items()},
                         {'netbootxyz-config': '2Gi', 'netbootxyz-assets': '50Gi'})
        for name, claim in claims.items():
            with self.subTest(claim=name):
                self.assertEqual(claim['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'],
                                 'disabled')
        volumes = {v['name']: v for v in self.pod['volumes']}
        mounts = {m['mountPath']: m['name'] for m in self.container['volumeMounts']}
        for path, claim in (('/config', 'netbootxyz-config'), ('/assets', 'netbootxyz-assets')):
            with self.subTest(path=path):
                self.assertEqual(volumes[mounts[path]].get('persistentVolumeClaim'),
                                 {'claimName': claim})
        self.assertFalse([v for v in self.pod['volumes'] if 'emptyDir' in v])

    def test_git_managed_menus_are_installed_read_only(self):
        """Configuration check only: the web app (nbxyz, no DAC_OVERRIDE) cannot save them."""
        for source, target in (('custom.ipxe', '"${menus}/custom/custom.ipxe"'),
                               ('local-vars.ipxe', '"${menus}/local-vars.ipxe"')):
            with self.subTest(file=source):
                self.assertRegex(self.data['entrypoint.sh'], r'(?m)install -m 0444 -o nbxyz -g nbxyz '
                                 + re.escape(f'/netboot/{source} {target}') + r' \\$')
                # Readiness still compares the served copy with the ConfigMap.
                self.assertIn(f'/netboot/{source}', self.data['ready.sh'])

    def test_release_check_rejects_error_pages_and_truncated_loaders(self):
        """Runs lib.sh's release(): what decides refetch, exit-to-retry and readiness."""
        good = {'menu.ipxe': b'#!ipxe\n', 'boot.cfg': b'#!ipxe\n', 'netboot.xyz.efi': pe_image()}
        loader = good['netboot.xyz.efi']
        broken = [('menu.ipxe', b'<html>404</html>'), ('boot.cfg', b''),
                  ('netboot.xyz.efi', loader[:-1]), ('netboot.xyz.efi', loader[:600]),
                  ('netboot.xyz.efi', loader[:88]),  # ends where the section table starts
                  ('netboot.xyz.efi', b'<html>404</html>'), ('netboot.xyz.efi', b'MZ' + bytes(2000))]
        for name, body in [(None, None)] + broken:
            with self.subTest(file=name, size=body and len(body)), \
                    tempfile.TemporaryDirectory() as tmp:
                pathlib.Path(tmp, 'lib.sh').write_text(self.data['lib.sh'])
                for file, content in {**good, **({name: body} if name else {})}.items():
                    pathlib.Path(tmp, file).write_bytes(content)
                code = subprocess.run(['bash', '-c', '. "$0/lib.sh"; release "$0"', tmp]).returncode
                self.assertEqual(code, 1 if name else 0)

    def test_every_exit_to_retry_check_also_refetches_on_the_next_start(self):
        lines = self.data['entrypoint.sh'].splitlines()
        checks = [set(re.findall(r'release "[^"]+"', next(s for s in lines if s.startswith(p))))
                  for p in ('if ! release', 'if [[ "$(cat /config/menuversion.txt')]
        self.assertLessEqual(*checks)  # else a damaged served copy crash-loops forever

    def test_talos_entry_needs_explicit_choice_and_passes_no_config(self):
        menu = self.data['custom.ipxe']
        self.assertTrue(menu.startswith('#!ipxe'))
        variables = dict(re.findall(r'^set (\S+) (\S+)$', menu, re.M))
        self.assertEqual(variables['talos_schematic'], SCHEMATIC)
        self.assertEqual(variables['talos_version'], 'v' + topf_talos_version())
        self.assertEqual(variables['talos_url'], 'https://pxe.factory.talos.dev/pxe/'
                         '${talos_schematic}/${talos_version}/metal-amd64')
        # Nothing boots Talos unless chosen: default is Back, and no timeout.
        self.assertRegex(menu, r'(?m)^choose --default back selected')
        self.assertNotIn('--timeout', menu)
        self.assertIn('chain --autofree ${talos_url} || goto failed', menu)
        # Only these iPXE commands run, so no config URL, disk boot or image edit.
        commands = {line.split()[0] for line in menu.splitlines()
                    if line.strip() and not line.startswith(('#', ':'))}
        self.assertEqual(commands, {'set', 'menu', 'item', 'choose', 'goto', 'echo', 'chain',
                                    'prompt', 'exit'})
        self.assertNotIn('talos.config', menu)
        self.assertIn('set custom_url tftp://${next-server}/custom', self.data['local-vars.ipxe'])


def pe_image(sections=((512, 512), (1024, 1000))):
    """Smallest PE layout pe() reads: DOS stub, COFF header, section table, section data."""
    head = bytearray(512)
    head[0:2] = b'MZ'
    struct.pack_into('<I', head, 60, 64)
    struct.pack_into('<4sHH12xHH', head, 64, b'PE\0\0', 0x8664, len(sections), 0, 0)
    for i, (pointer, size) in enumerate(sections):
        struct.pack_into('<16xII', head, 88 + 40 * i, size, pointer)
    return bytes(head) + bytes(max(p + s for p, s in sections) - len(head))


if __name__ == '__main__':
    unittest.main()
