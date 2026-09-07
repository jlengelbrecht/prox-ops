"""Durable deployment records: one ConfigMap per record in ``flagger-system``,
named from a deterministic idempotency key. The API's own ``409
AlreadyExists`` on create *is* the duplicate check — no read-modify-write."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from typing import Any, Mapping, Optional, Protocol, Sequence

from .identity import CandidateIdentity

_NAME_PREFIX = "flagger-recovery-"

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

def canary_label(namespace: str, canary_name: str) -> str:
    """The ``flagger-recovery/canary`` label value: unique across namespaces."""
    return f"{namespace}.{canary_name}"

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
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": f"{_NAME_PREFIX}{key}",
                "namespace": storage_namespace,
                "labels": {
                    "app.kubernetes.io/part-of": "flagger-recovery",
                    "flagger-recovery/canary": canary_label(self.identity.namespace, self.identity.canary_name),
                    "flagger-recovery/phase": self.phase,
                    "flagger-recovery/template-hash": self.identity.template_hash,
                },
            },
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
        url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps"
        status, body = self._request("POST", url, json.dumps(configmap).encode("utf-8"))
        if status == 201:
            return PutResult.CREATED
        if status == 409:
            return PutResult.DUPLICATE
        raise ApiWriteError("POST", url, status, body)

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
