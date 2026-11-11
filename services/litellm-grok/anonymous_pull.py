#!/usr/bin/env python3
"""Verify exact GHCR manifest bytes through an anonymous registry exchange."""
import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REFERENCE = re.compile(r"ghcr\.io/([a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)+)@sha256:([0-9a-f]{64})\Z")
ACCEPT = ", ".join(("application/vnd.oci.image.index.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json",
                    "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.v2+json"))
MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 15


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never send a bearer token to a redirect target."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("registry redirect denied")


def _open(request, opener):
    """Return an HTTP response, including a 401 challenge, without redirects."""
    try:
        return opener(request, timeout=TIMEOUT)
    except urllib.error.HTTPError as error:
        return error


def _read(request, opener, allowed):
    """Read a bounded response from exactly the requested GHCR URL."""
    with _open(request, opener) as response:
        if response.geturl() != request.full_url or response.status not in allowed:
            raise ValueError("unexpected GHCR response or redirect")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("GHCR response too large")
        return response.status, response.headers, body


def _challenge(header, repository):
    """Accept only GHCR's pull-scoped bearer challenge."""
    if not isinstance(header, str) or not header.startswith("Bearer "):
        raise ValueError("missing GHCR bearer challenge")
    if re.fullmatch(r'(?:\w+="[^"\r\n]*")(?:,\s*\w+="[^"\r\n]*"){2}', header[7:]) is None:
        raise ValueError("malformed GHCR bearer challenge")
    pairs = re.findall(r'(\w+)="([^"\r\n]*)"', header[7:])
    if len(pairs) != 3 or len(dict(pairs)) != 3:
        raise ValueError("malformed GHCR bearer challenge")
    values = dict(pairs)
    if (values.get("realm") != "https://ghcr.io/token" or
            values.get("service") != "ghcr.io" or
            values.get("scope") != f"repository:{repository}:pull"):
        raise ValueError("unexpected GHCR token scope or origin")
    return values


def verify_anonymous_access(reference, opener=None):
    """Fetch and hash exact manifest bytes without Docker cache or credentials."""
    match = REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError("expected exact GHCR digest reference")
    repository, hex_digest = match.groups()
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                             NoRedirect()).open
    manifest_url = f"https://ghcr.io/v2/{repository}/manifests/sha256:{hex_digest}"
    def manifest_request(token=None):
        headers = {"Accept": ACCEPT}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return urllib.request.Request(manifest_url, headers=headers)

    status, headers, _ = _read(manifest_request(), opener, {401})
    challenge = _challenge(headers.get("WWW-Authenticate"), repository)
    query = urllib.parse.urlencode({"service": challenge["service"],
                                    "scope": challenge["scope"]})
    token_url = challenge["realm"] + "?" + query
    status, _, body = _read(urllib.request.Request(token_url,
                             headers={"Accept": "application/json"}), opener, {200})
    try:
        token = json.loads(body)["token"]
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("missing anonymous GHCR token") from error
    if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
        raise ValueError("invalid anonymous GHCR token")
    status, headers, body = _read(manifest_request(token), opener, {200})
    if headers.get("Content-Type", "").split(";", 1)[0] not in ACCEPT.split(", "):
        raise ValueError("unexpected manifest media type")
    if headers.get("Docker-Content-Digest") != "sha256:" + hex_digest:
        raise ValueError("manifest digest header mismatch")
    if hashlib.sha256(body).hexdigest() != hex_digest:
        raise ValueError("manifest bytes do not match requested digest")


def main():
    """Stop promotion when the exact manifest cannot be read anonymously."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    args = parser.parse_args()
    try:
        verify_anonymous_access(args.reference)
    except (OSError, ValueError):
        print("anonymous GHCR digest read failed; owner: check package visibility, "
              "set Public if intended, then rerun. No tag was promoted by this run.",
              file=sys.stderr)
        return 1
    print("anonymous GHCR exact digest read passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
