"""Single-use links that move exact attachment bytes outside model context.

An agent asks for a transfer through an MCP tool and receives a short-lived URL that it
fetches with an ordinary HTTP client. The bytes never pass through the model, are never
re-encoded, and arrive with SHA-256 checksums the agent can verify.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import io
import json
import secrets
import threading
import time
import zipfile
from typing import Any, Iterator

TRANSFER_SECONDS = 600
MAX_TRANSFERS = 32
MAX_EXPORT_FILES = 1000
MAX_EXPORT_BYTES = 500 * 1024 * 1024
MANIFEST = ".obsidian-export.json"
CHECKSUMS = ".SHA256SUMS"
TOKEN_LENGTH = 43


@dataclass
class Transfer:
    kind: str
    client: str
    vault: str
    payload: Any
    expires: float = field(default=0.0)


class TransferLedger:
    """Outstanding links. Each token works once and expires; nothing survives a restart."""

    def __init__(self, ttl: float = TRANSFER_SECONDS, limit: int = MAX_TRANSFERS):
        self._ttl = ttl
        self._limit = limit
        self._entries: dict[str, Transfer] = {}
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        for token in [token for token, entry in self._entries.items() if entry.expires <= now]:
            del self._entries[token]

    def issue(self, kind: str, client: str, vault: str, payload: Any) -> tuple[str, float]:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            if len(self._entries) >= self._limit:
                raise OverflowError("too many outstanding transfers")
            token = secrets.token_urlsafe(32)
            self._entries[token] = Transfer(kind, client, vault, payload, now + self._ttl)
            return token, self._ttl

    def claim(self, token: str, kind: str) -> Transfer | None:
        """Remove and return a live transfer of this kind; any other token gets None."""
        if not isinstance(token, str) or len(token) != TOKEN_LENGTH:
            return None
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            entry = self._entries.get(token)
            if entry is None or entry.kind != kind:
                return None
            del self._entries[token]
            return entry


class _Sink:
    """A write-only, non-seekable file for ZipFile; the generator drains it as it goes."""

    def __init__(self):
        self._parts: deque[bytes] = deque()

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> Iterator[bytes]:
        while self._parts:
            yield self._parts.popleft()


def _zip_time(mtime_ms: int) -> tuple[int, int, int, int, int, int]:
    stamp = time.gmtime(max(mtime_ms / 1000, 315532800))  # ZIP cannot store dates before 1980
    return stamp[:6]


def export_stream(bridge, vault: str, infos: list[dict[str, Any]]) -> Iterator[bytes]:
    """A stored (uncompressed) zip of the exact files, then a manifest and sha256sum file.

    A file that changes or comes back short mid-export raises, which cuts the response off,
    so a partial download fails to unzip instead of looking complete."""
    sink = _Sink()
    manifest = []
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED) as archive:
        for info in infos:
            entry = zipfile.ZipInfo(info["path"], date_time=_zip_time(info["mtime"]))
            entry.compress_type = zipfile.ZIP_STORED
            entry.file_size = info["size"]
            digest = hashlib.sha256()
            size = 0
            with archive.open(entry, "w") as out:
                for chunk in bridge.iter_attachment(vault, info):
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                    yield from sink.drain()
            if size != info["size"]:
                raise ValueError("attachment changed during export")
            manifest.append({"path": info["path"], "size": size, "mime": info["mime"],
                             "sha256": digest.hexdigest()})
            yield from sink.drain()
        archive.writestr(CHECKSUMS, "".join(f"{item['sha256']}  {item['path']}\n" for item in manifest))
        archive.writestr(MANIFEST, json.dumps({"vault": vault, "files": manifest}, indent=2) + "\n")
    yield from sink.drain()


IMPORT_BODY_SECONDS = 300.0
IMPORT_STREAMS = 2
_PILLOW_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".gif": "GIF", ".webp": "WEBP"}
_MAGIC = {".png": (b"\x89PNG\r\n\x1a\n",), ".jpg": (b"\xff\xd8\xff",), ".jpeg": (b"\xff\xd8\xff",),
          ".gif": (b"GIF87a", b"GIF89a"), ".pdf": (b"%PDF-",)}


def content_matches(path: str, data: bytes) -> bool:
    """Whether the bytes really are the type the file name claims, so an upload cannot slip
    one kind of file into the vault under another's extension."""
    import os
    from PIL import Image

    extension = os.path.splitext(path)[1].lower()
    if extension == ".webp":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            return False
    elif not any(data.startswith(magic) for magic in _MAGIC.get(extension, ())):
        return False
    if extension not in _PILLOW_FORMATS:
        return True
    # verify() checks structure without decoding pixels. Images past Pillow's bomb limit
    # (about 179 megapixels) raise at open and are refused.
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != _PILLOW_FORMATS[extension]:
                return False
            image.verify()
    except Exception:
        return False
    return True
