#!/usr/bin/env python3
"""Fail closed before creating a GHCR commit tag."""
import argparse
import base64
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACCEPT = ("application/vnd.oci.image.manifest.v1+json, "
          "application/vnd.docker.distribution.manifest.v2+json, "
          "application/vnd.oci.image.index.v1+json, "
          "application/vnd.docker.distribution.manifest.list.v2+json")
REALM = "https://ghcr.io/token"
CHALLENGE_PART = re.compile(r'([a-z]+)="([^"\r\n]*)"')


class RemoteError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    """Reject HTTP redirects so credentials stay on GHCR."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        """Leave redirects as errors instead of following them."""
        return None


def _default_open(request, timeout):
    """Open one GHCR request without redirect handling."""
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def _response(request, opener):
    """Return the exact requested URL's response, including HTTP errors."""
    try:
        response = opener(request, timeout=15)
    except HTTPError as exc:
        if exc.url != request.full_url:
            raise RemoteError("unexpected response URL") from None
        return exc
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        raise RemoteError("registry request failed") from None
    if response.geturl() != request.full_url:
        response.close()
        raise RemoteError("unexpected response URL")
    return response


def _read(response, limit):
    """Read a bounded response while hiding network error details."""
    try:
        return response.read(limit)
    except (URLError, TimeoutError, OSError, ValueError, TypeError):
        raise RemoteError("registry response failed") from None


def _challenge(value, repository):
    """Accept only a scoped GHCR pull challenge for this repository."""
    if not isinstance(value, str) or not value.startswith("Bearer "):
        raise RemoteError("invalid registry challenge")
    parts = value[len("Bearer "):]
    attributes = {}
    while parts:
        match = CHALLENGE_PART.match(parts)
        if match is None or match.group(1) in attributes:
            raise RemoteError("invalid registry challenge")
        attributes[match.group(1)] = match.group(2)
        parts = parts[match.end():]
        if parts:
            if not parts.startswith(","):
                raise RemoteError("invalid registry challenge")
            parts = parts[1:]
            if not parts:
                raise RemoteError("invalid registry challenge")
    if attributes != {"realm": REALM, "service": "ghcr.io",
                      "scope": f"repository:{repository}:pull"}:
        raise RemoteError("unexpected registry challenge")
    return attributes


def _bearer(repository, actor, token, challenge, opener):
    """Exchange workflow credentials for a bounded GHCR Bearer token."""
    attributes = _challenge(challenge, repository)
    url = REALM + "?" + urlencode({"service": attributes["service"],
                                   "scope": attributes["scope"]})
    basic = base64.b64encode(f"{actor}:{token}".encode()).decode("ascii")
    request = Request(url, headers={"Authorization": "Basic " + basic,
                                    "Accept": "application/json"})
    with _response(request, opener) as response:
        if response.status != 200:
            raise RemoteError("token exchange failed")
        body = _read(response, 65537)
    if len(body) > 65536:
        raise RemoteError("invalid token response")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        raise RemoteError("invalid token response") from None
    if not isinstance(payload, dict):
        raise RemoteError("invalid token response")
    bearer = payload.get("token", payload.get("access_token"))
    if (not isinstance(bearer, str) or not bearer or not bearer.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in bearer)
            or ("token" in payload and "access_token" in payload
                and payload["token"] != payload["access_token"])):
        raise RemoteError("invalid token response")
    return bearer


def _manifest(repository, reference, bearer, opener, allow_absent=False):
    """Read a manifest digest, allowing 404 only for the commit tag."""
    url = f"https://ghcr.io/v2/{repository}/manifests/{reference}"
    request = Request(url, headers={"Authorization": "Bearer " + bearer,
                                    "Accept": ACCEPT})
    with _response(request, opener) as response:
        if response.status == 404 and allow_absent:
            body = _read(response, 65537)
            if len(body) > 65536:
                raise RemoteError("invalid manifest absence response")
            try:
                data = json.loads(body)
            except (ValueError, TypeError):
                raise RemoteError("invalid manifest absence response") from None
            errors = data.get("errors") if isinstance(data, dict) else None
            if (not isinstance(errors, list) or len(errors) != 1
                    or not isinstance(errors[0], dict)
                    or errors[0].get("code") != "MANIFEST_UNKNOWN"):
                raise RemoteError("ambiguous manifest absence")
            return None
        if response.status != 200:
            raise RemoteError("manifest lookup failed")
        digest = response.headers.get("Docker-Content-Digest", "")
        if DIGEST.fullmatch(digest) is None:
            raise RemoteError("invalid manifest digest")
        body = _read(response, 1048577)
    if len(body) > 1048576:
        raise RemoteError("invalid manifest body")
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        raise RemoteError("invalid manifest body") from None
    if not isinstance(data, dict) or data.get("schemaVersion") != 2:
        raise RemoteError("invalid manifest body")
    return digest


def tag_state(repository, candidate_reference, candidate_digest, commit_sha,
              actor, token, opener=_default_open):
    """Prove the candidate first, then classify the immutable commit tag."""
    if (not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", repository)
            or "//" in repository or repository.endswith("/")
            or DIGEST.fullmatch(candidate_digest) is None
            or SHA.fullmatch(commit_sha) is None
            or not re.fullmatch(r"candidate-[0-9]+-[0-9]+", candidate_reference)
            or not re.fullmatch(r"[A-Za-z0-9-]+(?:\[bot\])?", actor)
            or not token or "\r" in token or "\n" in token):
        raise RemoteError("invalid guard input")
    url = f"https://ghcr.io/v2/{repository}/manifests/{candidate_reference}"
    request = Request(url, headers={"Accept": ACCEPT})
    with _response(request, opener) as response:
        if response.status != 401:
            raise RemoteError("registry challenge required")
        challenge = response.headers.get("WWW-Authenticate")
    bearer = _bearer(repository, actor, token, challenge, opener)
    observed = _manifest(repository, candidate_reference, bearer, opener)
    if observed != candidate_digest:
        raise RemoteError("candidate manifest unavailable or changed")
    existing = _manifest(repository, "sha-" + commit_sha, bearer, opener,
                         allow_absent=True)
    return "absent" if existing is None else "present"


def main():
    """Run the guard with credentials supplied by the workflow environment."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--candidate-reference", required=True)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--commit-sha", required=True)
    args = parser.parse_args()
    try:
        state = tag_state(args.repository, args.candidate_reference,
                          args.candidate_digest, args.commit_sha,
                          os.environ.get("GITHUB_ACTOR", ""),
                          os.environ.get("GITHUB_TOKEN", ""))
    except RemoteError as exc:
        print(f"commit tag guard: {exc}", file=sys.stderr)
        return 1
    if state == "present":
        print("commit tag already exists; refusing overwrite", file=sys.stderr)
        return 1
    print("commit tag confirmed absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
