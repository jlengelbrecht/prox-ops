#!/usr/bin/env python3
"""Fetch pinned public GitHub release assets for CI, verifying bytes before use."""

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import signal
import shutil
import sys
import tarfile
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


API = "https://api.github.com"
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024
REQUEST_DEADLINE_SECONDS = 120
FLUX_ASSETS = {
    "kubernetes/apps/ai/kserve/app/kustomization.yaml": (
        ("kserve/kserve", "v0.21.0", "kserve.yaml"),
        ("kserve/kserve", "v0.21.0", "kserve-cluster-resources.yaml"),
    ),
    "kubernetes/apps/kube-system/knative-serving/app/kustomization.yaml": (
        ("knative/serving", "knative-v1.21.3", "serving-core.yaml"),
        ("knative/net-kourier", "knative-v1.21.1", "kourier.yaml"),
    ),
}


class DownloadError(Exception):
    """Report a verified-download failure without exposing response data."""
    pass


class NoRedirect(HTTPRedirectHandler):
    """Leave redirects to the caller's explicit endpoint checks."""
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        """Prevent urllib from following a redirect automatically."""
        return None


OPENER = build_opener(NoRedirect)


def read_before_deadline(response, size, deadline):
    """Bound a blocking body read on the Linux CI runner, even for slow peers."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DownloadError("GitHub download deadline exceeded")
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(_signum, _frame):
        """Interrupt a body read that outlives the shared deadline."""
        raise DownloadError("GitHub download deadline exceeded")

    try:
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, remaining)
        chunk = response.read(size)
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous_handler)
    if time.monotonic() >= deadline:
        raise DownloadError("GitHub download deadline exceeded")
    return chunk


def request_bytes(url, token="", asset=False, max_bytes=None):
    """Follow only GitHub asset redirects; never forward API authorization."""
    limit = max_bytes if max_bytes is not None else MAX_METADATA_BYTES
    if not isinstance(limit, int) or limit <= 0 or limit > MAX_ASSET_BYTES:
        raise DownloadError("Invalid download size limit")
    deadline = time.monotonic() + REQUEST_DEADLINE_SECONDS
    for attempt in range(3):
        current = url
        try:
            for redirect in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DownloadError("GitHub download deadline exceeded")
                parsed = urlparse(current)
                if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or parsed.hostname not in (
                    "api.github.com", "release-assets.githubusercontent.com",
                    "objects.githubusercontent.com",
                ):
                    raise DownloadError("Unexpected download endpoint")
                if not asset and parsed.hostname != "api.github.com":
                    raise DownloadError("Unexpected metadata endpoint")
                headers = {"Accept": "application/octet-stream" if asset else "application/vnd.github+json"}
                if token and parsed.hostname == "api.github.com":
                    headers["Authorization"] = "Bearer " + token
                req = Request(current, headers=headers)
                try:
                    with OPENER.open(req, timeout=min(15, remaining)) as response:
                        length = response.headers.get("Content-Length")
                        if length is not None and (not length.isdecimal() or int(length) > limit):
                            raise DownloadError("Download exceeds size limit")
                        chunks = []
                        size = 0
                        while True:
                            if time.monotonic() >= deadline:
                                raise DownloadError("GitHub download deadline exceeded")
                            chunk = read_before_deadline(response, min(65536, limit - size + 1), deadline)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > limit:
                                raise DownloadError("Download exceeds size limit")
                            chunks.append(chunk)
                        body = b"".join(chunks)
                        kind = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                        return body, kind
                except HTTPError as exc:
                    exc.close()
                    if exc.code not in (301, 302, 303, 307, 308):
                        raise
                    location = exc.headers.get("Location", "")
                    target = urlparse(location)
                    allowed_asset_host = (asset and parsed.hostname == "api.github.com" and
                                          target.hostname in ("release-assets.githubusercontent.com", "objects.githubusercontent.com"))
                    allowed_canonical_asset = (asset and parsed.hostname == "api.github.com" and
                                               target.hostname == "api.github.com" and
                                               re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/assets/[0-9]+", parsed.path) and
                                               re.fullmatch(r"/repositories/[0-9]+/releases/assets/[0-9]+", target.path) and
                                               parsed.path.rsplit("/", 1)[-1] == target.path.rsplit("/", 1)[-1] and
                                               not target.query and not target.fragment)
                    allowed_metadata = (not asset and parsed.hostname == "api.github.com" and
                                        target.hostname == "api.github.com" and
                                        re.fullmatch(r"/repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/tags/[A-Za-z0-9_.-]+", parsed.path) and
                                        re.fullmatch(r"/repositories/[0-9]+/releases/tags/[A-Za-z0-9_.-]+", target.path) and
                                        parsed.path.rsplit("/", 1)[-1] == target.path.rsplit("/", 1)[-1] and
                                        not target.query and not target.fragment)
                    if (target.scheme != "https" or target.username or target.password or target.port or
                            not (allowed_asset_host or allowed_canonical_asset or allowed_metadata)):
                        raise DownloadError("Unexpected release redirect") from exc
                    current = location
            raise DownloadError("Too many release redirects")
        except (HTTPError, URLError, OSError, http.client.HTTPException) as exc:
            if isinstance(exc, HTTPError):
                status = exc.code if type(exc.code) is int else None
                if status not in (408, 429, 500, 502, 503, 504):
                    raise DownloadError(f"GitHub download failed: HTTP {status or 'unknown'}") from exc
                detail = f"HTTP {status}"
            else:
                detail = type(exc).__name__
            if attempt == 2:
                raise DownloadError(f"GitHub download failed after bounded retries ({detail})") from exc
            if time.monotonic() + attempt + 1 >= deadline:
                raise DownloadError("GitHub download deadline exceeded") from exc
            time.sleep(attempt + 1)
    raise AssertionError("unreachable")


def release_assets(repo, tag, token=""):
    """Read one exact release's asset metadata from GitHub."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
        raise DownloadError("Invalid release identity")
    body, kind = request_bytes(f"{API}/repos/{repo}/releases/tags/{quote(tag)}", token)
    if kind != "application/json":
        raise DownloadError("Release metadata is not JSON")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise DownloadError("Invalid release metadata JSON") from exc
    if not isinstance(data, dict) or data.get("tag_name") != tag or not isinstance(data.get("assets"), list):
        raise DownloadError("Release tag mismatch or missing assets")
    if not all(isinstance(item, dict) for item in data["assets"]):
        raise DownloadError("Invalid release asset entries")
    return data["assets"]


