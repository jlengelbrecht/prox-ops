"""One authenticated HTTP MCP surface for enrolled running Obsidian vaults."""

from __future__ import annotations

import asyncio
import difflib
import base64
import html
import io
import json
import os
import re
import secrets
import stat
import time
import unicodedata
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from collections.abc import Mapping as _AbcMapping
from typing import Mapping
from urllib.parse import parse_qs, unquote

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, TokenVerifier
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import ImageContent, TextContent
from PIL import Image, UnidentifiedImageError
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from approval import (AdapterConflict, AgentContext, ApprovalCoordinator, ApprovalError,
                      Operation, Preview, Snapshot, StateEntry, VaultEnrollment)
from bridge import BridgeClient, BridgeError, VaultDirectory, VaultEndpoint, _safe_relative
from owner_auth import CsrfLedger, OwnerAuthConfig, OwnerAuthenticator, OwnerAuthError
from server import MAX_HTTP_BODY, MCP_PATH, _valid_token


_SAFE_CAPABILITIES = {
    "list_notes": "list", "read_note": "read", "search": "search",
    "create_note": "create", "append_note": "append", "read_media": "embed",
    "manage_vaults": "vaults", "create_folder": "mkdir", "move": "move",
}
# Destructive verbs: each needs the owner's approval through /owner/ before it runs.
_GUARDED_CAPABILITIES = {"replace_note": "replace", "trash_note": "trash", "trash_folder": "trash"}
_TOOL_VERBS = {
    "list_entries": "list_notes", "read_note": "read_note", "search_notes": "search",
    "create_note": "create_note", "append_note": "append_note",
    "read_embedded_image": "read_media", "read_note_with_images": "read_media",
    "list_all_vaults": "manage_vaults", "open_vault": "manage_vaults",
    "create_vault": "manage_vaults", "create_folder": "create_folder", "move": "move",
}


class _LiveVaults(_AbcMapping):
    """A read-only mapping over the vaults the bridge currently knows about."""

    def __init__(self, bridge: BridgeClient, value):
        self._bridge = bridge
        self._value = value

    def __getitem__(self, vault):
        if type(vault) is str and vault in self._bridge.vault_ids():
            return self._value(vault)
        raise KeyError(vault)

    def __iter__(self):
        return iter(self._bridge.vault_ids())

    def __len__(self):
        return len(self._bridge.vault_ids())
_MEDIA_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "GIF": "image/gif"}
_MAX_IMAGE_BYTES = 512 * 1024
_MAX_IMAGE_PIXELS = 4_000_000
_MAX_IMAGE_RESPONSE = 768 * 1024
_MAX_PREVIEW_HTML = 512 * 1024
_DISCOVERY_TIMEOUT = 1.0
_HTTP_BODY_TIMEOUT = 2.0
_MAX_VAULTS = 32
_MAX_REGISTRY_BYTES = 256 * 1024
_MAX_REGISTRY_TEXT = 1024
_EXECUTION_WORKERS = 8
_EXECUTION_WAITING = 8
_EXECUTION_ADMISSION_TIMEOUT = 2.0
_MCP_HOST = "obsidian-mcp.homelab0.org"
_MCP_HTTPS_ORIGINS = frozenset({f"https://{_MCP_HOST}", f"https://{_MCP_HOST}:443"})
_MCP_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})


class _BodyTooLarge(Exception):
    pass


async def _read_body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async with asyncio.timeout(_HTTP_BODY_TIMEOUT):
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise _BodyTooLarge
            body.extend(chunk)
    return bytes(body)


class RequestBodyLimit:
    """Bound complete MCP POST receipt before the SDK parses or authenticates it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != MCP_PATH or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        try:
            body = await _read_body(Request(scope, receive), MAX_HTTP_BODY)
        except _BodyTooLarge:
            return await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
        except TimeoutError:
            return await PlainTextResponse("Request timed out", status_code=408)(scope, receive, send)
        except ClientDisconnect:
            return

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _loopback_authority(host: str) -> bool:
    if host in _MCP_LOOPBACK_HOSTS:
        return True
    for name in _MCP_LOOPBACK_HOSTS:
        prefix = name + ":"
        if host.startswith(prefix):
            port = host[len(prefix):]
            return (0 < len(port) <= 5 and port.isascii() and port.isdecimal()
                    and 0 < int(port) <= 65535)
    return False


class MCPHostOriginGuard:
    """Limit the MCP route without changing the separate owner and probe topology."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in {MCP_PATH, MCP_PATH + "/"}:
            return await self.app(scope, receive, send)
        hosts = [value for name, value in scope["headers"] if name.lower() == b"host"]
        origins = [value for name, value in scope["headers"] if name.lower() == b"origin"]
        try:
            host = hosts[0].decode("ascii").lower() if len(hosts) == 1 else ""
            origin = origins[0].decode("ascii").lower() if len(origins) == 1 else None
        except UnicodeDecodeError:
            host, origin = "", None
        deployed = host in {_MCP_HOST, _MCP_HOST + ":443"}
        local = _loopback_authority(host)
        if not (deployed or local):
            return await PlainTextResponse("Misdirected Request", status_code=421)(scope, receive, send)
        if len(origins) > 1 or (origins and not (
                (deployed and origin in _MCP_HTTPS_ORIGINS)
                or (local and origin == "http://" + host))):
            return await PlainTextResponse("Forbidden Origin", status_code=403)(scope, receive, send)
        await self.app(scope, receive, send)


