"""Durable deployment records: one ConfigMap per record in ``flagger-system``,
named from a deterministic idempotency key. The API's own ``409
AlreadyExists`` on create *is* the duplicate check — no read-modify-write."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence

from .identity import CandidateIdentity

_NAME_PREFIX = "flagger-recovery-"
_LABEL_ANNOTATION = "flagger-recovery/canary-full"

# Kubernetes label VALUES: <=63 chars, alphanumeric/./-/_ , must start and end alphanumeric.
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")

class PutResult(Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"

class ApiWriteError(Exception):
    """Raised when the Kubernetes API returns an unexpected status for a write/read."""

    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {body!r}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body

class ForeignConfigMap(Exception):
    """Raised by ``ConfigMapStore.put`` when a ConfigMap occupies our
    deterministic name but isn't provably our record.

    A 409 on create proves only that the name is taken, not that its contents
    are our record, so ``put`` follows up with a GET and checks the
    ``app.kubernetes.io/part-of``, ``flagger-recovery/template-hash`` and
    ``flagger-recovery/phase`` labels before reporting ``PutResult.DUPLICATE``.
    This is raised instead when those labels don't match, or when the
    verification GET returns 404 (the ConfigMap was deleted between our
    failed create and our check) and a single retried create still can't
    land cleanly -- in both cases the caller must log and refuse rather than
    assume its record was ever stored."""

    def __init__(self, name: str) -> None:
        super().__init__(f"ConfigMap {name!r} exists but is not a flagger-recovery record for this key")
        self.name = name

def canary_label(namespace: str, canary_name: str) -> str:
    """The ``flagger-recovery/canary`` label value: unique across namespaces.

    Kubernetes caps label values at 63 characters and restricts the charset;
    inputs that fit are used verbatim, everything else falls back to a short,
    stable hash form. ``canary_label_full()`` recovers the original value in
    that case."""
    full = f"{namespace}.{canary_name}"
    if len(full) <= 63 and _LABEL_VALUE_RE.match(full):
        return full
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:12]
    return f"{full[:24]}-{digest}"

def canary_label_full(namespace: str, canary_name: str) -> Optional[str]:
    """The full ``namespace.canary_name`` value, or ``None`` when
    ``canary_label()`` returned it verbatim (no shortening needed) — the
    value stored under the ``flagger-recovery/canary-full`` annotation."""
    full = f"{namespace}.{canary_name}"
    if len(full) <= 63 and _LABEL_VALUE_RE.match(full):
        return None
    return full

def make_key(identity: CandidateIdentity, phase: str) -> str:
    """A deterministic 32-hex-char key. Changes with template hash or phase only."""
    material = f"{identity.namespace}/{identity.canary_name}/{identity.template_hash}/{phase}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

@dataclasses.dataclass(frozen=True)
class DeploymentRecord:
    phase: str
    identity: CandidateIdentity
    created_at: str  # ISO 8601; informational only, never part of the key

    def to_configmap(self, *, storage_namespace: str = "flagger-system") -> dict[str, Any]:
        key = make_key(self.identity, self.phase)
        payload = {"phase": self.phase, "created_at": self.created_at, "identity": self.identity.to_dict()}
        metadata: dict[str, Any] = {
            "name": f"{_NAME_PREFIX}{key}",
            "namespace": storage_namespace,
            "labels": {
                "app.kubernetes.io/part-of": "flagger-recovery",
                "flagger-recovery/canary": canary_label(self.identity.namespace, self.identity.canary_name),
                "flagger-recovery/phase": self.phase,
                "flagger-recovery/template-hash": self.identity.template_hash,
            },
        }
        full_canary = canary_label_full(self.identity.namespace, self.identity.canary_name)
        if full_canary is not None:
            metadata["annotations"] = {_LABEL_ANNOTATION: full_canary}
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {"record.json": json.dumps(payload, sort_keys=True)},
        }

    @classmethod
    def from_configmap(cls, configmap: Mapping[str, Any]) -> "DeploymentRecord":
        payload = json.loads(configmap["data"]["record.json"])
        return cls(
            phase=payload["phase"],
            created_at=payload["created_at"],
            identity=CandidateIdentity.from_dict(payload["identity"]),
        )

class RecordStore(Protocol):
    def put(self, record: DeploymentRecord) -> PutResult: ...
    def get(self, key: str) -> Optional[DeploymentRecord]: ...
    def list(self, canary: str) -> Sequence[DeploymentRecord]: ...

class Transport(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: Optional[bytes],
        ca_file: Optional[str], timeout: float,
    ) -> tuple[int, bytes]: ...

class _UrllibTransport:
    """The real transport: stdlib ``urllib`` only, GET and POST, never more."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: Optional[bytes],
        ca_file: Optional[str], timeout: float,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        context = ssl.create_default_context(cafile=ca_file) if ca_file else None
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

