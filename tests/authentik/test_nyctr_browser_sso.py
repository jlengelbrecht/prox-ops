"""Nyctr's explicit browser-test grant and narrowly managed role memberships."""
import copy
from pathlib import Path
import unittest

import yaml
from test_agent_browser_sso import BlueprintLoader, entry, reference

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINTS = ROOT / 'kubernetes/apps/security/authentik/app/configmap-blueprints.yaml'


def violations(config):
    entries = yaml.load(config['data']['nyctr.yaml'], Loader=BlueprintLoader)['entries']
    errors = []

    def require(condition, message):
        if not condition:
            errors.append(message)

    app = entry(entries, 'authentik_core.application', 'app-nyctr')
    grant = entry(entries, 'authentik_policies.policybinding', 'binding-nyctr-ai-browser-test')
    group = entry(entries, 'authentik_core.group', 'group-ai-browser-test-nyctr')
    account = entry(entries, 'authentik_core.user', 'user-ai-browser-test-nyctr-memberships')
    require(app['attrs']['policy_engine_mode'] == 'any', 'explicit grants must be alternatives')
    require(grant['identifiers'] == {'target': reference('KeyOf', 'app-nyctr'), 'order': 10}, 'grant must target Nyctr only')
    require(grant['attrs'] == {'group': reference('KeyOf', 'group-ai-browser-test-nyctr'), 'enabled': True}, 'grant must use the dedicated application group')
    require(group['attrs'] == {'name': 'ai-browser-test-nyctr', 'is_superuser': False,
                              'parents': [], 'roles': []}, 'grant group must not confer global privileges')
    require(account['identifiers'] == {'path': 'agents/browser-test/ai-browser-test'}, 'update the existing path-anchored identity')
    require(account.get('state') == 'present', 'memberships must reconcile on existing accounts')
    require(account['attrs'] == {'groups': [
        reference('Find', ['authentik_core.group', ['name', 'ai-browser-test-identity']]),
        reference('Find', ['authentik_core.group', ['name', 'ai-browser-test-paperclip']]),
        reference('KeyOf', 'group-ai-browser-test-nyctr'),
        reference('KeyOf', 'group-nyctr-admins'),
        reference('KeyOf', 'group-nyctr-members'),
    ]}, 'manage only explicit direct memberships while preserving existing grants')
    for group_id in ('group-nyctr-admins', 'group-nyctr-members'):
        role = entry(entries, 'authentik_core.group', group_id)
        require('users' not in role['attrs'], 'shared human membership must not be replaced')
    old = [e for e in entries if e['model'] == 'authentik_policies.policybinding'
           and e.get('identifiers') == {'target': reference('KeyOf', 'app-nyctr'), 'order': 90}]
    require(len(old) == 1 and old[0].get('state') == 'absent', 'retire the existing deny binding declaratively')
    first = entries[0]
    require(first['model'] == 'authentik_blueprints.metaapplyblueprint'
            and first['attrs'] == {'identifiers': {'name': 'Applications'}, 'required': True},
            'bootstrap the existing identity and grants before membership lookups')
    provider = entry(entries, 'authentik_providers_oauth2.oauth2provider', 'provider-nyctr')
    require(provider['attrs']['redirect_uris'] == [
        {'matching_mode': 'strict', 'url': 'https://nyctr.homelab0.org/api/auth/oidc/callback'},
        {'matching_mode': 'strict', 'url': 'https://blocked.homelab0.org/api/auth/oidc/callback'},
    ], 'both strict OIDC callbacks must remain')
    return errors


class NyctrBrowserSSOTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load(BLUEPRINTS.read_text())

    def test_explicit_grant_and_membership_contract(self):
        self.assertEqual(violations(self.config), [])

    def test_security_regressions_are_detected(self):
        cases = [
            ('policy_engine_mode: any', 'policy_engine_mode: all'),
            ('group: !KeyOf group-ai-browser-test-nyctr', 'group: !Find [authentik_core.group, [name, "authentik Admins"]]'),
            ('- !KeyOf group-nyctr-members', '- !KeyOf group-nyctr-admins'),
            ('- !Find [authentik_core.group, [name, ai-browser-test-paperclip]]', '- !KeyOf group-nyctr-admins'),
            ('state: present', 'state: created'),
            ('state: absent', 'state: present'),
            ('required: true', 'required: false'),
        ]
        # ConfigMap block text is dedented by the outer YAML parser.
        for old, new in cases:
            with self.subTest(change=old):
                config = copy.deepcopy(self.config)
                self.assertIn(old, config['data']['nyctr.yaml'])
                config['data']['nyctr.yaml'] = config['data']['nyctr.yaml'].replace(old, new, 1)
                self.assertTrue(violations(config))

    def test_membership_update_cannot_reset_password_or_activation(self):
        for field, value in [('password', 'not-a-real-password'), ('is_active', True)]:
            with self.subTest(field=field):
                config = copy.deepcopy(self.config)
                doc = yaml.load(config['data']['nyctr.yaml'], Loader=BlueprintLoader)
                account = entry(doc['entries'], 'authentik_core.user', 'user-ai-browser-test-nyctr-memberships')
                account['attrs'][field] = value
                config['data']['nyctr.yaml'] = yaml.safe_dump(doc)
                self.assertTrue(violations(config))


if __name__ == '__main__':
    unittest.main()
