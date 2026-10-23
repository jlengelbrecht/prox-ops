#!/usr/bin/env python3
"""Checksum-bound correction of interrupted-retain recovery in Hindsight 0.9.2.

A retry of a partially committed streaming retain reaches _try_delta_retain
first, which extracts every missing chunk in one batch (in-batch entity
clustering then folds distinct names) and links only the new units; the native
streaming RECOVERY path never runs. Two insertions into
engine/retain/orchestrator.py: (1) delta defers to streaming recovery when the
body hashes to the stored content_hash and chunks are only missing, never
changed or removed; (2) a first sub-batch recovery seeds the final semantic ANN
with every unit of the document. patch_bytes() accepts only the pinned bytes.

CLI: retain-recovery-patch.py SOURCE TARGET. Reads SOURCE (regular file, no
symlink) and atomically replaces TARGET in its existing directory, mode copied
from SOURCE (ownership too when root). Any refusal or I/O error prints one
bounded line, leaves TARGET untouched and exits 1. Stdlib only.
"""

from __future__ import annotations

import ast
import errno
import hashlib
import os
import stat
import sys

# ghcr.io/vectorize-io/hindsight-api:0.9.2@sha256:7b14a1f4062252992d0176758753615e0a2071d9a269995be007be223ab01812
PINNED_SHA256 = "6cf4b736672b8efcebc5a34740d97c176a02daf10596c540ed62c5c14622267c"
MAX_SOURCE_BYTES = 1 << 20

DELTA_HEAD = (
    '        logger.info(f"Delta retain: no unchanged chunks for {effective_doc_id}, falling back to full retain")\n'
    "        return None\n\n"
)
DELTA_TAIL = "    chunks_to_process = changed_indices + new_indices\n"
DELTA_INSERT = '''\
    # Same body as the stored document, nothing changed or removed, chunks missing: an interrupted
    # retain being retried, not an update. Streaming RECOVERY skips the committed chunks by hash
    # and extracts the rest in its normal per-batch order, which this one-shot delta would not.
    if new_indices and not changed_indices and not removed_indices and not _store_owned_delta and doc_hash_at_load:
        _body = document_body_override
        if _body is None:
            _body = "\\n".join([c.get("content", "") for c in contents_dicts])
        _body_hash = hashlib.sha256((fact_extraction._sanitize_text(_body) or "").encode()).hexdigest()
        if _body_hash == doc_hash_at_load:
            log_buffer.append("[delta] Same content with missing chunks — deferring to streaming recovery")
            return None

'''

ANN_HEAD = (
    '            log_buffer.append(f"[streaming] Recovery: loaded {len(all_unit_ids)} unit IDs from DB")\n\n'
)
ANN_TAIL = (
    "    # ---------------------------------------------------------------------------\n"
    "    # Final ANN pass: create semantic links for ALL committed units at once.\n"
)
ANN_INSERT = '''\
    # A recovered retain wrote only the chunks it was missing. Seed the final ANN with every unit of
    # the document so the ones committed before the interruption get their semantic links too.
    if is_recovery and not facts_already_committed and chunk_index_offset == 0 and not pipeline_aborted[0]:
        async with acquire_with_retry(pool) as conn:
            rows = await conn.fetch(
                f"""
                SELECT id::text FROM {fq_table("memory_units")}
                WHERE bank_id = $1 AND document_id = $2
                ORDER BY created_at
                """,
                bank_id,
                effective_doc_id,
            )
        all_unit_ids = [row["id"] for row in rows]

'''

HUNKS = (("delta", DELTA_HEAD, DELTA_TAIL, DELTA_INSERT), ("ann", ANN_HEAD, ANN_TAIL, ANN_INSERT))


class PatchRefused(Exception):
    """The source is not the pinned module or the anchors do not hold."""


def _apply(text: str) -> str:
    for name, head, tail, insert in HUNKS:
        if insert in text:
            raise PatchRefused(f"{name} hunk already applied")
        found = text.count(head + tail)
        if found != 1:
            raise PatchRefused(f"{name} anchor found {found} times, expected 1")
        text = text.replace(head + tail, head + insert + tail)
    return text


def patch_bytes(source: bytes) -> bytes:
    """Return the corrected module for the exact pinned upstream bytes."""
    if not isinstance(source, bytes):
        raise PatchRefused("source is not bytes")
    if hashlib.sha256(source).hexdigest() != PINNED_SHA256:
        raise PatchRefused("source sha256 does not match the pinned upstream module")
    try:
        patched = _apply(source.decode("utf-8")).encode("utf-8")
        ast.parse(patched)
    except (UnicodeError, SyntaxError, ValueError):
        raise PatchRefused("patched module is not valid UTF-8 Python") from None
    return patched


def _read_source(path: str) -> tuple[bytes, os.stat_result]:
    # O_NONBLOCK keeps a FIFO from stalling the open; it does not affect regular-file reads.
    with open(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC), "rb") as fh:
        st = os.fstat(fh.fileno())
        data = fh.read(MAX_SOURCE_BYTES + 1) if stat.S_ISREG(st.st_mode) else b""
    if not stat.S_ISREG(st.st_mode) or len(data) > MAX_SOURCE_BYTES:
        raise PatchRefused("source is not a regular file within the size bound")
    return data, st


def _write_target(path: str, data: bytes, src: os.stat_result) -> None:
    parent, name = os.path.split(path)
    if not name or name in (".", ".."):
        raise PatchRefused("target must name a file")
    dfd = os.open(parent or ".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    tmp = f".{name}.retain-recovery.{os.getpid()}.tmp"
    created = False
    try:
        try:
            cur = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            cur = None
        if cur is not None and (not stat.S_ISREG(cur.st_mode) or (cur.st_dev, cur.st_ino) == (src.st_dev, src.st_ino)):
            raise PatchRefused("target exists and is not a separate regular file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(tmp, flags, 0o600, dir_fd=dfd)
        created = True
        with open(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if os.geteuid() == 0:
                os.fchown(fh.fileno(), src.st_uid, src.st_gid)
            os.fchmod(fh.fileno(), stat.S_IMODE(src.st_mode))
            os.fsync(fh.fileno())
        os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        created = False
        os.fsync(dfd)
    finally:
        if created:
            os.unlink(tmp, dir_fd=dfd)
        os.close(dfd)


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise PatchRefused("usage: retain-recovery-patch.py SOURCE TARGET")
        data, src = _read_source(argv[0])
        _write_target(argv[1], patch_bytes(data), src)
    except PatchRefused as why:
        print(f"ERROR: retain-recovery patch refused: {why}", file=sys.stderr)
        return 1
    except (OSError, ValueError) as err:
        code = errno.errorcode.get(err.errno or 0, "EIO") if isinstance(err, OSError) else "EINVAL"
        print(f"ERROR: retain-recovery patch I/O failure: {code}", file=sys.stderr)
        return 1
    print("retain-recovery patch applied")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