class ScopedTokens(TokenVerifier):
    def __init__(self, credentials: Mapping[str, str]):
        super().__init__()
        self._credentials = dict(credentials)

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 128 or not token.isascii():
            return None
        found = None
        for client, expected in self._credentials.items():
            if secrets.compare_digest(token, expected):
                found = client
        return AccessToken(token=token, client_id=found, scopes=[]) if found else None


_MAX_PREVIEW_TEXT = 32000
_APPROVAL_SECONDS = 600


def _visible(char: str) -> str:
    """Show control and invisible format characters (bidi overrides, zero-width) as escapes."""
    if char in "\n\t":
        return char
    if ord(char) < 32 or unicodedata.category(char) in {"Cf", "Cc", "Co", "Cs", "Zl", "Zp"}:
        return f"\\u{ord(char):04x}"
    return char


class BridgeAdapter:
    """The only production note writer; the bridge itself selects a running app."""

    def __init__(self, bridge: BridgeClient, client_id: str | None = None):
        self.bridge = bridge
        self.client_id = client_id
        self._observed: dict[tuple[str, str, str], str] = {}

    def read(self, action: Operation) -> object:
        args = action.arguments
        if action.verb == "read_note":
            return self.bridge.read_note(action.vault, args["path"])
        if action.verb == "list_notes":
            return self.bridge.list_entries(action.vault, args.get("folder", ""), 1000)
        if action.verb == "search":
            return self.bridge.search(action.vault, args["query"], 20)
        raise BridgeError("unknown_operation")

    _GUARDED_KINDS = {"replace_note": ("note", "file"), "trash_note": ("note", "file"),
                      "trash_folder": ("folder", "folder")}

    def _guarded_state(self, action: Operation) -> tuple[dict, StateEntry]:
        kind, expected = self._GUARDED_KINDS[action.verb]
        path = action.arguments["path"]
        state = self.bridge.path_state(action.vault, path)
        if state["kind"] != expected or (kind == "note" and not path.lower().endswith(".md")):
            raise BridgeError("not_found")
        return state, StateEntry(kind, path, state["revision"])

    def preview(self, action: Operation) -> Preview:
        """Read only: exactly what the owner approves, with the revision it applies to."""
        state, entry = self._guarded_state(action)
        path, vault = action.arguments["path"], action.vault
        if action.verb == "replace_note":
            current = self.bridge.read_note(vault, path)["content"]
            diff = "".join(difflib.unified_diff(current.splitlines(True), action.arguments["content"].splitlines(True),
                                                "current", "proposed", n=1))
            summary = f"Replace the whole note {path} in vault {vault}.\n\n" + diff
        elif action.verb == "trash_note":
            summary = f"Move the note {path} in vault {vault} to that vault's .trash folder."
        else:
            summary = (f"Move the folder {path} in vault {vault}, with its {state['count']} files and "
                       "folders, to that vault's .trash folder.")
        summary = unicodedata.normalize("NFC", "".join(_visible(char) for char in summary))
        # Fail closed: an approval must show the whole change, so never truncate.
        if len(summary) > _MAX_PREVIEW_TEXT:
            raise BridgeError("limit_exceeded")
        return Preview(Snapshot((entry,), True, 1), summary)

    def observe(self, action: Operation, expected) -> Snapshot:
        state, entry = self._guarded_state(action)
        # The commit runs right after this, under the coordinator's vault lock; the plugin
        # re-checks this exact revision so a concurrent edit makes the commit fail.
        self._observed[(action.vault, action.verb, action.arguments["path"])] = state["revision"]
        return Snapshot((entry,), True, 1)

    def execute(self, action: Operation, *, package=None) -> object:
        args = action.arguments
        try:
            if action.verb in self._GUARDED_KINDS:
                revision = self._observed.pop((action.vault, action.verb, args["path"]), None)
                if revision is None:
                    raise AdapterConflict()
                if action.verb == "replace_note":
                    return self.bridge.replace_note(action.vault, args["path"], args["content"], revision)
                return self.bridge.trash_path(action.vault, args["path"], revision)
            if action.verb == "create_folder":
                return self.bridge.make_folder(action.vault, args["path"])
            if action.verb == "move":
                return self.bridge.move_path(action.vault, args["source"], args["destination"])
            if action.verb == "create_note":
                return self.bridge.create_note(action.vault, args["path"], args["content"],
                                               client_id=self.client_id or "bridge")
            if action.verb == "append_note":
                return self.bridge.append_note(action.vault, args["path"], args["content"],
                                               args["expected_revision"],
                                               client_id=self.client_id or "bridge")
        except BridgeError as exc:
            if exc.code == "conflict":
                raise AdapterConflict() from None
            raise
        raise BridgeError("unknown_operation")


