"""Exercise the registry HTTP contract used by image publication."""

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publish_image import Registry, publish as publish_verified  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_image  # noqa: E402
import verify_requirements  # noqa: E402


SHA = "a" * 40
MEDIA = "application/vnd.oci.image.manifest.v1+json"


def publish(registry, candidate, sha, current_main, smoke=None):
    return publish_verified(registry, candidate, sha, current_main,
                            smoke or (lambda reference: None))


def digest(body):
    return "sha256:" + hashlib.sha256(body).hexdigest()


class Response(io.BytesIO):
    def __init__(self, body, status=200, media=MEDIA):
        super().__init__(body)
        self.status = status
        self.headers = {"Docker-Content-Digest": digest(body), "Content-Type": media}


class FixtureRegistry:
    def __init__(self):
        self.candidate = b'{"schemaVersion":2,"config":{"digest":"fixture"}}'
        self.manifests = {digest(self.candidate): self.candidate}
        self.writes = []
        self.read_error = None
        self.on_read = None
        self.on_write = None

    def open(self, request, timeout):
        assert timeout == 20
        assert request.full_url.startswith("https://registry.test/v2/example/image/manifests/")
        assert request.get_header("Authorization") == "Bearer fixture"
        ref = request.full_url.rsplit("/", 1)[-1]
        if request.get_method() == "PUT":
            assert request.get_header("Content-type") == MEDIA
            body = request.data
            self.manifests[ref] = body
            self.writes.append(ref)
            if self.on_write:
                self.on_write(ref)
            return Response(b"", 201)
        if self.read_error is not None and ref == SHA:
            raise urllib.error.HTTPError(request.full_url, self.read_error, "fixture", {}, None)
        if self.on_read:
            self.on_read(ref)
        if ref not in self.manifests:
            raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)
        return Response(self.manifests[ref])

    def client(self):
        return Registry("https://registry.test", "example/image", "fixture", self.open)


def test_absent_tag_publishes_verified_commit_and_main():
    fixture = FixtureRegistry()
    candidate = digest(fixture.candidate)
    events = []
    fixture.on_write = lambda reference: events.append("tag:" + reference)
    assert publish(fixture.client(), candidate, SHA, lambda: SHA,
                   lambda reference: events.append("smoke:" + reference))["main_digest"] == candidate
    assert events == [f"smoke:ghcr.io/example/image@{candidate}", "tag:" + SHA, "tag:main"]
    assert fixture.writes == [SHA, "main"]
    assert fixture.manifests[SHA] == fixture.manifests["main"] == fixture.candidate


def test_same_commit_rerun_does_not_mutate_existing_tags():
    fixture = FixtureRegistry()
    candidate = digest(fixture.candidate)
    publish(fixture.client(), candidate, SHA, lambda: SHA)
    fixture.writes.clear()
    publish(fixture.client(), candidate, SHA, lambda: SHA)
    assert fixture.writes == []


def test_changed_build_refuses_existing_commit_without_mutation():
    fixture = FixtureRegistry()
    original = fixture.candidate
    first = digest(original)
    publish(fixture.client(), first, SHA, lambda: SHA)
    fixture.candidate = original + b"changed"
    second = digest(fixture.candidate)
    fixture.manifests[second] = fixture.candidate
    fixture.writes.clear()
    with pytest.raises(RuntimeError, match="commit tag already"):
        publish(fixture.client(), second, SHA, lambda: SHA)
    assert fixture.writes == []
    assert fixture.manifests[SHA] == fixture.manifests["main"] == original


@pytest.mark.parametrize("status", [401, 403, 500])
def test_registry_errors_are_not_treated_as_missing(status):
    fixture = FixtureRegistry()
    fixture.read_error = status
    with pytest.raises(RuntimeError, match=f"HTTP {status}"):
        publish(fixture.client(), digest(fixture.candidate), SHA, lambda: SHA)
    assert fixture.writes == []


def test_old_main_rerun_cannot_move_alias():
    fixture = FixtureRegistry()
    with pytest.raises(RuntimeError, match="main advanced"):
        publish(fixture.client(), digest(fixture.candidate), SHA, lambda: "b" * 40)
    assert fixture.writes == []


