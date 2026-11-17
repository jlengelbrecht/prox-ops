"""Make the Hindsight worker's own loop handler the only SIGINT/SIGTERM owner.

The worker registers a two-stage handler on its event loop, but
uvicorn.Server.serve() also captures signals and re-raises them once it
returns, so one TERM lands in the worker's second-signal branch and exits 1
mid-drain. The patched module builds its HTTP server from a subclass whose
capture_signals does nothing; uvicorn and every other installed file stay
untouched. Anything but the exact upstream bytes is refused before writing.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ORIGINAL_SHA256 = "00424d494d4ef0c55ac6d18e4bc927c08f7ee87798c6ecbd070502439733fd96"
MODULE = Path("hindsight_api", "worker", "main.py")

REPLACEMENTS = (
    ("import asyncio\nimport atexit\n", "import asyncio\nimport atexit\nimport contextlib\n"),
    ("        server = uvicorn.Server(uvicorn_config)\n",
     "        class _LoopSignalOwnedServer(uvicorn.Server):\n"
     "            # The loop handler above owns SIGINT/SIGTERM; uvicorn must not\n"
     "            # capture them and re-raise them after serve() returns.\n"
     "            @contextlib.contextmanager\n"
     "            def capture_signals(self):\n"
     "                yield\n"
     "\n"
     "        server = _LoopSignalOwnedServer(uvicorn_config)\n"),
)


class PatchRefused(Exception):
    pass


def patch_source(source: bytes, expected_sha256: str = ORIGINAL_SHA256) -> bytes:
    actual = hashlib.sha256(source).hexdigest()
    if actual != expected_sha256:
        raise PatchRefused(f"source sha256 {actual} does not match pinned {expected_sha256}")
    text = source.decode("utf-8")
    for anchor, _ in REPLACEMENTS:
        count = text.count(anchor)
        if count != 1:
            raise PatchRefused(f"anchor {anchor.strip()!r} must occur once, found {count}")
    for anchor, replacement in REPLACEMENTS:
        text = text.replace(anchor, replacement)
    try:
        compile(text, str(MODULE), "exec")
    except SyntaxError as exc:
        raise PatchRefused(f"patched source does not compile: {exc.msg}") from None
    return text.encode("utf-8")


def find_target(search_path: list[str]) -> Path:
    found = {(Path(entry) / MODULE).resolve() for entry in search_path if entry}
    found = sorted(path for path in found if path.is_file())
    if len(found) != 1:
        raise PatchRefused(f"expected one installed {MODULE}, found {len(found)}")
    return found[0]


def patch_file(target: Path, expected_sha256: str = ORIGINAL_SHA256) -> str:
    patched = patch_source(target.read_bytes(), expected_sha256)
    # Writing in place keeps the installed file's owner and mode.
    target.write_bytes(patched)
    # Bytecode compiled in unchecked-hash mode would still load the old module.
    for cached in target.parent.glob(f"__pycache__/{target.stem}.*.pyc"):
        cached.unlink()
    return hashlib.sha256(patched).hexdigest()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", type=Path, help=f"installed {MODULE} (default: the single copy on sys.path)")
    args = parser.parse_args(argv)
    try:
        target = args.target or find_target(sys.path)
        digest = patch_file(target)
    except (PatchRefused, OSError) as exc:
        raise SystemExit(f"worker signal patch refused: {exc}") from None
    print(f"patched {target} sha256 {digest}")


if __name__ == "__main__":
    main()
