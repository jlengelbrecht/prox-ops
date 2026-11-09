"""Offline failure and boundary tests for release_download."""

import hashlib
import http.client
import importlib.util
import io
import json
from email.message import Message
from pathlib import Path
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import yaml


SPEC = importlib.util.spec_from_file_location(
    "release_download", Path(__file__).resolve().parents[2] / "scripts/ci/release_download.py"
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def metadata(name, body):
    """Build release metadata with a digest for fixture bytes."""
    return {"name": name, "id": 123, "size": len(body), "digest": "sha256:" + hashlib.sha256(body).hexdigest()}


class ReleaseDownloadTests(unittest.TestCase):
    """Exercise CI release handling without network or cluster access."""
    def flux_fixture(self, root):
        """Write both pinned Kustomizations beneath a temporary root."""
        paths = {}
        for relative, assets in module.FLUX_ASSETS.items():
            path = root / relative
            path.parent.mkdir(parents=True)
            urls = [f"https://github.com/{repo}/releases/download/{tag}/{name}"
                    for repo, tag, name in assets]
            path.write_text("resources:\n  - ./namespace.yaml\n" +
                            "".join("  - " + url + "\n" for url in urls))
            paths[path] = urls
        return paths

    def test_asset_redirect_does_not_forward_token(self):
        """Keep API authorization off a release asset redirect."""
        seen = []

        class Response:
            headers = {"Content-Type": "application/octet-stream"}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, size):
                if not hasattr(self, "done"):
                    self.done = True
                    return b"verified later"
                return b""

        def open_request(request, timeout):
            seen.append(request)
            if len(seen) == 1:
                headers = Message()
                headers["Location"] = "https://release-assets.githubusercontent.com/object"
                raise HTTPError(request.full_url, 302, "Found", headers, None)
            return Response()

        with patch.object(module.OPENER, "open", side_effect=open_request):
            module.request_bytes("https://api.github.com/repos/example/repo/releases/assets/123", "secret", asset=True)
        self.assertEqual(seen[0].get_header("Authorization"), "Bearer secret")
        self.assertIsNone(seen[1].get_header("Authorization"))

    def test_rejects_missing_or_duplicate_asset_names(self):
        """Require one exact asset-name match."""
        for assets in ([], [metadata("a.yaml", b"a"), metadata("a.yaml", b"b")]):
            with self.subTest(assets=assets), patch.object(module, "release_assets", return_value=assets):
                with self.assertRaises(module.DownloadError):
                    module.fetch_asset("example/repo", "v1", "a.yaml")

    def test_rejects_malformed_metadata_and_entries(self):
        """Reject malformed release metadata before asset transfer."""
        for data in ([], {"tag_name": "v1", "assets": ["bad"]}):
            with self.subTest(data=data), patch.object(module, "request_bytes", return_value=(
                __import__("json").dumps(data).encode(), "application/json"
            )):
                with self.assertRaises(module.DownloadError):
                    module.release_assets("example/repo", "v1")
        with patch.object(module, "release_assets", return_value=["bad"]):
            with self.assertRaises(module.DownloadError):
                module.fetch_asset("example/repo", "v1", "a.yaml")

    def test_bounds_download_bytes_and_deadline(self):
        """Enforce size and wall-clock limits on the response body."""
        class Response:
            headers = {"Content-Type": "application/octet-stream"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size): return b"oversize"
        with patch.object(module.OPENER, "open", return_value=Response()):
            with self.assertRaisesRegex(module.DownloadError, "size limit"):
                module.request_bytes("https://api.github.com/repos/a/b/releases/assets/1", asset=True, max_bytes=4)
        with patch.object(module.time, "monotonic", side_effect=[0, 0, 0, 121]), patch.object(
            module.OPENER, "open", return_value=Response()
        ):
            with self.assertRaisesRegex(module.DownloadError, "deadline"):
                module.request_bytes("https://api.github.com/repos/a/b/releases/assets/1", asset=True, max_bytes=20)

    def test_truncated_http_body_retries_and_reports_clean_error(self):
        """Retry truncated reads without leaking partial body text."""
        class Truncated:
            headers = {"Content-Type": "application/octet-stream"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size): raise http.client.IncompleteRead(b"partial body", 8)
        with patch.object(module.OPENER, "open", return_value=Truncated()) as opened, patch.object(
            module.time, "sleep"
        ):
            with self.assertRaisesRegex(module.DownloadError, "bounded retries") as caught:
                module.request_bytes("https://api.github.com/repos/a/b/releases/assets/1", asset=True)
        self.assertEqual(opened.call_count, 3)
        self.assertNotIn("partial body", str(caught.exception))

    def test_http_status_retries_only_transient_failures(self):
        """Fail fast on permanent HTTP errors and diagnose exhausted retries."""
        url = "https://api.github.com/repos/a/b/releases/assets/1"
        for status, attempts in ((401, 1), (403, 1), (404, 1), (408, 3), (429, 3), (503, 3)):
            with self.subTest(status=status), patch.object(
                module.OPENER, "open", side_effect=HTTPError(url, status, "private body", Message(), None)
            ) as opened, patch.object(module.time, "sleep") as sleep:
                with self.assertRaises(module.DownloadError) as caught:
                    module.request_bytes(url, token="secret", asset=True)
                self.assertEqual(opened.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)
                self.assertIn(f"HTTP {status}", str(caught.exception))
                self.assertEqual("bounded retries" in str(caught.exception), attempts == 3)
                self.assertNotIn("private body", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_mid_body_connection_reset_retries(self):
        """Retry a mid-body socket reset without returning partial bytes."""
        class ResetResponse:
            headers = {"Content-Type": "application/octet-stream"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size): raise ConnectionResetError("private connection detail")
        with patch.object(module.OPENER, "open", return_value=ResetResponse()) as opened, patch.object(
            module.time, "sleep"
        ):
            with self.assertRaisesRegex(module.DownloadError, "bounded retries .*ConnectionResetError") as caught:
                module.request_bytes("https://api.github.com/repos/a/b/releases/assets/1", asset=True)
        self.assertEqual(opened.call_count, 3)
        self.assertNotIn("private connection detail", str(caught.exception))

    def test_slow_eof_cannot_succeed_after_wall_deadline(self):
        """Reject an EOF that arrives after the shared request deadline."""
        class SlowEOF:
            headers = {"Content-Type": "application/octet-stream"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size):
                module.time.sleep(0.05)
                return b""
        with patch.object(module, "REQUEST_DEADLINE_SECONDS", 0.01), patch.object(
            module.OPENER, "open", return_value=SlowEOF()
        ):
            with self.assertRaisesRegex(module.DownloadError, "deadline"):
                module.request_bytes("https://api.github.com/repos/a/b/releases/assets/1", asset=True)

    def test_canonical_metadata_redirect_is_narrow(self):
        """Allow only the matching canonical metadata redirect."""
        source = "https://api.github.com/repos/knative/net-kourier/releases/tags/knative-v1.21.1"
        target = "https://api.github.com/repositories/244394642/releases/tags/knative-v1.21.1"
        seen = []
        class Response:
            headers = {"Content-Type": "application/json"}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size):
                if not hasattr(self, "done"):
                    self.done = True
                    return b'{}'
                return b''
        def open_request(request, timeout):
            seen.append(request)
            if len(seen) == 1:
                headers = Message()
                headers["Location"] = target
                raise HTTPError(request.full_url, 301, "Moved", headers, None)
            return Response()
        with patch.object(module.OPENER, "open", side_effect=open_request):
            self.assertEqual(module.request_bytes(source, "secret"), (b"{}", "application/json"))
        self.assertEqual(seen[1].get_header("Authorization"), "Bearer secret")
        for bad in (target.replace("api.github.com", "evil.example"), target.replace("https:", "http:"),
                    target.replace("api.github.com", "api.github.com:443"),
                    target.replace("https://", "https://user@"), target.replace("knative-v1.21.1", "wrong-tag")):
            with self.subTest(bad=bad):
                headers = Message()
                headers["Location"] = bad
                with patch.object(module.OPENER, "open", side_effect=HTTPError(source, 301, "Moved", headers, None)):
                    with self.assertRaises(module.DownloadError):
                        module.request_bytes(source, "secret")

    def test_chained_canonical_redirects_preserve_asset_identity_and_token_boundary(self):
        """Preserve asset identity across GitHub's canonical redirect chain."""
        tag = "knative-v1.21.1"
        body = b"apiVersion: v1\nkind: List\n"
        meta = json.dumps({"tag_name": tag, "assets": [metadata("kourier.yaml", body)]}).encode()
        source_metadata = f"https://api.github.com/repos/knative/net-kourier/releases/tags/{tag}"
        canonical_metadata = f"https://api.github.com/repositories/244394642/releases/tags/{tag}"
        source_asset = "https://api.github.com/repos/knative/net-kourier/releases/assets/123"
        canonical_asset = "https://api.github.com/repositories/244394642/releases/assets/123"
        asset_host = "https://release-assets.githubusercontent.com/object"
        seen = []

        class Response:
            def __init__(self, payload, kind):
                self.payload = payload
                self.headers = {"Content-Type": kind}
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, size):
                payload, self.payload = self.payload[:size], self.payload[size:]
                return payload

        def open_request(request, timeout):
            seen.append(request)
            redirects = {source_metadata: (301, canonical_metadata),
                         source_asset: (301, canonical_asset),
                         canonical_asset: (302, asset_host)}
            if request.full_url in redirects:
                status, location = redirects[request.full_url]
                headers = Message()
                headers["Location"] = location
                raise HTTPError(request.full_url, status, "Moved", headers, None)
            if request.full_url == canonical_metadata:
                return Response(meta, "application/json")
            if request.full_url == asset_host:
                return Response(body, "application/octet-stream")
            self.fail("Unexpected request: " + request.full_url)

        with patch.object(module.OPENER, "open", side_effect=open_request):
            self.assertEqual(module.fetch_asset("knative/net-kourier", tag, "kourier.yaml", "secret"), body)
        self.assertEqual([request.full_url for request in seen],
                         [source_metadata, canonical_metadata, source_asset, canonical_asset, asset_host])
        for request in seen[:-1]:
            self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertIsNone(seen[-1].get_header("Authorization"))

    def test_canonical_asset_redirect_is_narrow(self):
        """Reject canonical asset redirects that alter identity or endpoint."""
        source = "https://api.github.com/repos/knative/net-kourier/releases/assets/436092261"
        target = "https://api.github.com/repositories/244394642/releases/assets/436092261"
        for bad in (target.replace("api.github.com", "evil.example"),
                    target.replace("https:", "http:"),
                    target.replace("api.github.com", "api.github.com:443"),
                    target.replace("https://", "https://user@"),
                    target.replace("436092261", "436092262"),
                    target + "?x=1", target + "#frag"):
            with self.subTest(bad=bad):
                headers = Message()
                headers["Location"] = bad
                with patch.object(module.OPENER, "open", side_effect=HTTPError(source, 301, "Moved", headers, None)):
                    with self.assertRaisesRegex(module.DownloadError, "Unexpected release redirect"):
                        module.request_bytes(source, "secret", asset=True)

    def test_rejects_missing_digest_json_and_partial_bytes(self):
        """Reject assets with missing integrity data or mismatched bytes."""
        good = metadata("a.yaml", b"some YAML")
        for item, body, kind in (
            ({**good, "digest": None}, b"some YAML", "application/octet-stream"),
            (good, b'{"message":"not an asset"}', "application/json"),
            (good, b"short", "application/octet-stream"),
            (good, b"bad bytes", "application/octet-stream"),
        ):
            with self.subTest(item=item, body=body), patch.object(module, "release_assets", return_value=[item]), patch.object(
                module, "request_bytes", return_value=(body, kind)
            ):
                with self.assertRaises(module.DownloadError):
                    module.fetch_asset("example/repo", "v1", "a.yaml")

    def test_gitleaks_requires_exact_single_checksum_before_extraction(self):
        """Verify the exact checksum line before extracting Gitleaks."""
        version = "8.30.1"
        archive_name = f"gitleaks_{version}_linux_x64.tar.gz"
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "gitleaks"
            bad_checksum = ("0" * 64 + "  wrong_linux_x64.tar.gz\n").encode()
            with patch.object(module, "fetch_asset", return_value=bad_checksum) as fetch:
                with self.assertRaises(module.DownloadError):
                    module.gitleaks(version, output)
                self.assertEqual(fetch.call_count, 1)
            self.assertFalse(output.exists())

            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
                payload = b"test executable"
                info = tarfile.TarInfo("gitleaks")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            archive = buffer.getvalue()
            checksum = (hashlib.sha256(archive).hexdigest() + "  " + archive_name + "\n").encode()
            with patch.object(module, "fetch_asset", side_effect=[checksum, archive]):
                module.gitleaks(version, output)
            self.assertEqual(output.read_bytes(), payload)

    def test_flux_rewrites_only_exact_pinned_urls(self):
        """Materialize only the four pinned remote references."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for relative, assets in module.FLUX_ASSETS.items():
                path = root / relative
                path.parent.mkdir(parents=True)
                urls = [f"https://github.com/{repo}/releases/download/{tag}/{name}" for repo, tag, name in assets]
                path.write_text("resources:\n" + "".join("  - " + url + "\n" for url in urls))
            with patch.object(module, "fetch_asset", return_value=b"apiVersion: v1\nkind: List\n") as fetch:
                module.materialize_flux(root)
            self.assertEqual(fetch.call_count, 4)
            for relative in module.FLUX_ASSETS:
                self.assertNotIn("https://github.com/", (root / relative).read_text())
                self.assertEqual(len(list((root / relative).parent.glob(".ci-release-*.yaml"))), 2)

    def test_flux_rejects_unexpected_version_without_fetching(self):
        """Reject unpinned remote versions before making requests."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            relative = next(iter(module.FLUX_ASSETS))
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text("resources:\n  - https://github.com/kserve/kserve/releases/download/v0.22.0/kserve.yaml\n")
            with patch.object(module, "fetch_asset") as fetch, self.assertRaises(module.DownloadError):
                module.materialize_flux(root)
            fetch.assert_not_called()

    def test_flux_failed_later_fetch_leaves_tree_unchanged(self):
        """Leave the source tree untouched when a later fetch fails."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            before = {}
            for relative, assets in module.FLUX_ASSETS.items():
                path = root / relative
                path.parent.mkdir(parents=True)
                urls = [f"https://github.com/{repo}/releases/download/{tag}/{name}" for repo, tag, name in assets]
                before[path] = ("resources:\n" + "".join("  - " + url + "\n" for url in urls)).encode()
                path.write_bytes(before[path])
            with patch.object(module, "fetch_asset", side_effect=[b"apiVersion: v1\n"] * 2 + [module.DownloadError("failed")]):
                with self.assertRaises(module.DownloadError):
                    module.materialize_flux(root)
            self.assertTrue(all(path.read_bytes() == body for path, body in before.items()))
            self.assertEqual(list(root.rglob(".ci-release-*.yaml")), [])

    def test_flux_requires_resources_not_comment_or_duplicate(self):
        """Require exact plain resources rather than comments or aliases."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = self.flux_fixture(root)
            path, urls = next(iter(paths.items()))
            original = path.read_text()
            for bad in (
                original.replace("  - " + urls[0], "  # " + urls[0]),
                original + "  - " + urls[0] + "\n",
                original.replace("  - " + urls[0], "  - &release " + urls[0] + "\n  - *release"),
                original.replace("  - " + urls[0], "  - https://example.com/a.yaml"),
                original.replace("  - " + urls[0], "  - {url: " + urls[0] + "}"),
            ):
                with self.subTest(bad=bad), patch.object(module, "fetch_asset") as fetch:
                    path.write_text(bad)
                    with self.assertRaises(module.DownloadError):
                        module.materialize_flux(root)
                    fetch.assert_not_called()
                    self.assertEqual(path.read_text(), bad)

    def test_flux_replacement_preserves_comments_and_resource_semantics(self):
        """Preserve comments and local resource entries during rewrite."""
        with tempfile.TemporaryDirectory() as folder:
            paths = self.flux_fixture(Path(folder))
            path, urls = next(iter(paths.items()))
            path.write_text(path.read_text() + "# pinned reference: " + urls[0] + "\n")
            with patch.object(module, "fetch_asset", return_value=b"apiVersion: v1\nkind: List\n"):
                module.materialize_flux(folder)
            self.assertIn("# pinned reference: " + urls[0], path.read_text())
            resources = [node.value for node in module.resource_nodes(path.read_text())]
            self.assertEqual(resources[0], "./namespace.yaml")
            self.assertEqual(len([resource for resource in resources if resource.startswith("./.ci-release-")]), 2)

    def test_flux_required_status_rejects_failed_or_missing_build(self):
        """Require explicit successful build outcomes for changed PRs."""
        workflow = yaml.load(Path(".github/workflows/flux-local.yaml").read_text(), Loader=yaml.BaseLoader)
        status = workflow["jobs"]["flux-local-status"]
        self.assertIn("pre-job", status["needs"])
        step = status["steps"][0]
        self.assertEqual(step["env"]["PRE_JOB_RESULT"], "${{ needs.pre-job.result }}")
        for pre, changed, test, expected in (
            ("success", "true", "success", 0),
            ("success", "false", "skipped", 0),
            ("failure", "", "skipped", 1),
            ("cancelled", "true", "skipped", 1),
            ("success", "true", "cancelled", 1),
            ("success", "true", "skipped", 1),
            ("success", "false", "success", 1),
            ("success", "", "skipped", 1),
        ):
            with self.subTest(pre=pre, changed=changed, test=test):
                env = {**os.environ, "PRE_JOB_RESULT": pre, "ANY_CHANGED": changed, "TEST_RESULT": test}
                result = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True)
                self.assertEqual(result.returncode == 0, expected == 0)


if __name__ == "__main__":
    unittest.main()
