#!/usr/bin/env python3
"""Extract the pinned server archive without path or link escape."""
import pathlib
import posixpath
import sys
import tarfile


def extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError("empty archive")
        names = [m.name.removeprefix("./").rstrip("/") for m in members]
        parts = [n.split("/") for n in names if n]
        prefix = parts[0][0] if parts and all(p[0] == parts[0][0] for p in parts) else ""
        if prefix not in (".", "..") and prefix and any(len(p) > 1 for p in parts):
            strip = prefix + "/"
        else:
            strip = ""
        items = []
        symlinks = set()
        for member, name in zip(members, names):
            if not name and member.isdir():
                continue
            if name == prefix and strip:
                continue
            if name.startswith("/") or "\\" in name or not name or name == "..":
                raise ValueError("unsafe archive path")
            if posixpath.normpath(name) != name or posixpath.normpath(name).startswith("../"):
                raise ValueError("unsafe archive path")
            relative = name.removeprefix(strip) if strip else name
            if relative == name and strip:
                raise ValueError("mixed archive roots")
            normalized = posixpath.normpath(relative)
            if normalized.startswith("../") or normalized in (".", "..") or normalized != relative:
                raise ValueError("unsafe archive path")
            if not (member.isdir() or member.isfile() or member.issym()):
                raise ValueError("unsupported archive member")
            if member.issym():
                target = posixpath.normpath(posixpath.join(posixpath.dirname(relative), member.linkname))
                if member.linkname.startswith("/") or target.startswith("../") or target == "..":
                    raise ValueError("escaping archive symlink")
                symlinks.add(relative)
            items.append((member, relative))
        for _, relative in items:
            ancestors = pathlib.PurePosixPath(relative).parents
            if any(str(a) in symlinks for a in ancestors):
                raise ValueError("member beneath symlink")
        destination.mkdir(parents=True, exist_ok=False)
        for member, relative in items:
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError("unreadable archive member")
                with source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                target.chmod(member.mode & 0o755)
        for member, relative in items:
            if member.issym():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: safe-extract.py ARCHIVE DESTINATION")
    try:
        extract(pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]))
    except (OSError, ValueError, tarfile.TarError) as error:
        sys.exit(f"safe-extract: {error}")
