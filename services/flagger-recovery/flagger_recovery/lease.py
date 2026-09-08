"""The Lease the Git correction writer holds while it writes.

``coordination.k8s.io/v1``, one object, and the only thing this package updates in place.
One replica plus ``Recreate`` is the first line against two writers; this covers the restart
overlap and the reconcile thread. Acquire or refuse — and only a 201 or a 200 is holding it."""

from __future__ import annotations

import json
import logging
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Mapping, Optional

from .policy import LockUnavailable
from .record import ApiWriteError, Transport
from .record import _UrllibTransport as _Transport

LOG = logging.getLogger("flagger_recovery.lease")

LEASE_NAME = "flagger-recovery-writer"
# The design's 60 s. A worst-case correction (nine round trips at ten seconds) can
# outlive its lease; the single replica and the create-only marker bound that residual.
DURATION_SECONDS = 60

def _stamp(moment: datetime) -> str:  # MicroTime: six fractional digits here, never zero
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

class Lease:
    """Callable, so a caller writes ``with lease():`` — ``gitwriter.corrector``'s ``lock``
    seam. GET, create and conditional update; nothing else."""

    def __init__(self, base_url: str, *, namespace: str, holder: str, token: Optional[str] = None,
                 ca_file: Optional[str] = None, timeout: float = 10.0,
                 transport: Optional[Transport] = None,
                 clock: Any = lambda: datetime.now(timezone.utc)) -> None:
        if not holder:  # an empty holder reads as "unheld", so every process would hold it
            raise ValueError("a Lease needs a holder identity (the pod name, from HOSTNAME)")
        self._collection = (f"{base_url.rstrip('/')}/apis/coordination.k8s.io/v1/namespaces/"
                            f"{urllib.parse.quote(namespace, safe='')}/leases")
        self._url = f"{self._collection}/{LEASE_NAME}"
        self._namespace, self._holder, self._clock = namespace, holder, clock
        self._token, self._ca_file, self._timeout = token, ca_file, timeout
        self._transport = transport or _Transport()

    def _request(self, method: str, url: str,
                 body: Optional[Mapping[str, Any]] = None) -> tuple[int, Mapping[str, Any]]:
        headers = {"Accept": "application/json",
                   **({"Content-Type": "application/json"} if body is not None else {}),
                   **({"Authorization": f"Bearer {self._token}"} if self._token else {})}
        status, raw = self._transport.request(
            method, url, headers=headers, body=None if body is None else json.dumps(body).encode("utf-8"),
            ca_file=self._ca_file, timeout=self._timeout)
        if status not in (200, 201, 404, 409):
            raise ApiWriteError(method, url, status, b"")  # empty body: nothing to log
        payload = json.loads(raw) if raw[:1] == b"{" else {}
        return status, payload if isinstance(payload, dict) else {}  # a caller checks the status

    def _object(self, moment: datetime, *, acquired: datetime, holder: str,
                resource_version: str = "") -> dict[str, Any]:
        metadata = {"name": LEASE_NAME, "namespace": self._namespace,
                    **({"resourceVersion": resource_version} if resource_version else {})}
        return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": metadata,
                "spec": {"holderIdentity": holder, "leaseDurationSeconds": DURATION_SECONDS,
                         "acquireTime": _stamp(acquired), "renewTime": _stamp(moment)}}

    def acquire(self) -> str:
        """The resourceVersion this process now holds, or ``LockUnavailable``. An expired lease
        is taken over — a holder that died mid-correction would else block every later one —
        conditionally on the version just read, so a race has exactly one winner."""
        moment = self._clock()
        status, existing = self._request("GET", self._url)
        if status == 404:
            status, created = self._request(
                "POST", self._collection, self._object(moment, acquired=moment, holder=self._holder))
            if status != 201:  # 409 is another writer creating it between our GET and this call
                raise LockUnavailable(f"lease {LEASE_NAME} was not created (HTTP {status})")
            return str(created.get("metadata", {}).get("resourceVersion") or "")
        spec = existing.get("spec") or {}
        held_by = str(spec.get("holderIdentity") or "")
        renewed = _parse(spec.get("renewTime"))  # unreadable: fail closed, it is a live holder
        expired = renewed is not None and renewed + timedelta(seconds=DURATION_SECONDS) <= moment
        if held_by and held_by != self._holder and not expired:
            raise LockUnavailable(f"lease {LEASE_NAME} is held by {held_by!r}")  # %r: a cluster field
        # Re-acquiring our own keeps acquireTime meaning "held since", not "last tried".
        acquired = _parse(spec.get("acquireTime")) if held_by == self._holder else None
        status, updated = self._request("PUT", self._url, self._object(
            moment, acquired=acquired or moment, holder=self._holder,
            resource_version=str(existing.get("metadata", {}).get("resourceVersion") or "")))
        if status != 200:  # 409 is somebody else winning the same expired lease; theirs, not ours
            raise LockUnavailable(f"lease {LEASE_NAME} was not taken (HTTP {status})")
        return str(updated.get("metadata", {}).get("resourceVersion") or "")

    def release(self, version: str) -> None:
        """Write it back unheld — an empty ``holderIdentity`` is what the check above reads as
        free — and log rather than raise on failure: the lease expires on its own anyway."""
        moment = self._clock()
        try:
            status = self._request("PUT", self._url, self._object(
                moment, acquired=moment, holder="", resource_version=version))[0]
        except Exception:  # noqa: BLE001 - best effort; the lease expires either way
            status = 0
        if status != 200:
            LOG.warning("lease %s not released (HTTP %d); it expires in %ds",
                        LEASE_NAME, status, DURATION_SECONDS)

    @contextmanager
    def __call__(self) -> Iterator[str]:
        version = self.acquire()
        LOG.info("lease %s held by %r for up to %ds", LEASE_NAME, self._holder, DURATION_SECONDS)
        try:
            yield version
        finally:
            self.release(version)