class CapabilityFilter(Middleware):
    def __init__(self, grants, bridge, destructive_verbs):
        self.grants = grants
        self.bridge = bridge
        self.destructive_verbs = destructive_verbs
        self._probe_slots = asyncio.Semaphore(8)

    async def _probe(self, vault: str, deadline: float) -> set[str] | None:
        await self._probe_slots.acquire()
        if time.monotonic() >= deadline:
            self._probe_slots.release()
            return None
        try:
            worker = asyncio.create_task(asyncio.to_thread(self.bridge.ready, vault, deadline=deadline))
        except BaseException:
            self._probe_slots.release()
            raise

        def finished(task):
            self._probe_slots.release()
            if not task.cancelled():
                task.exception()

        worker.add_done_callback(finished)
        try:
            result = await asyncio.shield(worker)
            if time.monotonic() >= deadline:
                return None
            return set(result["capabilities"])
        except (BridgeError, KeyError, TypeError):
            return None

    async def probe_grants(self, allowed, *, deadline: float | None = None) -> dict[str, set[str]]:
        """Use one deadline and the same eight actual worker slots for every readiness caller."""
        if not allowed:
            return {}
        probe_deadline = time.monotonic() + _DISCOVERY_TIMEOUT
        deadline = min(probe_deadline, deadline) if deadline is not None else probe_deadline
        probes = {asyncio.create_task(self._probe(vault, deadline)): vault for vault in allowed}
        try:
            done, _ = await asyncio.wait(probes, timeout=max(0, deadline - time.monotonic()))
        finally:
            for task in probes:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*probes, return_exceptions=True)
        return {probes[task]: task.result() for task in done
                if not task.cancelled() and task.result() is not None}

    async def ready_vaults(self, allowed) -> list[str]:
        ready = await self.probe_grants(allowed)
        return [vault for vault in allowed if vault in ready]

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        token = get_access_token()
        if token is None:
            return []
        client = token.client_id
        allowed = self.grants.get(client, {})
        verbs = set()
        for vault, capabilities in (await self.probe_grants(allowed)).items():
            granted = allowed[vault]
            verbs.update(verb for verb in granted if _SAFE_CAPABILITIES.get(verb) in capabilities)
            verbs.update(granted & self.destructive_verbs)
        names = {"list_vaults"}
        names.update(name for name, verb in _TOOL_VERBS.items() if verb in verbs)
        if "read_note" not in verbs:
            names.discard("read_note_with_images")
        if verbs & {"create_note", "append_note"}:
            names.add("mutation_receipt")
        if verbs & self.destructive_verbs:
            names.update({"prepare_action", "commit_action"})
        return [tool for tool in await call_next(context) if tool.name in names]

    async def on_list_resources(self, context: MiddlewareContext, call_next):
        return []

    async def on_list_resource_templates(self, context: MiddlewareContext, call_next):
        return []

    async def on_list_prompts(self, context: MiddlewareContext, call_next):
        return []


class _ExecutionPool:
    """Bound active synchronous work and admitted waiters across all tool calls."""

    def __init__(self, vaults=()):
        self._slots = asyncio.Semaphore(_EXECUTION_WORKERS)
        self._admitted = asyncio.Semaphore(_EXECUTION_WORKERS + _EXECUTION_WAITING)
        self._vaults = vaults
        self._vault_locks = {}
        self._executor = ThreadPoolExecutor(max_workers=_EXECUTION_WORKERS,
                                            thread_name_prefix="obsidian-tool")

    async def run(self, function, *args, deadline: float, preserve_result: bool = False,
                  vault: str | None = None):
        if time.monotonic() >= deadline or self._admitted.locked():
            raise ToolError("Tool request failed")
        vault_lock = None
        if vault is not None:
            if vault not in self._vaults:
                raise ToolError("Tool request failed")
            vault_lock = self._vault_locks.setdefault(vault, asyncio.Lock())
        await self._admitted.acquire()
        owns_slot = False
        owns_vault = False
        dispatched = False
        try:
            try:
                async with asyncio.timeout_at(deadline):
                    if vault_lock is not None:
                        await vault_lock.acquire()
                        owns_vault = True
                    await self._slots.acquire()
            except TimeoutError:
                raise ToolError("Tool request failed") from None
            owns_slot = True
            if time.monotonic() >= deadline:
                raise ToolError("Tool request failed")
            worker = asyncio.get_running_loop().run_in_executor(self._executor,
                                                                  partial(function, *args))
            dispatched = True

            def finished(task):
                self._slots.release()
                if vault_lock is not None:
                    vault_lock.release()
                self._admitted.release()
                if not task.cancelled():
                    task.exception()

            worker.add_done_callback(finished)
            if not preserve_result:
                try:
                    async with asyncio.timeout_at(deadline):
                        result = await asyncio.shield(worker)
                    if time.monotonic() >= deadline:
                        raise ToolError("Tool request failed")
                    return result
                except TimeoutError:
                    raise ToolError("Tool request failed") from None
            while True:
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    pass
        finally:
            if not dispatched:
                if owns_slot:
                    self._slots.release()
                if owns_vault:
                    vault_lock.release()
                self._admitted.release()

    def close(self):
        self._executor.shutdown(wait=False, cancel_futures=False)