def test_main_advancing_before_alias_leaves_alias_untouched():
    fixture = FixtureRegistry()
    fixture.on_write = lambda ref: setattr(fixture, "advanced", ref == SHA)
    with pytest.raises(RuntimeError, match="main advanced"):
        publish(fixture.client(), digest(fixture.candidate), SHA,
                lambda: "b" * 40 if getattr(fixture, "advanced", False) else SHA)
    assert fixture.writes == [SHA]
    assert "main" not in fixture.manifests


def test_main_advancing_during_alias_read_prevents_alias_write():
    fixture = FixtureRegistry()
    fixture.advanced = False
    fixture.on_read = lambda ref: setattr(fixture, "advanced", True) if ref == "main" else None
    with pytest.raises(RuntimeError, match="main advanced"):
        publish(fixture.client(), digest(fixture.candidate), SHA,
                lambda: "b" * 40 if fixture.advanced else SHA)
    assert fixture.writes == [SHA]
    assert "main" not in fixture.manifests


def test_head_advance_during_alias_put_cannot_roll_back_newer_success():
    fixture = FixtureRegistry()
    old_body = fixture.candidate
    fixture.advanced = False
    fixture.on_write = lambda ref: setattr(fixture, "advanced", True) if ref == "main" else None
    old = publish(fixture.client(), digest(old_body), SHA,
                  lambda: "b" * 40 if fixture.advanced else SHA)
    assert old["head_advanced_during_alias"] is True
    assert fixture.manifests["main"] == old_body

    fixture.on_write = None
    new_sha = "b" * 40
    new_body = old_body + b"new-success"
    fixture.manifests[digest(new_body)] = new_body
    publish(fixture.client(), digest(new_body), new_sha, lambda: new_sha)
    assert fixture.manifests["main"] == new_body
    with pytest.raises(RuntimeError, match="main advanced"):
        publish(fixture.client(), digest(old_body), SHA, lambda: new_sha)
    assert fixture.manifests["main"] == new_body


def test_log_timeout_still_stops_and_restores_fixture(monkeypatch):
    actions = []
    def fake_docker(*args, **kwargs):
        actions.append(args[0])
        if args[0] == "logs":
            raise subprocess.TimeoutExpired(["docker", "logs"], 5)
        return subprocess.CompletedProcess(args, 0, "0\n" if args[0] == "inspect" else "", "")
    monkeypatch.setattr(smoke_image, "docker", fake_docker)
    with tempfile.TemporaryDirectory() as directory:
        token_file = Path(directory) / "tokens.env"
        token_file.write_text("synthetic fixture")
        with pytest.raises(RuntimeError, match="fixture cleanup failed"):
            smoke_image.cleanup_fixture("fixture", Path(directory), started=True)
        assert actions == ["logs", "stop", "inspect", "rm"]
    assert not token_file.exists()


def test_diagnostic_log_is_private_at_creation():
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "container.log"
        old_umask = os.umask(0o022)
        try:
            smoke_image.write_private_log(target, "SMOKE_PHASE:exact_mutations\nAssertionError: private note and token")
        finally:
            os.umask(old_umask)
        assert target.stat().st_mode & 0o777 == 0o600
        assert target.read_text() == "SMOKE_PHASE:exact_mutations\nAssertionError\n"


def test_smoke_mounts_read_only_registry_and_app_roots(monkeypatch):
    compile(smoke_image.PEER, 'synthetic_app_peer', 'exec')
    compile(smoke_image.CLIENT, 'container_http_client', 'exec')
    mounts = []

    def fake_docker(*args, **kwargs):
        if args[:2] == ("run", "-d"):
            mounts.extend(args[index + 1] for index, value in enumerate(args[:-1])
                          if value == "--mount")
            registry = next(value for value in mounts if "dst=/etc/obsidian-mcp" in value)
            source = Path(registry.split(",", 1)[1].split("=", 1)[1].split(",", 1)[0])
            assert source.is_dir()
            assert (source.stat().st_mode & 0o777) == 0o555
            assert (source / "registry.json").stat().st_mode & 0o777 == 0o444
            assert "--user" not in args
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "1000:1000\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(smoke_image, "docker", fake_docker)
    monkeypatch.setattr(smoke_image, "cleanup_fixture", lambda *args, **kwargs: None)
    monkeypatch.setattr(smoke_image.time, "sleep", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["smoke_image.py", "--image", "fixture:image"])
    smoke_image.main()
    assert any(value.endswith("dst=/etc/obsidian-mcp,readonly") for value in mounts)
    assert any(value.endswith("dst=/vaults/iam,readonly") for value in mounts)
    assert any(value.endswith("dst=/vaults/homelab,readonly") for value in mounts)


