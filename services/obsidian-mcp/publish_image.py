"""Publish verified GHCR tags after a digest-only build."""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


MEDIA_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
ACCEPT = ", ".join(MEDIA_TYPES)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


class Registry:
    def __init__(self, base, repository, bearer, opener=urllib.request.urlopen):
        self.base = base.rstrip("/")
        self.repository = repository
        self.bearer = bearer
        self.opener = opener

    def manifest(self, reference, *, absent_ok=False):
        url = f"{self.base}/v2/{self.repository}/manifests/{reference}"
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.bearer}", "Accept": ACCEPT,
        })
        try:
            with self.opener(request, timeout=20) as response:
                body = response.read()
                digest = "sha256:" + hashlib.sha256(body).hexdigest()
                if response.headers.get("Docker-Content-Digest") != digest:
                    raise RuntimeError("registry digest disagrees with manifest bytes")
                return body, response.headers.get("Content-Type"), digest
        except urllib.error.HTTPError as exc:
            if absent_ok and exc.code == 404:
                return None
            raise RuntimeError(f"registry manifest read failed: HTTP {exc.code}") from None

    def tag(self, reference, body, media_type, digest):
        url = f"{self.base}/v2/{self.repository}/manifests/{reference}"
        request = urllib.request.Request(url, data=body, method="PUT", headers={
            "Authorization": f"Bearer {self.bearer}", "Content-Type": media_type,
        })
        try:
            with self.opener(request, timeout=20) as response:
                if response.status not in (201, 202):
                    raise RuntimeError("registry refused manifest tag")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"registry manifest tag failed: HTTP {exc.code}") from None
        actual = self.manifest(reference)
        if actual[2] != digest:
            raise RuntimeError("registry tag digest mismatch after publication")


def publish(registry, digest, sha, current_main, smoke_candidate):
    if not DIGEST.fullmatch(digest) or not SHA.fullmatch(sha):
        raise ValueError("invalid image digest or commit SHA")
    if current_main() != sha:
        raise RuntimeError("main advanced; refusing an old commit publication")
    candidate = registry.manifest(digest)
    if candidate[2] != digest or not candidate[1] or candidate[1].split(";")[0] not in MEDIA_TYPES:
        raise RuntimeError("candidate manifest is invalid")
    # The callback must exercise this immutable registry reference. No mutable
    # tag exists before it succeeds, including the commit tag.
    smoke_candidate(f"ghcr.io/{registry.repository}@{digest}")
    if current_main() != sha:
        raise RuntimeError("main advanced after candidate smoke")
    existing = registry.manifest(sha, absent_ok=True)
    if existing is not None and existing[2] != digest:
        raise RuntimeError("commit tag already points at another digest")
    if existing is None:
        registry.tag(sha, candidate[0], candidate[1], digest)
    if current_main() != sha:
        raise RuntimeError("main advanced before alias publication")
    main = registry.manifest("main", absent_ok=True)
    if current_main() != sha:
        raise RuntimeError("main advanced while checking alias")
    if main is None or main[2] != digest:
        # Only this workflow writes these tags. Its publish jobs are serialized;
        # a Git push between this check and PUT may advance HEAD, but cannot
        # represent a newer successfully published image until a later job runs.
        registry.tag("main", candidate[0], candidate[1], digest)
    head_at_completion = current_main()
    return {"image_digest": digest, "commit_tag": sha, "main_digest": digest,
            "head_advanced_during_alias": head_at_completion != sha}


def github_bearer(repository):
    actor = os.environ["GITHUB_ACTOR"]
    token = os.environ["GITHUB_TOKEN"]
    scope = f"repository:{repository}:pull,push"
    url = "https://ghcr.io/token?" + urllib.parse.urlencode({"service": "ghcr.io", "scope": scope})
    basic = base64.b64encode(f"{actor}:{token}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})
    with urllib.request.urlopen(request, timeout=20) as response:
        bearer = json.load(response).get("token")
    if not bearer:
        raise RuntimeError("registry did not issue a bearer credential")
    return bearer


def current_main_sha():
    result = subprocess.run(["git", "ls-remote", "--exit-code", "origin", "refs/heads/main"],
                            capture_output=True, text=True, check=True,
                            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    fields = result.stdout.strip().split()
    if len(fields) != 2 or fields[1] != "refs/heads/main" or not SHA.fullmatch(fields[0]):
        raise RuntimeError("could not verify current main commit")
    return fields[0]


def smoke_candidate(reference):
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", reference],
                   check=True, timeout=120)
    subprocess.run(["python3", "services/obsidian-mcp/tests/verify_requirements.py",
                    "--image", reference], check=True, timeout=240)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    if args.image != "ghcr.io/jlengelbrecht/obsidian-mcp":
        raise ValueError("unexpected publication image")
    digest = json.loads(args.metadata.read_text())["containerimage.digest"]
    sha = os.environ["GITHUB_SHA"]
    registry = Registry("https://ghcr.io", args.image.removeprefix("ghcr.io/"),
                        github_bearer(args.image.removeprefix("ghcr.io/")))
    receipt = publish(registry, digest, sha, current_main_sha, smoke_candidate)
    summary = os.environ["GITHUB_STEP_SUMMARY"]
    with open(summary, "a", encoding="utf-8") as output:
        output.write(f"Tested immutable image: `{args.image}@{digest}`; deployment pin: `{args.image}:main@{digest}`; commit tag: `{sha}`.\n")
        if receipt["head_advanced_during_alias"]:
            output.write("Main advanced during alias publication; the next successful serialized publication updates the alias.\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, urllib.error.URLError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"image publication failed: {type(exc).__name__}") from None
