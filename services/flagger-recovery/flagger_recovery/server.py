"""The webhook receiver: stdlib ``http.server`` on 8080, no framework.

``Receiver`` is the whole decision surface and takes bytes, not sockets, so
every response code below is unit-testable without a listener; ``_Handler``
is a thin HTTP shim over it.

Routes: ``POST /hooks/{pre-rollout,post-rollout,event}``, ``GET /healthz``,
``GET /readyz``. Bodies are capped at 64 KiB and must be a JSON object.
Nothing in a payload is ever executed, and the only cluster writes are the
ConfigMap creates ``ConfigMapStore`` performs.

Attribution failures answer 202, never 5xx: Flagger halts a rollout when a
``pre-rollout`` webhook fails, and "the receiver could not see candidate pods
yet" is not a reason to fail somebody's release. A failure to store durably
*is* — an unrecordable event is not an accepted one — so store errors reach
the handler's 500.
"""

from __future__ import annotations

import http.server
import json
import logging
import os
import pathlib
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, NamedTuple, Optional, Protocol

from . import auth
from .identity import AttributionRefused, resolve
from .inbox import (
    STATUS_ATTRIBUTION_PENDING,
    STATUS_RECEIVED,
    Inbox,
    MalformedPayload,
    WebhookEvent,
)
from .kube import ApiError, ApiReader, CandidateReader
from .record import ConfigMapStore, DeploymentRecord, PutResult, make_key, make_key_parts

LOG = logging.getLogger("flagger_recovery.server")

MAX_BODY_BYTES = 64 * 1024
RETRY_AFTER_SECONDS = 30

PHASE_CANDIDATE = "candidate"
PHASE_PROMOTED = "promoted"

HOOK_PATHS = {
    "/hooks/pre-rollout": "pre-rollout",
    "/hooks/post-rollout": "post-rollout",
    "/hooks/event": "event",
}
HEALTH_PATHS = ("/healthz", "/readyz")

_SERVICE_ACCOUNT = pathlib.Path("/var/run/secrets/kubernetes.io/serviceaccount")
_UNSAFE_LOG_CHARS = re.compile(r"[^\x20-\x7e]")

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

class CandidateSource(Protocol):
    """What ``identity.resolve()`` needs, fetched live. ``kube.CandidateReader``
    is the production implementation; tests pass a fake."""

    def read(self, namespace: str, canary_name: str) -> Mapping[str, Any]: ...

class Decision(NamedTuple):
    """The result of a decider call. ``kind`` mirrors FRP-006a's ``decide``
    contract (``Register``, ``Ignore``, ``ProposeCorrection``, ``Refuse``),
    though the default decider installed here only ever returns ``Ignore``."""

    kind: str
    reason: str = ""

def Ignore(reason: str) -> Decision:  # noqa: N802 - reads as the decide.py constructor it stands in for
    return Decision(kind="Ignore", reason=reason)

DECIDER_NOT_INSTALLED = "decider-not-installed"

# ``decide.py`` (slice 3) is the real implementation of this signature: given
# the stored candidate record and the event, decide what (if anything) to do.
# Injecting it here is what lets slice 3 plug in without adding or changing a
# route — every hook already flows through this call.
Decider = Callable[[Optional[DeploymentRecord], WebhookEvent], Decision]

def _default_decider(record: Optional[DeploymentRecord], event: WebhookEvent) -> Decision:
    return Ignore(DECIDER_NOT_INSTALLED)