def _image_content(bridge: BridgeClient, vault: str, source: str, target: str) -> ToolResult:
    if (not _safe_relative(source) or not source.lower().endswith(".md")
            or not isinstance(target, str) or len(target) > 512
            or "://" in target or target.startswith("/") or "%" in target
            or "\\" in target or ".." in target or "/." in target
            or re.search(r"(?:file|data):", target, re.IGNORECASE)
            or any(ord(char) < 32 or ord(char) == 127 for char in target)
            or target.lower().endswith(".svg")):
        raise ToolError("Image unavailable")
    result = bridge.resolve_embed(vault, source, target)
    if not _safe_relative(result["path"]):
        raise ToolError("Image unavailable")
    raw = base64.b64decode(result["data"], validate=True)
    if not raw or len(raw) > _MAX_IMAGE_BYTES or result["mime"] not in _MEDIA_MIME.values():
        raise ToolError("Image unavailable")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in _MEDIA_MIME or _MEDIA_MIME[image.format] != result["mime"]:
                raise ToolError("Image unavailable")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > _MAX_IMAGE_PIXELS:
                raise ToolError("Image unavailable")
            if getattr(image, "n_frames", 1) != 1:
                raise ToolError("Image unavailable")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            pixels = image.convert("RGBA" if image.mode in {"RGBA", "LA"} or "transparency" in image.info else "RGB")
            encoded = io.BytesIO()
            pixels.save(encoded, format="PNG")
    clean = encoded.getvalue()
    if len(clean) > _MAX_IMAGE_BYTES or len(clean) * 4 // 3 + 512 > _MAX_IMAGE_RESPONSE:
        raise ToolError("Image unavailable")
    context = f"Vault: {vault}; note: {source}; image: {result['path']}"
    if len(context) > 1300:
        raise ToolError("Image unavailable")
    return ToolResult(content=[TextContent(type="text", text=context),
                               ImageContent(type="image", data=base64.b64encode(clean).decode(),
                                            mimeType="image/png")])


def _embed_targets(text: str) -> list[str]:
    """Local image embeds in reading order: ![[target|...]] and ![alt](target "title").

    A bounded str.find scan instead of a regex, so hostile notes cannot cause backtracking.
    """
    targets = []
    index = text.find("![")
    while index != -1 and len(targets) < _MAX_EMBEDS:
        target = None
        if text.startswith("![[", index):
            close = text.find("]]", index + 3, index + 3 + 1100)
            if close != -1:
                inner = text[index + 3:close]
                target = re.split(r"[|#]", inner, maxsplit=1)[0]
        else:
            middle = text.find("](", index + 2, index + 2 + 520)
            # The alt text must not contain another image opener.
            if middle != -1 and text.find("![", index + 2, middle) == -1:
                close = text.find(")", middle + 2, middle + 2 + 1100)
                if close != -1:
                    inner = text[middle + 2:close].strip()
                    if inner.startswith("<"):
                        end = inner.find(">")
                        inner = inner[1:end] if end != -1 else ""
                    else:
                        inner = inner.split(" ", 1)[0]
                    target = unquote(inner)
        if target is not None:
            target = target.strip()
            if ("\n" not in target and "://" not in target and 0 < len(target) <= 512
                    and _EMBED_IMAGE.search(target)):
                targets.append(target)
        index = text.find("![", index + 2)
    return targets


_MAX_EMBEDS = 1000
_EMBED_IMAGE = re.compile(r"\.(?:png|jpe?g|webp|gif)$", re.IGNORECASE)
_PAGE_IMAGES = 10
_PAGE_SECONDS = 20.0
_PAGE_IMAGE_BUDGET = 6 * 1024 * 1024


def _note_with_images(bridge: BridgeClient, vault: str, path: str, start: int,
                      limit: int) -> ToolResult:
    """The note text plus one page of its local image embeds, in reading order."""
    note = bridge.read_note(vault, path)
    targets = _embed_targets(note["content"])
    total = len(targets)
    content = []
    if start == 0:
        content.append(TextContent(type="text", text=note["content"]))
    shown = 0
    budget = _PAGE_IMAGE_BUDGET
    index = start
    stop_at = time.monotonic() + _PAGE_SECONDS
    # Stop early on a slow page and hand the rest back through next_start.
    while index < total and shown < limit and (shown == 0 or time.monotonic() < stop_at):
        target = targets[index]
        try:
            image = _image_content(bridge, vault, path, target)
            size = len(image.content[1].data)
        except (ToolError, BridgeError, ValueError, TypeError, KeyError,
                UnidentifiedImageError, OSError, Image.DecompressionBombWarning):
            content.append(TextContent(type="text", text=f"Image {index + 1} of {total}: {target[:128]} (unavailable)"))
        else:
            if shown and size > budget:
                break
            budget -= size
            content.append(TextContent(type="text", text=f"Image {index + 1} of {total}: {target[:128]}"))
            content.append(image.content[1])
        shown += 1
        index += 1
    summary = {"vault": vault, "path": note["path"], "revision": note["revision"],
               "images_total": total, "images_start": start, "images_returned": index - start,
               "next_start": index if index < total else None}
    content.insert(0, TextContent(type="text", text=(
        f"Note {note['path']} in {vault}: {total} embedded images; this page has images "
        f"{start + 1 if index > start else 0}-{index}."
        + (f" Call again with start={index} for the rest." if index < total else ""))))
    return ToolResult(content=content, structured_content=summary)


