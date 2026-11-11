#!/usr/bin/env python3
"""Fail closed before creating a GHCR commit tag.

The candidate manifest must prove authenticated repository read access first.
Only a subsequent 404 for the commit tag means confirmed absence.
"""
import argparse
import base64
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
ACCEPT = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


class RemoteError(Exception):
    pass


def manifest(repository, reference, actor, token, opener=urlopen):
    authorization = base64.b64encode(f"{actor}:{token}".encode()).decode()
    request = Request(f"https://ghcr.io/v2/{repository}/manifests/{reference}",
                      headers={"Authorization": "Basic " + authorization, "Accept": ACCEPT})
    try:
        with opener(request, timeout=15) as response:
            if response.status != 200:
                raise RemoteError("unexpected manifest status")
            digest = response.headers.get("Docker-Content-Digest", "")
            if DIGEST.fullmatch(digest) is None:
                raise RemoteError("invalid manifest digest")
            data = json.loads(response.read(1024 * 1024 + 1))
            if not isinstance(data, dict) or data.get("schemaVersion") != 2:
                raise RemoteError("invalid manifest body")
            return digest
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise RemoteError("manifest lookup failed") from None
    except (URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
        raise RemoteError("manifest lookup failed") from None


def tag_state(repository, candidate_reference, candidate_digest, commit_sha,
              actor, token, opener=urlopen):
    if (not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*", repository)
            or DIGEST.fullmatch(candidate_digest) is None
            or SHA.fullmatch(commit_sha) is None
            or not re.fullmatch(r"candidate-[0-9]+-[0-9]+", candidate_reference)
            or not actor or not token):
        raise RemoteError("invalid guard input")
    observed = manifest(repository, candidate_reference, actor, token, opener)
    if observed != candidate_digest:
        raise RemoteError("candidate manifest unavailable or changed")
    existing = manifest(repository, "sha-" + commit_sha, actor, token, opener)
    return "absent" if existing is None else "present"


def main():
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
