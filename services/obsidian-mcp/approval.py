"""Transport-independent, exact-operation approval for one enrolled Obsidian vault.

The gateway must construct AgentContext and OwnerContext from separate trusted
authentication paths. It must never deserialize either from a client request.
The adapter owns path resolution, complete affected-set discovery, collision
checks for create-only calls, and atomic expected-revision checks for append.
It must expose fixed typed verbs only and must not execute shell or app commands
supplied by a caller. A vault lock serializes this coordinator's writes; Sync
and human editors remain outside that lock. The adapter must verify results
after mutation; an uncertain post-write result must never be retried blindly.
Read-only calls must use the side-effect-free read method. For plugin installs
and updates, preview_package hashes actual bytes without pinning; attest_package
creates a short-lived execution pin. Execute must atomically verify the pin
and run only those attested bytes. release_package must be idempotent and
discard the pin even after failed execution.
Guarded moves, renames and restores must atomically reject occupied targets.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol


_SAFE = {
    "read_note": {"path"},
    "list_notes": {"folder"},
    "list_folders": {"folder"},
    "search": {"query"},
    "read_media": {"path"},
    "plugin_catalog": {"query"},
    "diagnostics": {"kind"},
    "sync_status": set(),
    "create_note": {"path", "content", "expected_absent"},
    "create_folder": {"path", "expected_absent"},
    "create_attachment": {"path", "upload_ref", "digest", "size", "mime", "expected_absent"},
    "append_note": {"path", "content", "expected_revision"},
    "move": {"source", "destination"},
}
_GUARDED = {
    "replace_note": {"path", "content"},
    "remove_property": {"path", "property"},
    "rename_note": {"source", "destination"},
    "move_note": {"source", "destination"},
    "rename_folder": {"source", "destination"},
    "move_folder": {"source", "destination"},
    "trash_note": {"path"},
    "trash_folder": {"path"},
    "restore_note": {"trash_id", "destination"},
    "plugin_install": {"plugin_id", "version", "source", "digest"},
    "plugin_enable": {"plugin_id"},
    "plugin_disable": {"plugin_id"},
    "plugin_update": {"plugin_id", "version", "source", "digest"},
    "plugin_uninstall": {"plugin_id"},
    "setting_update": {"setting_id", "value"},
}
_OPTIONAL = {"list_notes": {"folder"}, "list_folders": {"folder"}, "plugin_catalog": {"query"}}
_READ_ONLY = frozenset({"read_note", "list_notes", "list_folders", "search", "read_media",
                        "plugin_catalog", "diagnostics", "sync_status"})
_PATH_FIELDS = frozenset({"path", "folder", "source", "destination"})
_PLUGIN_VERBS = frozenset(v for v in _GUARDED if v.startswith("plugin_"))
_PACKAGE_VERBS = frozenset({"plugin_install", "plugin_update"})
_DESTINATION_VERBS = frozenset({"rename_note", "move_note", "rename_folder", "move_folder", "restore_note"})
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_HEX = re.compile(r"[a-f0-9]{64}\Z")
_MAX_REQUEST = 8192
_MAX_TEXT = 4096
_MAX_PREVIEW = 32768  # the owner always sees the whole change; previews never truncate
_MAX_STATES = 64
_MAX_STATE_BYTES = 16384


class ApprovalError(Exception):
    """A bounded public error; adapter exceptions are never included."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AdapterConflict(Exception):
    """Adapter proved an expected-absence/revision conflict before mutation."""


@dataclass(frozen=True)
class AgentContext:
    client_id: str
    grants: Mapping[str, frozenset[str]]


@dataclass(frozen=True)
class OwnerContext:
    owner_id: str
    vault_ids: frozenset[str]


@dataclass(frozen=True)
class SettingRule:
    value_type: type
    allowed_values: tuple[object, ...]
    impact: str


@dataclass(frozen=True)
class VaultEnrollment:
    owner_ids: frozenset[str]
    setting_rules: Mapping[str, SettingRule]


@dataclass(frozen=True)
class Operation:
    vault: str
    verb: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class StateEntry:
    kind: str
    key: str
    revision: str
    source: str = ""
    version: str = ""
    digest: str = ""


