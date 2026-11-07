"""Offline contract checks; all identities and tokens below are synthetic."""
import asyncio
import importlib.util
import pathlib
import sys
import types
import unittest
from unittest.mock import patch

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE = ROOT / 'kubernetes/apps/ai/litellm/app/callbacks/copilot_session_guard.py'
APP = ROOT / 'kubernetes/apps/ai/litellm/app'


class AuthFailure(Exception):
    def __init__(self, **kwargs):
        super().__init__(kwargs['message'])


class Session:
    def __init__(self, token='synthetic-copilot-token', api_base='https://api.githubcopilot.com'):
        self.token, self.api_base = token, api_base


class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = [
            'litellm', 'litellm.integrations', 'litellm.integrations.custom_logger',
            'litellm.litellm_core_utils', 'litellm.litellm_core_utils.credential_accessor',
            'litellm.llms', 'litellm.llms.github_copilot', 'litellm.llms.github_copilot.per_user_auth',
            'litellm.constants', 'litellm.exceptions',
        ]
        cls.old = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules['litellm.integrations.custom_logger'].CustomLogger = type('CustomLogger', (), {})
        cls.accessor = type('CredentialAccessor', (), {'find_credential': staticmethod(lambda name: cls.credential)})
        sys.modules['litellm.litellm_core_utils.credential_accessor'].CredentialAccessor = cls.accessor
        cls.native_calls = 0
        async def attach(kwargs):
            cls.native_calls += 1
            if cls.credential is not None and cls.credential.credential_values.get('github_copilot_auth_type') == 'per_user_oauth':
                kwargs['github_copilot_user_session'] = Session()
                # Native attach removes caller Authorization from request headers.
                for key in ('headers', 'extra_headers'):
                    if isinstance(kwargs.get(key), dict):
                        kwargs[key] = {k: v for k, v in kwargs[key].items() if k.lower() != 'authorization'}
        provider = sys.modules['litellm.llms.github_copilot.per_user_auth']
        provider.GITHUB_COPILOT_USER_SESSION_KWARG_KEY = 'github_copilot_user_session'
        provider.GithubCopilotUserSession = Session
        provider.aattach_github_copilot_user_session = attach
        constants = sys.modules['litellm.constants']
        constants.GITHUB_COPILOT_AUTH_TYPE_KEY = 'github_copilot_auth_type'
        constants.GITHUB_COPILOT_PER_USER_AUTH_TYPE = 'per_user_oauth'
        sys.modules['litellm.exceptions'].CallerCredentialAuthenticationError = AuthFailure
        spec = importlib.util.spec_from_file_location('copilot_session_guard_test', MODULE)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def setUp(self):
        self.__class__.credential = types.SimpleNamespace(credential_values={'github_copilot_auth_type': 'per_user_oauth'})
        self.__class__.native_calls = 0

    def request(self):
        return {
            'model': 'github_copilot/gpt-6-luna',
            'litellm_credential_name': 'project-github-copilot',
            'litellm_metadata': {'deployment_model_name': 'project-copilot-explicit', 'deployment': 'github_copilot/gpt-6-luna', 'api_base': None},
            'secret_fields': {'user_provider_credentials_user_id': 'synthetic-owner', 'user_provider_credentials': {'project-github-copilot': 'synthetic-github-token'}},
        }

    def run_hook(self, request):
        return asyncio.run(self.module.CopilotSessionGuard().async_pre_call_deployment_hook(request, 'aresponses'))

    def test_attaches_pinned_session(self):
        request = self.request()
        request['headers'] = {'Authorization': 'synthetic-caller-override'}
        result = self.run_hook(request)
        self.assertIsInstance(result['github_copilot_user_session'], Session)
        self.assertEqual(result['github_copilot_user_session'].api_base, 'https://api.githubcopilot.com')
        self.assertNotIn('Authorization', result['headers'])
        self.assertEqual(self.native_calls, 1)

    def test_missing_wrong_and_changed_credential_fail(self):
        for value in (None, {'github_copilot_auth_type': 'shared'}):
            with self.subTest(value=value):
                self.__class__.credential = None if value is None else types.SimpleNamespace(credential_values=value)
                with self.assertRaises(AuthFailure):
                    self.run_hook(self.request())
                self.assertEqual(self.native_calls, 0)

    def test_change_during_attach_keeps_valid_session(self):
        async def changed(kwargs):
            self.__class__.credential.credential_values['github_copilot_auth_type'] = 'shared'
            kwargs['github_copilot_user_session'] = Session()
        with patch.object(self.module, 'aattach_github_copilot_user_session', changed):
            result = self.run_hook(self.request())
        self.assertIsInstance(result['github_copilot_user_session'], Session)

    def test_missing_owner_session_and_spoofed_fields_fail(self):
        cases = []
        for key in ('user_provider_credentials_user_id', 'user_provider_credentials'):
            request = self.request()
            del request['secret_fields'][key]
            cases.append(request)
        for key, value in (
            ('github_copilot_user_session', Session()),
            ('api_base', 'https://attacker.invalid'),
            ('base_url', 'https://attacker.invalid'),
            ('api_key', 'synthetic-caller-key'),
            ('litellm_credential_name', 'wrong'),
            ('model', 'github_copilot/other'),
        ):
            request = self.request()
            request[key] = value
            cases.append(request)
        for request in cases:
            with self.subTest(request=tuple(request)):
                with self.assertRaises(AuthFailure):
                    self.run_hook(request)

    def test_metadata_base_url_override_fails(self):
        for key in ('metadata', 'litellm_metadata'):
            with self.subTest(key=key):
                request = self.request()
                request[key] = dict(request['litellm_metadata'], base_url='https://attacker.invalid')
                with self.assertRaises(AuthFailure):
                    self.run_hook(request)

    def test_native_missing_or_untrusted_session_fails(self):
        async def missing(kwargs):
            pass
        async def untrusted(kwargs):
            kwargs['github_copilot_user_session'] = Session(api_base='https://attacker.invalid')
        for native in (missing, untrusted):
            with patch.object(self.module, 'aattach_github_copilot_user_session', native):
                with self.assertRaises(AuthFailure):
                    self.run_hook(self.request())

    def test_other_aliases_unchanged(self):
        for alias in ('gpt-4', 'gpt-3.5-turbo', 'anthropic/*'):
            request = {'model': alias, 'metadata': {'deployment_model_name': alias}}
            self.assertIs(self.run_hook(request), request)
        self.assertEqual(self.native_calls, 0)

    def test_config_and_source_contract(self):
        config = yaml.safe_load((APP / 'configmap.yaml').read_text().split('---', 1)[-1])
        inner = yaml.safe_load(config['data']['config.yaml'])
        self.assertEqual(inner['litellm_settings']['callbacks'], ['copilot_session_guard.proxy_handler_instance'])
        aliases = [m for m in inner['model_list'] if m['model_name'] == 'project-copilot-explicit']
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]['litellm_params']['num_retries'], 0)
        helm = yaml.safe_load((APP / 'helmrelease.yaml').read_text().split('---', 1)[-1])
        self.assertEqual(helm['spec']['values']['replicaCount'], 1)
        self.assertIn('litellm-copilot-session-guard', helm['spec']['values']['podAnnotations']['configmap.reloader.stakater.com/reload'])
        patch_text = helm['spec']['postRenderers'][0]['kustomize']['patches'][0]['patch']
        self.assertIn('mountPath: /opt/litellm-callbacks', patch_text)
        self.assertIn('name: PYTHONPATH', patch_text)
        kustomization = yaml.safe_load((APP / 'kustomization.yaml').read_text().split('---', 1)[-1])
        self.assertEqual(kustomization['configMapGenerator'][0]['files'], ['copilot_session_guard.py=callbacks/copilot_session_guard.py'])
        contract = yaml.safe_load((ROOT / 'tests/litellm/image_contract.json').read_text())
        self.assertEqual(contract['schema'], 'litellm-copilot-image-contract/1')
        self.assertEqual(contract['interfaces']['callback'], inner['litellm_settings']['callbacks'][0])
        self.assertTrue(contract['image'].startswith('ghcr.io/berriai/litellm-database:v1.108.0@sha256:'))
        self.assertIsInstance(self.module.proxy_handler_instance, self.module.CopilotSessionGuard)