def _page(view, nonce: str) -> str:
    def escaped(value: object) -> str:
        # Invisible format characters are shown as escapes so the owner sees exactly what runs.
        return html.escape("".join(_visible(char) for char in str(value)), quote=True)
    details = json.dumps(view.action.arguments, sort_keys=True, ensure_ascii=False)
    state = json.dumps([entry.__dict__ for entry in view.state], sort_keys=True, ensure_ascii=False)
    rows = (("Client", view.client_id), ("Vault", view.action.vault),
            ("Action", view.action.verb), ("Arguments", details),
            ("Observed state", state), ("Preview", view.preview),
            ("Digest", view.digest),
            ("Expires in seconds", max(0, int(view.expires_at - time.monotonic()))))
    listing = "".join(f"<dt>{escaped(label)}</dt><dd><pre>{escaped(value)}</pre></dd>" for label, value in rows)
    fields = (f'<input type="hidden" name="digest" value="{escaped(view.digest)}">'
              f'<input type="hidden" name="csrf" value="{escaped(nonce)}">')
    page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Obsidian approval</title>"
            "</head><body><h1>Exact Obsidian action</h1><dl>" + listing + "</dl>"
            '<form method="post">' + fields +
            '<button name="decision" value="approve">Approve</button>'
            '<button name="decision" value="reject">Reject</button></form></body></html>')
    if len(page.encode()) > _MAX_PREVIEW_HTML:
        raise ApprovalError("incomplete_preview")
    return page


_OWNER_HEADERS = {
    "Cache-Control": "no-store", "Pragma": "no-cache", "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
}