@dataclass(frozen=True)
class Snapshot:
    entries: tuple[StateEntry, ...]
    complete: bool
    affected_count: int


@dataclass(frozen=True)
class Preview:
    snapshot: Snapshot
    summary: str


@dataclass(frozen=True)
class PackageAttestation:
    source: str
    version: str
    digest: str
    pin: str


@dataclass(frozen=True)
class PackagePreview:
    source: str
    version: str
    digest: str


@dataclass(frozen=True)
class DiagnosticCount:
    code: str
    count: int


@dataclass(frozen=True)
class DiagnosticResult:
    kind: str
    status: str
    records: tuple[DiagnosticCount, ...]


@dataclass(frozen=True)
class PendingView:
    id: str
    digest: str
    expires_at: float
    preview: str
    action: Operation
    client_id: str
    state: tuple[StateEntry, ...]


@dataclass
class _Pending:
    client_id: str
    vault: str
    action_digest: str
    state: tuple[tuple[str, str, str, str, str, str], ...]
    expires_at: float
    action_bytes: bytes
    preview: str
    approved: bool = False
    approved_by: str | None = None


class TypedAdapter(Protocol):
    def preview(self, action: Operation) -> Preview:
        """Read only; include every affected item, destination and package source."""

    def observe(self, action: Operation, expected: tuple[StateEntry, ...]) -> Snapshot:
        """Read the complete affected set immediately before mutation."""

    def read(self, action: Operation) -> object:
        """Side-effect-free bounded read. Never write app, vault or plugin state."""

    def preview_package(self, action: Operation) -> PackagePreview:
        """Hash actual package bytes without retaining an artifact or pin."""

    def attest_package(self, action: Operation) -> PackageAttestation:
        """Hash actual bytes and pin an immutable artifact only for immediate execution."""

    def release_package(self, pin: str) -> None:
        """Idempotently release a pin, including after failed execution."""

    def execute(self, action: Operation, *, package: PackageAttestation | None = None) -> object:
        """Fixed mutation. Atomically check absence/revision and package pin before effects."""


def _diagnostics(result: object, kind: str) -> DiagnosticResult:
    if (type(result) is not DiagnosticResult or type(result.kind) is not str or result.kind != kind
            or type(result.status) is not str
            or result.status not in {"ok", "degraded", "unavailable"}
            or type(result.records) is not tuple or len(result.records) > 32):
        raise ApprovalError("invalid_diagnostics")
    codes = {"app_error", "plugin_error", "sync_error", "network_error", "warning"}
    if any(type(row) is not DiagnosticCount or type(row.code) is not str or row.code not in codes
           or type(row.count) is not int or not 0 <= row.count <= 100000
           for row in result.records):
        raise ApprovalError("invalid_diagnostics")
    return result


def _valid_text(value: object, maximum: int = 512, *, empty: bool = False) -> bool:
    return (type(value) is str and (empty or len(value) > 0) and len(value) <= maximum
            and unicodedata.normalize("NFC", value) == value
            and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
            and not any(ord(char) < 32 and char not in "\n\t" for char in value))


