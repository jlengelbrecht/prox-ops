#!/usr/bin/env python3
"""Checksum-bound overlay of the Hermes dashboard bundle (init container).

The pinned Hermes release drops the Hindsight `api_url`/`api_key` fields
from the Plugins-page save and setup requests in Local External mode: the
schema declares both keys once per mode, the page looks the key up with
`find()`, gets the hidden Cloud row and filters the field out. Upstream
issue #96691; no released image carries a fix. The correction is one
minified token in one chunk: a key stays in the request when ANY schema row
with that key is visible for the current values.

This script runs in an init container from the same image digest as the
dashboard. It copies the image's web_dist to an emptyDir that the dashboard
serves through the image-supported HERMES_WEB_DIST override. Only when every
pin below matches the copied bytes does it apply the correction and rename
the touched chunks, so browsers holding the old copy (assets are served
`Cache-Control: immutable` for a year) fetch the new one on an ordinary
reload: index.html (served no-store) names the entry chunk, the entry chunk
names the Plugins chunk, and the Sessions chunk imports the entry chunk.

Any pin mismatch (an image bump, a different bundle) leaves an unmodified
copy in place, logs a WARNING and records the reason in a status file kept
next to - never inside - the served directory. Hermes then runs the upstream
bundle and the Hindsight save defect is simply back until the pins are
refreshed or the overlay is removed. The pins are coupled to the image tag
in hermes.yaml: a bump must re-pin or drop this file.

Stdlib only, no network, no bytecode written. Exit 0 when applied and when
passing through; exit 1 only when no usable dist can be produced at all
(no index.html in the source, an output path that is not this script's own).

Environment:
  HERMES_WEB_DIST_SRC  image dist (default /opt/hermes/hermes_cli/web_dist)
  WEB_DIST_ROOT        emptyDir root (default /opt/web-dist); the served copy
                       is <root>/dist and the status file <root>/status.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

# ---- Pins for docker.io/nousresearch/hermes-agent:v2026.9.7 -----------------
# linux/amd64 manifest sha256:b3190406963c6b51ac955397ecef45346efaae9563ee305108f8eef0a77e267b
IMAGE_TAG = "v2026.9.7"
# Suffix added to every renamed chunk; bump it when the correction changes on
# the same base image so caches refresh again.
GENERATION = "hh1"

# Chunk carrying the defect (web/src/pages/PluginsPage.tsx onSaveMemoryProvider
# and currentVisibleMemoryValues, both `memoryConfig?.fields.find(...)`).
TARGET = "assets/PluginsPage-tfekLiaM.js"
TARGET_SHA256 = "a92d3b5d57d6aac8eeb8875c9461fcd94a2ed849b7fa5e624f0b50cd56c47359"
# Minified `const field = memoryConfig?.fields.find(c => c.key === key);
# return field ? fieldIsVisible(field, memoryValues) : true`, present at the
# save site and the setup site.
OLD = "H?.fields.find(t=>t.key===e);return!t||N(t,W)"
OLD_COUNT = 2
# `const rows = memoryConfig?.fields.filter(c => c.key === key) ?? [];
# return rows.length === 0 || rows.some(row => fieldIsVisible(row, memoryValues))`
NEW = "H?.fields.filter(t=>t.key===e);return!t?.length||t.some(r=>N(r,W))"
PATCHED_SHA256 = "bf32cfe1c0dfc0f2c131328a13ab0959f205afe16dec54186898ad2656b7f2c5"

# Rename cascade for cache safety. Every renamed file is pinned by sha256 and
# every holder must name it exactly the expected number of times.
ENTRY = "assets/index-0-lwe-sG.js"
SESSIONS = "assets/SessionsPage-CRM4U72X.js"
INDEX_HTML = "index.html"
PINNED_SHA256 = {
    TARGET: TARGET_SHA256,
    ENTRY: "4e1afc349c81295feb2b557a835a1ad7a3a2640bc3dafb9dc8dc0f332a7e2aba",
    SESSIONS: "6fa74d756b049e09010811de0618c02ed2f223fd104c3c4278fb5dbf933a373f",
    INDEX_HTML: "ab7902b7b84f4be8d894d0d9bfe0175bf9892e3f279ac754526c62adb33cd3a7",
}
# renamed file -> {holder: expected reference count}
REFS = {
    TARGET: {ENTRY: 2},
    ENTRY: {INDEX_HTML: 1, SESSIONS: 1},
    SESSIONS: {ENTRY: 2},
}
RENAMED = (TARGET, ENTRY, SESSIONS)
TEXT_SUFFIXES = {".js", ".css", ".html"}
# How the bundle names its own assets: `./chunk.js` imports inside chunks,
# `assets/chunk.js` in the preload manifest and `/assets/...` in index.html.
ASSET_REF = re.compile(r"(?:\./|/?assets/)([A-Za-z0-9_.-]+\.(?:js|css|woff2))")

DEFAULT_SRC = "/opt/hermes/hermes_cli/web_dist"
DEFAULT_ROOT = "/opt/web-dist"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def renamed(rel: str) -> str:
    p = Path(rel)
    return str(p.with_name(f"{p.stem}.{GENERATION}{p.suffix}"))


class Skip(Exception):
    """The overlay does not apply to this bundle; serve it unchanged."""


def patch(dist: Path) -> dict:
    """Compute the corrected files for an unmodified copy at *dist*.

    Reads only; returns {relative path: text} for every file to (re)write
    plus the list of files to remove. Raises Skip with the reason when any
    pin, count or reference check fails, before anything is written.
    """
    missing = [rel for rel in PINNED_SHA256 if not (dist / rel).is_file()]
    if missing:
        raise Skip("pinned file missing: " + ", ".join(missing))
    raw = {rel: (dist / rel).read_bytes() for rel in PINNED_SHA256}
    for rel, want in PINNED_SHA256.items():
        if sha256(raw[rel]) != want:
            raise Skip(f"{rel} sha256 mismatch (image bump? re-pin or drop the overlay)")
    text = {rel: data.decode("utf-8") for rel, data in raw.items()}

    found = text[TARGET].count(OLD)
    if found != OLD_COUNT:
        raise Skip(f"{TARGET} has {found} save-filter sites, expected {OLD_COUNT}")
    text[TARGET] = text[TARGET].replace(OLD, NEW)
    if sha256(text[TARGET].encode("utf-8")) != PATCHED_SHA256:
        raise Skip("patched chunk sha256 mismatch")

    for old_rel, holders in REFS.items():
        old_name, new_name = Path(old_rel).name, Path(renamed(old_rel)).name
        for holder, count in holders.items():
            have = text[holder].count(old_name)
            if have != count:
                raise Skip(f"{holder} names {old_name} {have}x, expected {count}")
            text[holder] = text[holder].replace(old_name, new_name)

    # The served tree as it will look: every text file, with the renamed ones
    # under their new names and the old names gone.
    served = {}
    for path in dist.rglob("*"):
        if path.is_file() and path.suffix in TEXT_SUFFIXES:
            rel = path.relative_to(dist).as_posix()
            if rel not in text:
                served[rel] = path.read_text(encoding="utf-8", errors="replace")
    served[INDEX_HTML] = text[INDEX_HTML]
    for rel in RENAMED:
        served[renamed(rel)] = text[rel]
    stale = {Path(rel).name for rel in RENAMED}
    assets = {p.name for p in (dist / "assets").iterdir() if p.is_file()} - stale
    assets |= {Path(renamed(rel)).name for rel in RENAMED}
    for rel, body in served.items():
        for name in stale:
            if name in body:
                raise Skip(f"{rel} still references {name}")
        dangling = sorted({m.group(1) for m in ASSET_REF.finditer(body)} - assets)
        if dangling:
            raise Skip(f"{rel} references missing assets: {', '.join(dangling)}")

    return {
        "write": {INDEX_HTML: text[INDEX_HTML], **{renamed(rel): text[rel] for rel in RENAMED}},
        "remove": list(RENAMED),
        "patched_sha256": sha256(text[TARGET].encode("utf-8")),
    }


def main(src: Path, root: Path) -> int:
    dist, staging, status_path = root / "dist", root / "dist.new", root / "status.json"
    status = {"image_tag": IMAGE_TAG, "generation": GENERATION, "applied": False, "reason": ""}

    if not (src / INDEX_HTML).is_file():
        print(f"ERROR: no index.html in source dist {src}", file=sys.stderr)
        return 1
    src, root = src.resolve(), root.resolve()
    # Only this script's own output is ever removed: the root must be a real
    # directory that neither contains nor lives inside the source dist.
    if root == Path("/") or not root.is_dir() or src == root or src in root.parents or root in src.parents:
        print(f"ERROR: refusing to manage {root} (source dist {src})", file=sys.stderr)
        return 1

    # Re-entry (a restarted pod keeps its emptyDir): rebuild from the image
    # copy so the result never depends on a previous run.
    for own in (staging, dist):
        if own.is_dir():
            shutil.rmtree(own)
    shutil.copytree(src, staging)

    try:
        plan = patch(staging)
    except Skip as why:
        status["reason"] = str(why)
        print(f"WARNING: web_dist patch NOT applied ({why}); serving the image bundle unchanged",
              file=sys.stderr)
    else:
        for rel, body in plan["write"].items():
            (staging / rel).write_text(body, encoding="utf-8")
        for rel in plan["remove"]:
            (staging / rel).unlink()
        status.update(applied=True, target=renamed(TARGET), entry=renamed(ENTRY),
                      patched_sha256=plan["patched_sha256"])
        print(f"web_dist patch {GENERATION} applied for {IMAGE_TAG}: {renamed(TARGET)}")

    # Publish the complete tree in one step; the dashboard only ever sees a
    # whole copy, patched or not.
    os.rename(staging, dist)
    status_path.write_text(json.dumps(status, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(os.environ.get("HERMES_WEB_DIST_SRC", DEFAULT_SRC)),
                  Path(os.environ.get("WEB_DIST_ROOT", DEFAULT_ROOT))))
