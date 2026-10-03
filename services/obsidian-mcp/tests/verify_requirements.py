"""Verify the published, hash-pinned install and service test surface."""

import argparse
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE.parents[1]
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)\Z")
HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}\Z")


def _name(value):
    return re.sub(r"[-_.]+", "-", value).lower()


def _entries(path):
    entries = []
    parts = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts.append(line.removesuffix("\\").strip())
        if not line.endswith("\\"):
            entries.append(" ".join(parts))
            parts.clear()
    if parts:
        raise ValueError(f"incomplete requirement in {path.name}")
    return entries


def validate_requirements():
    """Reject URL, unpinned, unhashed, or divergent public manifests."""
    manifests = {}
    for filename in ("requirements.txt", "requirements-dev.txt"):
        packages = {}
        for entry in _entries(SERVICE / filename):
            if " --hash=" not in entry or "://" in entry or " @ " in entry:
                raise ValueError(f"unsafe requirement in {filename}")
            spec, *hashes = entry.split(" --hash=")
            match = PIN.fullmatch(spec.split(" ; ", 1)[0])
            if not match or not hashes or not all(HASH.fullmatch("--hash=" + value) for value in hashes):
                raise ValueError(f"invalid pin or hash in {filename}")
            name, version = _name(match[1]), match[2]
            if name in packages:
                raise ValueError(f"duplicate requirement in {filename}: {name}")
            packages[name] = (version, frozenset(hashes))
        if not packages:
            raise ValueError(f"empty {filename}")
        manifests[filename] = packages
    runtime = manifests["requirements.txt"]
    dev = manifests["requirements-dev.txt"]
    if any(dev.get(name) != pin for name, pin in runtime.items()):
        raise ValueError("test requirements diverge from runtime pins")
    project = tomllib.loads((SERVICE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    for requirement in [*project["dependencies"], *project["optional-dependencies"]["test"]]:
        match = PIN.fullmatch(requirement)
        if not match or dev.get(_name(match[1]), (None,))[0] != match[2]:
            raise ValueError("project dependency pin is absent from test requirements")
    for requirement in project["dependencies"]:
        match = PIN.fullmatch(requirement)
        if runtime.get(_name(match[1]), (None,))[0] != match[2]:
            raise ValueError("project dependency pin is absent from runtime requirements")
    return len(runtime), len(dev)


def install_requirements(python, requirements, *, find_links=None):
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check",
               "--no-compile", "--require-hashes"]
    if find_links is not None:
        command.extend(["--no-index", "--find-links", str(find_links)])
    command.extend(["-r", str(requirements)])
    return subprocess.run(command, check=True, cwd=ROOT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image")
    args = parser.parse_args()
    runtime_count, dev_count = validate_requirements()
    print(f"validated {runtime_count} runtime and {dev_count} test pins", flush=True)
    with tempfile.TemporaryDirectory(prefix="obsidian-mcp-verification-") as directory:
        environment = Path(directory) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
        python = environment / "bin" / "python"
        install_requirements(python, SERVICE / "requirements-dev.txt")
        env = {**os.environ, "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
               "PATH": str(environment / "bin") + os.pathsep + os.environ.get("PATH", "")}
        if args.image:
            subprocess.run([str(python), str(SERVICE / "tests" / "smoke_image.py"),
                            "--image", args.image], check=True, cwd=ROOT, env=env)
        else:
            subprocess.run([str(python), "-m", "pytest", str(SERVICE / "tests"), "-q"],
                           check=True, cwd=ROOT, env=env)
            subprocess.run(["node", str(SERVICE / "tests" / "test_bridge_plugin.cjs")],
                           check=True, cwd=ROOT, env=env)
            subprocess.run(["node", str(SERVICE / "tests" / "test_bridge_plugin.cjs")],
                           check=True, cwd=ROOT,
                           env={**env, "OBSIDIAN_BRIDGE_REAL_IPC": "1"})


if __name__ == "__main__":
    main()
