#!/usr/bin/env python3
"""Verify pinned metrics tool archives before extracting only required binaries."""
import hashlib
import os
import pathlib
import stat
import sys
import tarfile

HELM_ARCHIVE = 'a7f81ce08007091b86d8bd696eb4d86b8d0f2e1b9f6c714be62f82f96a594496'
HELM_BINARY = 'e4722a77de9df824214aaf19d43687c49f7cbcf3b107905acea002173777d486'
PROM_ARCHIVE = 'a09972ced892cd298e353eb9559f1a90f499da3fb4ff0845be352fc138780ee7'
PROM_BINARY = '51ce9760b0369fbc8645942b44fe43a52db3e1dad83285fed25a4660e8d1d218'
CHART_ARCHIVE = 'c8a7eb0a27bcc38463a144bea9716897c1cce281ba98d49838f449e75098e257'
MAX_MEMBER = 200_000_000
MAX_TOTAL = 450_000_000


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def verified_tar(archive, expected_archive, target, expected_binary, destination, allowed):
    if digest(archive) != expected_archive:
        raise ValueError('archive checksum mismatch')
    with tarfile.open(archive, 'r:gz') as tar:
        members = tar.getmembers()
        names = [member.name.rstrip('/') for member in members]
        if len(names) != len(set(names)) or target not in names or sum(member.size for member in members) > MAX_TOTAL:
            raise ValueError('unsafe archive members')
        for member in members:
            name = member.name.rstrip('/')
            if not allowed(name) or member.size > MAX_MEMBER or not (member.isfile() or member.isdir()):
                raise ValueError('unsafe archive members')
        member = tar.getmember(target)
        if not member.isfile():
            raise ValueError('unsafe archive members')
        source = tar.extractfile(member)
        if source is None:
            raise ValueError('unsafe archive members')
        payload = source.read(MAX_MEMBER + 1)
        if len(payload) > MAX_MEMBER or hashlib.sha256(payload).hexdigest() != expected_binary:
            raise ValueError('binary checksum mismatch')
    destination = pathlib.Path(destination)
    if destination.exists():
        raise ValueError('destination exists')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.write_bytes(payload)
    destination.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def helm_member(name):
    return name in {'linux-amd64', 'linux-amd64/helm', 'linux-amd64/LICENSE', 'linux-amd64/README.md'}


def prometheus_member(name):
    root = 'prometheus-3.8.1.linux-amd64'
    if name == root:
        return True
    if not name.startswith(root + '/'):
        return False
    child = name[len(root) + 1:]
    return child in {'promtool', 'prometheus', 'LICENSE', 'NOTICE', 'prometheus.yml',
                     'consoles', 'console_libraries'} or child.startswith(('consoles/', 'console_libraries/')) and (
                         '..' not in pathlib.PurePosixPath(child).parts and len(child) < 180)


def main(argv):
    if len(argv) != 4:
        raise ValueError('usage')
    helm_archive, prom_archive, output_dir = map(pathlib.Path, argv[1:])
    verified_tar(helm_archive, HELM_ARCHIVE, 'linux-amd64/helm', HELM_BINARY,
                 output_dir / 'helm', helm_member)
    verified_tar(prom_archive, PROM_ARCHIVE, 'prometheus-3.8.1.linux-amd64/promtool',
                 PROM_BINARY, output_dir / 'promtool', prometheus_member)


if __name__ == '__main__':
    try:
        main(sys.argv)
    except (OSError, ValueError, tarfile.TarError):
        print('metrics assets unavailable: integrity or member check failed', file=sys.stderr)
        sys.exit(1)
