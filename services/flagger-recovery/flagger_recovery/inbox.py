"""Durable event intake.

Every authenticated webhook becomes exactly one ConfigMap in
``flagger-system`` with ``flagger-recovery/kind=event``, named from
``sha256(namespace/name + checksum + phase + hook)[:32]``. A repeat of the
same hook is a no-op: the key is identical, so the store's create returns
``PutResult.DUPLICATE`` and nothing is written twice.

Nothing from a payload is ever executed, and the shared token is stripped
before the event is stored — the whole point of the record is that it can be
read with ``kubectl get`` as evidence.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import Any, Mapping, Optional

from .record import Document, PutResult, canary_label, label_value

HOOKS = ("pre-rollout", "post-rollout", "event")

STATUS_RECEIVED = "received"
STATUS_ATTRIBUTION_PENDING = "attribution-pending"

KIND_EVENT = "event"

# Bounds on untrusted payload fields, enforced here rather than delegated to
# whatever transport is in front: no single field may dominate a ConfigMap, and
# no oversized map may be walked in full just to be truncated afterwards.
MAX_FIELD_LENGTH = 512
MAX_METADATA_ENTRIES = 32

class MalformedPayload(Exception):
    """The request was JSON but not a Flagger webhook payload."""

# Required fields (name, namespace) become Kubernetes object identifiers and
# key/label material, so beyond stripping surrounding whitespace they may not
# contain whitespace or control characters anywhere in the value. Optional
# fields (phase, checksum) are still clamped by label_value() before they
# ever reach a label, so they are only stripped/length-bounded here.
_INVALID_INNER_CHAR = frozenset(chr(code) for code in range(0x20)) | {chr(0x7F)}

def _text(payload: Mapping[str, Any], field: str, *, required: bool) -> str:
    value = payload.get(field)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise MalformedPayload(f"{field!r} must be a non-empty string")
    value = value.strip()
    if required and not value:
        raise MalformedPayload(f"{field!r} must be a non-empty string")
    if len(value) > MAX_FIELD_LENGTH:
        raise MalformedPayload(f"{field!r} exceeds {MAX_FIELD_LENGTH} characters")
    if required and any(char.isspace() or char in _INVALID_INNER_CHAR for char in value):
        raise MalformedPayload(f"{field!r} must not contain whitespace or control characters")
    return value

def _metadata(payload: Mapping[str, Any]) -> dict[str, str]:
    """The webhook's ``metadata`` map, minus the shared token, coerced to
    strings and bounded. Flagger puts arbitrary operator-authored values
    here, so nothing in it is trusted for anything but the record."""
    raw = payload.get("metadata")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MalformedPayload("'metadata' must be an object")
    metadata: dict[str, str] = {}
    for key, value in raw.items():
        if len(metadata) >= MAX_METADATA_ENTRIES:
            break  # stop scanning, rather than walk a hostile map to truncate it later
        name = str(key)[:MAX_FIELD_LENGTH]
        if name == "token":
            continue
        metadata[name] = str(value)[:MAX_FIELD_LENGTH]
    return dict(sorted(metadata.items()))

@dataclasses.dataclass(frozen=True)
class WebhookEvent:
    """One Flagger webhook call. ``checksum`` is the Canary's
    ``status.lastAppliedSpec`` — the candidate's pod-template hash, which is
    what ties an event to a ``DeploymentRecord``."""

    hook: str
    name: str
    namespace: str
    phase: str
    checksum: str
    metadata: Mapping[str, str]

    @property
    def key(self) -> str:
        material = f"{self.namespace}/{self.name}|{self.checksum}|{self.phase}|{self.hook}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    @classmethod
    def parse(cls, hook: str, payload: Mapping[str, Any]) -> "WebhookEvent":
        if hook not in HOOKS:
            raise MalformedPayload(f"unknown hook {hook!r}")
        if not isinstance(payload, Mapping):
            raise MalformedPayload("payload must be a JSON object")
        return cls(
            hook=hook,
            name=_text(payload, "name", required=True),
            namespace=_text(payload, "namespace", required=True),
            phase=_text(payload, "phase", required=False),
            checksum=_text(payload, "checksum", required=False),
            metadata=_metadata(payload),
        )

    def to_document(
        self,
        *,
        status: str,
        received_at: str,
        detail: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> Document:
        payload: dict[str, Any] = {
            "hook": self.hook,
            "name": self.name,
            "namespace": self.namespace,
            "phase": self.phase,
            "checksum": self.checksum,
            "metadata": dict(self.metadata),
            "status": status,
            "received_at": received_at,
        }
        if detail is not None:
            payload["detail"] = detail[:MAX_FIELD_LENGTH]
        if retry_after_seconds is not None:
            payload["retry_after_seconds"] = retry_after_seconds
        return Document(
            kind=KIND_EVENT,
            key=self.key,
            labels={
                "flagger-recovery/canary": canary_label(self.namespace, self.name),
                "flagger-recovery/hook": self.hook,
                "flagger-recovery/phase": label_value(self.phase or "none"),
                "flagger-recovery/status": status,
                "flagger-recovery/template-hash": label_value(self.checksum or "none"),
            },
            payload=payload,
        )

class Inbox:
    """Idempotent intake over any ``RecordStore``."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def seen(self, event: WebhookEvent) -> bool:
        """True when this exact hook was already stored. Cheap pre-check that
        keeps a redelivery from re-reading live state; the create's own 409
        remains the authoritative duplicate test."""
        return self._store.get_document(KIND_EVENT, event.key) is not None

    def accept(
        self,
        event: WebhookEvent,
        *,
        status: str,
        received_at: str,
        detail: Optional[str] = None,
        retry_after_seconds: Optional[int] = None,
    ) -> PutResult:
        return self._store.put_document(
            event.to_document(
                status=status,
                received_at=received_at,
                detail=detail,
                retry_after_seconds=retry_after_seconds,
            )
        )
