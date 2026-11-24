"""Access contract for the netboot.xyz management UI, its Authentik grant,
asset storage and Homepage card.

check() returns the violated invariants so each negative control below can
prove that a specific weakening is detected. It checks what this app adds;
other apps' outpost membership and Homepage cards are not frozen here.
"""

import copy
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
APP = ROOT / 'kubernetes/apps/network/netbootxyz'
AUTHENTIK = ROOT / 'kubernetes/apps/security/authentik/app'
HOMEPAGE = ROOT / 'kubernetes/apps/tools/homepage/app'
HOST = 'netboot.homelab0.org'
ROUTE, OUTPOST_ROUTE = 'netbootxyz', 'netbootxyz-outpost'
INTERNAL = [{'name': 'envoy-internal', 'namespace': 'network', 'sectionName': 'https'}]
AUTHENTIK_BACKEND = [{'name': 'authentik-server', 'namespace': 'security', 'port': 80}]
EDITOR_BACKEND = [{'name': 'netbootxyz-admin', 'port': 3000}]
ADMINS = {'tag': 'Find', 'value': ['authentik_core.group', ['name', 'authentik Admins']]}
EXCLUSION = {'tag': 'KeyOf', 'value': 'policy-deny-ungranted-ai-browser-test'}
APP_REF = {'tag': 'Find', 'value': ['authentik_core.application', ['slug', 'netbootxyz']]}
SOCKET_IO = {'type': 'PathPrefix', 'value': '/socket.io/'}
# UI route rules, in order: Socket.IO from the management origin, then any
# other WebSocket upgrade on /socket.io/ (no backend: gateway 500), then
# every remaining path. The first two tie on precedence, so order matters.
UI_MATCHES = [
    [{'path': SOCKET_IO, 'headers': [{'name': 'Origin', 'type': 'Exact', 'value': f'https://{HOST}'}]}],
    [{'path': SOCKET_IO, 'headers': [{'name': 'Upgrade', 'type': 'RegularExpression',
                                      'value': '(?i)websocket'}]}],
    [{'path': {'type': 'PathPrefix', 'value': '/'}}],
]


class BlueprintLoader(yaml.SafeLoader):
    pass


def construct_tag(loader, suffix, node):
    if isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        value = loader.construct_scalar(node)
    return {'tag': suffix, 'value': value}


BlueprintLoader.add_multi_constructor('!', construct_tag)


def find_provider(name):
    return {'tag': 'Find', 'value': ['authentik_providers_proxy.proxyprovider', ['name', name]]}


def load():
    docs = []
    for name in yaml.safe_load((APP / 'app/kustomization.yaml').read_text())['resources']:
        docs += [d for d in yaml.safe_load_all((APP / 'app' / name).read_text()) if d]
    blueprints = yaml.safe_load((AUTHENTIK / 'configmap-blueprints.yaml').read_text())['data']
    homepage = yaml.safe_load((HOMEPAGE / 'configmap.yaml').read_text())['data']
    return {
        'docs': docs,
        'blueprints': {key: yaml.load(blueprints[key], Loader=BlueprintLoader)['entries']
                       for key in ('providers-proxy.yaml', 'outposts.yaml', 'applications.yaml')},
        'authentik_resources': yaml.safe_load(
            (AUTHENTIK / 'kustomization.yaml').read_text())['resources'],
        'grant': yaml.safe_load((AUTHENTIK / 'referencegrant-network.yaml').read_text()),
        'services': yaml.safe_load(homepage['services.yaml']),
        'kubernetes': yaml.safe_load(homepage['kubernetes.yaml']),
        'homepage_text': '\n'.join(homepage.values()),
        'rbac': [d for d in yaml.safe_load_all((HOMEPAGE / 'rbac.yaml').read_text()) if d],
    }


def homepage_cards(services):
    """(section, card name) for every card, in file order."""
    return [(section, card) for group in services if isinstance(group, dict)
            for section, items in group.items() for item in (items or []) if isinstance(item, dict)
            for card in item]


