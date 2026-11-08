"""Focused synthetic checks for SUB164; no provider or cluster calls."""

import asyncio
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
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
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outer = yaml.safe_load((APP / "configmap.yaml").read_text())
        cls.config = yaml.safe_load(cls.outer["data"]["config.yaml"])
        cls.models = {model["model_name"]: model for model in cls.config["model_list"]}

    def test_isolated_mapping_mode_affinity_and_no_fallback(self):
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
        golden = json.loads((Path(__file__).parent / "protected-models-base.json").read_text())
        self.assertEqual(set(golden), {"merge-steward-review", "smart-router",
                                       "chatgpt-gpt-6-luna", "chatgpt-gpt-6-sol",
                                       "chatgpt-gpt-6.1-sol", "anthropic/*"})
        for name, expected in golden.items():
            self.assertEqual(self.models[name], expected)

    def test_rendered_guard_wiring(self):
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
        names = ("litellm", "litellm.integrations", "litellm.integrations.custom_logger",
                 "litellm.exceptions")
        cls.old = {name: sys.modules.get(name) for name in names}
        for name in names:
            sys.modules[name] = types.ModuleType(name)
        sys.modules["litellm.integrations.custom_logger"].CustomLogger = type("CustomLogger", (), {})
        cls.AuthError = type("AuthError", (Exception,), {
            "__init__": lambda self, **kw: Exception.__init__(self, kw["message"])})
        sys.modules["litellm.exceptions"].CallerCredentialAuthenticationError = cls.AuthError
        cls.guard = load_module("project_router_guard_test", APP / "callbacks/project_router_guard.py")

    @classmethod
    def tearDownClass(cls):
        for name, value in cls.old.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value

    def owner(self, **changes):
        fields = dict(via_virtual_key=True, user_id="synthetic-owner", models=["project-router"],
                      api_key="synthetic-hash")
        fields.update(changes)
        return types.SimpleNamespace(**fields)

    def request(self, **body_changes):
        body = {"model": "project-router", "messages": [{"role": "user", "content": "Hi"}]}
        body.update(body_changes)
        return {"model": "project-router", "proxy_server_request": {
            "body": body, "credential_fields": ()}, "secret_fields": {"raw_headers": {
                "authorization": "Bearer sk-ant-oat-synthetic",
                "x-litellm-api-key": "sk-synthetic-gateway"}}}

    def pre(self, request, owner=None):
        return asyncio.run(self.guard.proxy_handler_instance.async_pre_call_hook(
            owner or self.owner(), None, request, "acompletion"))

    def post(self, request, alias):
        request["model"] = self.guard.BACKENDS[alias]
        request["metadata"] = {"deployment_model_name": alias,
                               "user_api_key_user_id": "synthetic-owner"}
        request["provider_specific_header"] = {"custom_llm_provider": "anthropic",
                                                "extra_headers": {"Authorization": "Bearer sk-ant-oat-synthetic"}}
        return asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            request, "anthropic_messages"))

    def test_owner_and_native_oauth_required_before_upstream(self):
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
        for alias in (self.guard.CHATGPT_ALIAS, self.guard.CLAUDE_ALIAS):
            req = self.request()
            self.pre(req)
            result = self.post(req, alias)
            self.assertEqual(result["model"], self.guard.BACKENDS[alias])
            self.assertEqual("provider_specific_header" in result, alias == self.guard.CLAUDE_ALIAS)
        for mutation in (lambda r: r.update(api_base="https://wrong.invalid"),
                         lambda r: r["metadata"].update(deployment_model_name="wrong"),
                         lambda r: r["metadata"].pop("user_api_key_user_id"),
                         lambda r: r.update(model="chatgpt/other")):
            req = self.request()
            self.post(req, self.guard.CHATGPT_ALIAS)
            mutation(req)
            with self.assertRaises(self.AuthError):
                asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
                    req, "anthropic_messages"))

    def test_other_route_untouched(self):
        req = {"model": "merge-steward-review"}
        self.assertIs(self.pre(req), req)
        self.assertIs(asyncio.run(self.guard.proxy_handler_instance.async_pre_call_deployment_hook(
            req, "anthropic_messages")), req)


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = load_module("claude_project_test", CLIENT)

    def env(self, **changes):
        data = {"LITELLM_PROJECT_KEY": "sk-synthetic-owner-key", "PATH": "/usr/bin"}
        data.update(changes)
        return data

    def pre(self, env):
        return self.client.preflight(env, ROOT, ROOT, cli="/synthetic/claude",
                                     login_check=lambda cli, values: True)

    def test_missing_prerequisites_fail_without_traffic(self):
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
        response = types.SimpleNamespace(returncode=0, stdout='{"loggedIn":false}')
        with patch.object(self.client.subprocess, "run", return_value=response) as run:
            report = self.client.preflight(self.env(), ROOT, ROOT, cli="/synthetic/claude")
        run.assert_called_once()
        self.assertTrue(report["ready"])
        self.assertEqual(report["failures"], [])
        self.assertFalse(report["native_login_record_present"])
        self.assertFalse(report["native_login_live_verified"])

    def test_secret_only_in_child_environment_and_safe_cli(self):
        env = self.env()
        self.assertTrue(self.pre(env)["ready"])
        child = self.client._child_env(env, "LITELLM_PROJECT_KEY")
        command = self.client._command("claude", smoke=True, prompt="synthetic prompt")
        self.assertEqual(child["ANTHROPIC_MODEL"], "project-router")
        self.assertEqual(child["ANTHROPIC_BASE_URL"], self.client.GATEWAY)
        self.assertIn("x-litellm-api-key: Bearer sk-synthetic-owner-key",
                      child["ANTHROPIC_CUSTOM_HEADERS"])
        self.assertNotIn("LITELLM_PROJECT_KEY", child)
        self.assertNotIn("sk-synthetic-owner-key", json.dumps(command))
        self.assertIn("--print", command)
        self.assertIn("--verbose", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertIn("--tools", command)
        self.assertIn("Read", command)
        self.assertIn("--no-session-persistence", command)
        self.assertNotIn("--bare", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_login_record_receives_no_project_credentials_and_dry_run_is_secret_free(self):
        env = self.env(PROJECT_SECRET="sk-synthetic-extra", LITELLM_PROJECT_OTHER="synthetic",
                       HOME="/synthetic/home", XDG_CONFIG_HOME="/synthetic/config")
        response = types.SimpleNamespace(returncode=0, stdout=json.dumps({
            "loggedIn": True, "authMethod": "claude.ai"}))
        with patch.object(self.client.subprocess, "run", return_value=response) as run:
            report = self.client.preflight(env, ROOT, ROOT, cli="/synthetic/claude")
        self.assertTrue(report["ready"])
        self.assertFalse(report["native_login_live_verified"])
        sent = run.call_args.kwargs["env"]
        self.assertEqual(sent, {"PATH": "/usr/bin", "HOME": "/synthetic/home",
                                "XDG_CONFIG_HOME": "/synthetic/config"})
        self.assertNotIn("sk-synthetic-owner-key", json.dumps(report))
        with (patch.object(self.client.os, "environ", env),
              patch.object(self.client.shutil, "which", return_value="/synthetic/claude"),
              patch.object(self.client.Path, "cwd", return_value=ROOT),
              patch.object(self.client.Path, "home", return_value=ROOT),
              patch.object(self.client.subprocess, "run", return_value=response),
              patch.object(self.client.os, "execvpe", side_effect=AssertionError("launch")),
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(self.client.main(["--dry-run"]), 0)
        self.assertNotIn("sk-synthetic-owner-key", output.getvalue())

    def test_project_settings_local_override_blocks_launch(self):
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
                          patch.object(self.client.Path, "cwd", return_value=project),
                          patch.object(self.client.Path, "home", return_value=project),
                          patch.object(self.client, "_native_login_record", return_value=True),
                          patch.object(self.client.os, "execvpe", side_effect=AssertionError("launch")),
                          patch.object(self.client, "_bounded_smoke", side_effect=AssertionError("launch")),
                          contextlib.redirect_stdout(io.StringIO()) as output):
                        self.assertEqual(self.client.main([]), 2)
                    self.assertIn(expected, json.loads(output.getvalue())["failures"])

    def test_smoke_fixture_is_bounded(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            fixture = Path(temp) / "fact.txt"
            fixture.write_text("synthetic fact")
            self.assertIn("Read once", self.client._smoke_prompt(fixture, ROOT))
            with self.assertRaises(ValueError):
                self.client._smoke_prompt(Path(temp), ROOT)
            fixture.write_bytes(b"x" * 1025)
            with self.assertRaises(ValueError):
                self.client._smoke_prompt(fixture, ROOT)

    def test_smoke_evidence_requires_stream_linked_read_and_final_answer(self):
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


if __name__ == "__main__":
    unittest.main()
