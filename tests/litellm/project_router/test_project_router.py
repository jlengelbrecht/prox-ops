"""Focused synthetic checks for SUB164; no provider or cluster calls."""

import asyncio
import copy
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[3]
APP = ROOT / "kubernetes/apps/ai/litellm/app"
CLIENT = ROOT / "scripts/litellm/claude_project.py"


def load_module(name, path):
    """Load a local module without importing installed LiteLLM."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load shared configuration or module fixtures for this test class."""
        cls.outer = yaml.safe_load((APP / "configmap.yaml").read_text())
        cls.config = yaml.safe_load(cls.outer["data"]["config.yaml"])
        cls.models = {model["model_name"]: model for model in cls.config["model_list"]}

    def test_isolated_mapping_mode_affinity_and_no_fallback(self):
        """Verify isolated mapping mode affinity and no fallback."""
        marker = self.models["project-router"]["litellm_params"]
        routing = marker["complexity_router_config"]
        self.assertEqual(marker["model"], "auto_router/complexity_router")
        self.assertEqual((routing["classifier_type"], routing["adaptive"],
                          routing["session_affinity"], routing["classification_mode"]),
                         ("heuristic", False, True, "user_turn"))
        self.assertEqual(routing["tiers"], {
            "SIMPLE": "project-chatgpt-responses", "MEDIUM": "project-chatgpt-responses",
            "COMPLEX": "project-claude-native", "REASONING": "project-claude-native"})
        for name in ("project-router", "project-chatgpt-responses", "project-claude-native"):
            self.assertNotIn("fallbacks", self.models[name])
            self.assertNotIn("api_key", self.models[name]["litellm_params"])
            self.assertNotIn("api_base", self.models[name]["litellm_params"])
        self.assertEqual(self.models["project-chatgpt-responses"]["model_info"]["mode"], "responses")
        self.assertEqual(self.models["project-chatgpt-responses"]["litellm_params"]["model"],
                         "chatgpt/gpt-6.1-sol")
        self.assertEqual(self.models["project-claude-native"]["litellm_params"]["model"],
                         "anthropic/claude-sonnet-5-5")
        self.assertEqual(self.config["router_settings"]["num_retries"], 0)
        self.assertEqual(self.config["litellm_settings"]["model_group_settings"]
                         ["forward_client_headers_to_llm_api"], ["anthropic/*"])

    def test_existing_review_and_smart_route_are_unchanged(self):
        """Verify existing review and smart route are unchanged."""
        golden = json.loads((Path(__file__).parent / "protected-models-base.json").read_text())
        self.assertEqual(set(golden), {"merge-steward-review", "smart-router",
                                       "chatgpt-gpt-6-luna", "chatgpt-gpt-6-sol",
                                       "chatgpt-gpt-6.1-sol", "anthropic/*"})
        for name, expected in golden.items():
            self.assertEqual(self.models[name], expected)

    def test_rendered_guard_wiring(self):
        """Verify rendered guard wiring."""
        self.assertEqual(self.config["litellm_settings"]["callbacks"],
                         ["project_router_guard.proxy_handler_instance"])
        kust = yaml.safe_load((APP / "kustomization.yaml").read_text())
        self.assertIn("project_router_guard.py=callbacks/project_router_guard.py",
                      kust["configMapGenerator"][0]["files"])
        helm = yaml.safe_load((APP / "helmrelease.yaml").read_text())
        annotations = helm["spec"]["values"]["podAnnotations"]
        self.assertIn("litellm-copilot-session-guard", annotations["configmap.reloader.stakater.com/reload"])
        patch_text = helm["spec"]["postRenderers"][0]["kustomize"]["patches"][0]["patch"]
        self.assertIn("mountPath: /opt/litellm-callbacks", patch_text)
        self.assertIn("name: PYTHONPATH", patch_text)


class GuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load shared configuration or module fixtures for this test class."""
        names = ("litellm", "litellm.integrations", "litellm.integrations.custom_logger",
                 "litellm.exceptions", "litellm.llms", "litellm.llms.chatgpt",
                 "litellm.llms.chatgpt.common_utils")
        cls.old = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
        cls.AuthError = type("AuthError", (Exception,), {
            "__init__": lambda self, **kw: Exception.__init__(self, kw["message"])})
        sys.modules["litellm.exceptions"].CallerCredentialAuthenticationError = cls.AuthError
        sys.modules["litellm.llms.chatgpt.common_utils"].CHATGPT_API_BASE = (
            "https://chatgpt.com/backend-api/codex")
        cls.guard = load_module("project_router_guard_test", APP / "callbacks/project_router_guard.py")

    @classmethod
    def tearDownClass(cls):
        """Restore the module table after guard tests."""
        for name, value in cls.old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def owner(self, **changes):
        """Build synthetic owner metadata with optional overrides."""
        fields = dict(via_virtual_key=True, user_id="synthetic-owner", models=["project-router"],
                      api_key="synthetic-hash")
        fields.update(changes)
        return types.SimpleNamespace(**fields)

    def request(self, **body_changes):
        """Build a synthetic captured Messages request."""
        body = {"model": "project-router", "messages": [{"role": "user", "content": "Hi"}]}
        body.update(body_changes)
        return {"model": "project-router", "proxy_server_request": {
            "body": body, "credential_fields": ()}, "secret_fields": {"raw_headers": {
                "authorization": "Bearer sk-ant-oat-synthetic",
                "x-litellm-api-key": "sk-synthetic-gateway"}}}

    def pre(self, request, owner=None):
        """Run the synthetic pre-call or client preflight helper."""
        return asyncio.run(self.guard.proxy_handler_instance.async_pre_call_hook(
            owner or self.owner(), None, request, "acompletion"))

    def post(self, request, alias):
        """Add selected deployment fields and run its hook."""
        self.pre(request)
        request["model"] = self.guard.BACKENDS[alias]
        request["custom_llm_provider"] = self.guard.PROVIDERS[alias]
        request["metadata"] = {"deployment_model_name": alias,
                               "user_api_key_user_id": "synthetic-owner"}
        request["provider_specific_header"] = {"custom_llm_provider": "anthropic",
                                                "extra_headers": {"Authorization": "Bearer sk-ant-oat-synthetic"}}
        return asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            request, "anthropic_messages"))

    def test_owner_and_native_oauth_required_before_upstream(self):
        """Verify owner and native oauth required before upstream."""
        for owner in (self.owner(via_virtual_key=False), self.owner(user_id=""),
                      self.owner(models=[]), self.owner(models=["project-router", "gpt-4"]),
                      self.owner(api_key=None)):
            with self.subTest(owner=owner.models):
                with self.assertRaises(self.AuthError):
                    self.pre(self.request(), owner)
        for field in ("authorization", "x-litellm-api-key"):
            req = self.request()
            req["secret_fields"]["raw_headers"].pop(field)
            with self.assertRaises(self.AuthError):
                self.pre(req)

    def test_caller_overrides_rejected_but_proxy_anthropic_carrier_allowed(self):
        """Verify caller overrides rejected but proxy anthropic carrier allowed."""
        for name in ("api_key", "api_base", "base_url", "litellm_credential_name",
                     "custom_llm_provider", "provider", "provider_override", "api_type",
                     "extra_headers"):
            with self.subTest(name=name):
                req = self.request(**{name: "synthetic-override"})
                with self.assertRaises(self.AuthError):
                    self.pre(req)
        for name in ("api_key", "headers", "provider_specific_header"):
            req = self.request()
            req["proxy_server_request"]["credential_fields"] = (name,)
            with self.assertRaises(self.AuthError):
                self.pre(req)
        req = self.request()
        req["provider_specific_header"] = {"custom_llm_provider": "anthropic",
                                           "extra_headers": {"Authorization": "Bearer sk-ant-oat-synthetic"}}
        self.assertIs(self.pre(req), req)
        req = self.request()
        req["proxy_server_request"]["body"]["extra_headers"] = {"traceparent": "00-synthetic"}
        self.assertIs(self.pre(req), req)

    def test_messages_proxy_metadata_headers_are_not_caller_overrides(self):
        """Verify messages proxy metadata headers are not caller overrides."""
        req = self.request()
        cleaned = {"user-agent": "synthetic-client", "anthropic-version": "2023-06-01"}
        req["proxy_server_request"]["headers"] = cleaned
        req["proxy_server_request"]["body"]["litellm_metadata"] = {
            "headers": cleaned.copy(), "used_client_oauth_token": True}
        req["litellm_metadata"] = req["proxy_server_request"]["body"]["litellm_metadata"]
        self.assertIs(self.pre(req), req)

        for field, value in (("headers", {"authorization": "Bearer synthetic-override"}),
                             ("api_base", "https://wrong.invalid"),
                             ("extra_headers", {"Authorization": "Bearer synthetic-override"})):
            with self.subTest(field=field):
                altered = self.request()
                altered["proxy_server_request"]["headers"] = cleaned
                altered["proxy_server_request"]["body"]["litellm_metadata"] = {
                    "headers": cleaned.copy(), field: value}
                with self.assertRaises(self.AuthError):
                    self.pre(altered)
        req = self.request(metadata={"headers": cleaned})
        req["proxy_server_request"]["headers"] = cleaned
        req["proxy_server_request"]["body"]["litellm_metadata"] = {"headers": cleaned.copy()}
        with self.assertRaises(self.AuthError):
            self.pre(req)

    def test_nested_selected_credentials_and_model_binding(self):
        """Verify nested selected credentials and model binding."""
        for alias in (self.guard.CHATGPT_ALIAS, self.guard.CLAUDE_ALIAS):
            req = self.request()
            self.pre(req)
            result = self.post(req, alias)
            self.assertEqual(result["model"], self.guard.BACKENDS[alias])
            self.assertEqual("provider_specific_header" in result, alias == self.guard.CLAUDE_ALIAS)
        for mutation in (lambda r: r.update(api_base="https://wrong.invalid"),
                         lambda r: r.update(custom_llm_provider="wrong"),
                         lambda r: r["metadata"].update(deployment_model_name="wrong"),
                         lambda r: r["metadata"].pop("user_api_key_user_id"),
                         lambda r: r.update(model="chatgpt/other")):
            req = self.request()
            self.post(req, self.guard.CHATGPT_ALIAS)
            mutation(req)
            with self.assertRaises(self.AuthError):
                asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
                    req, "anthropic_messages"))

    def test_generated_chatgpt_responses_fields_need_approved_hook_sequence(self):
        """Accept SDK-resolved ChatGPT fields only after clean outer hooks."""
        req = self.request()
        self.post(req, self.guard.CHATGPT_ALIAS)
        asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            req, "acompletion"))
        req.update(api_base=self.guard.CHATGPT_API_BASE, api_key="synthetic-chatgpt-access")
        result = asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            req, "aresponses"))
        self.assertEqual(result["api_key"], "synthetic-chatgpt-access")
        self.assertNotIn("provider_specific_header", result)

        for change in (lambda r: r.update(api_base="https://wrong.invalid"),
                       lambda r: r.update(custom_llm_provider="anthropic"),
                       lambda r: r.update(provider="chatgpt"),
                       lambda r: r["proxy_server_request"].pop(
                           self.guard._PROVENANCE),
                       lambda r: r["proxy_server_request"]["body"].update(
                           api_key="synthetic-caller")):
            with self.subTest(change=change):
                bad = self.request()
                self.post(bad, self.guard.CHATGPT_ALIAS)
                asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
                    bad, "acompletion"))
                bad.update(api_base=self.guard.CHATGPT_API_BASE,
                           api_key="synthetic-chatgpt-access")
                change(bad)
                with self.assertRaises(self.AuthError):
                    asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
                        bad, "aresponses"))

    def test_system_text_blocks_become_chatgpt_completion_instructions(self):
        """Keep system text ahead of tools without altering captured Claude input."""
        original = [{"role": "system", "content": [
            {"type": "text", "text": "First instruction"},
            {"type": "text", "text": "Second instruction"}]},
            {"role": "system", "content": [
                {"type": "text", "text": "Third instruction"}]},
            {"role": "user", "content": "Do the work"},
            {"role": "assistant", "tool_calls": [{"id": "toolu_1"}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "result"}]
        req = self.request(messages=copy.deepcopy(original))
        req["messages"] = copy.deepcopy(original)
        captured = copy.deepcopy(req["proxy_server_request"]["body"])
        self.post(req, self.guard.CHATGPT_ALIAS)
        self.assertEqual(req["proxy_server_request"]["body"], captured)
        result = asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            req, "acompletion"))
        self.assertEqual(result["messages"][0]["content"],
                         "First instruction\nSecond instruction")
        self.assertEqual(result["messages"][1]["content"], "Third instruction")
        self.assertEqual(result["messages"][2:], original[2:])
        self.assertEqual(req["proxy_server_request"]["body"], captured)
        self.assertIsNot(result["messages"][0], req["proxy_server_request"]["body"]["messages"][0])
        result.update(api_base=self.guard.CHATGPT_API_BASE, api_key="synthetic-chatgpt-access")
        asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            result, "aresponses"))
        self.assertEqual(req["proxy_server_request"]["body"], captured)

        native = self.request(messages=copy.deepcopy(original))
        native["messages"] = copy.deepcopy(original)
        native_captured = copy.deepcopy(native["proxy_server_request"]["body"])
        self.post(native, self.guard.CLAUDE_ALIAS)
        asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            native, "acompletion"))
        self.assertEqual(native["messages"], original)
        self.assertEqual(native["proxy_server_request"]["body"], native_captured)
        self.assertEqual(native["provider_specific_header"]["extra_headers"]["Authorization"],
                         "Bearer sk-ant-oat-synthetic")

    def test_unsupported_system_block_fails_closed(self):
        """Reject unsupported leading system blocks without changing captured input."""
        invalid = ([], {}, None, [{"type": "image", "source": "synthetic"}],
                   [{"type": "text", "text": 7}], [{"type": "text"}],
                   [{"type": "text", "text": "ok", "extra": "unexpected"}])
        for content in invalid:
            with self.subTest(content=content):
                messages = [{"role": "system", "content": content},
                            {"role": "user", "content": "Hi"}]
                req = self.request(messages=copy.deepcopy(messages))
                req["messages"] = copy.deepcopy(messages)
                captured = copy.deepcopy(req["proxy_server_request"]["body"])
                self.post(req, self.guard.CHATGPT_ALIAS)
                with self.assertRaises(self.AuthError):
                    asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
                        req, "acompletion"))
                self.assertEqual(req["proxy_server_request"]["body"], captured)

    def test_nonstandard_messages_instructions_are_denied(self):
        """Do not silently lose a caller field the Messages API cannot carry."""
        req = self.request(instructions="Separate synthetic instruction")
        with self.assertRaises(self.AuthError):
            self.pre(req)
        self.assertEqual(req["proxy_server_request"]["body"]["instructions"],
                         "Separate synthetic instruction")

    def test_selected_provider_does_not_authorize_caller_override(self):
        """Permit the selected provider only when the original request is clean."""
        for alias in (self.guard.CHATGPT_ALIAS, self.guard.CLAUDE_ALIAS):
            req = self.request(custom_llm_provider=self.guard.PROVIDERS[alias])
            with self.subTest(alias=alias), self.assertRaises(self.AuthError):
                self.pre(req)

    def test_other_route_untouched(self):
        """Verify other route untouched."""
        req = {"model": "merge-steward-review"}
        self.assertIs(self.pre(req), req)
        self.assertIs(asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            req, "anthropic_messages")), req)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load shared configuration or module fixtures for this test class."""
        cls.client = load_module("claude_project_test", CLIENT)

    def env(self, **changes):
        """Build synthetic launcher environment values."""
        data = {"LITELLM_PROJECT_KEY": "sk-synthetic-owner-key", "PATH": "/usr/bin"}
        data.update(changes)
        return data

    def pre(self, env):
        """Run the synthetic pre-call or client preflight helper."""
        with patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"):
            return self.client.preflight(env, ROOT, ROOT, cli="/synthetic/claude",
                                         login_check=lambda cli, values: True)

    def test_missing_prerequisites_fail_without_traffic(self):
        """Verify missing prerequisites fail without traffic."""
        for env in (self.env(LITELLM_PROJECT_KEY=""), self.env(ANTHROPIC_API_KEY="sk-test"),
                    self.env(ANTHROPIC_AUTH_TOKEN="synthetic"),
                    self.env(ANTHROPIC_BASE_URL="https://wrong.invalid"),
                    self.env(ANTHROPIC_CUSTOM_HEADERS="x-api-key: synthetic"),
                    self.env(AFK_DISPATCH_ID="synthetic-worker"),
                    self.env(CLAUDE_CODE_USE_BEDROCK="1")):
            with self.subTest(names=tuple(env)):
                with patch.object(self.client.subprocess, "Popen", side_effect=AssertionError("upstream")):
                    self.assertFalse(self.pre(env)["ready"])

    def test_negative_login_record_is_diagnostic_only(self):
        """Verify negative login record is diagnostic only."""
        response = types.SimpleNamespace(returncode=0, stdout='{"loggedIn":false}')
        with (patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"),
              patch.object(self.client.subprocess, "run", return_value=response) as run):
            report = self.client.preflight(self.env(), ROOT, ROOT, cli="/synthetic/claude")
        run.assert_called_once()
        self.assertTrue(report["ready"])
        self.assertEqual(report["failures"], [])
        self.assertFalse(report["native_login_record_present"])
        self.assertFalse(report["native_login_live_verified"])

    def test_relative_claude_path_never_runs_diagnostic_or_child(self):
        """Reject a project-resolved CLI before it sees even login context."""
        for cli in ("bin/claude", "./.venv/bin/claude", "claude"):
            with self.subTest(cli=cli), patch.object(self.client.subprocess, "run",
                                                    side_effect=AssertionError("diagnostic ran")):
                report = self.client.preflight(self.env(), ROOT, ROOT, cli=cli)
                self.assertFalse(report["ready"])
                self.assertIn("native_claude_cli_missing", report["failures"])
        with (patch.object(self.client.os, "environ", self.env()),
              patch.object(self.client.shutil, "which", return_value="bin/claude"),
              patch.object(self.client.subprocess, "run", side_effect=AssertionError("diagnostic ran")),
              patch.object(self.client.os, "execvpe", side_effect=AssertionError("child ran")),
              patch.object(self.client, "_bounded_smoke", side_effect=AssertionError("smoke ran")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(self.client.main([]), 2)
        self.assertIn("native_claude_cli_missing", json.loads(output.getvalue())["failures"])

    def test_native_install_only_and_no_project_executable_diagnostic(self):
        """Reject absolute project binaries and symlinks before the auth diagnostic."""
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            project = home / "project"
            project.mkdir()
            project_cli = project / "claude"
            project_cli.write_text("#!/bin/sh\nexit 99\n")
            project_cli.chmod(0o755)
            versions = home / ".local/share/claude/versions"
            versions.mkdir(parents=True)
            native = versions / "2.1.319"
            native.write_text("#!/bin/sh\nexit 0\n")
            native.chmod(0o755)
            binary_dir = home / ".local/bin"
            binary_dir.mkdir()
            entry = binary_dir / "claude"
            entry.symlink_to(native)
            checked = []

            def login(cli, _env):
                """Record the resolved executable without starting a process."""
                checked.append(cli)
                return True

            for candidate in (str(project_cli), str(project / "missing"),
                              "bin/claude"):
                with self.subTest(candidate=candidate):
                    report = self.client.preflight(self.env(), project, home,
                                                   cli=candidate, login_check=login)
                    self.assertFalse(report["ready"])
                    self.assertIn("native_claude_cli_missing", report["failures"])
            self.assertEqual(checked, [])
            self.assertTrue(self.client.preflight(self.env(), project, home,
                                                  cli=str(entry), login_check=login)["ready"])
            self.assertEqual(checked, [str(native)])
            entry.unlink()
            entry.symlink_to(project_cli)
            report = self.client.preflight(self.env(), project, home,
                                           cli=str(entry), login_check=login)
            self.assertFalse(report["ready"])
            self.assertEqual(checked, [str(native)])
            self.assertIsNone(self.client._trusted_cli(str(entry), home))
            for unsafe in (str(project_cli), str(entry)):
                with (self.subTest(unsafe=unsafe),
                      patch.object(self.client.os, "environ", self.env()),
                      patch.object(self.client.shutil, "which", return_value=unsafe),
                      patch.object(self.client.Path, "cwd", return_value=project),
                      patch.object(self.client.Path, "home", return_value=home),
                      patch.object(self.client.subprocess, "run",
                                   side_effect=AssertionError("diagnostic ran")),
                      patch.object(self.client, "_bounded_smoke",
                                   side_effect=AssertionError("child ran")),
                      contextlib.redirect_stdout(io.StringIO()) as output):
                    self.assertEqual(self.client.main(["--dry-run"]), 2)
                self.assertIn("native_claude_cli_missing",
                              json.loads(output.getvalue())["failures"])

    def test_main_uses_canonical_native_executable_for_smoke(self):
        """Pass the validated version path to both diagnostic and launch command."""
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            native = home / ".local/share/claude/versions/2.1.319"
            native.parent.mkdir(parents=True)
            native.write_text("#!/bin/sh\nexit 0\n")
            native.chmod(0o755)
            entry = home / ".local/bin/claude"
            entry.parent.mkdir()
            entry.symlink_to(native)
            fixture = ROOT / "tests/litellm/project_router/smoke-fixture.txt"
            response = types.SimpleNamespace(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai"}')
            result = {"exit_code": 0, "bounded": True, "stream_event_seen": True,
                      "linked_read_tool": True, "final_answer_nonempty": True,
                      "terminal_success": True, "final_answer_marker": True,
                      "final_answer_fact": True}
            with (patch.object(self.client.os, "environ", self.env()),
                  patch.object(self.client.shutil, "which", return_value=str(entry)),
                  patch.object(self.client.Path, "cwd", return_value=ROOT),
                  patch.object(self.client.Path, "home", return_value=home),
                  patch.object(self.client, "_settings_issues", return_value=[]),
                  patch.object(self.client.subprocess, "run", return_value=response) as diagnostic,
                  patch.object(self.client, "_bounded_smoke", return_value=result) as smoke,
                  contextlib.redirect_stdout(io.StringIO()) as output):
                code = self.client.main(["--smoke-test", str(fixture)])
            self.assertEqual(code, 0, output.getvalue())
            self.assertEqual(diagnostic.call_args.args[0][0], str(native))
            self.assertEqual(smoke.call_args.args[0][0], str(native))

    def test_secret_only_in_child_environment_and_safe_cli(self):
        """Verify secret only in child environment and safe cli."""
        env = self.env()
        self.assertTrue(self.pre(env)["ready"])
        child = self.client._child_env(env, "LITELLM_PROJECT_KEY")
        command = self.client._command("claude", smoke=True, prompt="synthetic prompt")
        self.assertEqual(child["ANTHROPIC_MODEL"], "project-router")
        for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                     "CLAUDE_CODE_SUBAGENT_MODEL"):
            self.assertEqual(child[name], "project-router")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], self.client.GATEWAY)
        self.assertIn("x-litellm-api-key: Bearer sk-synthetic-owner-key",
                      child["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertNotIn("LITELLM_PROJECT_KEY", child)
        self.assertNotIn("sk-synthetic-owner-key", json.dumps(command))
        self.assertIn("--print", command)
        self.assertIn("--verbose", command)
        self.assertIn("--safe-mode", command)
        self.assertIn("--strict-mcp-config", command)
        self.assertNotIn("--strict-mcp-config", self.client._command("claude"))
        self.assertEqual(command[command.index("--setting-sources") + 1], "user")
        self.assertNotIn("--setting-sources", self.client._command("claude"))
        self.assertNotIn("--restricted", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--tools", command)
        self.assertIn("Read", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--bare", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_explicit_secondary_model_overrides_fail_preflight(self):
        """Reject model overrides before constructing the gateway child environment."""
        for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                     "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
                     "ANTHROPIC_SMALL_FAST_MODEL"):
            with self.subTest(name=name):
                report = self.pre(self.env(**{name: "synthetic-other-model"}))
                self.assertFalse(report["ready"])
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            home = Path(temp)
            settings = home / ".claude/settings.json"
            settings.parent.mkdir()
            for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL",
                         "ANTHROPIC_SMALL_FAST_MODEL"):
                with self.subTest(setting=name):
                    settings.write_text(json.dumps({"env": {name: "synthetic-other-model"}}))
                    self.assertIn("settings_credential_or_provider_env",
                                  self.client._settings_issues(home, home))

    def test_login_record_receives_no_project_credentials_and_dry_run_is_secret_free(self):
        """Verify login record receives no project credentials and dry run is secret free."""
        env = self.env(PROJECT_SECRET="sk-synthetic-extra", LITELLM_PROJECT_OTHER="synthetic",
                       HOME="/synthetic/home", XDG_CONFIG_HOME="/synthetic/config")
        response = types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "loggedIn": True, "authMethod": "claude.ai"}))
        with (patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"),
              patch.object(self.client.subprocess, "run", return_value=response) as run):
            report = self.client.preflight(env, ROOT, ROOT, cli="/synthetic/claude")
        self.assertTrue(report["ready"])
        self.assertFalse(report["native_login_live_verified"])
        sent = run.call_args.kwargs["env"]
        self.assertEqual(sent, {"PATH": "/usr/bin", "HOME": "/synthetic/home",
                                "XDG_CONFIG_HOME": "/synthetic/config"})
        self.assertNotIn("sk-synthetic-owner-key", json.dumps(report))
        with (patch.object(self.client.os, "environ", env),
              patch.object(self.client.shutil, "which", return_value="/synthetic/claude"),
              patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"),
              patch.object(self.client.Path, "cwd", return_value=ROOT),
              patch.object(self.client.Path, "home", return_value=ROOT),
              patch.object(self.client.subprocess, "run", return_value=response),
              patch.object(self.client.os, "execvpe", side_effect=AssertionError("launch")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(self.client.main(["--dry-run"]), 0)
        self.assertNotIn("sk-synthetic-owner-key", output.getvalue())

    def test_project_settings_local_override_blocks_launch(self):
        """Verify project settings local override blocks launch."""
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            settings = project / ".claude" / "settings.local.json"
            settings.parent.mkdir()
            for data, expected in (({"env": {"ANTHROPIC_BASE_URL": "https://wrong.invalid"}},
                                    "settings_credential_or_provider_env"),
                                   ({"provider": "synthetic-override"},
                                    "settings_provider_or_model_override")):
                with self.subTest(expected=expected):
                    settings.write_text(json.dumps(data))
                    with (patch.object(self.client.os, "environ", self.env()),
                          patch.object(self.client.shutil, "which", return_value="/synthetic/claude"),
                          patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"),
                          patch.object(self.client.Path, "cwd", return_value=project),
                          patch.object(self.client.Path, "home", return_value=project),
                          patch.object(self.client, "_native_login_record", return_value=True),
                          patch.object(self.client.os, "execvpe", side_effect=AssertionError("launch")),
                          patch.object(self.client, "_bounded_smoke", side_effect=AssertionError("launch")),
                          contextlib.redirect_stdout(io.StringIO()) as output):
                        self.assertEqual(self.client.main([]), 2)
                    self.assertIn(expected, json.loads(output.getvalue())["failures"])

    def test_project_settings_env_fail_closed_without_blocking_user_settings(self):
        """Verify project settings env fail closed without blocking user settings."""
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            home = Path(temp)
            project = home / "project"
            (home / ".claude").mkdir()
            (project / ".claude").mkdir(parents=True)
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"env": {"KUBECONFIG": "synthetic"}}))
            settings = project / ".claude" / "settings.json"
            self.assertEqual(self.client._settings_issues(project, home), [])
            for name in ("HTTPS_PROXY", "LD_PRELOAD", "BASH_ENV", "NODE_OPTIONS",
                         "PATH", "FUTURE_UNRECOGNIZED_KEY"):
                with self.subTest(name=name):
                    settings.write_text(json.dumps({"env": {name: "synthetic"}}))
                    self.assertIn("settings_credential_or_provider_env",
                                  self.client._settings_issues(project, home))
            settings.write_text(json.dumps({"env": {}}))
            self.assertEqual(self.client._settings_issues(project, home), [])

    def test_interactive_launch_requires_tty_for_native_trust(self):
        """Verify interactive launch requires tty for native trust."""
        with (patch.object(self.client.os, "environ", self.env()),
              patch.object(self.client.shutil, "which", return_value="/synthetic/claude"),
              patch.object(self.client, "_trusted_cli", return_value="/synthetic/claude"),
              patch.object(self.client.Path, "cwd", return_value=ROOT),
              patch.object(self.client.Path, "home", return_value=ROOT),
              patch.object(self.client, "_native_login_record", return_value=True),
              patch.object(self.client.os, "execvpe", side_effect=AssertionError("launch")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(self.client.main([]), 2)
        self.assertEqual(json.loads(output.getvalue())["failure"],
                         "interactive_tty_required_for_workspace_trust")

    def test_smoke_fixture_is_bounded(self):
        """Verify smoke fixture is bounded."""
        fixture = ROOT / "tests/litellm/project_router/smoke-fixture.txt"
        self.assertIn("Read once", self.client._smoke_prompt(fixture, ROOT))
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            fixture = Path(temp) / "fact.txt"
            fixture.write_text("synthetic fact")
            with self.assertRaises(ValueError):
                self.client._smoke_prompt(fixture, ROOT)
            with self.assertRaises(ValueError):
                self.client._smoke_prompt(Path(temp), ROOT)
            fixture.write_bytes(b"x" * 1025)
            with self.assertRaises(ValueError):
                self.client._smoke_prompt(fixture, ROOT)

    def test_settings_preflight_rejects_special_and_oversized_files(self):
        """Verify settings preflight rejects special and oversized files."""
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            project = Path(temp)
            settings = project / ".claude" / "settings.json"
            settings.parent.mkdir()
            for kind in ("fifo", "device", "oversize", "symlink_oversize"):
                with self.subTest(kind=kind):
                    settings.unlink(missing_ok=True)
                    if kind == "fifo":
                        os.mkfifo(settings)
                    elif kind == "device":
                        settings.symlink_to("/dev/zero")
                    elif kind == "oversize":
                        settings.write_bytes(b" " * (self.client.SETTINGS_MAX_BYTES + 1))
                    else:
                        target = project / "large.json"
                        target.write_bytes(b" " * (self.client.SETTINGS_MAX_BYTES + 1))
                        settings.symlink_to(target)
                    result = self.client._settings_issues(project, project)
                    self.assertIn("settings_unreadable_or_invalid", result)
            settings.unlink()
            settings.write_text('{"hooks":{"SessionStart":[]}}')
            self.assertEqual(self.client._settings_issues(project, project), [])

    def test_smoke_evidence_requires_stream_linked_read_and_final_answer(self):
        """Verify smoke evidence requires stream linked read and final answer."""
        fixture = ROOT / "tests/litellm/project_router/smoke-fixture.txt"
        events = [
            {"type": "stream_event", "event": {"type": "content_block_delta"}},
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "id": "tool-synthetic",
                 "input": {"file_path": str(fixture.relative_to(ROOT))}}]}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "tool-synthetic", "is_error": False}]}},
            {"type": "result", "subtype": "success", "is_error": False,
             "result": "PROJECT_ROUTER_SMOKE cedar"},
        ]
        parse = lambda items: self.client._smoke_evidence(
            (json.dumps(item) for item in items), fixture, ROOT)
        self.assertEqual(parse(events), {"stream_event_seen": True,
                                         "linked_read_tool": True,
                                         "final_answer_nonempty": True,
                                         "terminal_success": True,
                                         "final_answer_marker": True,
                                         "final_answer_fact": True})
        mutations = (
            (lambda e: e[1]["message"]["content"][0]["input"].update(file_path="wrong.txt"),
             "linked_read_tool"),
            (lambda e: e[2]["message"]["content"][0].update(is_error=True),
             "linked_read_tool"),
            (lambda e: e[2]["message"]["content"][0].update(tool_use_id="wrong"),
             "linked_read_tool"),
            (lambda e: e[3].update(result="unrelated answer"), "final_answer_marker"),
            (lambda e: e[3].update(result="cedar"), "final_answer_marker"),
            (lambda e: e[3].update(result="PROJECT_ROUTER_SMOKE other"), "final_answer_fact"),
            (lambda e: e[3].update(is_error=True), "terminal_success"),
        )
        for mutate, field in mutations:
            broken = json.loads(json.dumps(events))
            mutate(broken)
            with self.subTest(field=field, event=broken):
                self.assertFalse(parse(broken)[field])
        self.assertFalse(parse(events[:-1])["final_answer_nonempty"])
        self.assertFalse(parse(events + [{"type": "assistant"}])["terminal_success"])
        self.assertFalse(parse(events[1:])["stream_event_seen"])

    def test_bounded_smoke_terminates_descendants_on_timeout_and_output_cap(self):
        """Verify bounded smoke terminates descendants on timeout and output cap."""
        fixture = ROOT / "tests/litellm/project_router/smoke-fixture.txt"
        for overflow in (False, True):
            with self.subTest(overflow=overflow), tempfile.TemporaryDirectory(dir=ROOT) as temp:
                parent_pid = Path(temp) / "parent.pid"
                grandchild_pid = Path(temp) / "grandchild.pid"
                code = ("import os,subprocess,sys,time\n"
                        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                        f"open({str(parent_pid)!r},'w').write(str(os.getpid()))\n"
                        f"open({str(grandchild_pid)!r},'w').write(str(child.pid))\n"
                        + ("os.write(1,b'x'*2048)\n" if overflow else "")
                        + "time.sleep(60)\n")
                result = self.client._bounded_smoke(
                    [sys.executable, "-c", code], self.env(), ROOT,
                    timeout_s=1.0, output_cap=1024, fixture=fixture)
                self.assertEqual(result["failure"], "output_limit" if overflow else "timeout")
                self.assertFalse(result["bounded"])
                pid = int(parent_pid.read_text())
                descendant = int(grandchild_pid.read_text())
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
                for _ in range(20):
                    stat = Path(f"/proc/{descendant}/stat")
                    if not stat.exists() or stat.read_text().split()[2] in ("Z", "X"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("smoke descendant survived bounded failure")

    def test_bounded_smoke_interrupt_reaps_child_and_stops_descendant(self):
        """Verify bounded smoke interrupt reaps child and stops descendant."""
        fixture = ROOT / "tests/litellm/project_router/smoke-fixture.txt"
        for interruption in (KeyboardInterrupt, RuntimeError):
            with self.subTest(interruption=interruption.__name__), tempfile.TemporaryDirectory(dir=ROOT) as temp:
                parent_pid = Path(temp) / "parent.pid"
                grandchild_pid = Path(temp) / "grandchild.pid"
                code = ("import os,subprocess,sys,time\n"
                        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                        f"open({str(parent_pid)!r},'w').write(str(os.getpid()))\n"
                        f"open({str(grandchild_pid)!r},'w').write(str(child.pid))\n"
                        "time.sleep(60)\n")
                selector_type = self.client.selectors.DefaultSelector

                def interrupt_select(selector, timeout=None):
                    """Raise after the detached child and descendant exist."""
                    deadline = time.monotonic() + 2
                    while not (parent_pid.exists() and grandchild_pid.exists()):
                        if time.monotonic() > deadline:
                            self.fail("smoke process did not start")
                        time.sleep(0.01)
                    raise interruption("synthetic interruption")

                with patch.object(selector_type, "select", interrupt_select):
                    with self.assertRaises(interruption):
                        self.client._bounded_smoke([sys.executable, "-c", code],
                                                   self.env(), ROOT, fixture=fixture)
                pid = int(parent_pid.read_text())
                descendant = int(grandchild_pid.read_text())
                with self.assertRaises(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
                for _ in range(20):
                    stat = Path(f"/proc/{descendant}/stat")
                    if not stat.exists() or stat.read_text().split()[2] in ("Z", "X"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("smoke descendant survived interruption")

    def test_selector_setup_failure_reaps_detached_child(self):
        """A failure immediately after Popen still stops and reaps the child."""
        spawned = []
        real_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            """Capture the real detached process for reap assertions."""
            process = real_popen(*args, **kwargs)
            spawned.append(process)
            return process

        for failure in (OSError, KeyboardInterrupt):
            with self.subTest(failure=failure.__name__):
                with (patch.object(self.client.subprocess, "Popen", side_effect=capture_popen),
                      patch.object(self.client.selectors, "DefaultSelector",
                                   side_effect=failure("selector setup"))):
                    with self.assertRaises(failure):
                        self.client._bounded_smoke(
                            [sys.executable, "-c", "import time;time.sleep(60)"],
                            self.env(), ROOT, fixture=ROOT / "tests/litellm/project_router/smoke-fixture.txt")
                self.assertIsNotNone(spawned[-1].returncode)
                with self.assertRaises(ChildProcessError):
                    os.waitpid(spawned[-1].pid, os.WNOHANG)

    def test_sigterm_stops_smoke_descendant(self):
        """Verify sigterm stops smoke descendant."""
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            descendant_pid = Path(temp) / "descendant.pid"
            child_code = ("import subprocess,sys,time\n"
                          "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
                          f"open({str(descendant_pid)!r},'w').write(str(p.pid))\n"
                          "time.sleep(60)\n")
            launcher_code = ("from scripts.litellm.claude_project import _bounded_smoke\n"
                             "import sys\n"
                             f"_bounded_smoke([sys.executable,'-c',{child_code!r}],{{}},'.')\n")
            launcher = subprocess.Popen([sys.executable, "-c", launcher_code], cwd=ROOT,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 3
                while not descendant_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(descendant_pid.exists(), "smoke child did not start")
                launcher.terminate()
                self.assertEqual(launcher.wait(timeout=3), 128 + 15)
                descendant = int(descendant_pid.read_text())
                for _ in range(20):
                    status = Path(f"/proc/{descendant}/stat")
                    if not status.exists() or status.read_text().split()[2] in ("Z", "X"):
                        break
                    time.sleep(0.05)
                else:
                    self.fail("smoke descendant survived SIGTERM")
            finally:
                if launcher.poll() is None:
                    launcher.kill()
                    launcher.wait()


if __name__ == "__main__":
    unittest.main()