class Receiver:
    """inbox -> attribute -> store, with no HTTP in sight."""

    def __init__(
        self,
        *,
        token: str,
        store: Any,
        candidates: CandidateSource,
        clock: Any = _now,
        decider: Decider = _default_decider,
    ) -> None:
        if not token:
            raise ValueError("Receiver requires a token; see auth.load_token()")
        self._token = token
        self._store = store
        self._candidates = candidates
        self._clock = clock
        self._decider = decider
        self._inbox = Inbox(store)
        self.unauthorised = 0

    def handle(self, hook: str, headers: Optional[Mapping[str, str]], body: bytes) -> tuple[int, dict[str, Any]]:
        if len(body) > MAX_BODY_BYTES:
            return 413, {"result": "PayloadTooLarge", "limit_bytes": MAX_BODY_BYTES}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return 400, {"result": "MalformedJSON"}
        if not isinstance(payload, dict):
            return 400, {"result": "MalformedJSON", "detail": "payload must be a JSON object"}
        if not auth.token_matches(self._token, auth.presented_token(headers, payload)):
            self.unauthorised += 1
            return 401, {"result": "Unauthorised"}
        try:
            event = WebhookEvent.parse(hook, payload)
        except MalformedPayload as exc:
            return 400, {"result": "MalformedPayload", "detail": str(exc)}

        # ``attribution-pending`` is not terminal: the document store is
        # create-only, so a pending event can never be overwritten in place
        # once the candidate pods show up. A redelivery of the same hook must
        # re-run resolution instead of short-circuiting on that stale
        # placeholder; only a status the store actually finished with
        # (``STATUS_RECEIVED``) is a real duplicate.
        previous_status = self._inbox.status(event)
        if previous_status is not None and previous_status != STATUS_ATTRIBUTION_PENDING:
            return 202, {"result": "Duplicate", "event": event.key}
        if hook == "pre-rollout":
            return self._register(event)
        if hook == "post-rollout" and event.phase == "Succeeded":
            return self._promote(event)
        return self._decide(event)

    def _decide(self, event: WebhookEvent) -> tuple[int, dict[str, Any]]:
        """Everything not already handled above: a ``post-rollout`` that is
        not ``Succeeded`` (chiefly ``Failed``, FRP-006a's AC2), and every plain
        ``event`` hook. The candidate record is a stored-record lookup, same
        as ``_promote`` — never a live re-resolve. The decider's result is
        recorded on the accepted event; the default installed here always
        answers ``Ignore(decider-not-installed)`` until slice 3 injects the
        real ``decide()``."""
        key = make_key_parts(event.namespace, event.name, event.checksum, PHASE_CANDIDATE) if event.checksum else ""
        record = self._store.get(key) if key else None
        decision = self._decider(record, event)
        self._inbox.accept(
            event,
            status=STATUS_RECEIVED,
            received_at=self._clock(),
            detail=f"{decision.kind}: {decision.reason}" if decision.reason else decision.kind,
        )
        return 202, {"result": decision.kind, "event": event.key, "detail": decision.reason}

    def _register(self, event: WebhookEvent) -> tuple[int, dict[str, Any]]:
        """AC1: attribute the candidate the first time it is seen. The
        durable record write happens before the inbox is marked seen: if the
        write fails, the exception reaches the handler's 500 (a failure to
        store durably is not an accepted event) and a retried request finds
        the event still unseen, so it tries the write again instead of
        answering ``Duplicate`` for a record that was never created."""
        try:
            identity = resolve(**self._candidates.read(event.namespace, event.name))
        except (AttributionRefused, ApiError, KeyError) as exc:
            return self._pending(event, f"{type(exc).__name__}: {exc}")
        result = self._store.put(
            DeploymentRecord(phase=PHASE_CANDIDATE, identity=identity, created_at=self._clock())
        )
        self._inbox.accept(event, status=STATUS_RECEIVED, received_at=self._clock())
        return 202, {
            "result": "Registered" if result is PutResult.CREATED else "Duplicate",
            "event": event.key,
            "record": make_key(identity, PHASE_CANDIDATE),
        }

    def _promote(self, event: WebhookEvent) -> tuple[int, dict[str, Any]]:
        """A promotion re-states the identity recorded at pre-rollout; it never
        re-resolves it, because Flagger has already scaled the candidate pods
        away by the time this hook fires. As in ``_register``, the record
        write happens before the inbox is marked seen so a failed write can
        be retried instead of being masked as a duplicate."""
        key = make_key_parts(event.namespace, event.name, event.checksum, PHASE_CANDIDATE) if event.checksum else ""
        record = self._store.get(key) if key else None
        if record is None:
            return self._pending(event, f"no {PHASE_CANDIDATE} record for checksum {event.checksum!r}")
        self._store.put(
            DeploymentRecord(phase=PHASE_PROMOTED, identity=record.identity, created_at=self._clock())
        )
        self._inbox.accept(event, status=STATUS_RECEIVED, received_at=self._clock())
        return 202, {
            "result": "Promoted",
            "event": event.key,
            "record": make_key(record.identity, PHASE_PROMOTED),
        }

    def _pending(self, event: WebhookEvent, detail: str) -> tuple[int, dict[str, Any]]:
        self._inbox.accept(
            event,
            status=STATUS_ATTRIBUTION_PENDING,
            received_at=self._clock(),
            detail=detail,
            retry_after_seconds=RETRY_AFTER_SECONDS,
        )
        return 202, {
            "result": "AttributionPending",
            "event": event.key,
            "retry_after_seconds": RETRY_AFTER_SECONDS,
            "detail": detail,
        }

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "flagger-recovery"
    sys_version = ""
    timeout = 15

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        path = urllib.parse.urlsplit(self.path).path
        if path in HEALTH_PATHS:
            self._respond(200, {"result": "ok"})
        elif path in HOOK_PATHS:
            self._respond(405, {"result": "MethodNotAllowed"})
        else:
            self._respond(404, {"result": "NotFound"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        path = urllib.parse.urlsplit(self.path).path
        hook = HOOK_PATHS.get(path)
        if hook is None:
            if path in HEALTH_PATHS:
                self._respond(405, {"result": "MethodNotAllowed"})
            else:
                self._respond(404, {"result": "NotFound"})
            return
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.strip().isdigit():
            self._respond(411, {"result": "LengthRequired"})
            return
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            # Refuse before reading: an oversized body is never buffered.
            self._respond(413, {"result": "PayloadTooLarge", "limit_bytes": MAX_BODY_BYTES})
            return
        body = self.rfile.read(length)
        try:
            status, payload = self.server.receiver.handle(hook, self.headers, body)
        except Exception:  # noqa: BLE001 - a receiver bug must not leak internals to the caller
            LOG.exception("receiver failed handling %s", hook)
            status, payload = 500, {"result": "InternalError"}
        self._respond(status, payload)

    def _respond(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        # The default logs the raw request line to stderr, which lets a caller
        # inject newlines into the log. Log a sanitised, bounded line instead.
        LOG.info("%s", _UNSAFE_LOG_CHARS.sub("?", (fmt % args))[:512])

class ReceiverServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], receiver: Receiver) -> None:
        super().__init__(address, _Handler)
        self.receiver = receiver

def build_server(receiver: Receiver, *, host: str = "", port: int = 8080) -> ReceiverServer:
    """Bind a server. ``host=""`` binds every interface, which is what a
    container with a NetworkPolicy in front of it wants; pass ``127.0.0.1``
    in tests and ``port=0`` for an ephemeral port."""
    return ReceiverServer((host, port), receiver)

def _main(argv: Optional[list[str]] = None) -> None:
    """In-cluster entry point. Exits rather than serving without a token."""
    import argparse

    parser = argparse.ArgumentParser(description="Flagger recovery webhook receiver")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--storage-namespace", default=os.environ.get("STORAGE_NAMESPACE", "flagger-system"))
    parser.add_argument("--kustomization-namespace", default="flagger-system")
    parser.add_argument("--kustomization-name", default="flagger-pilot-app")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = auth.load_token()

    api_host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    api_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    base_url = f"https://{api_host}:{api_port}"
    ca_file = str(_SERVICE_ACCOUNT / "ca.crt")
    api_token = (_SERVICE_ACCOUNT / "token").read_text(encoding="utf-8").strip()

    store = ConfigMapStore(base_url, namespace=args.storage_namespace, token=api_token, ca_file=ca_file)
    candidates = CandidateReader(
        ApiReader(base_url, token=api_token, ca_file=ca_file),
        kustomization_namespace=args.kustomization_namespace,
        kustomization_name=args.kustomization_name,
    )
    server = build_server(Receiver(token=token, store=store, candidates=candidates), port=args.port)
    LOG.info("listening on :%d, storing records in %s", args.port, args.storage_namespace)
    server.serve_forever()

if __name__ == "__main__":
    _main()
