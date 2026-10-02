"""Authenticated, explicitly scoped HTTP surface over obsidian-remote-mcp 2.1.0."""

from __future__ import annotations

import asyncio
import base64
from collections import defaultdict, deque
from functools import partial
import json
import logging
import os
import re
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.exceptions import ToolError
from starlette.middleware import Middleware as ASGIMiddleware
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, PlainTextResponse


CLIENTS = ("codex", "claude", "opencode", "antigravity")
ALLOWED_TOOLS = frozenset({"list_vaults_tool", "read_note_tool", "search_notes_tool",
                           "write_note_tool", "append_to_note_tool"})
PROTECTED_READ = (".obsidian/", ".trash/", "private/", "_AI_INSTRUCTIONS.md")
MCP_PATH = "/mcp"
MAX_HTTP_BODY = 1024 * 1024
MAX_NOTE_CONTENT = 256 * 1024
MAX_STORED_NOTE = 1024 * 1024
MAX_SEARCH_FILES = 10_000
MAX_SEARCH_BYTES = 128 * 1024 * 1024
MAX_SEARCH_ENTRIES = 50_000
MAX_SEARCH_DEPTH = 32
CALLS_PER_MINUTE = 120
SEARCHES_PER_MINUTE = 16


class RequestBodyLimit:
    """Bound the complete HTTP body before the MCP transport parses JSON."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != MCP_PATH or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            async for chunk in Request(scope, receive).stream():
                body.extend(chunk)
                if len(body) > MAX_HTTP_BODY:
                    return await PlainTextResponse("Request too large", status_code=413)(scope, receive, send)
        except ClientDisconnect:
            return

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def _valid_token(value: str) -> bool:
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,}", value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return False
    return len(decoded) >= 32 and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() == value


def _note_path(value: object) -> bool:
    if not isinstance(value, str) or not value.lower().endswith(".md"):
        return False
    candidate = value
    for _ in range(len(value) + 1):
        candidate = candidate.replace("\\", "/")
        if any(part in (".", "..") for part in candidate.split("/")):
            return False
        decoded = unquote(candidate)
        if decoded == candidate:
            return True
        candidate = decoded
    return False


def _registry() -> tuple[dict, Path, Path]:
    configured = os.environ.get("REGISTRY_CONFIG")
    if not configured:
        raise ValueError("Registry is required")
    path = Path(os.path.abspath(configured))
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    # Both the projected link and its target must be on a read-only mount.
    # Mode 0444 alone is reversible by a runtime-owned file's owner.
    if not stat.S_ISREG(metadata.st_mode) or any(
        not os.statvfs(component).f_flag & os.ST_RDONLY
        for component in (path.parent, resolved.parent, resolved)
    ):
        raise ValueError("Registry must be on a read-only mount")
    source = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or set(source) != {"vaults", "identities"}:
        raise ValueError("Invalid registry")
    raw_vaults = source["vaults"]
    if not isinstance(raw_vaults, dict) or not raw_vaults:
        raise ValueError("Invalid vault enrollment")
    vaults = {}
    for name, entry in raw_vaults.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name):
            raise ValueError("Invalid vault ID")
        if not isinstance(entry, dict) or not {"path", "description", "read_paths", "write_paths"} <= set(entry):
            raise ValueError("Invalid vault entry")
        if not isinstance(entry["path"], str) or not Path(entry["path"]).is_absolute():
            raise ValueError("Vault root must be absolute")
        if not isinstance(entry["description"], str) or not entry["description"]:
            raise ValueError("Vault display name is required")
        for key in ("read_paths", "write_paths", "exclude_paths", "deny_read_paths", "deny_write_paths"):
            if key in entry and (not isinstance(entry[key], list) or not all(isinstance(x, str) for x in entry[key])):
                raise ValueError("Invalid path policy")
        if set(entry) - {"path", "description", "read_paths", "write_paths", "exclude_paths", "deny_read_paths", "deny_write_paths", "read_only"}:
            raise ValueError("Unsupported vault setting")
        vault = dict(entry)
        vault["exclude_paths"] = list(dict.fromkeys([*entry.get("exclude_paths", []), *PROTECTED_READ]))
        vault["deny_read_paths"] = list(dict.fromkeys([*entry.get("deny_read_paths", []), *PROTECTED_READ]))
        vault["deny_write_paths"] = list(dict.fromkeys([*entry.get("deny_write_paths", []), *PROTECTED_READ]))
        vaults[name] = vault

    raw_clients = source["identities"]
    if not isinstance(raw_clients, list) or len(raw_clients) != len(CLIENTS):
        raise ValueError("Four client enrollments are required")
    identities = []
    seen_clients = set()
    seen_tokens = set()
    for entry in raw_clients:
        if not isinstance(entry, dict) or set(entry) != {"client", "vaults"}:
            raise ValueError("Invalid client enrollment")
        client = entry["client"]
        allowed = entry["vaults"]
        if client not in CLIENTS or client in seen_clients:
            raise ValueError("Invalid client ID")
        if not isinstance(allowed, list) or not allowed or len(allowed) != len(set(allowed)) or any(name not in vaults for name in allowed):
            raise ValueError("Invalid client vault scope")
        token = os.environ.get(f"OBSIDIAN_MCP_TOKEN_{client.upper()}", "")
        if not _valid_token(token) or token in seen_tokens:
            raise ValueError("Invalid client credential")
        seen_clients.add(client)
        seen_tokens.add(token)
        identities.append({"type": "api_key", "value": token, "vaults": allowed})
    if seen_clients != set(CLIENTS):
        raise ValueError("Incomplete client enrollment")
    return {"vaults": vaults, "identities": identities}, path, resolved


def _prepare_runtime() -> Path:
    host = os.environ.get("HOST", "0.0.0.0")
    port = os.environ.get("PORT", "8000")
    if host not in ("0.0.0.0", "127.0.0.1"):
        raise ValueError("Unsupported bind host")
    if not re.fullmatch(r"[0-9]+", port) or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid bind port")
    document, registry_path, registry_target = _registry()
    state_path = Path(os.path.abspath(os.environ.get("STATE_DIR", "/tmp/state")))
    if any(part.is_symlink() for part in (state_path, *state_path.parents)):
        raise ValueError("State directory cannot be a symlink")
    state = state_path.resolve()
    for vault in document["vaults"].values():
        root_path = Path(os.path.abspath(vault["path"]))
        root = root_path.resolve()
        if state.is_relative_to(root) or state_path.is_relative_to(root_path):
            raise ValueError("State directory overlaps a vault")
        if any(source.is_relative_to(directory) for source in (registry_path, registry_target)
               for directory in (root_path, root)):
            raise ValueError("Registry overlaps a vault")
    if registry_path.is_relative_to(state_path) or registry_target.is_relative_to(state):
        raise ValueError("Registry overlaps runtime state")
    parent = state.parent.stat()
    if not stat.S_ISDIR(parent.st_mode) or (parent.st_mode & 0o022 and (
        not parent.st_mode & stat.S_ISVTX or parent.st_uid not in (0, os.geteuid())
    )):
        raise ValueError("State directory parent is unsafe")
    state.mkdir(mode=0o700, exist_ok=True)
    details = state.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise ValueError("State directory must be owned and private")
    config_path = state / "vaults.json"
    # A previous process may have been killed without running Python cleanup.
    config_path.unlink(missing_ok=True)
    try:
        fd, temporary = tempfile.mkstemp(prefix="vaults-", suffix=".json", dir=state)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream)
            os.replace(temporary, config_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        for key in ("API_KEY", "OBSIDIAN_MCP_API_KEY", "OAUTH_GITHUB_CLIENT_ID", "OAUTH_GITHUB_CLIENT_SECRET"):
            os.environ.pop(key, None)
        for key in ("CANVAS", "EXCALIDRAW", "KANBAN", "BASES", "MOVE", "FOLDER_RENAME", "BULK_REPLACE", "DELETE"):
            os.environ[f"ENABLE_{key}"] = "false"
        os.environ.update({
            "VAULTS_CONFIG": str(config_path), "TRANSPORT": "http",
            "REQUIRE_WRITE_PRECONDITIONS": "true", "FASTMCP_HOME": str(state / "fastmcp"),
            "LOCK_PATH": str(state / "locks"), "AUDIT_LOG_PATH": str(state / "audit.jsonl"),
            "HOST": host, "PORT": port,
        })
        return config_path
    except BaseException:
        config_path.unlink(missing_ok=True)
        raise


class NoteBoundary(Middleware):
    def __init__(self, stock, available):
        self.stock = stock
        self.available = available
        self.calls = defaultdict(deque)
        self.searches = defaultdict(deque)
        self.search_slots = asyncio.Semaphore(2)

    def _charge(self, client, search):
        now = time.monotonic()
        for ledger, budget in ((self.calls[client], CALLS_PER_MINUTE),
                               (self.searches[client], SEARCHES_PER_MINUTE)) if search else (
                                   (self.calls[client], CALLS_PER_MINUTE),):
            while ledger and ledger[0] <= now - 60:
                ledger.popleft()
            if len(ledger) >= budget:
                raise PermissionError("Call budget exceeded")
        self.calls[client].append(now)
        if search:
            self.searches[client].append(now)

    async def on_list_tools(self, context: MiddlewareContext, call_next):
        return [tool for tool in await call_next(context) if tool.name in ALLOWED_TOOLS]

    def _storage(self, vault, identity):
        from obsidian_mcp.config import (reset_current_identity, reset_current_vault,
                                         set_current_identity, set_current_vault)
        from obsidian_mcp.storage.filesystem import VaultStorage

        vault_token = set_current_vault(vault)
        identity_token = set_current_identity(identity)
        try:
            return VaultStorage.from_config(self.stock.get_config())
        finally:
            reset_current_identity(identity_token)
            reset_current_vault(vault_token)

    def _stored_note_size(self, storage, path, *, read):
        try:
            size = storage.stat(path, read=read).st_size
        except (FileNotFoundError, NotADirectoryError):
            if read:
                raise
            return 0
        if size > MAX_STORED_NOTE:
            raise PermissionError("Stored note exceeds limit")
        return size

    def _check_search_work(self, storage, excluded, identity):
        from obsidian_mcp.storage.filesystem import _dir_flags, _opened_dir
        from obsidian_mcp.storage.policy import ReadPermissionError, VaultPathError
        from obsidian_mcp.tools.read import _is_excluded

        policy = storage.policy
        entries = files = total_bytes = 0

        def walk(fd, prefix, depth):
            nonlocal entries, files, total_bytes
            with os.scandir(fd) as items:
                for item in items:
                    entries += 1
                    if entries > MAX_SEARCH_ENTRIES:
                        raise PermissionError("Search work exceeds limit")
                    rel = f"{prefix}/{item.name}" if prefix else item.name
                    try:
                        info = item.stat(follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            continue
                        policy.authorize_discovered_read(rel, info, identity=identity)
                    except (VaultPathError, ReadPermissionError, OSError):
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        if depth >= MAX_SEARCH_DEPTH:
                            raise PermissionError("Search work exceeds limit")
                        try:
                            child = os.open(item.name, _dir_flags(), dir_fd=fd)
                        except OSError:
                            continue
                        try:
                            walk(child, rel, depth + 1)
                        finally:
                            os.close(child)
                    elif rel.lower().endswith(".md") and not _is_excluded(rel, excluded):
                        files += 1
                        total_bytes += info.st_size
                        if (files > MAX_SEARCH_FILES or info.st_size > MAX_STORED_NOTE
                                or total_bytes > MAX_SEARCH_BYTES):
                            raise PermissionError("Search work exceeds limit")

        with _opened_dir(policy.root, "") as root_fd:
            walk(root_fd, "", 0)

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        # FastMCP's argument ValidationError branch bypasses mask_error_details.
        # The stock revision guard returns a structured ToolResult, not an exception.
        try:
            return await self._call_tool(context, call_next)
        except Exception:
            raise ToolError("Tool request failed") from None

    async def _call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        if name not in ALLOWED_TOOLS:
            raise PermissionError("Tool unavailable")
        identity = self.stock._resolve_identity(self.stock.get_config())
        self._charge(identity.value, name == "search_notes_tool")
        if name != "list_vaults_tool":
            args = context.message.arguments or {}
            if not isinstance(args, dict) or not isinstance(args.get("vault"), str) or not args["vault"]:
                raise PermissionError("Explicit vault is required")
            if args["vault"] not in identity.vaults or not self.available(args["vault"]):
                raise PermissionError("Vault unavailable")
            if name in ("read_note_tool", "write_note_tool", "append_to_note_tool") and not _note_path(args.get("path")):
                raise PermissionError("Markdown note path required")
            if name == "write_note_tool" and args.get("create_only") is not True:
                raise PermissionError("Creation requires create_only=true")
            if name in ("write_note_tool", "append_to_note_tool") and (
                not isinstance(args.get("content"), str) or len(args["content"].encode("utf-8")) > MAX_NOTE_CONTENT):
                raise PermissionError("Note content exceeds limit")
            if name == "append_to_note_tool" and (
                args.get("create") is not False or not isinstance(args.get("expected_revision"), str)
                or not args["expected_revision"]
            ):
                raise PermissionError("Append requires create=false and expected_revision")
            if name == "append_to_note_tool" and args.get("section") is not None:
                raise PermissionError("Section append unavailable")
            if name == "read_note_tool" and args.get("mode", "full") not in ("full", "outline"):
                raise PermissionError("Read mode unavailable")
            if name == "search_notes_tool" and (
                args.get("mode", "exact") != "exact" or not isinstance(args.get("query"), str)
                or len(args["query"]) > 256 or type(args.get("limit", 20)) is not int
                or not 1 <= args.get("limit", 20) <= 20
                or args.get("frontmatter_filter") is not None
                or args.get("field") not in (None, "body", "filename")
                or args.get("threshold", 0.8) != 0.8
                or (args.get("tag") is not None and (
                    not isinstance(args["tag"], str) or len(args["tag"]) > 128))
            ):
                raise PermissionError("Search arguments unavailable")
            if name in ("read_note_tool", "write_note_tool", "append_to_note_tool"):
                storage = self._storage(args["vault"], identity)
                size = self._stored_note_size(storage, args["path"], read=name != "write_note_tool")
                if name == "append_to_note_tool" and (
                    size + len(args["content"].encode("utf-8")) + 2 > MAX_STORED_NOTE
                ):
                    raise PermissionError("Appended note exceeds limit")
        if name == "search_notes_tool":
            async with self.search_slots:
                storage = self._storage(args["vault"], identity)
                excluded = self.stock.get_config().vaults[args["vault"]].exclude_paths
                await asyncio.to_thread(self._check_search_work, storage, excluded, identity)
                return await call_next(context)
        return await call_next(context)

    async def on_list_resources(self, context: MiddlewareContext, call_next):
        return []

    async def on_list_resource_templates(self, context: MiddlewareContext, call_next):
        return []

    async def on_read_resource(self, context: MiddlewareContext, call_next):
        raise PermissionError("Resources unavailable")

    async def on_list_prompts(self, context: MiddlewareContext, call_next):
        return []

    async def on_get_prompt(self, context: MiddlewareContext, call_next):
        raise PermissionError("Prompts unavailable")


async def _narrow_surface(stock) -> None:
    cfg = stock.get_config()
    roots = {name: (vault.path, vault.path.stat()) for name, vault in cfg.vaults.items()}

    def available(name: str) -> bool:
        if name not in roots:
            return False
        root, original = roots[name]
        try:
            current = root.stat(follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                return False
            with os.scandir(root):
                pass
            index = stock._indices.get(name)
            if index is None or not index.is_ready():
                return False
            status = index.reconcile_status()
            return status["last_reconcile_at"] is not None and status["last_reconcile_error"] is None
        except (OSError, ValueError):
            return False

    tools = await stock.mcp.list_tools(run_middleware=False)
    if not ALLOWED_TOOLS <= {tool.name for tool in tools}:
        raise RuntimeError("Upstream tool surface changed")
    for tool in tools:
        if tool.name not in ALLOWED_TOOLS or tool.name == "list_vaults_tool":
            stock.mcp.local_provider.remove_tool(tool.name)

    from obsidian_mcp.envelope import list_result

    @stock.mcp.tool(name="list_vaults_tool")
    def list_vaults_tool() -> dict:
        """List accessible vault IDs and display names; pass vault on every note call."""
        cfg = stock.get_config()
        identity = stock._resolve_identity(cfg)
        return list_result([{"name": name, "description": cfg.vaults[name].description}
                            for name in identity.vaults if available(name)])

    if {tool.name for tool in await stock.mcp.list_tools(run_middleware=False)} != ALLOWED_TOOLS:
        raise RuntimeError("Upstream tool removal failed")

    routes = stock.mcp._additional_http_routes
    if {route.path for route in routes} != {"/health", "/attachments/{path:path}"}:
        raise RuntimeError("Upstream HTTP routes changed")
    routes.clear()

    @stock.mcp.custom_route("/health", methods=["GET"])
    async def health_route(request):
        ready = all(available(name) for name in cfg.vaults)
        return JSONResponse({"status": "ok" if ready else "starting"}, status_code=200 if ready else 503)

    if {route.path for route in routes} != {"/health"}:
        raise RuntimeError("HTTP surface restriction failed")
    stock.mcp.add_middleware(NoteBoundary(stock, available))
    stock.mcp.instructions = "Call list_vaults_tool, then provide vault on every note call. Create only or append with a read revision."


def main() -> None:
    os.umask(0o002)
    def stop_on_term(signum, frame):
        raise SystemExit(0)

    previous_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, stop_on_term)
    config_path = None
    try:
        config_path = _prepare_runtime()
        logging.disable(logging.CRITICAL)
        import fastmcp

        # FastMCP reads its environment at import; keep inherited settings from
        # changing the exposed protocol, error surface, or private runtime path.
        fastmcp.settings.home = config_path.parent / "fastmcp"
        fastmcp.settings.streamable_http_path = MCP_PATH
        fastmcp.settings.debug = False
        fastmcp.settings.mask_error_details = True
        fastmcp.settings.strict_input_validation = False
        fastmcp.settings.ssrf_trust_proxy = False
        fastmcp.settings.server_dependencies = []
        fastmcp.settings.json_response = False
        fastmcp.settings.stateless_http = False
        fastmcp.settings.http_host_origin_protection = False
        fastmcp.settings.http_allowed_hosts = None
        fastmcp.settings.http_allowed_origins = None
        fastmcp.settings.decorator_mode = "function"
        fastmcp.settings.check_for_updates = "off"
        from obsidian_mcp import server as stock

        asyncio.run(_narrow_surface(stock))
        try:
            stock.mcp.run = partial(
                stock.mcp.run, path=MCP_PATH, middleware=[ASGIMiddleware(RequestBodyLimit)],
                json_response=False, stateless_http=False, host_origin_protection=False,
                show_banner=False,
            )
            stock.main()
        finally:
            for watcher in stock._watchers.values():
                watcher.stop()
    finally:
        if config_path is not None:
            config_path.unlink(missing_ok=True)
        signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Obsidian MCP startup failed", file=sys.stderr)
        raise SystemExit(1) from None