def fetch_asset(repo, tag, name, token=""):
    """Return an asset only after matching its declared size and digest."""
    assets = release_assets(repo, tag, token)
    if not isinstance(assets, list) or not all(isinstance(item, dict) for item in assets):
        raise DownloadError("Invalid release asset entries")
    matches = [item for item in assets if item.get("name") == name]
    if len(matches) != 1:
        raise DownloadError("Missing or ambiguous release asset: " + name)
    item = matches[0]
    digest = item.get("digest", "")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        raise DownloadError("Missing SHA256 in GitHub release metadata: " + name)
    asset_id = item.get("id")
    if (type(asset_id) is not int or asset_id <= 0 or type(item.get("size")) is not int or
            not 0 < item["size"] <= MAX_ASSET_BYTES):
        raise DownloadError("Invalid release asset metadata: " + name)
    body, kind = request_bytes(f"{API}/repos/{repo}/releases/assets/{asset_id}", token, asset=True,
                               max_bytes=item["size"])
    if kind in ("application/json", "text/html") or len(body) != item["size"]:
        raise DownloadError("Non-asset or incomplete response: " + name)
    if hashlib.sha256(body).hexdigest() != digest[7:].lower():
        raise DownloadError("Release asset digest mismatch: " + name)
    return body


def gitleaks(version, output, token=""):
    """Verify and extract the single Gitleaks executable from its release."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise DownloadError("Invalid Gitleaks version")
    repo, tag = "gitleaks/gitleaks", "v" + version
    filename = f"gitleaks_{version}_linux_x64.tar.gz"
    checksum_name = f"gitleaks_{version}_checksums.txt"
    checksum = fetch_asset(repo, tag, checksum_name, token).decode("ascii")
    entries = []
    for line in checksum.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})  \*?([^/\\\s]+)", line)
        if match and match.group(2) == filename:
            entries.append(match.group(1).lower())
    if len(entries) != 1:
        raise DownloadError("Missing or ambiguous exact Gitleaks checksum")
    archive = fetch_asset(repo, tag, filename, token)
    if hashlib.sha256(archive).hexdigest() != entries[0]:
        raise DownloadError("Gitleaks checksum mismatch")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "release.tar.gz"
        path.write_bytes(archive)
        with tarfile.open(path, "r:gz") as tar:
            members = [member for member in tar.getmembers() if member.name == "gitleaks" and member.isfile()]
            if len(members) != 1:
                raise DownloadError("Gitleaks executable missing or ambiguous in archive")
            source = tar.extractfile(members[0])
            if source is None:
                raise DownloadError("Cannot extract Gitleaks executable")
            with Path(output).open("wb") as target:
                shutil.copyfileobj(source, target)
    Path(output).chmod(0o755)


def resource_nodes(content):
    """Locate plain scalar resources without YAML aliases."""
    import yaml  # Flux jobs install the repository's existing pinned PyYAML version.

    class ResourceLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            """Reject aliases before they can change resource semantics."""
            if self.check_event(yaml.AliasEvent):
                raise DownloadError("YAML aliases are not supported in release resources")
            return super().compose_node(parent, index)

    try:
        document = yaml.compose(content, Loader=ResourceLoader)
    except yaml.YAMLError as exc:
        raise DownloadError("Invalid Kustomization YAML") from exc
    if not isinstance(document, yaml.MappingNode):
        raise DownloadError("Invalid Kustomization document")
    matches = [value for key, value in document.value
               if isinstance(key, yaml.ScalarNode) and key.value == "resources"]
    if len(matches) != 1 or not isinstance(matches[0], yaml.SequenceNode):
        raise DownloadError("Expected one resources sequence")
    resources = matches[0].value
    if not all(isinstance(node, yaml.ScalarNode) and node.style is None and
               node.tag == "tag:yaml.org,2002:str" for node in resources):
        raise DownloadError("Unexpected resource shape")
    return resources


def materialize_flux(root, token=""):
    """Stage verified manifests and rewrite only their pinned resource URLs."""
    root = Path(root).resolve()
    planned = []
    for relative, assets in FLUX_ASSETS.items():
        path = root / relative
        content = path.read_text()
        nodes = resource_nodes(content)
        expected = {f"https://github.com/{repo}/releases/download/{tag}/{name}"
                    for repo, tag, name in assets}
        values = [node.value for node in nodes]
        if any("://" in value and value not in expected for value in values):
            raise DownloadError("Unexpected remote resource in " + relative)
        if any(values.count(url) != 1 for url in expected):
            raise DownloadError("Expected exactly one of each pinned release resource in " + relative)
        replacements = {}
        for repo, tag, name in assets:
            url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
            body = fetch_asset(repo, tag, name, token)
            if not body.strip() or body.lstrip().startswith((b"{", b"<")):
                raise DownloadError("Non-YAML release asset: " + name)
            local = ".ci-release-" + hashlib.sha256(url.encode()).hexdigest()[:16] + ".yaml"
            planned.append((path.parent / local, body))
            replacements[url] = "./" + local
        for node in sorted((node for node in nodes if node.value in replacements),
                           key=lambda node: node.start_mark.index, reverse=True):
            content = (content[:node.start_mark.index] + replacements[node.value] +
                       content[node.end_mark.index:])
        rewritten = [node.value for node in resource_nodes(content)]
        if rewritten != [replacements.get(value, value) for value in values]:
            raise DownloadError("Rewritten resources differ from pinned mapping")
        planned.append((path, content.encode()))
    # No source changes occur until every pinned asset has passed validation.
    staged = []
    originals = {}
    installed = []
    try:
        for path, body in planned:
            originals[path] = path.read_bytes() if path.exists() else None
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".ci-release-stage-", delete=False) as temp:
                temp.write(body)
                staged.append(Path(temp.name))
        for (path, _), temp in zip(planned, staged):
            os.replace(temp, path)
            installed.append(path)
    except OSError:
        for path in reversed(installed):
            previous = originals[path]
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        raise
    finally:
        for temp in staged:
            temp.unlink(missing_ok=True)


def main():
    """Run the requested CI download operation with sanitized failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)
    leak = command.add_parser("gitleaks")
    leak.add_argument("--version", required=True)
    leak.add_argument("--output", required=True)
    flux = command.add_parser("flux")
    flux.add_argument("--root", required=True)
    args = parser.parse_args()
    try:
        if args.command == "gitleaks":
            gitleaks(args.version, args.output, os.environ.get("GITHUB_TOKEN", ""))
        else:
            materialize_flux(args.root, os.environ.get("GITHUB_TOKEN", ""))
    except (DownloadError, OSError, UnicodeError, ValueError, tarfile.TarError) as exc:
        print("release_download: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
