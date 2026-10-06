#!/usr/bin/env python3
"""Verify logging tool archives and safe members before writing required binaries."""
import hashlib
import pathlib
import stat
import sys
import tarfile
import zipfile

HELM_ARCHIVE = 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496'
HELM_BINARY = 'e4722a77de9df824214aaf19d43687c49f7cbcf3b107905acea002173777d486'
PROMTAIL_ARCHIVE = 'b70d5a5e259a64f6f9b6805bd42d725e50978bcd520fb46113a86cd8b418bbca'
PROMTAIL_BINARY = '3b0e6d48f9973244e0b17c79e478eae53edae60378eaeaad32b749d42dbe4609'
# Renovate bumps the tag and the official alloy-linux-amd64.zip digest together,
# grouped with the image tag; the archive digest alone pins the member bytes.
# renovate: datasource=github-release-attachments depName=grafana/alloy
ALLOY_VERSION = 'v1.20.1'
ALLOY_ARCHIVE = '451fe650e8277d22d69cb8db50bba809f581fe78decba7fce4027ef185457be9'
ALLOY_URL = f'https://github.com/grafana/alloy/releases/download/{ALLOY_VERSION}/alloy-linux-amd64.zip'
# The chart the workflow pulls, pinned by its grafana/helm-charts release tag and
# alloy-<version>.tgz digest; grouped with the HelmRelease chart version.
# renovate: datasource=github-release-attachments depName=alloy packageName=grafana/helm-charts
ALLOY_CHART_TAG = 'alloy-1.13.0'
ALLOY_CHART_ARCHIVE = 'bfdda6cb770c3526444897b9cb5a4fb33711c608d364d9e7857ec699a2fff4fb'
ALLOY_CHART_VERSION = ALLOY_CHART_TAG.removeprefix('alloy-')
MAX_MEMBER = 200_000_000
# Alloy bundles every component into one binary, several times Promtail's size.
MAX_ALLOY_MEMBER = 800_000_000


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_verified(payload, expected, destination, limit=MAX_MEMBER):
    if len(payload) > limit or expected is not None and hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError('binary checksum mismatch')
    destination = pathlib.Path(destination)
    if destination.exists():
        raise ValueError('destination exists')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def verified_helm(archive, destination):
    if digest(archive) != HELM_ARCHIVE:
        raise ValueError('archive checksum mismatch')
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = [member.name.rstrip('/') for member in members]
        allowed = {'linux-amd64', 'linux-amd64/helm', 'linux-amd64/LICENSE', 'linux-amd64/README.md'}
        if len(names) != len(set(names)) or not set(names) <= allowed or 'linux-amd64/helm' not in names:
            raise ValueError('unsafe archive members')
        if any(member.size > MAX_MEMBER or not (member.isfile() or member.isdir()) for member in members):
            raise ValueError('unsafe archive members')
        member = tar.getmember('linux-amd64/helm')
        if not member.isfile():
            raise ValueError('unsafe archive members')
        source = tar.extractfile(member)
        if source is None:
            raise ValueError('unsafe archive members')
        write_verified(source.read(MAX_MEMBER + 1), HELM_BINARY, destination)


def verified_promtail(archive, destination):
    if digest(archive) != PROMTAIL_ARCHIVE:
        raise ValueError('archive checksum mismatch')
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if len(entries) != 1 or entries[0].filename != 'promtail-linux-amd64' or (
                entries[0].file_size > MAX_MEMBER or entries[0].is_dir()):
            raise ValueError('unsafe archive members')
        write_verified(zf.read(entries[0]), PROMTAIL_BINARY, destination)


def verified_alloy(archive, destination):
    if digest(archive) != ALLOY_ARCHIVE:
        raise ValueError('archive checksum mismatch')
    with zipfile.ZipFile(archive) as zf:
        entries = zf.infolist()
        if len(entries) != 1 or entries[0].filename != 'alloy-linux-amd64' or (
                entries[0].file_size > MAX_ALLOY_MEMBER or entries[0].is_dir()):
            raise ValueError('unsafe archive members')
        with zf.open(entries[0]) as source:
            write_verified(source.read(MAX_ALLOY_MEMBER + 1), None, destination, MAX_ALLOY_MEMBER)


def main(argv):
    if len(argv) != 5:
        raise ValueError('usage')
    helm_archive, promtail_archive, alloy_archive, output_dir = map(pathlib.Path, argv[1:])
    verified_helm(helm_archive, output_dir / 'helm')
    verified_promtail(promtail_archive, output_dir / 'promtail-linux-amd64')
    verified_alloy(alloy_archive, output_dir / 'alloy-linux-amd64')


if __name__ == '__main__':
    try:
        main(sys.argv)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
        print('logging assets unavailable: integrity or member check failed', file=sys.stderr)
        sys.exit(1)
