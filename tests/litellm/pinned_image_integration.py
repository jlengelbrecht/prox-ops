"""Offline checks inside the exact pinned LiteLLM image; synthetic data only.

Prints closed test names and statuses. This file must never contact a provider.
"""
import asyncio
import importlib
import json
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

CASES = {}
FAILURES = {}
CURRENT_PREDICATE = 'case_execution'


def check(name, fn):
    global CURRENT_PREDICATE
    CURRENT_PREDICATE = 'case_execution'
    try:
        fn()
    except BaseException as error:
        CASES[name] = 'fail'
        error_class = type(error).__name__
        if not error_class.isidentifier() or len(error_class) > 80:
            error_class = 'UnknownException'
        FAILURES[name] = {'class': error_class, 'predicate': CURRENT_PREDICATE}
    else:
        CASES[name] = 'pass'


def require(condition, label):
    global CURRENT_PREDICATE
    CURRENT_PREDICATE = label
    if not condition:
        raise AssertionError


def expect_denied(coro):
    try:
        asyncio.run(coro)
    except Exception as error:
        from litellm.exceptions import CallerCredentialAuthenticationError
        assert isinstance(error, CallerCredentialAuthenticationError)
        assert str(error).find('Project Copilot connection unavailable') >= 0
    else:
        raise AssertionError('accepted')


def request(alias='project-copilot-explicit'):
    return {
        'model': 'github_copilot/gpt-6-luna',
        'litellm_credential_name': 'project-github-copilot',
        'litellm_metadata': {'deployment_model_name': alias, 'deployment': 'github_copilot/gpt-6-luna'},
        'secret_fields': {'user_provider_credentials_user_id': 'test-owner',
                          'user_provider_credentials': {'project-github-copilot': 'synthetic-github-token'}},
        'headers': {'Authorization': 'synthetic-caller-bearer'},
    }


