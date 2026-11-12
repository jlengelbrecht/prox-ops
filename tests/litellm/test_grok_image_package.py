"""Fast local controls for the tracked Grok image package."""
import asyncio
import ast
import base64
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler
from urllib.response import addinfourl
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Load the release controls without contacting Docker or GHCR."""
        cls.runner = load("grok_image_runner", HERE / "run_grok_pinned_image.py")
        cls.fixture = load("grok_image_fixture", HERE / "grok_pinned_image_integration.py")
        cls.patch = load("grok_image_patch", ROOT / "services/litellm-grok/patch_image.py")
        cls.guard = load("grok_commit_tag_guard", ROOT / "services/litellm-grok/commit_tag_guard.py")
        cls.anonymous = load("grok_anonymous_pull", ROOT / "services/litellm-grok/anonymous_pull.py")

    def test_anonymous_manifest_transport_and_cache_independence(self):
        """Even a cached image requires three direct GHCR requests and exact bytes."""
        import hashlib
        body = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
        digest = hashlib.sha256(body).hexdigest()
        reference = "ghcr.io/jlengelbrecht/litellm-grok@sha256:" + digest
        challenge = ('Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                     'scope="repository:jlengelbrecht/litellm-grok:pull"')
        calls = []

        class Response:
            def __init__(self, request, status, headers, body=b"", url=None):
                self.status, self.headers, self.body = status, headers, body
                self.url = url or request.full_url
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return self.url
            def read(self, limit): return self.body[:limit]

        def transport(request, timeout):
            self.assertEqual(timeout, 15)
            calls.append(request)
            if len(calls) == 1:
                return Response(request, 401, {"WWW-Authenticate": challenge})
            if len(calls) == 2:
                return Response(request, 200, {}, b'{"token":"anonymous-token"}')
            return Response(request, 200, {
                "Content-Type": "application/vnd.oci.image.manifest.v1+json",
                "Docker-Content-Digest": "sha256:" + digest}, body)

        with patch("subprocess.run", side_effect=AssertionError("Docker cache used")):
            self.anonymous.verify_anonymous_access(reference, transport)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r.get_method() for r in calls], ["GET"] * 3)
        self.assertEqual(calls[0].full_url, calls[2].full_url)
        self.assertEqual(calls[1].full_url,
            "https://ghcr.io/token?service=ghcr.io&scope=repository%3Ajlengelbrecht%2Flitellm-grok%3Apull")
        self.assertIsNone(calls[0].get_header("Authorization"))
        self.assertIsNone(calls[1].get_header("Authorization"))
        self.assertEqual(calls[2].get_header("Authorization"), "Bearer anonymous-token")
        self.assertEqual(calls[2].get_header("Accept"), self.anonymous.ACCEPT)

    def test_anonymous_manifest_fails_closed(self):
        """Reject private, redirected, malformed, and mismatched exchanges."""
        import hashlib
        body = b'{"schemaVersion":2}'
        digest = hashlib.sha256(body).hexdigest()
        reference = "ghcr.io/jlengelbrecht/litellm-grok@sha256:" + digest
        challenge = ('Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                     'scope="repository:jlengelbrecht/litellm-grok:pull"')
        class Response:
            def __init__(self, request, status, headers=None, body=b"", url=None):
                self.status, self.headers, self.body = status, headers or {}, body
                self.url = url or request.full_url
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def geturl(self): return self.url
            def read(self, limit): return self.body[:limit]
        def run(first=None, second=None, third=None):
            answers = [first, second, third]
            calls = []
            def transport(request, timeout):
                calls.append(request)
                default = (Response(request, 401, {"WWW-Authenticate": challenge})
                           if len(calls) == 1 else
                           Response(request, 200, {}, b'{"token":"public"}')
                           if len(calls) == 2 else
                           Response(request, 200, {
                               "Content-Type": "application/vnd.oci.image.manifest.v1+json",
                               "Docker-Content-Digest": "sha256:" + digest}, body))
                selected = answers[len(calls)-1]
                return selected(request) if selected else default
            with self.assertRaises(ValueError):
                self.anonymous.verify_anonymous_access(reference, transport)
            return len(calls)
        self.assertEqual(run(first=lambda r: Response(r, 403)), 1)
        self.assertEqual(run(first=lambda r: Response(r, 401, {"WWW-Authenticate":
            challenge.replace(":pull", ":push")})), 1)
        self.assertEqual(run(first=lambda r: Response(r, 401, {"WWW-Authenticate":
            challenge.replace("ghcr.io/token", "evil.example/token")})), 1)
        self.assertEqual(run(second=lambda r: Response(r, 200, {}, b'{}')), 2)
        self.assertEqual(run(second=lambda r: Response(r, 200, {},
            b'{"token":"public"}', "https://evil.example/token")), 2)
        self.assertEqual(run(third=lambda r: Response(r, 403)), 3)
        self.assertEqual(run(third=lambda r: Response(r, 200, {
            "Content-Type": "application/vnd.oci.image.manifest.v1+json",
            "Docker-Content-Digest": "sha256:" + digest}, b'wrong')), 3)
        self.assertEqual(run(third=lambda r: Response(r, 200, {
            "Content-Type": "application/vnd.oci.image.manifest.v1+json",
            "Docker-Content-Digest": "sha256:" + digest}, body,
            "https://evil.example/manifest")), 3)
        with self.assertRaises(ValueError):
            self.anonymous.verify_anonymous_access("ghcr.io/x:main", lambda *_: None)
        with self.assertRaises(ValueError):
            self.anonymous.NoRedirect().redirect_request(None, None, 302, "", {},
                "https://evil.example/manifest")

    def test_accepted_source_hashes(self):
        self.assertEqual(self.runner.CALLBACK_HASH, self.runner.sha(self.runner.CALLBACK))
        for path, expected in self.runner.FILES.items():
            self.assertEqual(expected, self.runner.sha(Path(path)), path)
        self.assertEqual(len(self.runner.CHECKS), 34)
        self.assertIn("routed_asgi_litellm_metadata_only_fixed_502", self.runner.CHECKS)

    def test_baseline_check_path_and_candidate_reachability(self):
        """Walk the fixture's actual mode branches without importing LiteLLM."""
        tree = ast.parse((HERE / "grok_pinned_image_integration.py").read_text())
        qualify = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "qualify")

        def path_checks(baseline):
            seen = []
            env = {"GROK_BASELINE_NONALIAS": "1"} if baseline else {}

            def walk(statements):
                for statement in statements:
                    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef,
                                              ast.ClassDef)):
                        continue
                    if isinstance(statement, ast.Return):
                        return True
                    if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                            and isinstance(statement.value.func, ast.Name)
                            and statement.value.func.id == "check"):
                        name = statement.value.args[0]
                        seen.append(name.value if isinstance(name, ast.Constant) else "dynamic")
                    if isinstance(statement, ast.If):
                        if "GROK_BASELINE_NONALIAS" in ast.unparse(statement.test):
                            selected = eval(compile(ast.Expression(statement.test), "<fixture-mode>", "eval"),
                                            {"os": SimpleNamespace(environ=env)})
                            if walk(statement.body if selected else statement.orelse):
                                return True
                        else:
                            walk(statement.body)
                            walk(statement.orelse)
                    elif isinstance(statement, (ast.With, ast.AsyncWith, ast.For,
                                                ast.AsyncFor, ast.While)):
                        if walk(statement.body):
                            return True
                        walk(getattr(statement, "orelse", []))
                    elif isinstance(statement, ast.Try):
                        walk(statement.body)
                        for handler in statement.handlers:
                            walk(handler.body)
                        walk(statement.orelse)
                        walk(statement.finalbody)
                return False

            walk(qualify.body)
            return seen

        self.assertEqual(path_checks(True),
                         ["baseline_generic_denied", "baseline_nonalias_reached"])
        candidate = path_checks(False)
        self.assertTrue({"common_litellm_metadata_fixed_502",
                         "common_metadata_spoof_rejected", "common_canonical_denial_401",
                         "common_prefixed_denial_closed",
                         "common_unrelated_original_preserved",
                         "routed_asgi_litellm_metadata_only_fixed_502"}.issubset(candidate))

    def test_routed_boundary_requires_router_selected_deployment(self):
        tree = ast.parse((HERE / "grok_pinned_image_integration.py").read_text())
        qualify = next(node for node in tree.body
                       if isinstance(node, ast.AsyncFunctionDef) and node.name == "qualify")
        hooks = [node for node in ast.walk(qualify)
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == "maybe_fault_hook"]
        self.assertEqual(len(hooks), 1)
        self.assertNotIn("pre_call_with_selected_deployment",
                         (HERE / "grok_pinned_image_integration.py").read_text())

        async def original_failure_hook(*_, **__):
            return None

        state = {"nonalias_hook_mode": None, "hook_fault": False}
        evidence = {}
        scope = {"CASE": "routed_litellm_metadata", "state": state, "EVIDENCE": evidence,
                 "ALIAS": "project-grok-native", "BACKEND": "xai/grok-4.7-build-fast",
                 "original_failure_hook": original_failure_hook}
        exec(compile(ast.Module(body=hooks, type_ignores=[]), "<fixture-hook>", "exec"), scope)
        selected_metadata = {
            "deployment_model_name": "project-router", "deployment": "xai/grok-4.7-build-fast",
            "model_info": {"id": "synthetic-grok-route"}}
        data = {"model": "project-router", "proxy_server_request": {
            "body": {"model": "project-router", "litellm_metadata": selected_metadata}},
            "litellm_metadata": selected_metadata}

        def inspect(data, stamp="synthetic-grok-route"):
            original = RuntimeError("synthetic failure")
            if stamp is not None:
                original.failed_deployment_id = stamp
            asyncio.run(scope["maybe_fault_hook"](request_data=data, original_exception=original))
            return state["routed_failure_boundary"], dict(evidence["routed_trace"])

        selected, trace = inspect(data)
        self.assertTrue(selected)
        self.assertEqual(trace, {"request_model": "router", "carrier_model": "router",
            "selected_group": "router", "selected_backend": "backend", "selected_id": True,
            "router_stamped_id": True, "original_selected_fields": True,
            "other_selected_fields": False})
        self.assertFalse(inspect(data, stamp=None)[0])
        self.assertFalse(inspect(data, stamp="other-deployment")[0])
        for changed in (
            {"litellm_metadata": {"deployment_model_name": "project-grok-native"}},
            {"litellm_metadata": {**data["litellm_metadata"], "deployment": "other"}},
            {"metadata": {"deployment_model_name": "project-router"}},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(inspect({**data, **changed})[0])

    def test_patch_classifiers_match_real_router_stamp_and_reject_caller_stamp(self):
        tree = ast.parse((ROOT / "services/litellm-grok/patch_image.py").read_text())
        fragments = [node.value for node in ast.walk(tree)
                     if isinstance(node, ast.Constant) and isinstance(node.value, str)
                     and 'get("deployment_model_name") == "project-router"' in node.value]
        self.assertEqual(len(fragments), 4)
        expressions = []
        for fragment in fragments:
            marker = "grok_alias_failure =" if "grok_alias_failure =" in fragment else "is_grok_alias ="
            lines = fragment[fragment.index(marker):].splitlines()
            for count in range(1, len(lines) + 1):
                try:
                    parsed = ast.parse(textwrap.dedent("\n".join(lines[:count])))
                except SyntaxError:
                    continue
                if len(parsed.body) == 1 and isinstance(parsed.body[0], ast.Assign):
                    break
            else:
                self.fail("patched classifier expression did not parse")
            expressions.append(compile(ast.Expression(parsed.body[0].value), "<routed-classifier>", "eval"))

        data = {"model": "project-router", "proxy_server_request": {"body": {
            "model": "project-router"}}, "litellm_metadata": {
            "deployment_model_name": "project-router", "deployment": "xai/grok-4.7-build-fast",
            "model_info": {"id": "synthetic-grok-route"}}}
        def classified(expression, data, stamp="synthetic-grok-route"):
            body = data["proxy_server_request"]["body"]
            original = RuntimeError("synthetic failure")
            if stamp is not None:
                original.failed_deployment_id = stamp
            return eval(expression, {"request_data": data, "self": SimpleNamespace(data=data),
                                     "carrier_body": body, "e": original,
                                     "original_exception": original})

        for expression in expressions:
            self.assertTrue(classified(expression, data))
            self.assertFalse(classified(expression, data, stamp=None))
            self.assertFalse(classified(expression, data, stamp="other-deployment"))
            alias_tag = {**data, "litellm_metadata": {"deployment_model_name": "project-grok-native",
                "deployment": "xai/grok-4.7-build-fast", "model_info": {"id": "synthetic-alias"}}}
            self.assertFalse(classified(expression, alias_tag, stamp=None))
            self.assertTrue(classified(expression, alias_tag, stamp="synthetic-alias"))
            forged = {**data, "litellm_metadata": {**data["litellm_metadata"],
                "deployment": "synthetic-other"}, "metadata": {
                "deployment_model_name": "project-grok-native",
                "deployment": "xai/grok-4.7-build-fast",
                "model_info": {"id": "synthetic-grok-route"}}}
            self.assertFalse(classified(expression, forged))
            self.assertFalse(classified(expression, {**forged, "litellm_metadata": {}}))
            self.assertTrue(classified(expression, {**data, "metadata": {
                "deployment_model_name": "synthetic-other", "deployment": "synthetic-other",
                "model_info": {"id": "synthetic-grok-route"}}}))
            self.assertFalse(classified(expression, {**data, "model": "synthetic-other"}))
            self.assertFalse(classified(expression, {**data, "litellm_metadata": {
                **data["litellm_metadata"], "deployment": "synthetic-other"}}))
            self.assertFalse(classified(expression, {**data, "litellm_metadata": {
                **data["litellm_metadata"], "model_info": {}}}))
            self.assertFalse(classified(expression, {**data, "proxy_server_request": {
                "body": {"model": "synthetic-other"}}}))

    def test_docker_command_output_is_discarded(self):
        with patch.object(self.runner.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 7)) as run:
            self.assertEqual(self.runner.run_closed(["docker", "build"], 240), 7)
        self.assertEqual(run.call_args.args[0], ["docker", "build"])
        self.assertEqual(run.call_args.kwargs, {
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            "timeout": 240, "check": False})

    def test_inspect_accepts_narrow_output_and_rejects_oversize(self):
        real_popen = subprocess.Popen
        image_id = "sha256:" + "a" * 64
        digest = "example/image@sha256:" + "b" * 64
        valid = (json.dumps(image_id) + "|" + json.dumps([digest])).encode()
        processes = []

        def inspect_with_output(data):
            def launch(command, **kwargs):
                self.assertEqual(command, ["docker", "image", "inspect", "--format",
                    "{{json .Id}}|{{json .RepoDigests}}", "synthetic-image"])
                self.assertEqual(kwargs, {"stdout": subprocess.PIPE,
                                          "stderr": subprocess.DEVNULL})
                process = real_popen([sys.executable, "-c",
                    "import os; os.write(1, " + repr(data) + ")"], **kwargs)
                processes.append(process)
                return process
            with patch.object(self.runner.subprocess, "Popen", side_effect=launch):
                return self.runner.inspect("synthetic-image")

        self.assertEqual(inspect_with_output(valid), {"Id": image_id, "RepoDigests": [digest]})
        self.assertIsNone(inspect_with_output(b"x" * 8193))
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_inspect_timeout_kills_child(self):
        real_popen = subprocess.Popen
        processes = []

        class NoEvents:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def register(self, *_): pass
            def select(self, *_): return []

        def launch(_command, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 **kwargs)
            processes.append(process)
            return process

        with patch.object(self.runner.subprocess, "Popen", side_effect=launch), \
                patch.object(self.runner.selectors, "DefaultSelector", NoEvents):
            self.assertIsNone(self.runner.inspect("synthetic-image"))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())

    def test_receipt_transport_has_hard_bounds(self):
        for baseline in (False, True):
            command = self.runner.container_command(
                "synthetic-run", "sha256:" + "a" * 64, Path("/synthetic/input"), baseline)
            mounts = [command[index + 1] for index, value in enumerate(command[:-1])
                      if value == "--mount"]
            self.assertEqual(mounts, ["type=bind,source=/synthetic/input,target=/opt/grok,readonly"])
            self.assertEqual(command[command.index("--log-driver") + 1], "none")
            self.assertIn(self.runner.RECEIPT_TMPFS, command)
            self.assertIn("size=128k", self.runner.RECEIPT_TMPFS)
            self.assertEqual("GROK_BASELINE_NONALIAS=1" in command, baseline)
            self.assertEqual(command[command.index("--entrypoint") + 1], "/bin/sh")
            self.assertIn("/opt/grok-result/result.json", command[-1])
        with patch.object(self.runner, "run_bounded", return_value=(0, b"{}")) as run:
            self.assertEqual(self.runner.run_receipt(["docker", "run"]), (0, b"{}"))
        run.assert_called_once_with(["docker", "run"], 240, 65536)

        exact = b"x" * self.runner.RECEIPT_LIMIT
        def emit(data):
            return self.runner.run_bounded([sys.executable, "-c",
                "import os; os.write(1, " + repr(data) + ")"], 2, self.runner.RECEIPT_LIMIT)
        self.assertEqual(emit(exact), (0, exact))
        self.assertEqual(emit(exact + b"x"), (-1, None))

    def test_wrong_base_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in self.patch.EXPECTED:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("wrong upstream source")
            before = {relative: (root / relative).read_bytes() for relative in self.patch.EXPECTED}
            result = subprocess.run([sys.executable,
                str(ROOT / "services/litellm-grok/patch_image.py"),
                "--source-root", str(root)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected LiteLLM source hash", result.stderr)
            for relative in self.patch.EXPECTED:
                self.assertEqual((root / relative).read_bytes(), before[relative])

    def test_malformed_terminal_error_and_unlinked_result(self):
        self.assertFalse(self.fixture.terminal_error_event("event: error\ndata: {}\n\n"))
        valid_start = {"type": "message_start", "message": {
            "type": "message", "role": "assistant", "content": []}}
        error = {"type": "error", "error": {"type": "api_error",
                  "message": "Project Grok provider failed"}}
        frame = lambda value: "event: " + value["type"] + "\ndata: " + json.dumps(value) + "\n\n"
        self.assertTrue(self.fixture.terminal_error_event(frame(valid_start) + frame(error)))
        self.assertFalse(self.fixture.terminal_error_event(
            frame({"type": "message_start"}) + frame(error)))
        self.assertFalse(self.fixture.terminal_error_event(
            frame(valid_start) + frame({"type": "message_stop"}) + frame(error)))
        self.assertIsNone(self.fixture.linked_tool_result([
            {"role": "assistant", "tool_calls": [{"id": "synthetic-call", "type": "function",
                "function": {"name": "Read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "wrong", "content": "cedar"}]))

    def test_published_reference_is_digest_only(self):
        self.assertIsNotNone(self.runner.PUBLISHED.fullmatch(
            "ghcr.io/jlengelbrecht/litellm-grok@sha256:" + "a" * 64))
        self.assertIsNone(self.runner.PUBLISHED.fullmatch("ghcr.io/jlengelbrecht/litellm-grok:main"))

    def test_commit_tag_guard_states(self):
        digest = "sha256:" + "a" * 64
        sha = "b" * 40
        absent_body = b'{"errors":[{"code":"MANIFEST_UNKNOWN","message":"unknown manifest"}]}'
        class Response:
            def __init__(self, request, status=200, headers=None, body=None, url=None):
                self.status = status
                self.headers = headers or {}
                self.body = body if body is not None else b""
                self.url = url or request.full_url
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, limit): return self.body[:limit]
            def geturl(self): return self.url
            def close(self): pass
        challenge = 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:owner/litellm-grok:pull"'
        def run(final=404, challenge_value=challenge, token_body=None,
                token_status=200, candidate_status=200, redirect=None,
                final_body=absent_body, final_media_type=None, actor="actor"):
            calls = []
            def opener(request, timeout):
                self.assertEqual(timeout, 15)
                calls.append(request)
                if len(calls) == 1:
                    return Response(request, 401, {"WWW-Authenticate": challenge_value})
                if len(calls) == 2:
                    self.assertEqual(request.full_url,
                        "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aowner%2Flitellm-grok%3Apull")
                    self.assertEqual(request.get_header("Authorization"),
                        "Basic " + base64.b64encode(f"{actor}:token".encode()).decode())
                    return Response(request, token_status, body=token_body if token_body is not None
                                    else b'{"token":"opaque-bearer"}',
                                    url="https://evil.example/token" if redirect == "token" else None)
                self.assertEqual(request.get_header("Authorization"), "Bearer opaque-bearer")
                if len(calls) == 3:
                    return Response(request, candidate_status,
                                    {"Docker-Content-Digest": digest},
                                    json.dumps({"schemaVersion": 2}).encode(),
                                    url="https://evil.example/manifest" if redirect == "manifest" else None)
                return Response(request, final,
                                {"Docker-Content-Digest": digest},
                                (final_body if final == 404 else json.dumps({
                                    "schemaVersion": 2, **({"mediaType": final_media_type,
                                    "manifests": []} if final_media_type else {})}).encode()))
            try:
                state = self.guard.tag_state("owner/litellm-grok", "candidate-1-1",
                    digest, sha, actor, "token", opener)
                return state, calls
            except self.guard.RemoteError:
                return "remote_error", calls
        state, calls = run()
        self.assertIsNone(state)
        self.assertEqual([request.full_url for request in calls], [
            "https://ghcr.io/v2/owner/litellm-grok/manifests/candidate-1-1",
            "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aowner%2Flitellm-grok%3Apull",
            "https://ghcr.io/v2/owner/litellm-grok/manifests/candidate-1-1",
            "https://ghcr.io/v2/owner/litellm-grok/manifests/sha-" + sha])
        self.assertEqual([request.get_method() for request in calls], ["GET"] * 4)
        self.assertEqual([request.get_header("Accept") for request in calls],
                         [self.guard.ACCEPT, "application/json",
                          self.guard.ACCEPT, self.guard.ACCEPT])
        self.assertIsNone(calls[0].get_header("Authorization"))
        self.assertEqual(calls[1].get_header("Authorization"),
                         "Basic " + base64.b64encode(b"actor:token").decode())
        self.assertEqual([request.get_header("Authorization") for request in calls[2:]],
                         ["Bearer opaque-bearer"] * 2)
        self.assertEqual(run(200)[0], digest)
        for actor in ("github-actions[bot]", "dependabot[bot]"):
            with self.subTest(actor=actor):
                self.assertIsNone(run(actor=actor)[0])
                self.assertEqual(run(200, actor=actor)[0], digest)
        for actor in ("", "[bot]", "actor[bot]extra", "actor[admin]",
                      "actor/name", "actor:token", "actor\r\nInjected: value",
                      "actor\x00name", "actor name"):
            with self.subTest(invalid_actor=actor):
                state, calls = run(actor=actor)
                self.assertEqual(state, "remote_error")
                self.assertEqual(calls, [])
        # An existing OCI index is still an existing tag (schemaVersion 2).
        self.assertEqual(run(200, final_media_type=
            "application/vnd.oci.image.index.v1+json")[0], digest)
        for body in (b"", b"not-json", b"{}", b"[]", b"x" * 65537,
                     b'{"errors":[]}', b'{"errors":"MANIFEST_UNKNOWN"}',
                     b'{"errors":[{"code":"NAME_UNKNOWN"}]}',
                     b'{"errors":[{"code":"DENIED"}]}',
                     b'{"errors":[{"code":"OTHER"}]}',
                     b'{"errors":[{"code":"MANIFEST_UNKNOWN"},{"code":"DENIED"}]}'):
            self.assertEqual(run(final_body=body)[0], "remote_error", body[:80])
        for status in (401, 403, 429, 500):
            self.assertEqual(run(final=status)[0], "remote_error")
            self.assertEqual(run(token_status=status)[0], "remote_error")
        self.assertEqual(run(candidate_status=404)[0], "remote_error")
        for value in (None, "", "Basic realm=\"https://ghcr.io/token\"",
                      challenge.replace("ghcr.io/token", "evil.example/token"),
                      challenge.replace("service=\"ghcr.io\"", "service=\"evil.example\""),
                      challenge.replace(":pull\"", ":push\""),
                      challenge + ',realm="https://ghcr.io/token"'):
            self.assertEqual(run(challenge_value=value)[0], "remote_error")
        for body in (b"{}", b'{"token":""}', b'{"token":42}',
                     b'{"token":"a b"}', b'{"token":"one","access_token":"two"}',
                     b"not-json", b"x" * 65537):
            self.assertEqual(run(token_body=body)[0], "remote_error")
        self.assertIsNone(run(token_body=b'{"access_token":"opaque-bearer"}')[0])
        self.assertIsNone(run(token_body=b'{"token":"opaque-bearer","access_token":"opaque-bearer"}')[0])
        for where in ("token", "manifest"):
            self.assertEqual(run(redirect=where)[0], "remote_error")
        def offline(request, timeout):
            raise URLError("offline")
        with self.assertRaises(self.guard.RemoteError):
            self.guard.tag_state("owner/litellm-grok", "candidate-1-1", digest,
                                 sha, "actor", "token", offline)

    def test_default_opener_rejects_redirect_without_following(self):
        requests = []
        class RedirectTransport(HTTPHandler):
            def http_open(self, request):
                requests.append(request.full_url)
                response = addinfourl(BytesIO(), {"Location": "https://evil.example/token"},
                                      request.full_url, 302)
                response.msg = "Found"
                return response
        real_build_opener = self.guard.build_opener
        with patch.object(self.guard, "build_opener", side_effect=lambda *handlers:
                          real_build_opener(*handlers, RedirectTransport())):
            with self.assertRaises(HTTPError) as raised:
                self.guard._default_open(self.guard.Request("http://registry.test/token"), 15)
        self.assertEqual(raised.exception.code, 302)
        self.assertEqual(requests, ["http://registry.test/token"])
        raised.exception.close()

    def test_release_workflow_coverage_and_permissions(self):
        """Keep anonymous publication gates ahead of all mutable tag writes."""
        workflow = (ROOT / ".github/workflows/litellm-grok.yaml").read_text()
        self.assertEqual(workflow.count("- tests/litellm/test_grok_image_package.py"), 2)
        self.assertIn("python3 -m unittest tests/litellm/test_grok_image_package.py", workflow)
        self.assertIn("python3 tests/litellm/run_grok_pinned_image.py --base-image-id", workflow)
        self.assertIn("name: grok-verify-receipt", workflow)
        self.assertIn("  contents: read\n", workflow)
        self.assertIn("  publish:\n", workflow)
        self.assertEqual(workflow.count("      packages: write\n"), 1)
        self.assertIn("      queue: max\n", workflow)
        self.assertIn("      cancel-in-progress: false\n", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("publication_state.py fresh", workflow)
        self.assertIn("publication_state.py decide", workflow)
        self.assertLess(workflow.index("grok-pre-create-tag-state.json"),
                        workflow.index("publication_state.py create-ready"))
        self.assertLess(workflow.index("publication_state.py create-ready"),
                        workflow.index('docker push "$image:sha-$GITHUB_SHA"'))
        self.assertEqual(workflow.count("- tests/litellm/test_grok_publication.py"), 2)
        candidate_gate = workflow.index("name: Require anonymous registry read of candidate digest")
        self.assertLess(workflow.index("grok-published/receipt.json"), candidate_gate)
        self.assertLess(candidate_gate, workflow.index("name: Inspect commit tag"))
        self.assertLess(candidate_gate, workflow.index("publication_state.py create-ready"))
        self.assertLess(candidate_gate, workflow.index('docker push "$image:main"'))
        candidate_call = workflow.index('python3 services/litellm-grok/anonymous_pull.py',
                                        candidate_gate)
        existing_gate = workflow.index('python3 services/litellm-grok/anonymous_pull.py',
                                       candidate_call + 1)
        self.assertLess(existing_gate, workflow.index("existing-inspection.json"))


if __name__ == "__main__":
    unittest.main()
