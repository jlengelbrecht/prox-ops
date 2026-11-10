"""Synthetic owner and deployment boundary tests for the isolated Grok alias."""

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "kubernetes/apps/ai/litellm/app"


class GrokTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = ("httpx", "litellm", "litellm.exceptions", "litellm.integrations",
                 "litellm.integrations.custom_logger", "litellm.llms", "litellm.llms.xai",
                 "litellm.llms.xai.oauth")
        cls.old = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        # Only the HTTP client shape is needed: network calls are replaced by
        # synthetic clients in the tests that exercise identity resolution.
        sys.modules["httpx"].Timeout = lambda *_args, **_kwargs: None
        sys.modules["httpx"].AsyncClient = type("UnusedHTTPClient", (), {})
        sys.modules["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
        cls.AuthError = type("AuthError", (Exception,), {
            "__init__": lambda self, **kw: Exception.__init__(self, kw["message"])})
        sys.modules["litellm.exceptions"].CallerCredentialAuthenticationError = cls.AuthError
        sys.modules["litellm.llms.xai.oauth"].XAIOAuthAuthenticator = type("Auth", (), {})
        spec = importlib.util.spec_from_file_location(
            "project_grok_native_test", APP / "callbacks/project_grok_native.py")
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)
        cls.hook = cls.module.proxy_handler_instance

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def request(self):
        return {"model": "project-grok-native", "proxy_server_request": {
            "body": {"model": "project-grok-native", "messages": [
                {"role": "user", "content": "synthetic"}]}, "credential_fields": ()},
            "secret_fields": {"raw_headers": {
                "authorization": "Bearer sk-ant-oat-synthetic",
                "x-litellm-api-key": "Bearer synthetic-owner-key"}}}

    def owner(self, **changes):
        data = dict(via_virtual_key=True, user_id="synthetic-owner",
                    models=["project-grok-native"], api_key="synthetic-hash")
        data.update(changes)
        return types.SimpleNamespace(**data)

    def pre(self, request, owner=None):
        return asyncio.run(self.hook.async_pre_call_hook(
            owner or self.owner(), None, request, "anthropic_messages"))

    def post(self, request):
        request.setdefault("provider_specific_header", {
            "custom_llm_provider": "anthropic",
            "extra_headers": {"authorization": "Bearer sk-ant-oat-synthetic"},
        })
        request["model"] = "xai/grok-4.7-build-fast"
        request["custom_llm_provider"] = "xai"
        request["use_xai_oauth"] = False  # Pinned Router default for this deployment.
        request["metadata"] = {"deployment_model_name": "project-grok-native",
                               "user_api_key_user_id": "synthetic-owner"}
        async def identity():
            return "synthetic-oauth-token", "synthetic-subject"
        with patch.object(self.module, "_resolve_identity", identity):
            return asyncio.run(self.hook.async_pre_call_deployment_hook(request, "acompletion"))

    def test_owner_and_caller_overrides_denied(self):
        for owner in (self.owner(models=["project-router"]), self.owner(models=[]),
                      self.owner(via_virtual_key=False), self.owner(user_id=""),
                      self.owner(api_key=None)):
            with self.assertRaises(self.AuthError):
                self.pre(self.request(), owner)
        for field in self.module._FORBIDDEN:
            request = self.request()
            request["proxy_server_request"]["body"][field] = "synthetic-override"
            with self.subTest(field=field), self.assertRaises(self.AuthError):
                self.pre(request)
        for field in ("authorization", "x-litellm-api-key"):
            request = self.request()
            del request["secret_fields"]["raw_headers"][field]
            with self.assertRaises(self.AuthError):
                self.pre(request)

    def test_same_token_and_subject_in_fixed_deployment(self):
        request = self.request()
        self.pre(request)
        self.assertIs(request["proxy_server_request"][self.module._PROOF],
                      copy.deepcopy(request)["proxy_server_request"][self.module._PROOF])
        result = self.post(request)
        self.assertEqual(result["api_key"], "synthetic-oauth-token")
        self.assertEqual(result["api_base"], self.module.BASE)
        self.assertEqual(result["extra_headers"]["x-userid"], "synthetic-subject")
        self.assertEqual(result["extra_headers"]["x-grok-client-version"], "1.0.77")
        self.assertEqual(result["extra_headers"]["x-grok-model-override"], "grok-4.7-build-fast")
        self.assertIs(result["use_xai_oauth"], False)

    def test_pinned_snapshot_refresh_and_shallow_hook_copy(self):
        request = self.request()
        self.pre(request)
        carrier = request["proxy_server_request"]
        # common_request_processing refreshes the body after request hooks.
        # utils.async_pre_call_deployment_hook then copies only the top level.
        carrier["body"] = {**carrier["body"], "litellm_metadata": {
            "requester_metadata": {}, "generated_trace": "synthetic"}}
        deployment = dict(request)
        self.assertIs(deployment["proxy_server_request"], carrier)
        result = self.post(deployment)
        self.assertEqual(result["api_key"], "synthetic-oauth-token")
        self.assertNotIn("provider_specific_header", result)

        replay = copy.deepcopy(result)
        with self.assertRaises(self.AuthError):
            self._invoke_post(replay)

    def test_repeated_deployment_hook_keeps_first_native_binding(self):
        request = self.request()
        self.pre(request)
        first = self.post(request)
        async def no_second_lookup():
            self.fail("identity lookup repeated")
        with patch.object(self.module, "_resolve_identity", no_second_lookup):
            second = asyncio.run(self.hook.async_pre_call_deployment_hook(
                dict(first), "acompletion"))
        self.assertEqual(second["api_key"], first["api_key"])
        self.assertEqual(second["extra_headers"], first["extra_headers"])
        self.assertNotIn("provider_specific_header", second)

    def test_repeated_hook_rejects_tampering_and_replay(self):
        for mutate in (
                lambda r: r.update(api_key="synthetic-other-token"),
                lambda r: r.update(api_base="https://wrong.invalid"),
                lambda r: r["extra_headers"].update({"x-userid": "other-subject"}),
                lambda r: r["extra_headers"].update({"authorization": "Bearer sk-ant-oat-fake"}),
                lambda r: r.update(provider_specific_header={"custom_llm_provider": "anthropic",
                    "extra_headers": {"authorization": "Bearer sk-ant-oat-fake"}}),
                lambda r: r.update(model="xai/other"),
                lambda r: r["metadata"].update(deployment_model_name="other"),
                lambda r: r["metadata"].update(user_api_key_user_id="other-owner"),
                lambda r: r["proxy_server_request"]["body"].update(messages=[]),
                lambda r: r["proxy_server_request"].update(credential_fields=("api_key",)),
                lambda r: r["proxy_server_request"].pop(self.module._PROOF),
        ):
            request = self.request()
            self.pre(request)
            self.post(request)
            repeated = dict(request)
            repeated["metadata"] = copy.deepcopy(request["metadata"])
            repeated["extra_headers"] = copy.deepcopy(request["extra_headers"])
            mutate(repeated)
            with self.subTest(mutate=mutate), self.assertRaises(self.AuthError):
                self._invoke_post(repeated)

    def test_unbound_hook_requires_generated_carrier(self):
        request = self.request()
        self.pre(request)
        request.update(model=self.module.BACKEND, custom_llm_provider="xai",
                       use_xai_oauth=False, metadata={
                           "deployment_model_name": self.module.ALIAS,
                           "user_api_key_user_id": "synthetic-owner"})
        with self.assertRaises(self.AuthError):
            self._invoke_post(request)

    def test_xai_oauth_mode_only_from_selected_deployment(self):
        for value in (False, True, None, "false", 0):
            request = self.request()
            request["proxy_server_request"]["body"]["use_xai_oauth"] = value
            with self.subTest(caller_value=value), self.assertRaises(self.AuthError):
                self.pre(request)
        for value in (True, None, "false", 0):
            request = self.request()
            self.pre(request)
            request["model"] = self.module.BACKEND
            request["custom_llm_provider"] = "xai"
            request["use_xai_oauth"] = value
            request["metadata"] = {"deployment_model_name": self.module.ALIAS,
                                   "user_api_key_user_id": "synthetic-owner"}
            with self.subTest(deployment_value=value), self.assertRaises(self.AuthError):
                self._invoke_post(request)

    def test_missing_proof_selected_model_and_generated_overrides(self):
        request = self.request()
        with self.assertRaises(self.AuthError):
            self.post(request)
        for mutate in (
                lambda r: r["metadata"].update(deployment_model_name="gpt-4"),
                lambda r: r.update(model="xai/other"),
                lambda r: r.update(api_base="https://wrong.invalid"),
                lambda r: r.update(api_key="synthetic-attacker"),
                lambda r: r["metadata"].pop("user_api_key_user_id")):
            request = self.request()
            self.pre(request)
            request["model"] = "xai/grok-4.7-build-fast"
            request["use_xai_oauth"] = False
            request["metadata"] = {"deployment_model_name": "project-grok-native",
                                   "user_api_key_user_id": "synthetic-owner"}
            mutate(request)
            with self.assertRaises(self.AuthError):
                self._invoke_post(request)

    def _invoke_post(self, request):
        async def identity():
            return "synthetic-oauth-token", "synthetic-subject"
        with patch.object(self.module, "_resolve_identity", identity):
            return asyncio.run(self.hook.async_pre_call_deployment_hook(request, "acompletion"))

    def test_other_models_unchanged(self):
        request = {"model": "gpt-4"}
        self.assertIs(self.pre(request), request)
        self.assertIs(asyncio.run(self.hook.async_pre_call_deployment_hook(
            request, "acompletion")), request)

    def test_provider_error_is_fixed_and_scoped(self):
        class HTTPException(Exception):
            def __init__(self, status_code, detail):
                self.status_code, self.detail = status_code, detail
        fastapi = types.ModuleType("fastapi")
        fastapi.HTTPException = HTTPException
        request = self.request()
        self.pre(request)
        raw = RuntimeError("synthetic-oauth-token synthetic-subject")
        with patch.dict(sys.modules, {"fastapi": fastapi}):
            safe = asyncio.run(self.hook.async_post_call_failure_hook(
                request, raw, self.owner()))
            self.assertEqual((safe.status_code, safe.detail),
                             (502, "Project Grok provider failed"))
            self.assertNotIn("synthetic", str(safe.detail))
            self.assertIsNone(asyncio.run(self.hook.async_post_call_failure_hook(
                {"model": "project-router"}, raw, self.owner())))

    def test_refresh_identity_failure_and_concurrent_file_change(self):
        state = {"token": "synthetic-refreshed-token", "called": 0}
        class Auth:
            def get_access_token(self):
                state["called"] += 1
                return state["token"]
        class Response:
            content = b'{"sub":"synthetic-subject"}'
            def raise_for_status(self):
                return None
            async def aiter_bytes(self):
                yield self.content
        class Stream:
            async def __aenter__(self):
                return Response()
            async def __aexit__(self, *_):
                return None
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return None
            def stream(self, method, url, headers):
                self.request = (method, url, headers)
                state["token"] = "synthetic-other-account-token"
                return Stream()
        async def immediate(fn):
            # This host's Python 3.14/3.15 asyncio executor shutdown stalls;
            # the pinned image gate exercises the real off-loop operation.
            return fn()
        with patch.object(self.module, "XAIOAuthAuthenticator", Auth), \
             patch.object(self.module.httpx, "AsyncClient", Client), \
             patch.object(self.module.asyncio, "to_thread", immediate):
            token, subject = asyncio.run(self.module._resolve_identity())
        self.assertEqual((token, subject), ("synthetic-refreshed-token", "synthetic-subject"))
        self.assertEqual(state["called"], 1)

        class FailedAuth:
            def get_access_token(self):
                raise RuntimeError("synthetic-secret-must-not-escape")
        with patch.object(self.module, "XAIOAuthAuthenticator", FailedAuth), \
             patch.object(self.module.asyncio, "to_thread", immediate):
            with self.assertRaises(self.AuthError) as caught:
                asyncio.run(self.module._resolve_identity())
        self.assertEqual(str(caught.exception), "Project Grok request rejected")

    def test_failed_identity_lookup_is_redacted(self):
        class Auth:
            def get_access_token(self):
                return "synthetic-oauth-token"
        class Response:
            def raise_for_status(self):
                return None
            async def aiter_bytes(self):
                yield b'{"sub":""}'
        class Stream:
            async def __aenter__(self):
                return Response()
            async def __aexit__(self, *_):
                return None
        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_):
                return None
            def stream(self, *_args, **_kwargs):
                return Stream()
        async def immediate(fn):
            return fn()
        with patch.object(self.module, "XAIOAuthAuthenticator", Auth), \
             patch.object(self.module.httpx, "AsyncClient", Client), \
             patch.object(self.module.asyncio, "to_thread", immediate):
            with self.assertRaises(self.AuthError) as caught:
                asyncio.run(self.module._resolve_identity())
        self.assertEqual(str(caught.exception), "Project Grok request rejected")

    def test_duplicate_x_api_key_denied(self):
        request = self.request()
        request["secret_fields"]["raw_headers"].update({
            "x-api-key": "synthetic-one", "X-API-Key": "synthetic-two"})
        with self.assertRaises(self.AuthError):
            self.pre(request)

    def test_duplicate_proxy_authorization_denied(self):
        request = self.request()
        request["secret_fields"]["raw_headers"].update({
            "proxy-authorization": "synthetic-one",
            "Proxy-Authorization": "synthetic-two"})
        with self.assertRaises(self.AuthError):
            self.pre(request)

    def test_generated_anthropic_carrier_removed(self):
        request = self.request()
        self.pre(request)
        request["provider_specific_header"] = {
            "custom_llm_provider": "anthropic",
            "extra_headers": {"authorization": "Bearer sk-ant-oat-synthetic"},
        }
        result = self.post(request)
        self.assertNotIn("provider_specific_header", result)
        self.assertEqual(result["api_key"], "synthetic-oauth-token")
        self.assertNotIn("authorization", result["extra_headers"])

    def test_pinned_two_entry_carrier_removed_before_xai(self):
        request = self.request()
        self.pre(request)
        request["provider_specific_header"] = [
            {"custom_llm_provider": "anthropic,bedrock,bedrock_mantle,vertex_ai",
             "extra_headers": {"anthropic-version": "2023-06-01"}},
            {"custom_llm_provider": "anthropic",
             "extra_headers": {"authorization": "Bearer sk-ant-oat-synthetic"}},
        ]
        result = self.post(request)
        self.assertNotIn("provider_specific_header", result)
        self.assertEqual(result["api_key"], "synthetic-oauth-token")
        self.assertEqual(result["api_base"], self.module.BASE)
        self.assertNotIn("authorization", result["extra_headers"])
        self.assertNotIn("anthropic-version", result["extra_headers"])

    def test_malformed_generated_carriers_denied_before_identity(self):
        api = {"custom_llm_provider": "anthropic,bedrock,bedrock_mantle,vertex_ai",
               "extra_headers": {"anthropic-version": "2023-06-01"}}
        oauth = {"custom_llm_provider": "anthropic",
                 "extra_headers": {"authorization": "Bearer sk-ant-oat-synthetic"}}
        invalid = (
            [], [api], [api, oauth, oauth], [api, api], [oauth, oauth],
            [oauth, api],
            [api, "invalid"], [api, None],
            [{**api, "custom_llm_provider": "xai"}, oauth],
            [{**api, "custom_llm_provider": "anthropic,bedrock"}, oauth],
            [{**api, "extra_headers": {"authorization": "synthetic-override"}}, oauth],
            [api, {**oauth, "extra_headers": {"x-api-key": "synthetic-override"}}],
            [api, {**oauth, "extra_headers": {}}],
            [api, {**oauth, "extra_headers": ["authorization"]}],
            [api, {**oauth, "extra_headers": {"authorization": None}}],
            [api, {**oauth, "extra_headers": {"authorization": "Bearer other-token"}}],
            [api, {**oauth, "unexpected": "synthetic-override"}],
            {"custom_llm_provider": "anthropic", "extra_headers": {
                "authorization": "synthetic", "Authorization": "synthetic"}},
        )
        for carrier in invalid:
            request = self.request()
            self.pre(request)
            request["provider_specific_header"] = carrier
            with self.subTest(carrier=carrier), self.assertRaises(self.AuthError):
                self.post(request)