def _canonical_action(action: Operation) -> tuple[Operation, bytes]:
    if type(action) is not Operation or not _valid_text(action.vault, 128) or not _ID.fullmatch(action.vault):
        raise ApprovalError("invalid_action")
    if type(action.verb) is not str:
        raise ApprovalError("invalid_action")
    schema = _SAFE.get(action.verb, _GUARDED.get(action.verb))
    if schema is None or type(action.arguments) is not dict:
        raise ApprovalError("invalid_action")
    args = action.arguments
    if set(args) - schema or schema - _OPTIONAL.get(action.verb, set()) - set(args):
        raise ApprovalError("invalid_action")
    for key, value in args.items():
        if key in {"expected_absent"}:
            if value is not True:
                raise ApprovalError("invalid_action")
        elif key == "size":
            if type(value) is not int or not 0 <= value <= 16 * 1024 * 1024:
                raise ApprovalError("invalid_action")
        elif key == "value":
            if type(value) not in (str, int, bool) or (type(value) is str and not _valid_text(value, _MAX_TEXT, empty=True)):
                raise ApprovalError("invalid_action")
        elif not _valid_text(value, _MAX_TEXT if key == "content" else 512, empty=key == "content"):
            raise ApprovalError("invalid_action")
        if key in _PATH_FIELDS:
            if (value.startswith("/") or "\\" in value or any(ord(char) < 32 for char in value)
                    or any(part in ("", ".", "..", ".obsidian") for part in value.split("/"))):
                raise ApprovalError("invalid_action")
        if key == "digest" and not _HEX.fullmatch(value):
            raise ApprovalError("invalid_action")
        if key == "plugin_id" and not _ID.fullmatch(value):
            raise ApprovalError("invalid_action")
    if action.verb == "diagnostics" and args["kind"] not in {"console", "errors", "sync"}:
        raise ApprovalError("invalid_action")
    normalized = Operation(action.vault, action.verb, dict(args))
    try:
        encoded = json.dumps([normalized.vault, normalized.verb, normalized.arguments],
                             sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        raise ApprovalError("invalid_action") from None
    if len(encoded) > _MAX_REQUEST:
        raise ApprovalError("invalid_action")
    return normalized, encoded


def _canonical_state(snapshot: Snapshot, action: Operation) -> tuple[tuple[str, str, str, str, str, str], ...]:
    if (type(snapshot) is not Snapshot or snapshot.complete is not True
            or type(snapshot.affected_count) is not int
            or type(snapshot.entries) is not tuple
            or not 1 <= len(snapshot.entries) <= _MAX_STATES
            or snapshot.affected_count != len(snapshot.entries)):
        raise ApprovalError("incomplete_preview")
    entries = []
    seen = set()
    for entry in snapshot.entries:
        if type(entry) is not StateEntry:
            raise ApprovalError("incomplete_preview")
        fields = (entry.kind, entry.key, entry.revision, entry.source, entry.version, entry.digest)
        if (not all(type(value) is str for value in fields)
                or entry.kind not in {"note", "folder", "attachment", "destination", "link", "trash", "setting", "plugin", "package"}
                or not all(_valid_text(value, 512) for value in fields[:3])
                or not all(type(value) is str and (not value or _valid_text(value)) for value in fields[3:])
                or (entry.kind, entry.key) in seen):
            raise ApprovalError("incomplete_preview")
        if entry.kind == "plugin":
            if entry.revision == "absent":
                if any(fields[3:]):
                    raise ApprovalError("incomplete_preview")
            elif not all(_valid_text(value) for value in fields[3:]) or not _HEX.fullmatch(entry.digest):
                raise ApprovalError("incomplete_preview")
        if entry.kind == "package" and (entry.revision != "attested"
                                        or not all(_valid_text(value) for value in fields[3:])
                                        or not _HEX.fullmatch(entry.digest)):
            raise ApprovalError("incomplete_preview")
        seen.add((entry.kind, entry.key))
        entries.append(fields)
    if len(json.dumps(entries, ensure_ascii=False).encode("utf-8")) > _MAX_STATE_BYTES:
        raise ApprovalError("incomplete_preview")
    primary = {
        "replace_note": ("note", "path"), "remove_property": ("note", "path"),
        "trash_note": ("note", "path"), "trash_folder": ("folder", "path"),
        "rename_note": ("note", "source"),
        "move_note": ("note", "source"), "rename_folder": ("folder", "source"),
        "move_folder": ("folder", "source"), "restore_note": ("trash", "trash_id"),
    }.get(action.verb)
    if primary and not any(entry[0] == primary[0] and entry[1] == action.arguments[primary[1]]
                           for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb in _PLUGIN_VERBS and not any(
            entry[0] == "plugin" and entry[1] == action.arguments["plugin_id"] for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb == "plugin_install" and not any(
            entry[0] == "plugin" and entry[1] == action.arguments["plugin_id"] and entry[2] == "absent"
            for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb == "plugin_update" and not any(
            entry[0] == "plugin" and entry[1] == action.arguments["plugin_id"] and entry[2] != "absent"
            for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb in _PACKAGE_VERBS and not any(
            entry[0] == "package" and entry[1] == action.arguments["plugin_id"]
            and entry[3:] == (action.arguments["source"], action.arguments["version"], action.arguments["digest"])
            for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb == "setting_update" and not any(
            entry[0] == "setting" and entry[1] == action.arguments["setting_id"] for entry in entries):
        raise ApprovalError("incomplete_preview")
    if action.verb in _DESTINATION_VERBS:
        destination = next((entry for entry in entries if entry[0] == "destination"
                            and entry[1] == action.arguments["destination"]), None)
        if destination is None:
            raise ApprovalError("incomplete_preview")
        if destination[2] != "absent" or any(destination[3:]):
            raise ApprovalError("stale_state")
    return tuple(sorted(entries))


def _action_digest(client_id: str, encoded: bytes,
                   state: tuple[tuple[str, str, str, str, str, str], ...]) -> str:
    payload = json.dumps([client_id, json.loads(encoded), state], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_registry(registry: Mapping[str, VaultEnrollment]) -> dict[str, VaultEnrollment]:
    registry = dict(registry)
    if any(type(vault) is not str or not _ID.fullmatch(vault)
           or type(enrollment) is not VaultEnrollment
           or type(enrollment.owner_ids) is not frozenset
           or not enrollment.owner_ids
           or type(enrollment.setting_rules) is not dict
           or any(type(owner) is not str or not _ID.fullmatch(owner)
                  for owner in enrollment.owner_ids)
           or any(type(setting) is not str or not _ID.fullmatch(setting)
                  or type(rule) is not SettingRule
                  or rule.value_type not in (str, int, bool)
                  or type(rule.allowed_values) is not tuple
                  or not 1 <= len(rule.allowed_values) <= 16
                  or any(type(value) is not rule.value_type
                         or (type(value) is str and not _valid_text(value, _MAX_TEXT, empty=True))
                         for value in rule.allowed_values)
                  or type(rule.impact) is not str
                  or rule.impact not in {"security", "sync", "plugin", "other"}
                  for setting, rule in enrollment.setting_rules.items())
           for vault, enrollment in registry.items()):
        raise ValueError("invalid vault enrollment")
    return {vault: VaultEnrollment(entry.owner_ids, dict(entry.setting_rules))
            for vault, entry in registry.items()}


class ApprovalCoordinator:
    """In-memory ledger. A new process starts with no pending grants."""

    def __init__(self, adapter: TypedAdapter, registry: Mapping[str, VaultEnrollment], *,
                 clock: Callable[[], float] = time.monotonic, ttl: int = 120,
                 max_pending: int = 128):
        # Vaults register at runtime, so an empty registry at startup is valid.
        if not 1 <= ttl <= 600 or not 1 <= max_pending <= 1024:
            raise ValueError("invalid coordinator configuration")
        self._adapter = adapter
        # A live mapping (vaults that register at runtime) is re-read on each use.
        self._source = registry
        self._static = None if not isinstance(registry, dict) else _validated_registry(registry)
        _validated_registry(registry)
        self._clock = clock
        self._ttl = ttl
        self._max_pending = max_pending
        self._vault_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()
        self._active = threading.local()
        self._ledger_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}


    @property
    def _registry(self) -> dict[str, VaultEnrollment]:
        return self._static if self._static is not None else _validated_registry(self._source)

    def _lock_for(self, vault: str) -> threading.Lock:
        with self._locks_lock:
            return self._vault_locks.setdefault(vault, threading.Lock())

    @contextmanager
    def _vault_guard(self, vault: str):
        if getattr(self._active, "vault", None) is not None:
            raise ApprovalError("reentrant_call")
        with self._lock_for(vault):
            self._active.vault = vault
            try:
                yield
            finally:
                self._active.vault = None

    def _agent_action(self, agent: AgentContext, action: Operation) -> tuple[Operation, bytes]:
        normalized, encoded = _canonical_action(action)
        if normalized.vault not in self._registry:
            raise ApprovalError("unknown_vault")
        if (type(agent) is not AgentContext or not _valid_text(agent.client_id, 128)
                or not _ID.fullmatch(agent.client_id)
                or type(agent.grants) is not dict
                or normalized.verb not in agent.grants.get(normalized.vault, frozenset())):
            raise ApprovalError("not_allowed")
        if normalized.verb == "setting_update":
            rule = self._registry[normalized.vault].setting_rules.get(normalized.arguments["setting_id"])
            value = normalized.arguments["value"]
            if rule is None or type(value) is not rule.value_type or value not in rule.allowed_values:
                raise ApprovalError("not_allowed")
        return normalized, encoded

    def _attested_package(self, action: Operation) -> PackageAttestation:
        attestation = self._adapter.attest_package(action)
        if (type(attestation) is not PackageAttestation
                or attestation.source != action.arguments["source"]
                or attestation.version != action.arguments["version"]
                or type(attestation.digest) is not str
                or not _HEX.fullmatch(attestation.digest)
                or not secrets.compare_digest(attestation.digest, action.arguments["digest"])
                or type(attestation.pin) is not str
                or not _ID.fullmatch(attestation.pin)):
            if type(attestation) is PackageAttestation and type(attestation.pin) is str:
                self._adapter.release_package(attestation.pin)
            raise ApprovalError("stale_state")
        return attestation

    def _preview_package(self, action: Operation) -> None:
        preview = self._adapter.preview_package(action)
        if (type(preview) is not PackagePreview
                or preview.source != action.arguments["source"]
                or preview.version != action.arguments["version"]
                or type(preview.digest) is not str
                or not _HEX.fullmatch(preview.digest)
                or not secrets.compare_digest(preview.digest, action.arguments["digest"])):
            raise ApprovalError("stale_state")

    def execute(self, agent: AgentContext, action: Operation) -> object:
        normalized, _ = self._agent_action(agent, action)
        if normalized.verb not in _SAFE:
            raise ApprovalError("approval_required")
        with self._vault_guard(normalized.vault):
            if normalized.verb in _READ_ONLY:
                try:
                    result = self._adapter.read(normalized)
                except Exception:
                    raise ApprovalError("adapter_failure") from None
                return _diagnostics(result, normalized.arguments["kind"]) if normalized.verb == "diagnostics" else result
            try:
                return self._adapter.execute(normalized)
            except AdapterConflict:
                raise ApprovalError("stale_state") from None
            except Exception:
                raise ApprovalError("uncertain_result") from None

    def prepare(self, agent: AgentContext, action: Operation) -> PendingView:
        normalized, encoded = self._agent_action(agent, action)
        if normalized.verb not in _GUARDED:
            raise ApprovalError("invalid_action")
        with self._vault_guard(normalized.vault):
            try:
                preview = self._adapter.preview(normalized)
            except Exception:
                raise ApprovalError("adapter_failure") from None
            if type(preview) is not Preview or not _valid_text(preview.summary, _MAX_PREVIEW):
                raise ApprovalError("incomplete_preview")
            state = _canonical_state(preview.snapshot, normalized)
            if normalized.verb in _PACKAGE_VERBS:
                try:
                    self._preview_package(normalized)
                except ApprovalError:
                    raise ApprovalError("incomplete_preview") from None
                except Exception:
                    raise ApprovalError("adapter_failure") from None
            digest = _action_digest(agent.client_id, encoded, state)
            expires_at = self._clock() + self._ttl
            with self._ledger_lock:
                self._pending = {key: value for key, value in self._pending.items()
                                 if value.expires_at > self._clock()}
                if len(self._pending) >= self._max_pending:
                    raise ApprovalError("capacity")
                pending_id = secrets.token_urlsafe(32)
                if pending_id in self._pending:
                    raise ApprovalError("capacity")
                self._pending[pending_id] = _Pending(agent.client_id, normalized.vault, digest, state,
                                                     expires_at, encoded, preview.summary)
        return PendingView(pending_id, digest, expires_at, preview.summary, normalized,
                           agent.client_id, tuple(StateEntry(*entry) for entry in state))

    def _owner_record(self, owner: OwnerContext, pending_id: str, digest: str) -> _Pending:
        if (type(owner) is not OwnerContext or type(owner.owner_id) is not str
                or type(owner.vault_ids) is not frozenset):
            raise ApprovalError("invalid_context")
        if type(pending_id) is not str or len(pending_id) > 128 or type(digest) is not str or not _HEX.fullmatch(digest):
            raise ApprovalError("mismatch")
        record = self._pending.get(pending_id)
        if record is None:
            raise ApprovalError("unknown_pending")
        if record.expires_at <= self._clock():
            del self._pending[pending_id]
            raise ApprovalError("expired")
        if (owner.owner_id not in self._registry[record.vault].owner_ids
                or record.vault not in owner.vault_ids):
            raise ApprovalError("not_allowed")
        if not secrets.compare_digest(record.action_digest, digest):
            raise ApprovalError("mismatch")
        return record

    def owner_view(self, owner: OwnerContext, pending_id: str) -> PendingView:
        """Return bounded exact action and preview to an independently verified owner."""
        if (type(owner) is not OwnerContext or type(owner.owner_id) is not str
                or type(owner.vault_ids) is not frozenset
                or type(pending_id) is not str or len(pending_id) > 128):
            raise ApprovalError("invalid_context")
        with self._ledger_lock:
            record = self._pending.get(pending_id)
            if record is None:
                raise ApprovalError("unknown_pending")
            if record.expires_at <= self._clock():
                del self._pending[pending_id]
                raise ApprovalError("expired")
            if (owner.owner_id not in self._registry[record.vault].owner_ids
                    or record.vault not in owner.vault_ids):
                raise ApprovalError("not_allowed")
            vault, verb, arguments = json.loads(record.action_bytes)
            return PendingView(pending_id, record.action_digest, record.expires_at,
                               record.preview, Operation(vault, verb, arguments),
                               record.client_id, tuple(StateEntry(*entry) for entry in record.state))

    def approve(self, owner: OwnerContext, pending_id: str, digest: str) -> None:
        with self._ledger_lock:
            record = self._owner_record(owner, pending_id, digest)
            if record.approved:
                raise ApprovalError("approval_required")
            record.approved = True
            record.approved_by = owner.owner_id

    def reject(self, owner: OwnerContext, pending_id: str, digest: str) -> None:
        with self._ledger_lock:
            self._owner_record(owner, pending_id, digest)
            del self._pending[pending_id]

    def commit(self, agent: AgentContext, pending_id: str, action: Operation) -> object:
        normalized, encoded = self._agent_action(agent, action)
        if normalized.verb not in _GUARDED:
            raise ApprovalError("invalid_action")
        if type(pending_id) is not str or len(pending_id) > 128:
            raise ApprovalError("unknown_pending")
        with self._vault_guard(normalized.vault):
            with self._ledger_lock:
                record = self._pending.get(pending_id)
                if record is None:
                    raise ApprovalError("unknown_pending")
                if record.expires_at <= self._clock():
                    del self._pending[pending_id]
                    raise ApprovalError("expired")
                if record.client_id != agent.client_id or record.vault != normalized.vault:
                    raise ApprovalError("mismatch")
                if not secrets.compare_digest(_action_digest(agent.client_id, encoded, record.state), record.action_digest):
                    raise ApprovalError("mismatch")
                if not record.approved or record.approved_by not in self._registry[record.vault].owner_ids:
                    raise ApprovalError("approval_required")
                del self._pending[pending_id]  # consume before any adapter call
            try:
                expected = tuple(StateEntry(*entry) for entry in record.state)
                observed = self._adapter.observe(normalized, expected)
                actual = _canonical_state(observed, normalized)
            except ApprovalError:
                raise ApprovalError("stale_state") from None
            except Exception:
                raise ApprovalError("uncertain_result") from None
            if actual != record.state:
                raise ApprovalError("stale_state")
            package = None
            if normalized.verb in _PACKAGE_VERBS:
                try:
                    package = self._attested_package(normalized)
                except Exception:
                    raise ApprovalError("stale_state") from None
            try:
                if record.expires_at <= self._clock():
                    raise ApprovalError("expired")
                return self._adapter.execute(normalized, package=package)
            except ApprovalError:
                raise
            except AdapterConflict:
                raise ApprovalError("stale_state") from None
            except Exception:
                raise ApprovalError("uncertain_result") from None
            finally:
                if package is not None:
                    self._adapter.release_package(package.pin)
