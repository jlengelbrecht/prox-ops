"""Fast local controls for the tracked Grok image package."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.error import HTTPError, URLError

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
        class Response:
            status = 200
            headers = {"Docker-Content-Digest": digest}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return json.dumps({"schemaVersion": 2}).encode()
        def run(second):
            calls = []
            def opener(request, timeout):
                calls.append(request.full_url)
                if len(calls) == 1: return Response()
                if isinstance(second, Exception): raise second
                return second
            try:
                state = self.guard.tag_state("owner/litellm-grok", "candidate-1-1",
                    digest, sha, "actor", "token", opener)
                return state, calls
            except self.guard.RemoteError:
                return "remote_error", calls
        absent = HTTPError("url", 404, "missing", {}, None)
        self.assertEqual(run(absent)[0], "absent")
        self.assertEqual(run(Response())[0], "present")
        for error in (HTTPError("url", 401, "auth", {}, None),
                      HTTPError("url", 403, "forbidden", {}, None),
                      HTTPError("url", 429, "rate", {}, None),
                      URLError("offline"), TimeoutError()):
            self.assertEqual(run(error)[0], "remote_error")
        self.assertEqual(len(run(absent)[1]), 2)
        def uncertain(request, timeout):
            raise HTTPError(request.full_url, 404, "missing", {}, None)
        with self.assertRaises(self.guard.RemoteError):
            self.guard.tag_state("owner/litellm-grok", "candidate-1-1", digest,
                sha, "actor", "token", uncertain)
        class Malformed(Response):
            headers = {"Docker-Content-Digest": "bad"}
        def malformed(request, timeout): return Malformed()
        with self.assertRaises(self.guard.RemoteError):
            self.guard.tag_state("owner/litellm-grok", "candidate-1-1", digest,
                sha, "actor", "token", malformed)

    def test_release_workflow_order_and_coverage(self):
        workflow = (ROOT / ".github/workflows/litellm-grok.yaml").read_text()
        self.assertEqual(workflow.count("- tests/litellm/test_grok_image_package.py"), 2)
        self.assertIn("python3 -m unittest tests/litellm/test_grok_image_package.py", workflow)
        stages = ["docker push \"$image:$candidate\"",
                  "--candidate-image \"$published\"",
                  "python3 services/litellm-grok/commit_tag_guard.py",
                  "docker push \"$image:sha-$GITHUB_SHA\"",
                  "docker push \"${published%@*}:main\""]
        offsets = [workflow.index(stage) for stage in stages]
        self.assertEqual(offsets, sorted(offsets))


if __name__ == "__main__":
    unittest.main()