def create_gateway(
    bridge: BridgeClient,
    credentials: Mapping[str, str],
    grants: Mapping[str, Mapping[str, frozenset[str]]],
    owner_auth: OwnerAuthenticator,
    enrollments: Mapping[str, VaultEnrollment],
    *,
    destructive_adapter=None,
    destructive_verbs: frozenset[str] = frozenset(),
):
    if not isinstance(enrollments, Mapping) or len(enrollments) > _MAX_VAULTS:
        raise ValueError(f"gateway supports 1 to {_MAX_VAULTS} enrolled vaults")
    if (not isinstance(grants, Mapping)
            or any(not isinstance(scoped, Mapping) or len(scoped) > _MAX_VAULTS
                   for scoped in grants.values())):
        raise ValueError(f"gateway grants support at most {_MAX_VAULTS} vaults per client")
    if (set(credentials) != {"codex", "claude", "opencode", "antigravity"}
            or any(not _valid_token(value) or len(value) > 128 for value in credentials.values())
            or len(set(credentials.values())) != 4
            or set(grants) != set(credentials)
            or any(vault not in enrollments or not verbs
                   or not verbs <= set(_SAFE_CAPABILITIES) | destructive_verbs
                   for client in grants for vault, verbs in grants[client].items())
            or (destructive_verbs and destructive_adapter is None)
            or (destructive_adapter is not None and not destructive_verbs)):
        raise ValueError("invalid gateway configuration")
    # Live grants stay live: vaults that register later must appear without a restart.
    grants = {client: scoped if isinstance(scoped, _LiveVaults)
              else {vault: frozenset(verbs) for vault, verbs in scoped.items()}
              for client, scoped in grants.items()}
    # Ten minutes: long enough for the owner to notice the link and review it.
    coordinator = ApprovalCoordinator(destructive_adapter or BridgeAdapter(bridge), enrollments,
                                      ttl=_APPROVAL_SECONDS)
    execution = _ExecutionPool(enrollments)
    @asynccontextmanager
    async def lifespan(_server):
        try:
            yield
        finally:
            await owner_auth.aclose()
            execution.close()

    mcp = FastMCP("Obsidian app gateway", auth=ScopedTokens(credentials), lifespan=lifespan,
                  mask_error_details=True, strict_input_validation=True)
    readiness = CapabilityFilter(grants, bridge, destructive_verbs)
    mcp.add_middleware(readiness)
    csrf = CsrfLedger()

    def agent() -> AgentContext:
        token = get_access_token()
        if token is None or token.client_id not in grants:
            raise ToolError("Tool request failed")
        # A per-call snapshot of the (possibly live) grants.
        return AgentContext(token.client_id, dict(grants[token.client_id]))

    async def permitted(context: AgentContext, vault: str, verb: str,
                        deadline: float | None = None) -> None:
        if type(vault) is not str or verb not in context.grants.get(vault, frozenset()):
            raise ToolError("Tool request failed")
        capabilities = (await readiness.probe_grants({vault: context.grants[vault]},
                                                     deadline=deadline)).get(vault)
        if capabilities is None:
            raise ToolError("Tool request failed") from None
        needed = _SAFE_CAPABILITIES.get(verb) or _GUARDED_CAPABILITIES.get(verb)
        if needed and needed not in capabilities:
            raise ToolError("Tool request failed")

    async def call(verb: str, vault: str, arguments: dict) -> object:
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        context = agent()
        await permitted(context, vault, verb, deadline)
        try:
            # Built per call so vaults that registered after startup are included.
            current = ApprovalCoordinator(
                BridgeAdapter(bridge, context.client_id if verb in {"create_note", "append_note"} else None),
                enrollments)
            return await execution.run(current.execute, context,
                                       Operation(vault, verb, arguments), deadline=deadline,
                                       preserve_result=verb in {"create_note", "append_note"},
                                       vault=vault)
        except ApprovalError:
            raise ToolError("Tool request failed") from None

    @mcp.tool
    async def list_vaults() -> dict:
        """List the open vaults this client can use. Pass a vault's `id` on every other call."""
        context = agent()
        ready = await readiness.ready_vaults(context.grants)
        return {"vaults": [{"id": vault, "name": bridge.vault_name(vault) or vault} for vault in ready]}

    async def control_vault(context: AgentContext, deadline: float) -> str:
        """Any open vault this client may use for app-level vault management."""
        allowed = {vault: verbs for vault, verbs in context.grants.items() if "manage_vaults" in verbs}
        ready = await readiness.probe_grants(allowed, deadline=deadline)
        for vault in sorted(ready):
            if _SAFE_CAPABILITIES["manage_vaults"] in ready[vault]:
                return vault
        raise ToolError("No open vault can manage vaults right now")

    @mcp.tool
    async def list_all_vaults() -> dict:
        """Every vault the Obsidian desktop knows about, open or closed. Open a closed vault
        with open_vault; it then appears in list_vaults."""
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        context = agent()
        vault = await control_vault(context, deadline)
        try:
            known = await execution.run(bridge.app_vaults, vault, deadline=deadline)
        except BridgeError:
            raise ToolError("Tool request failed") from None
        ids = {bridge.vault_name(item): item for item in bridge.vault_ids()}
        return {"vaults": [{"name": entry["name"], "open": entry["open"], "id": ids.get(entry["name"])}
                           for entry in known["vaults"]]}

    @mcp.tool
    async def open_vault(name: str) -> dict:
        """Open a vault the desktop knows about (see list_all_vaults) in its own window."""
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        vault = await control_vault(agent(), deadline)
        try:
            return await execution.run(bridge.open_vault, vault, name, deadline=deadline)
        except BridgeError:
            raise ToolError("Tool request failed") from None

    @mcp.tool
    async def create_vault(name: str) -> dict:
        """Create a new empty vault (letters, digits, spaces, '.', '_' or '-') and open it.
        It appears in list_vaults within a few seconds. Connecting it to Obsidian Sync is a
        one-time step in the desktop."""
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        vault = await control_vault(agent(), deadline)
        try:
            return await execution.run(bridge.create_vault, vault, name, deadline=deadline)
        except BridgeError:
            raise ToolError("Tool request failed") from None

    @mcp.tool
    async def list_entries(vault: str, folder: str = "") -> dict:
        """List the files and folders directly inside one folder (vault root by default)."""
        return await call("list_notes", vault, {} if folder == "" else {"folder": folder})

    @mcp.tool
    async def read_note(vault: str, path: str) -> dict:
        """Read one Markdown note and its revision from the running app."""
        return await call("read_note", vault, {"path": path})

    @mcp.tool
    async def search_notes(vault: str, query: str) -> dict:
        """Literal bounded search in one running vault."""
        return await call("search", vault, {"query": query})

    @mcp.tool
    async def create_note(vault: str, path: str, content: str) -> dict:
        """Create a new note only when the path is absent."""
        return await call("create_note", vault, {"path": path, "content": content, "expected_absent": True})

    @mcp.tool
    async def append_note(vault: str, path: str, content: str, expected_revision: str) -> dict:
        """Append through the app only when the note revision matches."""
        return await call("append_note", vault, {"path": path, "content": content,
                                                 "expected_revision": expected_revision})

    @mcp.tool
    async def mutation_receipt(vault: str, operation: str, path: str, content: str,
                               receipt: str, expected_revision: str = "") -> dict:
        """Reconcile the exact original create or append; this never starts a write."""
        if operation not in {"create", "append"} or (operation == "create" and expected_revision):
            raise ToolError("Tool request failed")
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        context = agent()
        await permitted(context, vault, "create_note" if operation == "create" else "append_note", deadline)
        mutation = {"path": path, "content": content}
        if operation == "append":
            mutation["expected_revision"] = expected_revision
        try:
            return await execution.run(bridge.mutation_receipt, vault, operation, mutation,
                                       receipt, context.client_id, deadline=deadline, vault=vault)
        except BridgeError:
            raise ToolError("Tool request failed") from None

    @mcp.tool
    async def create_folder(vault: str, path: str) -> dict:
        """Create a folder (and any missing parent folders) in a vault."""
        return await call("create_folder", vault, {"path": path, "expected_absent": True})

    @mcp.tool
    async def move(vault: str, source: str, destination: str) -> dict:
        """Move or rename a note, attachment or folder inside one vault. Obsidian rewrites links
        to it in other notes, as it does when you move a file in the app; moving a folder moves
        everything in it. Fails if the destination exists; missing destination folders are created."""
        return await call("move", vault, {"source": source, "destination": destination})

    @mcp.tool
    async def read_note_with_images(vault: str, path: str, start: int = 0,
                                    limit: int = _PAGE_IMAGES) -> ToolResult:
        """Read a note and its embedded local images in reading order. Images come in pages:
        the first call returns the text and up to `limit` images; use `next_start` for more."""
        if not 0 <= start <= 10_000 or not 1 <= limit <= _PAGE_IMAGES:
            raise ToolError("Tool request failed")
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        context = agent()
        await permitted(context, vault, "read_note", deadline)
        await permitted(context, vault, "read_media", deadline)
        try:
            return await execution.run(_note_with_images, bridge, vault, path, start, limit,
                                       deadline=deadline)
        except (BridgeError, ValueError, TypeError, KeyError):
            raise ToolError("Tool request failed") from None

    @mcp.tool
    async def read_embedded_image(vault: str, source: str, target: str) -> ToolResult:
        """Resolve a local note image through the app and return validated image bytes."""
        deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
        await permitted(agent(), vault, "read_media", deadline)
        try:
            return await execution.run(_image_content, bridge, vault, source, target,
                                       deadline=deadline)
        except ToolError:
            raise ToolError("Image unavailable") from None
        except (BridgeError, ValueError, TypeError, KeyError, UnidentifiedImageError, OSError,
                Image.DecompressionBombWarning):
            raise ToolError("Image unavailable") from None

    if destructive_verbs:
        @mcp.tool
        async def prepare_action(vault: str, verb: str, arguments: dict[str, object]) -> dict:
            """Ask the owner to approve a destructive action. Nothing changes yet.

            Verbs and arguments:
              replace_note  {"path": "Folder/Note.md", "content": "<the whole new note>"}
              trash_note    {"path": "Folder/Note.md"}      (moves it to the vault's .trash)
              trash_folder  {"path": "Folder"}              (moves it and everything in it to .trash)
            Returns an approval `url`. Give it to the user; they open it, review exactly what
            will change and approve it. Then call commit_action with the same vault, verb,
            arguments and the returned `id` as pending_id. Approvals expire after ten minutes.
            """
            deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
            context = agent()
            await permitted(context, vault, verb, deadline)
            if verb not in destructive_verbs:
                raise ToolError("Tool request failed")
            try:
                view = await execution.run(coordinator.prepare, context,
                                           Operation(vault, verb, arguments), deadline=deadline,
                                           vault=vault)
            except ApprovalError:
                if verb == "replace_note":
                    raise ToolError("Could not prepare this replacement. The owner must see the whole "
                                    "change, so very large diffs are refused; split it into smaller "
                                    "replacements or use append_note.") from None
                raise ToolError("Tool request failed") from None
            return {"id": view.id, "digest": view.digest,
                    "expires_at": time.time() + max(0, view.expires_at - time.monotonic()),
                    "url": owner_auth.config.origin + "/owner/" + view.id}

        @mcp.tool
        async def commit_action(vault: str, verb: str, arguments: dict[str, object], pending_id: str) -> object:
            """Run an action the owner approved via prepare_action's url. Pass the same vault, verb
            and arguments, and the prepare_action id as pending_id. It runs at most once, and
            fails without changing anything if the note or folder changed since it was approved."""
            deadline = time.monotonic() + _EXECUTION_ADMISSION_TIMEOUT
            context = agent()
            await permitted(context, vault, verb, deadline)
            if verb not in destructive_verbs:
                raise ToolError("Tool request failed")
            try:
                return await execution.run(coordinator.commit, context, pending_id,
                                           Operation(vault, verb, arguments), deadline=deadline,
                                           preserve_result=True, vault=vault)
            except ApprovalError:
                raise ToolError("Tool request failed") from None

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request):
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    @mcp.custom_route("/owner/{pending_id}", methods=["GET", "POST"])
    async def owner_route(request: Request):
        pending_id = request.path_params["pending_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", pending_id):
            return PlainTextResponse("Unavailable", status_code=404, headers=_OWNER_HEADERS)
        try:
            owner = await owner_auth.verify(request)
        except OwnerAuthError:
            return PlainTextResponse(
                "Owner session required. Sign in at " + owner_auth.config.origin + "/ in this browser, "
                "then reload this page.", status_code=401, headers=_OWNER_HEADERS)
        try:
            view = coordinator.owner_view(owner, pending_id)
        except ApprovalError:
            return PlainTextResponse("Unavailable", status_code=404, headers=_OWNER_HEADERS)
        if request.method == "GET":
            try:
                nonce = csrf.issue(owner.owner_id, pending_id, view.digest)
                page = _page(view, nonce)
            except (OwnerAuthError, ApprovalError):
                return PlainTextResponse("Unavailable", status_code=404, headers=_OWNER_HEADERS)
            response = HTMLResponse(page, headers=_OWNER_HEADERS)
            response.set_cookie("__Host-obsidian_csrf", nonce, secure=True, httponly=True,
                                samesite="strict", path="/", max_age=120)
            return response
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
            return PlainTextResponse("Forbidden", status_code=403, headers=_OWNER_HEADERS)
        try:
            body = await _read_body(request, 4096)
        except _BodyTooLarge:
            return PlainTextResponse("Forbidden", status_code=403, headers=_OWNER_HEADERS)
        except TimeoutError:
            return PlainTextResponse("Request timed out", status_code=408, headers=_OWNER_HEADERS)
        except ClientDisconnect:
            return PlainTextResponse("Forbidden", status_code=403, headers=_OWNER_HEADERS)
        try:
            fields = parse_qs(body.decode("ascii"), strict_parsing=True)
            if set(fields) != {"decision", "digest", "csrf"} or any(len(values) != 1 for values in fields.values()):
                raise ValueError
            decision, digest, nonce = (fields[key][0] for key in ("decision", "digest", "csrf"))
        except (ValueError, UnicodeError):
            return PlainTextResponse("Forbidden", status_code=403, headers=_OWNER_HEADERS)
        origins = request.headers.getlist("origin")
        if (decision not in {"approve", "reject"} or not secrets.compare_digest(digest, view.digest)
                or len(origins) != 1
                or not csrf.consume(owner.owner_id, pending_id, digest, nonce,
                                    request.cookies.get("__Host-obsidian_csrf", ""),
                                    origins[0], owner_auth.config.origin)):
            return PlainTextResponse("Forbidden", status_code=403, headers=_OWNER_HEADERS)
        try:
            if decision == "approve":
                coordinator.approve(owner, pending_id, digest)
            else:
                coordinator.reject(owner, pending_id, digest)
        except ApprovalError:
            return PlainTextResponse("Unavailable", status_code=404, headers=_OWNER_HEADERS)
        response = PlainTextResponse("Decision recorded", headers=_OWNER_HEADERS)
        response.delete_cookie("__Host-obsidian_csrf", path="/", secure=True, httponly=True,
                               samesite="strict")
        return response

    return mcp.http_app(path="/mcp", json_response=True, stateless_http=True,
                        middleware=[ASGIMiddleware(MCPHostOriginGuard),
                                    ASGIMiddleware(RequestBodyLimit)],
                        # FastMCP's app-wide guard would also gate the owner host and probes.
                        host_origin_protection=False)


