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
import math
import os
import pathlib
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, NamedTuple, Optional, Protocol

from . import auth
from .identity import AttributionRefused, CandidateIdentity, resolve
from .inbox import (
    STATUS_ATTRIBUTION_PENDING,
    STATUS_RECEIVED,
    Inbox,
    MalformedPayload,
    WebhookEvent,
)
from .kube import ApiError, ApiReader, CandidateReader, LiveCanary
from .record import ConfigMapStore, DeploymentRecord, PutResult, canary_label, make_key, make_key_parts

LOG = logging.getLogger("flagger_recovery.server")

MAX_BODY_BYTES = 64 * 1024
RETRY_AFTER_SECONDS = 30

# The reconcile pass (FRP-006a AC5) runs at startup and then on this interval.
# ``0`` disables the timer; anything under the floor is raised to it, so a typo
# cannot make the pass a hot loop against the API server.
RECONCILE_INTERVAL_ENV = "RECONCILE_INTERVAL_SECONDS"
DEFAULT_RECONCILE_INTERVAL = 300.0
MIN_RECONCILE_INTERVAL = 10.0

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

def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

class CandidateSource(Protocol):
    """What ``identity.resolve()`` needs, fetched live. ``kube.CandidateReader``
    is the production implementation; tests pass a fake."""

    def read(self, namespace: str, canary_name: str) -> Mapping[str, Any]: ...

class Decision(NamedTuple):
    """The result of a decider call. ``kind`` mirrors FRP-006a's ``decide``
    contract (``Register``, ``Ignore``, ``ProposeCorrection``, ``Refuse``).
    ``proposal`` carries a ``proposal.Proposal`` on a ``ProposeCorrection`` and
    is ``None`` on every other kind; its type is left open so that ``decide``
    can import this module without this module importing it."""

    kind: str
    reason: str = ""
    proposal: Any = None

def Ignore(reason: str) -> Decision:  # noqa: N802 - reads as the decide.py constructor it stands in for
    return Decision(kind="Ignore", reason=reason)