def test_smoke_rejects_image_without_non_root_user(monkeypatch):
    def fake_docker(*args, **kwargs):
        if args[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(smoke_image, "docker", fake_docker)
    monkeypatch.setattr(smoke_image, "cleanup_fixture", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["smoke_image.py", "--image", "fixture:image"])
    with pytest.raises(RuntimeError, match="1000:1000"):
        smoke_image.main()


def test_failed_exact_candidate_smoke_never_advances_tags():
    fixture = FixtureRegistry()
    candidate = digest(fixture.candidate)
    seen = []
    def fail(reference):
        seen.append(reference)
        raise RuntimeError("synthetic smoke failure")
    with pytest.raises(RuntimeError, match="synthetic smoke failure"):
        publish(fixture.client(), candidate, SHA, lambda: SHA, fail)
    assert seen == [f"ghcr.io/example/image@{candidate}"]
    assert fixture.writes == []


def test_publisher_runs_pull_and_smoke_on_same_immutable_reference(monkeypatch):
    import publish_image
    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(publish_image.subprocess, "run", fake_run)
    reference = "ghcr.io/jlengelbrecht/obsidian-mcp@sha256:" + "1" * 64
    publish_image.smoke_candidate(reference)
    assert calls[0] == ["docker", "pull", "--platform", "linux/amd64", reference]
    assert calls[1][:2] == ["python3", "services/obsidian-mcp/tests/verify_requirements.py"]
    assert calls[1][-2:] == ["--image", reference]
    assert all(call[0] != "docker" or call[1] != "tag" for call in calls)


def test_runtime_compatibility_module_is_packaged_directly():
    import server
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert 'COPY --chown=0:0 --chmod=0444 gateway.py approval.py bridge.py owner_auth.py server.py /app/' in dockerfile
    assert 'CMD ["python", "/app/gateway.py"]' in dockerfile
    assert 'RUN printf' not in dockerfile
    assert server.MCP_PATH == '/mcp' and server.MAX_HTTP_BODY == 1024 * 1024


def test_public_requirements_are_complete_and_hashed():
    runtime, dev = verify_requirements.validate_requirements()
    assert runtime >= 50 and dev >= runtime
    assert not (verify_requirements.SERVICE / 'uv.lock').exists()


def test_pip_rejects_corrupted_artifact_hash(tmp_path):
    import zipfile
    wheel = tmp_path / 'integrity_probe-1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('integrity_probe/__init__.py', '')
        archive.writestr('integrity_probe-1.0.dist-info/METADATA',
                         'Metadata-Version: 2.1\nName: integrity-probe\nVersion: 1.0\n')
        archive.writestr('integrity_probe-1.0.dist-info/WHEEL',
                         'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        archive.writestr('integrity_probe-1.0.dist-info/RECORD', '')
    requirements = tmp_path / 'corrupt.txt'
    requirements.write_text('integrity-probe==1.0 --hash=sha256:' + '0' * 64 + '\n')
    with pytest.raises(subprocess.CalledProcessError):
        verify_requirements.install_requirements(sys.executable, requirements, find_links=tmp_path)


def test_workflow_only_publishes_after_verify_and_digest_build():
    workflow = (Path(__file__).resolve().parents[3] /
                ".github/workflows/obsidian-mcp.yaml").read_text()
    publish_job = workflow.split("  publish:\n", 1)[1]
    assert "needs: verify" in publish_job
    assert "group: obsidian-mcp-publication" in publish_job
    assert "cancel-in-progress: false" in publish_job
    assert "push-by-digest=true,name-canonical=true" in publish_job
    assert publish_job.index("docker buildx build") < publish_job.index("publish_image.py")
    assert "docker tag" not in publish_job
    assert 'uv run --locked' not in workflow
    verify_job = workflow.split("  verify:\n", 1)[1].split("  publish:\n", 1)[0]
    assert verify_job.index('verify_requirements.py\n') < verify_job.index('docker build')
    assert verify_job.index('docker build') < verify_job.index('verify_requirements.py --image')