def create_gateway_from_env():
    """Production entrypoint. The reviewed registry is mounted read only."""
    path = os.environ.get("OBSIDIAN_GATEWAY_REGISTRY", "")
    if not path or not Path(path).is_absolute():
        raise ValueError("gateway registry required")
    configured = Path(path)
    source = configured.resolve(strict=True)
    if (not stat.S_ISREG(source.stat().st_mode)
            or any(not os.statvfs(location).f_flag & os.ST_RDONLY
                   for location in (configured.parent, source.parent, source))):
        raise ValueError("gateway registry must be read only")
    with source.open("rb") as registry:
        raw = registry.read(_MAX_REGISTRY_BYTES + 1)
    if len(raw) > _MAX_REGISTRY_BYTES:
        raise ValueError("gateway registry exceeds size limit")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("invalid gateway registry JSON") from None
    if (not isinstance(document, dict) or set(document) != {"version", "clients", "owner"}
            or document["version"] != 2):
        raise ValueError("invalid gateway registry")
    clients = document["clients"]
    owner = document["owner"]
    if (not isinstance(clients, list) or sorted(clients) != ["antigravity", "claude", "codex", "opencode"]
            or not isinstance(owner, dict) or set(owner) != {"check_url", "origin", "owner_ids"}):
        raise ValueError("invalid gateway registry")

    def bounded_text(value, limit=_MAX_REGISTRY_TEXT):
        return type(value) is str and 0 < len(value) <= limit

    owner_ids = owner["owner_ids"]
    if (not bounded_text(owner["check_url"]) or not bounded_text(owner["origin"])
            or type(owner_ids) is not list or not 1 <= len(owner_ids) <= 16
            or any(not bounded_text(value, 128)
                   or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value)
                   for value in owner_ids)):
        raise ValueError("invalid owner registry fields")
    runtime = os.environ.get("OBSIDIAN_BRIDGE_DIR", "")
    if not runtime or not Path(runtime).is_absolute():
        raise ValueError("bridge runtime directory required")
    parent = os.environ.get("OBSIDIAN_BRIDGE_VAULT_PARENT", "")
    if not parent or not Path(parent).is_absolute():
        raise ValueError("vault parent directory required")
    bridge = BridgeClient(VaultDirectory(Path(runtime), parent=Path(parent)))
    credentials = {client: os.environ.get(f"OBSIDIAN_MCP_TOKEN_{client.upper()}", "") for client in clients}
    # Every client gets every safe capability on every vault the desktop has open.
    every = frozenset(_SAFE_CAPABILITIES) | frozenset(_GUARDED_CAPABILITIES)
    grants = {client: _LiveVaults(bridge, lambda _vault: every) for client in clients}
    owners = frozenset(owner_ids)
    enrollments = _LiveVaults(bridge, lambda _vault: VaultEnrollment(owners, {}))
    auth = OwnerAuthenticator(OwnerAuthConfig(owner["check_url"], owner["origin"], owners,
                                               frozenset(), vault_source=bridge.vault_ids))
    return create_gateway(bridge, credentials, grants, auth, enrollments,
                          destructive_adapter=BridgeAdapter(bridge),
                          destructive_verbs=frozenset(_GUARDED_CAPABILITIES))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(create_gateway_from_env(), host="0.0.0.0", port=8000,
                access_log=False, log_level="warning")
