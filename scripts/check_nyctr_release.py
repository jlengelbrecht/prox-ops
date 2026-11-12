"""Fail closed unless Nyctr's chart, image version and optional registry digest agree."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

import yaml

APP = Path('kubernetes/apps/security/nyctr/app')
IMAGE = 'ghcr.io/homelab-0/nyctr'
VERSION = re.compile(r'v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?\Z')
ANNOTATION = re.compile(r'^\s*# renovate: datasource=docker depName=ghcr\.io/homelab-0/nyctr version=(\S+)\s*$', re.M)
PIN = re.compile(r'# renovate: datasource=docker depName=ghcr\.io/homelab-0/nyctr version=(\S+)[ \t]*\n[ \t]*digest:[ \t]*(sha256:[a-f0-9]{64})[ \t]*$', re.M)
DIGEST = re.compile(r'sha256:[a-f0-9]{64}\Z')
LIMIT = 1024 * 1024


class ReleaseError(ValueError):
    pass


def version(value):
    match = VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or any(p.isdigit() and len(p) > 1 and p[0] == '0'
                        for p in (match[4] or '').split('.')):
        raise ReleaseError('Nyctr release versions must be complete semantic versions')
    return value.removeprefix('v')


def check(root):
    try:
        text = (root / APP / 'helmrelease.yaml').read_text()
        hr = yaml.safe_load(text)
        chart = yaml.safe_load((root / APP / 'ocirepository.yaml').read_text())
        image = hr['spec']['values']['image']
        tags = ANNOTATION.findall(text)
        if len(tags) != 1 or not tags[0].startswith('v'):
            raise ReleaseError('Expected exactly one tracked Nyctr image version annotation')
        if image['repository'] != IMAGE or 'tag' in image or not DIGEST.fullmatch(image['digest']):
            raise ReleaseError('Nyctr must use its expected image repository and a digest-only pin')
        if PIN.findall(text) != [(tags[0], image['digest'])]:
            raise ReleaseError('Image version annotation must track the adjacent image digest')
        if chart['spec']['url'] != 'oci://ghcr.io/homelab-0/charts/nyctr':
            raise ReleaseError('Unexpected Nyctr chart repository')
        ref = chart['spec']['ref']
        if set(ref) - {'tag', 'digest'}:
            raise ReleaseError('Nyctr chart reference must use only tag and optional digest')
        chart_digest = ref.get('digest')
        if 'digest' in ref and (not isinstance(chart_digest, str) or not DIGEST.fullmatch(chart_digest)):
            raise ReleaseError('Nyctr chart digest must be a complete SHA-256 pin')
        if version(ref['tag']) != version(tags[0]):
            raise ReleaseError('Nyctr chart tag and image version do not match')
        return tags[0], image['digest'], ref['tag'], chart_digest
    except (KeyError, TypeError, OSError, yaml.YAMLError) as error:
        raise ReleaseError('Missing or invalid Nyctr release manifests') from None


def fetch(request):
    with urllib.request.urlopen(request, timeout=20) as response:
        body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise ReleaseError('Registry response exceeded the size limit')
    return body


def verify_digest(tag, digest, repository="homelab-0/nyctr"):
    if repository not in {"homelab-0/nyctr", "homelab-0/charts/nyctr"}:
        raise ReleaseError("Unexpected Nyctr registry repository")
    # The public registry's anonymous pull token stays in process memory.
    # Never emit response bodies, request headers, tokens or raw HTTP errors.
    try:
        token_url = 'https://ghcr.io/token?' + urllib.parse.urlencode({
            'service': 'ghcr.io', 'scope': f'repository:{repository}:pull'})
        token = json.loads(fetch(urllib.request.Request(token_url)))['token']
        request = urllib.request.Request(
            f'https://ghcr.io/v2/{repository}/manifests/' + urllib.parse.quote(tag, safe=''),
            headers={'Authorization': 'Bearer ' + token, 'Accept': ', '.join([
                'application/vnd.oci.image.index.v1+json',
                'application/vnd.docker.distribution.manifest.list.v2+json',
                'application/vnd.oci.image.manifest.v1+json',
                'application/vnd.docker.distribution.manifest.v2+json'])})
        actual = 'sha256:' + hashlib.sha256(fetch(request)).hexdigest()
        if digest is not None and actual != digest:
            raise ReleaseError('Pinned Nyctr artifact digest does not match its published version tag')
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise ReleaseError(f'Nyctr registry lookup failed (HTTP {status})') from None
    except (urllib.error.URLError, TimeoutError, KeyError, TypeError, ValueError) as error:
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError('Nyctr registry lookup could not be verified') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--verify-digest', action='store_true')
    args = parser.parse_args()
    try:
        tag, digest, chart_tag, chart_digest = check(args.root)
        if args.verify_digest:
            verify_digest(tag, digest)
            verify_digest(chart_tag, chart_digest, 'homelab-0/charts/nyctr')
    except ReleaseError as error:
        print(f'Nyctr release check failed: {error}', file=sys.stderr)
        return 1
    print(f'Nyctr release matches: {tag}' + ('; published image and chart artifacts verified' if args.verify_digest else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