def write_promoted_record(store: Any, identity: CandidateIdentity, clock: Callable[[], str] = now) -> DeploymentRecord:
    """Persist the ``promoted`` record for a candidate that has just succeeded
    — the in-band path (``Receiver._promote``) and the reconcile pass (a
    delayed candidate landing after an ``attribution-pending`` promotion
    event) both call this. The key is the identity's template hash and
    ``PHASE_PROMOTED``, and the store is create-only, so whichever of the two
    gets there first wins and the other is a no-op duplicate."""
    record = DeploymentRecord(phase=PHASE_PROMOTED, identity=identity, created_at=clock())
    store.put(record)
    return record

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
        clock: Any = now,
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
        recorded on the accepted event; the default answers
        ``Ignore(decider-not-installed)`` unless ``decide.decider()`` is
        injected. A decision carrying a proposal stores it before the event is
        marked seen, for the same reason ``_register`` writes its record first:
        a failed proposal write must reach the handler's 500 and leave the
        event retryable. The proposal's key is the candidate identity, so a
        repeated or delayed ``Failed`` hook creates nothing."""
        key = make_key_parts(event.namespace, event.name, event.checksum, PHASE_CANDIDATE) if event.checksum else ""
        record = self._store.get(key) if key else None
        decision = self._decider(record, event)
        if decision.proposal is not None:
            self._store.put_document(decision.proposal.to_document())
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
        promoted = write_promoted_record(self._store, record.identity, self._clock)
        self._inbox.accept(event, status=STATUS_RECEIVED, received_at=self._clock())
        return 202, {
            "result": "Promoted",
            "event": event.key,
            "record": make_key(promoted.identity, PHASE_PROMOTED),
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

def reconcile_interval(environ: Optional[Mapping[str, str]] = None) -> float:
    """The interval in seconds, from ``RECONCILE_INTERVAL_SECONDS``. An unset
    or unparsable value falls back to the default rather than failing to
    start; a non-positive one disables the timer."""
    raw = (os.environ if environ is None else environ).get(RECONCILE_INTERVAL_ENV, "")
    try:
        interval = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_RECONCILE_INTERVAL
    # NaN and infinity parse but defeat every comparison below: ``nan <= 0`` is
    # False and ``max(nan, floor)`` is nan, which ``Event.wait`` treats as no
    # wait at all — the floor exists precisely to stop that hot loop.
    if not math.isfinite(interval):
        return DEFAULT_RECONCILE_INTERVAL
    return 0.0 if interval <= 0 else max(interval, MIN_RECONCILE_INTERVAL)

def start_reconcile_loop(pass_fn: Callable[[], Any], interval: float) -> threading.Event:
    """Run ``pass_fn`` once, then every ``interval`` seconds if positive — all
    on one daemon thread, so this returns before the first pass finishes and
    ``_main()`` can bind the HTTP server without waiting on API-server
    latency. A pass that raises is logged, never fatal — Flagger depends on
    the in-band path, and the next tick (or the next process start, if the
    timer is disabled) tries again. Returns the stop event."""
    stop = threading.Event()
    def _once() -> None:
        try:
            LOG.info("reconcile: %s", pass_fn())
        except Exception:  # noqa: BLE001 - a reconcile bug must not take the receiver down
            LOG.exception("reconcile pass failed")

    def _loop() -> None:
        _once()
        while interval > 0 and not stop.wait(interval):
            _once()

    threading.Thread(target=_loop, name="flagger-recovery-reconcile", daemon=True).start()
    return stop

def _reconcile_startup_note(canary_namespace: str, canary_name: str, interval: float) -> str:
    """The reconcile clause of the startup log line. ``interval <= 0`` means
    the periodic timer is off (only the startup pass runs), which reads very
    differently from a hot loop and must not be logged as "every 0s"."""
    if interval <= 0:
        return "reconcile: startup pass only, periodic timer disabled"
    return f"reconciling {canary_namespace}/{canary_name} every {interval:.0f}s"

def _main(argv: Optional[list[str]] = None) -> None:
    """In-cluster entry point. Exits rather than serving without a token."""
    import argparse

    parser = argparse.ArgumentParser(description="Flagger recovery webhook receiver")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    parser.add_argument("--storage-namespace", default=os.environ.get("STORAGE_NAMESPACE", "flagger-system"))
    parser.add_argument("--kustomization-namespace", default="flagger-system")
    parser.add_argument("--kustomization-name", default="flagger-pilot-app")
    parser.add_argument("--canary-namespace", default="flagger-pilot", help="the canary the reconcile pass covers")
    parser.add_argument("--canary-name", default="podinfo")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    token = auth.load_token()

    api_host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    api_port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    base_url = f"https://{api_host}:{api_port}"
    ca_file = str(_SERVICE_ACCOUNT / "ca.crt")
    api_token = (_SERVICE_ACCOUNT / "token").read_text(encoding="utf-8").strip()

    # Imported here, not at module scope: ``decide`` imports this module for
    # the ``Decision`` contract, so the dependency runs one way everywhere but
    # this entry point, which is what installs the real decider.
    from .decide import decider, reconcile

    api = ApiReader(base_url, token=api_token, ca_file=ca_file)
    store = ConfigMapStore(base_url, namespace=args.storage_namespace, token=api_token, ca_file=ca_file)
    candidates = CandidateReader(
        api,
        kustomization_namespace=args.kustomization_namespace,
        kustomization_name=args.kustomization_name,
    )
    # A fresh reader per event: each caches its own reads, one snapshot per decision.
    live_for = lambda namespace, name: LiveCanary(api, namespace, name)  # noqa: E731
    receiver = Receiver(token=token, store=store, candidates=candidates, decider=decider(store, live_for))
    server = build_server(receiver, port=args.port)

    interval = reconcile_interval()
    start_reconcile_loop(
        lambda: reconcile(
            store, live_for(args.canary_namespace, args.canary_name),
            canary=canary_label(args.canary_namespace, args.canary_name),
        ),
        interval,
    )
    LOG.info("listening on :%d, storing records in %s, %s", args.port, args.storage_namespace,
             _reconcile_startup_note(args.canary_namespace, args.canary_name, interval))
    server.serve_forever()

if __name__ == "__main__":
    _main()
