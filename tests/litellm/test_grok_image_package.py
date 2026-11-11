"""Fast local controls for the tracked Grok image package."""
import base64
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
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
        cls.runner = load("grok_image_runner", HERE / "run_grok_pinned_image.py")
        cls.fixture = load("grok_image_fixture", HERE / "grok_pinned_image_integration.py")
        cls.patch = load("grok_image_patch", ROOT / "services/litellm-grok/patch_image.py")
        cls.guard = load("grok_commit_tag_guard", ROOT / "services/litellm-grok/commit_tag_guard.py")

    def test_accepted_source_hashes(self):
        self.assertEqual(self.runner.CALLBACK_HASH, self.runner.sha(self.runner.CALLBACK))
        for path, expected in self.runner.FILES.items():
            self.assertEqual(expected, self.runner.sha(Path(path)), path)
        self.assertEqual(len(self.runner.CHECKS), 26)

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


if __name__ == "__main__":
    unittest.main()