def main():
    try:
        import litellm
        from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
        from litellm.llms.github_copilot import per_user_auth
        from litellm.llms.github_copilot.responses.transformation import GithubCopilotResponsesAPIConfig
        from litellm.proxy.common_utils.callback_utils import initialize_callbacks_on_proxy
        from litellm.utils import async_pre_call_deployment_hook
        from copilot_session_guard import CopilotSessionGuard
    except BaseException:
        print(json.dumps({'schema': 'litellm-copilot-image-result/1', 'checks': {'installed_import': 'fail'}}))
        return 1
    CASES['installed_import'] = 'pass'
    original_callbacks = litellm.callbacks
    litellm.callbacks = []
    try:
        def registered():
            initialize_callbacks_on_proxy(
                value=['copilot_session_guard.proxy_handler_instance'],
                premium_user=False, config_file_path='/tmp/litellm-copilot-config.yaml',
                litellm_settings={'callbacks': ['copilot_session_guard.proxy_handler_instance']},
                callback_specific_params={},
            )
            assert any(isinstance(cb, CopilotSessionGuard) for cb in litellm.callbacks)
        check('actual_registration', registered)

        def broken_import():
            try:
                initialize_callbacks_on_proxy(
                    value=['copilot_missing_module.NoGuard'], premium_user=False,
                    config_file_path='/tmp/litellm-copilot-config.yaml', litellm_settings={},
                    callback_specific_params={},
                )
            except BaseException:
                return
            raise AssertionError('missing callback accepted')
        check('missing_import_fails', broken_import)

        good = SimpleNamespace(credential_values={'github_copilot_auth_type': 'per_user_oauth'})
        wrong = SimpleNamespace(credential_values={'github_copilot_auth_type': 'shared'})
        state = {'credential': good, 'exchange': 0, 'shared': 0}
        def find(name):
            assert name == 'project-github-copilot'
            return state['credential']
        async def exchange(**kwargs):
            state['exchange'] += 1
            assert kwargs['user_id'] == 'test-owner'
            assert kwargs['github_token'] == 'synthetic-github-token'
            return per_user_auth.GithubCopilotUserSession(
                token='synthetic-session-token', api_base='https://api.githubcopilot.com')
        def shared(*args, **kwargs):
            state['shared'] += 1
            raise AssertionError('shared auth used')
        with patch.object(CredentialAccessor, 'find_credential', staticmethod(find)), \
             patch.object(per_user_auth, 'aexchange_github_token', exchange), \
             tempfile.TemporaryDirectory(prefix='copilot-empty-', dir='/tmp') as token_dir, \
             patch.dict('os.environ', {'GITHUB_COPILOT_TOKEN_DIR': token_dir}), \
             patch('litellm.llms.github_copilot.authenticator.Authenticator.get_api_key', shared), \
             patch('litellm.llms.github_copilot.authenticator.Authenticator.get_api_base', shared):
            hook = CopilotSessionGuard()
            def normal():
                global CURRENT_PREDICATE
                state['credential'] = good
                req = request()
                CURRENT_PREDICATE = 'guard_attaches'
                result = asyncio.run(hook.async_pre_call_deployment_hook(req, 'aresponses'))
                session = result.get('github_copilot_user_session')
                require(isinstance(session, per_user_auth.GithubCopilotUserSession), 'session_type')
                require(session.token == 'synthetic-session-token', 'session_token')
                require('Authorization' not in result['headers'], 'caller_header_removed')
                # The installed native step must preserve the same object.
                CURRENT_PREDICATE = 'native_reattach'
                asyncio.run(per_user_auth.aattach_github_copilot_user_session(result))
                require(result['github_copilot_user_session'] is session, 'session_identity_preserved')
                # Shared auth stays patched through all subsequent denial cases.
                CURRENT_PREDICATE = 'responses_config_init'
                config = GithubCopilotResponsesAPIConfig()
                CURRENT_PREDICATE = 'responses_validate_environment'
                headers = config.validate_environment({}, 'gpt-6-luna', result)
                require(headers.get('Authorization') == 'Bearer synthetic-session-token', 'responses_header_session')
                CURRENT_PREDICATE = 'responses_get_complete_url'
                require(config.get_complete_url(None, result) == 'https://api.githubcopilot.com/responses', 'responses_url_session')
                require(state['shared'] == 0, 'shared_auth_unused')
            check('session_precedence_responses', normal)

            def actual_hook():
                state['credential'] = good
                req = request()
                result = asyncio.run(async_pre_call_deployment_hook(req, 'aresponses'))
                assert isinstance(result.get('github_copilot_user_session'), per_user_auth.GithubCopilotUserSession)
            check('actual_callback_chain', actual_hook)

            for label, credential in [('missing_credential', None), ('wrong_mode', wrong)]:
                def denied(value=credential):
                    state['credential'] = value
                    req = request()
                    req['github_copilot_auth_type'] = 'per_user_oauth'
                    expect_denied(hook.async_pre_call_deployment_hook(req, 'aresponses'))
                check(label, denied)
            def no_owner():
                state['credential'] = good
                req = request()
                del req['secret_fields']['user_provider_credentials_user_id']
                expect_denied(hook.async_pre_call_deployment_hook(req, 'aresponses'))
            check('missing_owner', no_owner)
            def spoofed():
                state['credential'] = good
                req = request()
                req['github_copilot_user_session'] = per_user_auth.GithubCopilotUserSession('synthetic-attacker', 'https://api.githubcopilot.com')
                expect_denied(hook.async_pre_call_deployment_hook(req, 'aresponses'))
            check('spoofed_session', spoofed)
            def no_session():
                state['credential'] = good
                async def rejected(**kwargs):
                    raise RuntimeError('synthetic rejection')
                with patch.object(per_user_auth, 'aexchange_github_token', rejected):
                    expect_denied(hook.async_pre_call_deployment_hook(request(), 'aresponses'))
            check('rejected_exchange', no_session)
            def changed_between_check_and_attach():
                state['credential'] = good
                lookups = [good, wrong]
                with patch.object(CredentialAccessor, 'find_credential',
                                  staticmethod(lambda name: lookups.pop(0))):
                    expect_denied(hook.async_pre_call_deployment_hook(request(), 'aresponses'))
            check('mode_change_before_attach', changed_between_check_and_attach)
            def changed_before_attach():
                state['credential'] = good
                async def flip(**kwargs):
                    state['credential'] = wrong
                    return await exchange(**kwargs)
                # Native mode is rechecked after the guard's own check.
                with patch.object(per_user_auth, 'aexchange_github_token', flip):
                    req = request()
                    asyncio.run(hook.async_pre_call_deployment_hook(req, 'aresponses'))
                    assert isinstance(req['github_copilot_user_session'], per_user_auth.GithubCopilotUserSession)
            check('change_during_exchange_keeps_session', changed_before_attach)
            def other_alias():
                state['credential'] = None
                req = request('gpt-4')
                req['model'] = 'openai/gpt-4'
                req['litellm_credential_name'] = 'other-credential'
                req['litellm_metadata']['deployment'] = 'openai/gpt-4'
                assert asyncio.run(hook.async_pre_call_deployment_hook(req, 'aresponses')) is req
            check('other_alias_unchanged', other_alias)
            def mismatched_alias():
                state['credential'] = good
                expect_denied(hook.async_pre_call_deployment_hook(request('gpt-4'), 'aresponses'))
            check('protected_backend_alias_mismatch_denied', mismatched_alias)
            def base_url_override():
                state['credential'] = good
                for placement in ('top', 'metadata', 'litellm_metadata'):
                    req = request()
                    if placement == 'top':
                        req['base_url'] = 'https://attacker.invalid'
                    else:
                        req[placement] = dict(req['litellm_metadata'], base_url='https://attacker.invalid')
                    expect_denied(hook.async_pre_call_deployment_hook(req, 'aresponses'))
            check('base_url_override_denied', base_url_override)
            def no_shared_auth():
                assert state['shared'] == 0
            check('shared_auth_never_invoked', no_shared_auth)
    finally:
        litellm.callbacks = original_callbacks
    print(json.dumps({'schema': 'litellm-copilot-image-result/1', 'checks': CASES,
                      'failures': FAILURES}, sort_keys=True))
    return 0 if CASES and all(value == 'pass' for value in CASES.values()) else 1


if __name__ == '__main__':
    sys.exit(main())