class ConfigMapStore:
    """Create and read only: ``put`` is a POST create, ``get``/``list`` are
    GETs. Never a PATCH, PUT or DELETE."""

    def __init__(
        self, base_url: str, *, namespace: str = "flagger-system", token: Optional[str] = None,
        ca_file: Optional[str] = None, timeout: float = 10.0, transport: Optional[Transport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._namespace = namespace
        self._token = token
        self._ca_file = ca_file
        self._timeout = timeout
        self._transport = transport or _UrllibTransport()

    def put(self, record: DeploymentRecord) -> PutResult:
        configmap = record.to_configmap(storage_namespace=self._namespace)
        name = configmap["metadata"]["name"]
        create_url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps"
        create_body = json.dumps(configmap).encode("utf-8")

        status, body = self._request("POST", create_url, create_body)
        if status == 201:
            return PutResult.CREATED
        if status != 409:
            raise ApiWriteError("POST", create_url, status, body)

        # A 409 only proves the name is taken -- confirm the existing
        # ConfigMap is actually our record before calling it a duplicate.
        get_url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps/{name}"
        status, body = self._request("GET", get_url, None)
        if status == 404:
            # Deleted between our failed create and this check. Retry once;
            # a second conflict means something is actively racing us for
            # this name, so give up rather than loop.
            status, body = self._request("POST", create_url, create_body)
            if status == 201:
                return PutResult.CREATED
            if status == 409:
                raise ForeignConfigMap(name)
            raise ApiWriteError("POST", create_url, status, body)
        if status != 200:
            raise ApiWriteError("GET", get_url, status, body)

        existing_labels = json.loads(body).get("metadata", {}).get("labels", {})
        wanted_labels = configmap["metadata"]["labels"]
        if (
            existing_labels.get("app.kubernetes.io/part-of") == "flagger-recovery"
            and existing_labels.get("flagger-recovery/template-hash") == wanted_labels["flagger-recovery/template-hash"]
            and existing_labels.get("flagger-recovery/phase") == wanted_labels["flagger-recovery/phase"]
        ):
            return PutResult.DUPLICATE
        raise ForeignConfigMap(name)

    def get(self, key: str) -> Optional[DeploymentRecord]:
        url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps/{_NAME_PREFIX}{key}"
        status, body = self._request("GET", url, None)
        if status == 404:
            return None
        if status != 200:
            raise ApiWriteError("GET", url, status, body)
        return DeploymentRecord.from_configmap(json.loads(body))

    def list(self, canary: str) -> Sequence[DeploymentRecord]:
        selector = urllib.parse.urlencode({"labelSelector": f"flagger-recovery/canary={canary}"})
        url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps?{selector}"
        status, body = self._request("GET", url, None)
        if status != 200:
            raise ApiWriteError("GET", url, status, body)
        items = json.loads(body).get("items", [])
        return [DeploymentRecord.from_configmap(item) for item in items]

    def _request(self, method: str, url: str, body: Optional[bytes]) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return self._transport.request(
            method, url, headers=headers, body=body, ca_file=self._ca_file, timeout=self._timeout
        )

class InMemoryStore:
    """The same ``RecordStore`` protocol, held in a dict — for the receiver's
    own tests (FRP-006), never for production durability."""

    def __init__(self) -> None:
        self._by_key: dict[str, DeploymentRecord] = {}
        self._by_canary: dict[str, list[str]] = {}

    def put(self, record: DeploymentRecord) -> PutResult:
        key = make_key(record.identity, record.phase)
        if key in self._by_key:
            return PutResult.DUPLICATE
        self._by_key[key] = record
        label = canary_label(record.identity.namespace, record.identity.canary_name)
        self._by_canary.setdefault(label, []).append(key)
        return PutResult.CREATED

    def get(self, key: str) -> Optional[DeploymentRecord]:
        return self._by_key.get(key)

    def list(self, canary: str) -> Sequence[DeploymentRecord]:
        return [self._by_key[key] for key in self._by_canary.get(canary, [])]