def check(f):
    problems = []

    def require(condition, message):
        if not condition:
            problems.append(message)

    by_kind = {}
    for doc in f['docs']:
        by_kind.setdefault(doc['kind'], []).append(doc)
    routes = {r['metadata']['name']: r for r in by_kind.get('HTTPRoute', [])}
    policies = by_kind.get('SecurityPolicy', [])

    # Routes: internal HTTPS gateway only, one hostname, no bypass path.
    require(set(routes) == {ROUTE, OUTPOST_ROUTE}, 'only the UI and outpost routes may exist')
    for name, route in routes.items():
        spec = route['spec']
        require(spec.get('parentRefs') == INTERNAL, f'{name} must attach to envoy-internal https only')
        require(spec.get('hostnames') == [HOST], f'{name} must serve {HOST} only')
        require(route['metadata'].get('namespace', 'network') == 'network', f'{name} must stay in network')
    ui_rules = routes.get(ROUTE, {}).get('spec', {}).get('rules', [])
    require([rule.get('matches') for rule in ui_rules] == UI_MATCHES,
            'the UI route must be: Origin-checked /socket.io/, then the WebSocket deny, then /')
    backendless = [rule for rule in ui_rules if not rule.get('backendRefs')]
    require(backendless == [{'matches': UI_MATCHES[1]}],
            'only the WebSocket deny may lack a backend, and it must have no filter')
    require(all(rule['backendRefs'] == EDITOR_BACKEND for rule in ui_rules if rule.get('backendRefs')),
            'the UI route must reach only the admin service')
    # nosniff is load-bearing: the JSONP polling form (?j=) is text/plain and
    # nosniff stops a cross-site <script> from executing it and leaking the sid.
    for index, rule in enumerate(ui_rules):
        if rule.get('backendRefs'):
            set_headers = [header for flt in rule.get('filters', [])
                           if flt.get('type') == 'ResponseHeaderModifier'
                           for header in flt.get('responseHeaderModifier', {}).get('set', [])]
            require({'name': 'X-Content-Type-Options', 'value': 'nosniff'} in set_headers,
                    f'UI rule {index} must set X-Content-Type-Options: nosniff')
    require(routes.get(ROUTE, {}).get('metadata', {}).get('annotations', {})
            .get('external-dns.alpha.kubernetes.io/hostname') == HOST,
            'internal DNS must publish the management hostname')
    outpost_rules = routes.get(OUTPOST_ROUTE, {}).get('spec', {}).get('rules', [])
    require(outpost_rules == [{
        'matches': [{'path': {'type': 'PathPrefix', 'value': '/outpost.goauthentik.io/'}}],
        'backendRefs': AUTHENTIK_BACKEND}],
        'the callback route must match the outpost prefix and reach only Authentik')
    for name, route in routes.items():
        for rule in route['spec'].get('rules', []):
            for ref in rule.get('backendRefs', []):
                if ref.get('name') != 'authentik-server':
                    require(name == ROUTE, f'{name} reaches the editor without forward auth')

    # Forward auth: one fail-closed policy on the UI route, never the callback.
    require(len(policies) == 1, 'exactly one SecurityPolicy')
    for policy in policies:
        spec = policy['spec']
        require(spec.get('targetRefs') == [{'group': 'gateway.networking.k8s.io',
                                            'kind': 'HTTPRoute', 'name': ROUTE}],
                'forward auth must target exactly the UI route')
        ext = spec.get('extAuth', {})
        require(ext.get('failOpen') is False, 'forward auth must fail closed')
        require(ext.get('http', {}).get('backendRefs') == AUTHENTIK_BACKEND,
                'forward auth must ask the Authentik server')
        require(ext.get('http', {}).get('path') == '/outpost.goauthentik.io/auth/envoy',
                'forward auth must use the Authentik Envoy endpoint')
        require('cookie' in ext.get('headersToExtAuth', []), 'the session cookie must reach Authentik')
        require(set(spec) == {'targetRefs', 'extAuth'}, 'no other security settings on the route')
    # Traffic policy: timeouts only, merged into the gateway-wide policy.
    for policy in by_kind.get('BackendTrafficPolicy', []):
        spec = policy['spec']
        require(set(spec) <= {'targetRefs', 'mergeType', 'timeout', 'tcpKeepalive'},
                'the traffic policy may only set timeouts')
        require(spec.get('mergeType') == 'StrategicMerge',
                'the route traffic policy must merge with the gateway policy, not replace it')

    # Cross-namespace reference: network routes/policies to the Authentik Service only.
    grant = f['grant']
    require(grant['metadata'].get('namespace') == 'security', 'grant must live beside Authentik')
    require(grant['spec'].get('from') == [
        {'group': 'gateway.envoyproxy.io', 'kind': 'SecurityPolicy', 'namespace': 'network'},
        {'group': 'gateway.networking.k8s.io', 'kind': 'HTTPRoute', 'namespace': 'network'}],
        'grant must admit only network SecurityPolicies and HTTPRoutes')
    require(grant['spec'].get('to') == [{'group': '', 'kind': 'Service', 'name': 'authentik-server'}],
            'grant must be scoped to the authentik-server Service')
    require('./referencegrant-network.yaml' in f['authentik_resources'], 'grant must be applied')

    # Authentik: dedicated provider, both outpost lists, admin-only application.
    providers = [e for e in f['blueprints']['providers-proxy.yaml'] if e.get('id') == 'proxy-netbootxyz']
    require(len(providers) == 1, 'one netboot.xyz proxy provider')
    for provider in providers:
        attrs = provider.get('attrs', {})
        require(provider.get('identifiers') == {'name': 'netboot.xyz'} and attrs.get('name') == 'netboot.xyz',
                'provider name is the outpost and application lookup key')
        require(attrs.get('mode') == 'forward_single' and attrs.get('external_host') == f'https://{HOST}',
                'provider must forward-auth the management hostname')
        require('skip_path_regex' not in attrs, 'no unauthenticated paths on the editor')
        require(attrs.get('intercept_header_auth') is False,
                'browser-only editor: app passwords and tokens must not authenticate')
        require(attrs.get('authorization_flow') == {'tag': 'Context', 'value': 'authorization_flow'},
                'provider must use the shared authorization flow')
    outposts = [e for e in f['blueprints']['outposts.yaml'] if e.get('id') == 'outpost-embedded']
    require(len(outposts) == 1, 'one embedded outpost entry')
    netboot, lightrag = find_provider('netboot.xyz'), find_provider('LightRAG')
    for outpost in outposts:
        providers_attr = outpost.get('attrs', {}).get('providers', {})
        lists = providers_attr.get('value', [None, [], []]) if isinstance(providers_attr, dict) else [None, [], []]
        require(providers_attr.get('tag') == 'If' and len(lists) == 3 and lists[0] == lightrag,
                'outpost keeps its LightRAG condition')
        if len(lists) == 3:
            for label, members in (('with', lists[1]), ('without', lists[2])):
                require(members.count(netboot) == 1,
                        f'netboot.xyz must be on the outpost list {label} LightRAG exactly once')
            require(lightrag in lists[1] and lists[2] == [p for p in lists[1] if p != lightrag],
                    'the two outpost lists must differ only by LightRAG')
    apps = f['blueprints']['applications.yaml']
    app = [e for e in apps if e.get('model') == 'authentik_core.application'
           and e.get('identifiers') == {'slug': 'netbootxyz'}]
    require(len(app) == 1, 'one netboot.xyz application')
    for entry in app:
        attrs = entry.get('attrs', {})
        require(entry.get('state', 'present') == 'present', 'application must be present')
        require(attrs.get('policy_engine_mode') == 'all', 'every binding must pass, not any one')
        require(attrs.get('provider') == netboot, 'application uses its own provider')
    bindings = [e for e in apps if e.get('model') == 'authentik_policies.policybinding'
                and e.get('identifiers', {}).get('target') == APP_REF]
    require(sorted([b.get('attrs') for b in bindings], key=repr) == sorted([
        {'group': ADMINS, 'enabled': True}, {'policy': EXCLUSION, 'enabled': True}], key=repr),
        'only authentik Admins may pass, and the agent exclusion must apply')
    require(len({b['identifiers'].get('order') for b in bindings}) == len(bindings),
            'bindings need distinct orders')
    # The change touches nothing else in Authentik: no group, user, flow or
    # other application entry may mention it.
    for key, entries in f['blueprints'].items():
        for entry in entries:
            if 'netboot' in repr(entry):
                require(entry.get('model') in {'authentik_providers_proxy.proxyprovider',
                                               'authentik_outposts.outpost',
                                               'authentik_core.application',
                                               'authentik_policies.policybinding'},
                        f'unexpected netboot reference in {key}: {entry.get("model")}')
    for binding_id in ('binding-obsidian-admins', 'binding-paperclip-admins'):
        existing = [e for e in apps if e.get('id') == binding_id]
        require(len(existing) == 1 and existing[0].get('attrs') == {'group': ADMINS, 'enabled': True},
                f'{binding_id} must remain unchanged')

    # Assets: a separate retained volume that survives restarts and app removal.
    claims = {c['metadata']['name']: c for c in by_kind.get('PersistentVolumeClaim', [])}
    assets = claims.get('netbootxyz-assets', {})
    require(assets.get('spec', {}).get('resources', {}).get('requests', {}).get('storage') == '50Gi',
            'asset volume must be 50Gi')
    require(assets.get('spec', {}).get('storageClassName') == 'ceph-block-retain',
            'asset volume must use the retained Ceph class')
    require(assets.get('metadata', {}).get('annotations', {}).get('kustomize.toolkit.fluxcd.io/prune')
            == 'disabled', 'Flux must never prune the asset volume')
    config = claims.get('netbootxyz-config', {})
    require(config.get('spec', {}).get('resources', {}).get('requests', {}).get('storage') == '2Gi'
            and config.get('metadata', {}).get('annotations', {}).get('kustomize.toolkit.fluxcd.io/prune')
            == 'disabled', 'config volume must stay as it was')
    pod = by_kind['Deployment'][0]['spec']['template']['spec']
    volumes = {v['name']: v for v in pod['volumes']}
    require(volumes.get('assets', {}).get('persistentVolumeClaim') == {'claimName': 'netbootxyz-assets'},
            'assets must be mounted from the retained volume')
    require(volumes.get('config', {}).get('persistentVolumeClaim') == {'claimName': 'netbootxyz-config'},
            'config must stay on its own volume')
    require(not any('emptyDir' in v for v in pod['volumes']), 'no ephemeral storage for served files')
    mounts = {m['name']: m['mountPath'] for m in pod['containers'][0]['volumeMounts']}
    require(mounts.get('assets') == '/assets' and mounts.get('config') == '/config',
            'volumes must stay at the image paths')

    # Homepage: explicit in-cluster connection, no discovery, one card in
    # Network without any direct call to the editor.
    kube = f['kubernetes'] or {}
    require(kube.get('mode') == 'cluster', 'Homepage must use the in-cluster service account')
    for source in ('ingress', 'gateway', 'traefik'):
        require(kube.get(source) is False, f'Homepage {source} discovery must be off')
    require([section for section, card in homepage_cards(f['services']) if card == 'netboot.xyz']
            == ['Network'], 'the netboot.xyz card must appear once, in the Network section only')
    network = next((s['Network'] for s in f['services'] if isinstance(s, dict) and 'Network' in s), [])
    card = next((c['netboot.xyz'] for c in network if isinstance(c, dict) and 'netboot.xyz' in c), {})
    require(card.get('href') == f'https://{HOST}', 'card must link to the protected hostname')
    require(card.get('namespace') == 'network' and card.get('app') == 'netbootxyz',
            'card must read pod status from the Kubernetes API')
    require(card.get('podSelector') == 'app.kubernetes.io/name=netbootxyz', 'card must select the pod')
    require(not {'widget', 'widgets', 'siteMonitor', 'ping'} & set(card), 'card must not call the editor')
    require('netbootxyz-admin' not in f['homepage_text'] and ':3000' not in repr(card),
            'Homepage must never reach the admin port')
    for role in f['rbac']:
        for rule in role.get('rules', []):
            require(set(rule.get('verbs', [])) <= {'get', 'list'} and 'secrets' not in rule.get('resources', []),
                    'Homepage RBAC must stay read-only without Secrets')
    return problems


class AccessContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load()

    def mutated(self, change):
        fixture = copy.deepcopy(self.fixture)
        change(fixture)
        return check(fixture)

    @staticmethod
    def doc(fixture, kind, name=None):
        return next(d for d in fixture['docs'] if d['kind'] == kind
                    and (name is None or d['metadata']['name'] == name))

    @staticmethod
    def entry(fixture, key, **match):
        return next(e for e in fixture['blueprints'][key]
                    if all(e.get(k) == v for k, v in match.items()))

    def ui_rules(self, fixture):
        return self.doc(fixture, 'HTTPRoute', ROUTE)['spec']['rules']

    def test_contract(self):
        self.assertEqual(check(self.fixture), [])

    def test_route_bypasses_are_rejected(self):
        def extra_api_route(f):
            route = copy.deepcopy(self.doc(f, 'HTTPRoute', ROUTE))
            route['metadata']['name'] = 'netbootxyz-api'
            f['docs'].append(route)

        def external_gateway(f):
            self.doc(f, 'HTTPRoute', ROUTE)['spec']['parentRefs'][0]['name'] = 'envoy-external'

        def outpost_to_editor(f):
            self.doc(f, 'HTTPRoute', OUTPOST_ROUTE)['spec']['rules'][0]['backendRefs'] = [
                {'name': 'netbootxyz-admin', 'port': 3000}]

        def broad_callback(f):
            self.doc(f, 'HTTPRoute', OUTPOST_ROUTE)['spec']['rules'][0]['matches'][0]['path']['value'] = '/'

        def extra_ui_rule(f):
            rules = self.ui_rules(f)
            rules.append(copy.deepcopy(rules[-1]))

        def other_backend(f):
            self.ui_rules(f)[-1]['backendRefs'] = [{'name': 'netbootxyz', 'port': 8080}]

        for change in (extra_api_route, external_gateway, outpost_to_editor, broad_callback,
                       extra_ui_rule, other_backend):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_cross_site_websocket_guard_is_required(self):
        def deny_removed(f):
            del self.ui_rules(f)[1]

        def origin_widened(f):
            self.ui_rules(f)[0]['matches'][0]['headers'][0] = {
                'name': 'Origin', 'type': 'RegularExpression', 'value': '.*homelab0.org'}

        def allow_after_deny(f):
            rules = self.ui_rules(f)
            rules[0], rules[1] = rules[1], rules[0]

        def deny_given_backend(f):
            self.ui_rules(f)[1]['backendRefs'] = copy.deepcopy(EDITOR_BACKEND)

        def deny_redirected(f):
            self.ui_rules(f)[1]['filters'] = [{'type': 'RequestRedirect',
                                               'requestRedirect': {'hostname': 'example.org'}}]

        def root_nosniff_removed(f):
            modifier = self.ui_rules(f)[-1]['filters'][0]['responseHeaderModifier']
            modifier['set'] = [h for h in modifier['set'] if h['name'] != 'X-Content-Type-Options']

        for change in (deny_removed, origin_widened, allow_after_deny, deny_given_backend,
                       deny_redirected, root_nosniff_removed):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_forward_auth_weakening_is_rejected(self):
        def fail_open(f):
            self.doc(f, 'SecurityPolicy')['spec']['extAuth']['failOpen'] = True

        def omit_fail_open(f):
            del self.doc(f, 'SecurityPolicy')['spec']['extAuth']['failOpen']

        def target_outpost(f):
            self.doc(f, 'SecurityPolicy')['spec']['targetRefs'][0]['name'] = OUTPOST_ROUTE

        def no_policy(f):
            f['docs'] = [d for d in f['docs'] if d['kind'] != 'SecurityPolicy']

        def wide_grant(f):
            del f['grant']['spec']['to'][0]['name']

        def unregistered_grant(f):
            f['authentik_resources'].remove('./referencegrant-network.yaml')

        def traffic_policy_replaces_gateway(f):
            del self.doc(f, 'BackendTrafficPolicy')['spec']['mergeType']

        def traffic_policy_adds_settings(f):
            self.doc(f, 'BackendTrafficPolicy')['spec']['rateLimit'] = {'type': 'Local'}

        for change in (fail_open, omit_fail_open, target_outpost, no_policy, wide_grant,
                       unregistered_grant, traffic_policy_replaces_gateway,
                       traffic_policy_adds_settings):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_authentik_grant_weakening_is_rejected(self):
        def any_mode(f):
            self.entry(f, 'applications.yaml', id='app-netbootxyz')['attrs']['policy_engine_mode'] = 'any'

        def other_group(f):
            self.entry(f, 'applications.yaml', id='binding-netbootxyz-admins')['attrs']['group'] = {
                'tag': 'Find', 'value': ['authentik_core.group', ['name', 'grafana-viewers']]}

        def negated_or_extra_grant(f):
            f['blueprints']['applications.yaml'].append({
                'model': 'authentik_policies.policybinding',
                'identifiers': {'target': APP_REF, 'order': 5},
                'attrs': {'group': {'tag': 'Find', 'value': ['authentik_core.group', ['name', 'users']]},
                          'enabled': True}})

        def no_exclusion(f):
            f['blueprints']['applications.yaml'] = [
                e for e in f['blueprints']['applications.yaml']
                if not (e.get('identifiers', {}).get('target') == APP_REF
                        and e.get('attrs', {}).get('policy') == EXCLUSION)]

        def skip_paths(f):
            self.entry(f, 'providers-proxy.yaml', id='proxy-netbootxyz')['attrs']['skip_path_regex'] = '^/socket.io/'

        def header_auth(f):
            self.entry(f, 'providers-proxy.yaml', id='proxy-netbootxyz')['attrs']['intercept_header_auth'] = True

        def outpost_dropped(f):
            members = self.entry(f, 'outposts.yaml', id='outpost-embedded')['attrs']['providers']['value'][2]
            members.remove(find_provider('netboot.xyz'))

        def outpost_member_lost(f):
            self.entry(f, 'outposts.yaml', id='outpost-embedded')['attrs']['providers']['value'][1].pop(0)

        def outpost_duplicated(f):
            self.entry(f, 'outposts.yaml', id='outpost-embedded')['attrs']['providers']['value'][1].append(
                find_provider('netboot.xyz'))

        def group_membership(f):
            f['blueprints']['applications.yaml'].append({
                'model': 'authentik_core.group', 'identifiers': {'name': 'netboot-editors'}})

        for change in (any_mode, other_group, negated_or_extra_grant, no_exclusion, skip_paths,
                       header_auth, outpost_dropped, outpost_member_lost, outpost_duplicated,
                       group_membership):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_storage_regressions_are_rejected(self):
        def ephemeral(f):
            volumes = self.doc(f, 'Deployment')['spec']['template']['spec']['volumes']
            assets = next(v for v in volumes if v['name'] == 'assets')
            assets.clear()
            assets.update({'name': 'assets', 'emptyDir': {'sizeLimit': '1Gi'}})

        def prunable(f):
            del self.doc(f, 'PersistentVolumeClaim', 'netbootxyz-assets')['metadata']['annotations']

        def shared_with_config(f):
            volumes = self.doc(f, 'Deployment')['spec']['template']['spec']['volumes']
            next(v for v in volumes if v['name'] == 'assets')['persistentVolumeClaim'] = {
                'claimName': 'netbootxyz-config'}

        def deleting_class(f):
            self.doc(f, 'PersistentVolumeClaim', 'netbootxyz-assets')['spec']['storageClassName'] = 'ceph-block'

        for change in (ephemeral, prunable, shared_with_config, deleting_class):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_homepage_regressions_are_rejected(self):
        def network(f):
            return next(s['Network'] for s in f['services'] if 'Network' in s)

        def take_card(f):
            cards = network(f)
            return cards.pop(next(i for i, c in enumerate(cards) if 'netboot.xyz' in c))

        def discovery_on(f):
            f['kubernetes']['ingress'] = True

        def editor_widget(f):
            next(c for c in network(f) if 'netboot.xyz' in c)['netboot.xyz']['widget'] = {
                'type': 'customapi', 'url': 'http://netbootxyz-admin.network.svc.cluster.local:3000'}

        def editor_widgets(f):
            next(c for c in network(f) if 'netboot.xyz' in c)['netboot.xyz']['widgets'] = [
                {'type': 'customapi', 'url': f'https://{HOST}'}]

        def card_moved(f):
            card = take_card(f)
            other = next(s for s in f['services'] if 'Network' not in s)
            next(iter(other.values())).append(card)

        def card_duplicated(f):
            other = next(s for s in f['services'] if 'Network' not in s)
            next(iter(other.values())).append(copy.deepcopy(
                next(c for c in network(f) if 'netboot.xyz' in c)))

        def card_lost(f):
            take_card(f)

        def wider_rbac(f):
            f['rbac'][1]['rules'].append({'apiGroups': [''], 'resources': ['secrets'], 'verbs': ['get']})

        for change in (discovery_on, editor_widget, editor_widgets, card_moved, card_duplicated,
                       card_lost, wider_rbac):
            with self.subTest(change=change.__name__):
                self.assertTrue(self.mutated(change))

    def test_unrelated_additions_are_not_frozen(self):
        """Another app's outpost member or Network card must not break this suite."""
        def other_forward_auth_app(f):
            for members in self.entry(f, 'outposts.yaml', id='outpost-embedded')['attrs']['providers']['value'][1:]:
                members.append(find_provider('SomeNewApp'))

        def other_network_card(f):
            network = next(s['Network'] for s in f['services'] if 'Network' in s)
            network.append({'SomeNewCard': {'href': 'https://example.homelab0.org'}})

        for change in (other_forward_auth_app, other_network_card):
            with self.subTest(change=change.__name__):
                self.assertEqual(self.mutated(change), [])

    def test_management_behaviour_is_documented(self):
        readme = (APP / 'README.md').read_text()
        for term in (HOST, 'authentik Admins', '/config/menus/local', 'custom/custom.ipxe',
                     'netbootxyz-assets', '10.20.67.28', 'Origin', '0444'):
            self.assertIn(term, readme)


if __name__ == '__main__':
    unittest.main()
