"""Typed, version 1 private IPC adapter for running Obsidian vaults.

All vault windows inherit OBSIDIAN_BRIDGE_INSTANCE=kubernetes-obsidian and
OBSIDIAN_BRIDGE_DIR, one 0700 runtime directory outside every vault. When the
bridge plugin loads in a vault it registers itself: it creates a 0700
<runtime>/<vault_id> directory holding a fresh 0600 credential and a 0600
endpoint.json (version, vault_id, app_name, app_root). The abstract AF_UNIX
address is derived from <vault_id>.sock and that credential; no socket pathname
is created. The gateway discovers vaults with VaultDirectory and never mounts
vault contents; client input may select only a discovered vault ID.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
from typing import Any, Mapping


VERSION = 1
MAX_REQUEST = 384 * 1024
MAX_RESPONSE = 1024 * 1024
ERRORS = frozenset({"invalid_request", "invalid_path", "not_found", "conflict",
                    "limit_exceeded", "unsupported_media", "unknown_operation",
                    "unauthorized", "internal_error", "unknown_receipt"})
RESULT_KEYS = {
    "health": {"protocol", "vault", "app_name", "app_root", "capabilities"},
    "list": {"entries"}, "read": {"path", "content", "revision"},
    "search": {"matches"}, "create": {"receipt", "status", "result", "error"},
    "append": {"receipt", "status", "result", "error"}, "embed": {"path", "mime", "data"},
    "reserve": {"receipt"}, "receipt": {"receipt", "status", "result", "error"},
    "vaults": {"vaults"}, "open_vault": {"name", "opened"}, "create_vault": {"name", "created"},
    "state": {"path", "kind", "revision", "count"}, "mkdir": {"path", "created"},
    "move": {"source", "destination", "kind"}, "replace": {"path", "revision"},
    "trash": {"path", "trashed"},
    "logs": {"entries"}, "plugins": {"community", "core"}, "settings": {"file", "settings"},
    "plugin_state": {"plugin_id", "installed", "enabled", "version", "digest"},
    "plugin_set": {"plugin_id", "enabled"}, "plugin_remove": {"plugin_id", "removed"},
    "setting_state": {"setting_id", "value", "revision"}, "setting_write": {"setting_id", "written"},
}
REVISION_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
# Every plugin serves these; vault management ops are optional extras.
BASE_CAPABILITIES = frozenset({"health", "list", "read", "search", "create", "append", "embed",
                               "reserve", "receipt"})
VAULT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,99}\Z")
RECEIPT_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")


class BridgeError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class VaultEndpoint:
    vault_id: str
    app_name: str
    app_root: Path
    socket_path: Path
    credential_file: Path


def _endpoint(entry: VaultEndpoint) -> None:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", entry.vault_id):
        raise BridgeError("unavailable")
    if not entry.app_name or len(entry.app_name) > 128:
        raise BridgeError("unavailable")
    root = entry.app_root
    directory = entry.socket_path.parent
    shared = directory.parent
    if not root.is_absolute() or not directory.is_absolute() or not entry.credential_file.is_absolute():
        raise BridgeError("unavailable")
    if (os.path.normpath(root) != str(root) or directory.resolve(strict=True) != directory
            or shared.resolve(strict=True) != shared):
        raise BridgeError("unavailable")
    if (shared == root or shared.is_relative_to(root) or root.is_relative_to(shared)
            or directory == root or directory.is_relative_to(root) or root.is_relative_to(directory)):
        raise BridgeError("unavailable")
    if (directory.name != entry.vault_id or entry.socket_path != directory / f"{entry.vault_id}.sock"
            or entry.credential_file != directory / "credential"):
        raise BridgeError("unavailable")
    for location in (shared, directory):
        details = location.lstat()
        if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
            raise BridgeError("unavailable")


def _private_file(path: Path) -> str:
    details = path.lstat()
    if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) != 0o600 or details.st_size > 4096):
        raise BridgeError("unavailable")
    return path.read_text(encoding="utf-8").strip()


def _socket_address(entry: VaultEndpoint, token: str) -> str:
    digest = hashlib.sha256((str(entry.socket_path) + "\0" + token).encode()).hexdigest()
    return "\0obsidian-bridge-" + digest


def _safe_relative(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 1024 and
            not value.startswith("/") and "\\" not in value and "%" not in value and
            not any(ord(char) < 32 or ord(char) == 127 for char in value) and
            all(part and not part.startswith(".") for part in value.split("/")))


def _result(operation: str, value: Any, args: dict[str, Any]) -> bool:
    if not isinstance(value, dict) or set(value) != RESULT_KEYS[operation]:
        return False
    if operation == "reserve":
        return isinstance(value["receipt"], str) and RECEIPT_RE.fullmatch(value["receipt"]) is not None
    if operation in ("receipt", "create", "append"):
        if not isinstance(value["receipt"], str) or RECEIPT_RE.fullmatch(value["receipt"]) is None:
            return False
        if operation != "receipt" and value["receipt"] != args["receipt"]:
            return False
        if operation == "receipt" and value["receipt"] != args["receipt"]:
            return False
        if value["status"] not in ("pending", "committed", "indeterminate", "failed"):
            return False
        if value["status"] == "committed":
            original = args["mutation"]
            result = value["result"]
            return (isinstance(result, dict) and set(result) == {"path", "revision"} and
                    result["path"] == original["path"] and
                    isinstance(result["revision"], str) and
                    re.fullmatch(r"sha256:[0-9a-f]{64}", result["revision"]) is not None and
                    value["error"] is None)
        if value["status"] == "failed":
            return value["result"] is None and value["error"] in ERRORS
        return value["result"] is None and value["error"] is None
    if operation == "vaults":
        return (isinstance(value["vaults"], list) and len(value["vaults"]) <= 256 and
                all(isinstance(item, dict) and set(item) == {"name", "open"} and
                    isinstance(item["name"], str) and VAULT_NAME_RE.fullmatch(item["name"]) and
                    isinstance(item["open"], bool) for item in value["vaults"]))
    if operation == "state":
        return (value["path"] == args["path"] and value["kind"] in ("file", "folder", "absent")
                and isinstance(value["count"], int) and 0 <= value["count"] <= 5000
                and isinstance(value["revision"], str)
                and (value["revision"] == "absent") == (value["kind"] == "absent")
                and (value["kind"] == "absent" or REVISION_RE.fullmatch(value["revision"]) is not None))
    if operation == "logs":
        return (isinstance(value["entries"], list) and len(value["entries"]) <= 500 and
                all(isinstance(item, dict) and set(item) == {"time", "level", "message"}
                    and item["level"] in ("error", "warn", "info", "log")
                    and isinstance(item["time"], str) and len(item["time"]) <= 40
                    and isinstance(item["message"], str) and len(item["message"]) <= 1000
                    for item in value["entries"]))
    if operation == "plugins":
        return (isinstance(value["community"], list) and len(value["community"]) <= 500
                and all(isinstance(item, dict) and set(item) == {"id", "name", "version", "enabled"}
                        and all(isinstance(item[key], str) and len(item[key]) <= 200
                                for key in ("id", "name", "version"))
                        and isinstance(item["enabled"], bool) for item in value["community"])
                and isinstance(value["core"], list) and len(value["core"]) <= 200
                and all(isinstance(item, dict) and set(item) == {"id", "enabled"}
                        and isinstance(item["id"], str) and len(item["id"]) <= 200
                        and isinstance(item["enabled"], bool) for item in value["core"]))
    if operation == "settings":
        return value["file"] == args["file"] and isinstance(value["settings"], (dict, list, str, int, float, bool))
    if operation == "plugin_state":
        return (value["plugin_id"] == args["plugin_id"] and isinstance(value["installed"], bool)
                and isinstance(value["enabled"], bool) and isinstance(value["version"], str)
                and len(value["version"]) <= 64 and isinstance(value["digest"], str)
                and (re.fullmatch(r"[0-9a-f]{64}", value["digest"]) is not None) == value["installed"])
    if operation == "plugin_set":
        return value["plugin_id"] == args["plugin_id"] and value["enabled"] is args["enabled"]
    if operation == "plugin_remove":
        return value["plugin_id"] == args["plugin_id"] and value["removed"] is True
    if operation == "setting_state":
        return (value["setting_id"] == args["setting_id"]
                and (value["value"] is None or isinstance(value["value"], (str, int, float, bool)))
                and isinstance(value["revision"], str) and REVISION_RE.fullmatch(value["revision"]) is not None)
    if operation == "setting_write":
        return value["setting_id"] == args["setting_id"] and value["written"] is True
    if operation == "mkdir":
        return value["path"] == args["path"] and value["created"] is True
    if operation == "move":
        return (value["source"] == args["source"] and value["destination"] == args["destination"]
                and value["kind"] in ("file", "folder"))
    if operation == "replace":
        return (value["path"] == args["path"] and isinstance(value["revision"], str)
                and REVISION_RE.fullmatch(value["revision"]) is not None)
    if operation == "trash":
        return value["path"] == args["path"] and value["trashed"] is True
    if operation in ("open_vault", "create_vault"):
        flag = "opened" if operation == "open_vault" else "created"
        return value["name"] == args["name"] and value[flag] is True
    if operation == "health":
        return (value["protocol"] == VERSION and isinstance(value["vault"], str) and
                isinstance(value["app_name"], str) and isinstance(value["app_root"], str) and
                isinstance(value["capabilities"], list) and
                len(set(value["capabilities"])) == len(value["capabilities"]) and
                BASE_CAPABILITIES <= set(value["capabilities"]) <= set(RESULT_KEYS))
    if operation == "list":
        return (isinstance(value["entries"], list) and len(value["entries"]) <= 1000 and
                all(isinstance(item, dict) and set(item) == {"path", "kind"} and
                    _safe_relative(item["path"]) and item["kind"] in ("file", "folder")
                    for item in value["entries"]))
    if operation == "search":
        return (isinstance(value["matches"], list) and len(value["matches"]) <= 50 and
                all(isinstance(item, dict) and set(item) == {"path", "offset", "revision"} and
                    _safe_relative(item["path"]) and isinstance(item["offset"], int) and
                    item["offset"] >= 0 and isinstance(item["revision"], str) and
                    re.fullmatch(r"sha256:[0-9a-f]{64}", item["revision"])
                    for item in value["matches"]))
    if not _safe_relative(value["path"]):
        return False
    if operation == "embed":
        if value["mime"] not in ("image/png", "image/jpeg", "image/webp", "image/gif") or not isinstance(value["data"], str):
            return False
        try:
            return len(base64.b64decode(value["data"], validate=True)) <= 512 * 1024
        except ValueError:
            return False
    if value["path"] != args["path"] or not isinstance(value["revision"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["revision"]):
        return False
    return operation != "read" or (isinstance(value["content"], str) and len(value["content"].encode()) <= 256 * 1024)


def _unambiguous(registry: Mapping[str, VaultEndpoint]) -> bool:
    if any(key != entry.vault_id for key, entry in registry.items()):
        return False
    roots = [entry.app_root for entry in registry.values()]
    return not any(left == right or left.is_relative_to(right) or right.is_relative_to(left)
                   for index, left in enumerate(roots) for right in roots[index + 1:])


class VaultDirectory:
    """Vaults whose bridge plugin registered itself under the runtime directory."""

    MAX_VAULTS = 64

    def __init__(self, runtime: Path, ttl: float = 2.0, parent: Path | None = None):
        if (not runtime.is_absolute() or not 0 < ttl <= 60
                or (parent is not None and not parent.is_absolute())):
            raise ValueError("Invalid vault directory")
        self._runtime = runtime
        self._parent = parent
        self._ttl = ttl
        self._cached: dict[str, VaultEndpoint] = {}
        self._scanned = float("-inf")

    def endpoints(self) -> dict[str, VaultEndpoint]:
        if time.monotonic() - self._scanned >= self._ttl:
            self._cached = self._scan()
            self._scanned = time.monotonic()
        return dict(self._cached)

    def _scan(self) -> dict[str, VaultEndpoint]:
        found: dict[str, VaultEndpoint] = {}
        try:
            details = self._runtime.lstat()
            if (not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid()
                    or stat.S_IMODE(details.st_mode) != 0o700):
                return found
            children = sorted(self._runtime.iterdir())
        except OSError:
            return found
        for directory in children[:self.MAX_VAULTS * 2]:
            if len(found) >= self.MAX_VAULTS:
                break
            vault_id = directory.name
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", vault_id):
                continue
            try:
                document = json.loads(_private_file(directory / "endpoint.json"))
                if (not isinstance(document, dict)
                        or set(document) != {"version", "vault_id", "app_name", "app_root"}
                        or document["version"] != VERSION or document["vault_id"] != vault_id
                        or not isinstance(document["app_name"], str)
                        or not VAULT_NAME_RE.fullmatch(document["app_name"])
                        or not isinstance(document["app_root"], str)
                        or (self._parent is not None
                            and document["app_root"] != str(self._parent / document["app_name"]))):
                    continue
                entry = VaultEndpoint(vault_id, document["app_name"], Path(document["app_root"]),
                                      directory / f"{vault_id}.sock", directory / "credential")
                _endpoint(entry)
            except (BridgeError, OSError, ValueError, TypeError, UnicodeError):
                continue
            found[vault_id] = entry
        return found if _unambiguous(found) else {}


class BridgeClient:
    """Fixed operations only; caller never supplies a socket, token or verb."""

    def __init__(self, registry: Mapping[str, VaultEndpoint] | VaultDirectory, timeout: float = 3.0):
        if not 0 < timeout <= 30:
            raise ValueError("Invalid bridge configuration")
        if isinstance(registry, VaultDirectory):
            self._directory: VaultDirectory | None = registry
            self._static: dict[str, VaultEndpoint] = {}
        else:
            if not registry or any(key != entry.vault_id for key, entry in registry.items()):
                raise ValueError("Invalid bridge registry")
            if not _unambiguous(dict(registry)):
                raise ValueError("Ambiguous bridge registry")
            self._directory = None
            self._static = dict(registry)
        self._timeout = timeout

    @property
    def _registry(self) -> dict[str, VaultEndpoint]:
        return self._directory.endpoints() if self._directory is not None else self._static

    def vault_ids(self) -> list[str]:
        return sorted(self._registry)

    def vault_name(self, vault: str) -> str | None:
        entry = self._registry.get(vault)
        return entry.app_name if entry else None

    def _transport(self, vault: str, operation: str, args: dict[str, Any], deadline: float | None = None) -> dict[str, Any]:
        if deadline is None:
            deadline = time.monotonic() + self._timeout
        def remaining() -> float:
            left = deadline - time.monotonic()
            if left <= 0:
                raise BridgeError("unavailable")
            return left
        if operation not in RESULT_KEYS:
            raise BridgeError("unknown_operation")
        if not isinstance(vault, str):
            raise BridgeError("unknown_vault")
        entry = self._registry.get(vault)
        if entry is None:
            raise BridgeError("unknown_vault")
        try:
            _endpoint(entry)
            token = _private_file(entry.credential_file)
            if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
                raise BridgeError("unavailable")
            payload = json.dumps({"v": VERSION, "vault": vault, "token": token,
                                  "op": operation, "args": args}, ensure_ascii=False).encode() + b"\n"
            if len(payload) > MAX_REQUEST:
                raise BridgeError("limit_exceeded")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(remaining())
                connection.connect(_socket_address(entry, token))
                connection.settimeout(remaining())
                connection.sendall(payload)
                response = bytearray()
                while True:
                    connection.settimeout(remaining())
                    chunk = connection.recv(min(65536, MAX_RESPONSE + 1 - len(response)))
                    remaining()
                    if not chunk:
                        raise BridgeError("unavailable")
                    response.extend(chunk)
                    if len(response) > MAX_RESPONSE:
                        raise BridgeError("limit_exceeded")
                    if b"\n" in chunk:
                        break
            if response.count(b"\n") != 1 or not response.endswith(b"\n"):
                raise BridgeError("unavailable")
            reply = json.loads(response[:-1])
            if not isinstance(reply, dict) or set(reply) not in ({"v", "vault", "ok", "result"}, {"v", "vault", "ok", "error"}) or reply.get("v") != VERSION or reply.get("vault") != vault:
                raise BridgeError("unavailable")
            if reply["ok"] is True and _result(operation, reply.get("result"), args):
                return reply["result"]
            if reply["ok"] is False and reply.get("error") in ERRORS:
                raise BridgeError(reply["error"])
            raise BridgeError("unavailable")
        except BridgeError:
            raise
        except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
            raise BridgeError("unavailable") from None

    def _call(self, vault: str, operation: str, args: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout
        if operation != "health":
            self.ready(vault, deadline=deadline)
        return self._transport(vault, operation, args, deadline)

    def ready(self, vault: str, *, deadline: float | None = None) -> dict[str, Any]:
        result = self._transport(vault, "health", {}, deadline)
        entry = self._registry[vault]
        if result.get("protocol") != VERSION or result.get("vault") != vault or result.get("app_name") != entry.app_name or result.get("app_root") != str(entry.app_root):
            raise BridgeError("unavailable")
        return result

    def list_entries(self, vault: str, prefix: str = "", limit: int = 1000) -> dict[str, Any]:
        return self._call(vault, "list", {"prefix": prefix, "limit": limit})

    def read_note(self, vault: str, path: str) -> dict[str, Any]:
        return self._call(vault, "read", {"path": path})

    def search(self, vault: str, query: str, limit: int = 50) -> dict[str, Any]:
        return self._call(vault, "search", {"query": query, "limit": limit})

    def create_note(self, vault: str, path: str, content: str, *, client_id: str = "bridge") -> dict[str, Any]:
        return self._mutation(vault, "create", {"path": path, "content": content}, client_id)

    def append_note(self, vault: str, path: str, content: str, expected_revision: str,
                    *, client_id: str = "bridge") -> dict[str, Any]:
        return self._mutation(vault, "append", {"path": path, "content": content,
                                                 "expected_revision": expected_revision}, client_id)

    def _mutation(self, vault: str, operation: str, mutation: dict[str, Any], client_id: str) -> dict[str, Any]:
        reservation = self._call(vault, "reserve", {"client": client_id, "operation": operation,
                                                    "mutation": mutation})
        receipt = reservation["receipt"]
        try:
            result = self._call(vault, operation, {"client": client_id, "receipt": receipt,
                                                   "mutation": mutation})
        except BridgeError as error:
            if error.code not in {"unavailable", "internal_error"}:
                raise
            return {"receipt": receipt, "status": "pending"}
        if result["status"] == "failed":
            raise BridgeError(result["error"])
        if result["status"] == "committed":
            return {**result["result"], "receipt": receipt, "status": "committed"}
        return {"receipt": receipt, "status": result["status"]}

    def mutation_receipt(self, vault: str, operation: str, mutation: dict[str, Any],
                         receipt: str, client_id: str) -> dict[str, Any]:
        if not isinstance(receipt, str) or RECEIPT_RE.fullmatch(receipt) is None:
            raise BridgeError("invalid_request")
        try:
            result = self._call(vault, "receipt", {"client": client_id, "operation": operation,
                                                   "mutation": mutation, "receipt": receipt})
        except BridgeError as error:
            if error.code != "unavailable":
                raise
            return {"receipt": receipt, "status": "indeterminate"}
        if result["status"] == "failed":
            return {"receipt": receipt, "status": "failed", "error": result["error"]}
        if result["status"] == "committed":
            return {**result["result"], "receipt": receipt, "status": "committed"}
        return {"receipt": receipt, "status": result["status"]}

    def app_vaults(self, vault: str) -> dict[str, Any]:
        """Every vault the desktop app knows about, asked through one running vault."""
        return self._call(vault, "vaults", {})

    def open_vault(self, vault: str, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not VAULT_NAME_RE.fullmatch(name):
            raise BridgeError("invalid_request")
        return self._call(vault, "open_vault", {"name": name})

    def create_vault(self, vault: str, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not VAULT_NAME_RE.fullmatch(name):
            raise BridgeError("invalid_request")
        return self._call(vault, "create_vault", {"name": name})

    def logs(self, vault: str, level: str = "all", limit: int = 100) -> dict[str, Any]:
        return self._call(vault, "logs", {"level": level, "limit": limit})

    def plugins(self, vault: str) -> dict[str, Any]:
        return self._call(vault, "plugins", {})

    def settings(self, vault: str, file: str) -> dict[str, Any]:
        return self._call(vault, "settings", {"file": file})

    def plugin_state(self, vault: str, plugin_id: str) -> dict[str, Any]:
        return self._call(vault, "plugin_state", {"plugin_id": plugin_id})

    def set_plugin(self, vault: str, plugin_id: str, enabled: bool, expected: str) -> dict[str, Any]:
        return self._call(vault, "plugin_set", {"plugin_id": plugin_id, "enabled": enabled,
                                                "expected_digest": expected})

    def remove_plugin(self, vault: str, plugin_id: str, expected: str) -> dict[str, Any]:
        return self._call(vault, "plugin_remove", {"plugin_id": plugin_id, "expected_digest": expected})

    def setting_state(self, vault: str, setting_id: str) -> dict[str, Any]:
        return self._call(vault, "setting_state", {"setting_id": setting_id})

    def write_setting(self, vault: str, setting_id: str, value, expected_revision: str) -> dict[str, Any]:
        return self._call(vault, "setting_write", {"setting_id": setting_id, "value": value,
                                                   "expected_revision": expected_revision})

    def path_state(self, vault: str, path: str) -> dict[str, Any]:
        return self._call(vault, "state", {"path": path})

    def make_folder(self, vault: str, path: str) -> dict[str, Any]:
        return self._call(vault, "mkdir", {"path": path})

    def move_path(self, vault: str, source: str, destination: str) -> dict[str, Any]:
        return self._call(vault, "move", {"source": source, "destination": destination})

    def replace_note(self, vault: str, path: str, content: str, expected_revision: str) -> dict[str, Any]:
        return self._call(vault, "replace", {"path": path, "content": content,
                                             "expected_revision": expected_revision})

    def trash_path(self, vault: str, path: str, expected_revision: str) -> dict[str, Any]:
        return self._call(vault, "trash", {"path": path, "expected_revision": expected_revision})

    def resolve_embed(self, vault: str, source: str, target: str) -> dict[str, Any]:
        return self._call(vault, "embed", {"source": source, "target": target})
