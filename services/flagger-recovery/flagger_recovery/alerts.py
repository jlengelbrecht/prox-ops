"""The Alertmanager path: ``POST /hooks/alert``, hold-first.

A failure *during* an analysis arrives as a ``post-rollout`` hook (``decide.py``);
one *after* promotion gets no hook at all, so the only witness is monitoring — a
weak one, since an alert says a symptom is present and never that a revision caused
it. Hence a closed set of ``Hold`` reasons with one allow at the bottom, and
attribution to the **promoted revision**, never to what changed most recently; the
README's "The Alertmanager path" says what each rung is for. Nothing in an alert is
executed, and no field of one reaches a URL, a header, a correction's bound or an
unencoded log line.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional

from .inbox import MAX_FIELD_LENGTH, MAX_METADATA_ENTRIES
from .kube import ApiError
from .proposal import PHASE_ALERT_PROPOSAL, build_proposal
from .record import (ApiWriteError, DeploymentRecord, Document, PutResult, canary_label,
                     label_value, make_key_parts)
from .server import PHASE_PROMOTED, Decision, _log_field, now

LOG = logging.getLogger("flagger_recovery.alerts")

ALERT_VERSION = "4"
STATUS_FIRING, STATUS_RESOLVED = "firing", "resolved"
_ALERT_STATUSES = (STATUS_FIRING, STATUS_RESOLVED)
MAX_ALERTS = 64  # one notification may not become an unbounded number of writes

# Three kinds through the same create-only store. ``alert-resolved`` is its own kind
# precisely so no sweep over held alerts can pick a resolution up (AC1).
KIND_ALERT, KIND_ALERT_RESOLVED, KIND_RECEIVER_RUN = "alert", "alert-resolved", "receiver-run"

# The identity FRP-008b's PrometheusRule stamps on every pilot alert: label *names*,
# constants rather than configuration, because a rule that could rename them could
# attribute somebody else's symptom to this pilot.
LABEL_NAMESPACE, LABEL_CANARY, LABEL_SEVERITY = "namespace", "canary", "severity"
REQUIRED_SEVERITY, ANNOTATION_EVIDENCE = "critical", "recovery_evidence"

HOLD, RECORDED, DUPLICATE, PROPOSE_CORRECTION = "Hold", "Recorded", "Duplicate", "ProposeCorrection"
UNATTRIBUTED = "unattributed"  # the canary label for an alert that named none

HOLD_UNATTRIBUTED = "unattributed"  # no identity labels: this pilot cannot claim it
HOLD_ROLLOUT_IN_PROGRESS = "rollout-in-progress"  # Flagger owns a rollout while it runs
HOLD_NO_PROMOTED_RECORD = "no-promoted-record"  # nothing recorded the promotion
HOLD_REVISION_MISMATCH = "revision-mismatch"  # the record and Flux name different revisions
HOLD_NO_PRIOR_PROMOTED = "no-prior-promoted"  # there is nothing to restore to
HOLD_INSUFFICIENT_EVIDENCE = "insufficient-evidence"  # the alert corroborates nothing (AC2)
HOLD_HEALTH_UNKNOWN = "health-unknown"  # something could not be read at all (AC4)
HOLD_REASONS = (HOLD_UNATTRIBUTED, HOLD_ROLLOUT_IN_PROGRESS, HOLD_NO_PROMOTED_RECORD,
                HOLD_REVISION_MISMATCH, HOLD_NO_PRIOR_PROMOTED, HOLD_INSUFFICIENT_EVIDENCE,
                HOLD_HEALTH_UNKNOWN)
FUNCTIONAL_NOT_CONSULTED, FUNCTIONAL_PASSING, FUNCTIONAL_FAILING = "not-consulted", "passing", "failing"

# "Could not be read", enumerated rather than a bare ``Exception``: the API client's
# error, the store's, the transport's (``URLError``/``TimeoutError`` are ``OSError``)
# and the shape errors an absent status makes. A bug is none of these and still 500s.
_UNREADABLE = (ApiError, ApiWriteError, OSError, AttributeError, KeyError, ValueError)

class MalformedAlert(Exception):
    """The request was JSON but not an Alertmanager v4 webhook notification."""

def Hold(reason: str) -> Decision:  # noqa: N802 - a Decision constructor, as in decide.py
    return Decision(kind=HOLD, reason=reason)

def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_FIELD_LENGTH:
        raise MalformedAlert(f"{field!r} must be a non-empty string of <= {MAX_FIELD_LENGTH} chars")
    return value.strip()

def _string_map(raw: Any, field: str) -> dict[str, str]:
    """Bounded as ``inbox._metadata`` bounds metadata: a hostile map is stopped being
    read, never walked in full so it can be truncated afterwards."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MalformedAlert(f"{field!r} must be an object")
    bounded: dict[str, str] = {}
    for key, value in raw.items():
        if len(bounded) >= MAX_METADATA_ENTRIES:
            break
        bounded[str(key)[:MAX_FIELD_LENGTH]] = str(value)[:MAX_FIELD_LENGTH]
    return dict(sorted(bounded.items()))

@dataclasses.dataclass(frozen=True)
class Alert:
    """One bounded entry of a v4 notification's ``alerts[]``."""

    status: str
    fingerprint: str
    starts_at: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]

    @property
    def key(self) -> str:
        """``(fingerprint, startsAt, status)``: Alertmanager repeats a firing alert and
        resolves it under one fingerprint, which alone would collapse the two."""
        return hashlib.sha256(
            f"{self.fingerprint}|{self.starts_at}|{self.status}".encode("utf-8")).hexdigest()[:32]

    @property
    def kind(self) -> str:
        return KIND_ALERT if self.status == STATUS_FIRING else KIND_ALERT_RESOLVED

    @property
    def canary(self) -> Optional[tuple[str, str]]:
        """``(namespace, canary)`` from the alert's own labels, or ``None``."""
        namespace, canary = self.labels.get(LABEL_NAMESPACE, ""), self.labels.get(LABEL_CANARY, "")
        return (namespace, canary) if namespace and canary else None

def parse(payload: Mapping[str, Any]) -> tuple[str, tuple[Alert, ...]]:
    """A v4 notification as ``(groupKey, alerts)``. ``version`` must be the *string*
    ``"4"``, not something that stringifies to it; any other shape raises."""
    if payload.get("version") != ALERT_VERSION or payload.get("status") not in _ALERT_STATUSES:
        raise MalformedAlert(f"not a v4 {_ALERT_STATUSES} notification: "
                             f"version={payload.get('version')!r} status={payload.get('status')!r}")
    entries = payload.get("alerts")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ALERTS:
        raise MalformedAlert(f"'alerts' must be a list of 1..{MAX_ALERTS} objects")
    alerts = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("status") not in _ALERT_STATUSES:
            raise MalformedAlert(f"every alert must be an object with a {_ALERT_STATUSES} status")
        alerts.append(Alert(status=str(entry["status"]),
                            fingerprint=_text(entry.get("fingerprint"), "fingerprint"),
                            starts_at=_text(entry.get("startsAt"), "startsAt"),
                            labels=_string_map(entry.get("labels"), "labels"),
                            annotations=_string_map(entry.get("annotations"), "annotations")))
    return str(payload.get("groupKey") or "")[:MAX_FIELD_LENGTH], tuple(alerts)

@dataclasses.dataclass(frozen=True)
class AlertSweepReport:
    """What one pass saw, derived from live state and never stored: a held alert's
    record is create-only, so a cleared hold is found by re-running the ladder."""

    alerts_held: int = 0
    alerts_resolved: int = 0
    proposals_written: int = 0
    interruption_window_seconds: Optional[float] = None

def _seconds_between(earlier: Optional[str], later: str) -> Optional[float]:
    """``None`` when either end is missing or unreadable — never ``0.0``, which reads
    as "nothing was missed" for a window nobody could measure."""
    def _at(value: Optional[str]) -> Optional[datetime]:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
        except (TypeError, ValueError):
            return None
    start, end = _at(earlier), _at(later)
    return None if start is None or end is None else max((end - start).total_seconds(), 0.0)

def _alert_from(document: Document) -> Optional[Alert]:
    """The alert a held document recorded, so the sweep re-runs one ladder over the same
    inputs. ``None`` if a field it reads is missing: stay held, never guess."""
    payload = document.payload
    labels, annotations = payload.get("labels"), payload.get("annotations")
    fingerprint, starts_at = payload.get("fingerprint"), payload.get("starts_at")
    if not (isinstance(labels, Mapping) and isinstance(annotations, Mapping)
            and isinstance(fingerprint, str) and isinstance(starts_at, str)):
        return None
    return Alert(status=STATUS_FIRING, fingerprint=fingerprint, starts_at=starts_at,
                 labels={str(key): str(value) for key, value in labels.items()},
                 annotations={str(key): str(value) for key, value in annotations.items()})

class AlertRouter:
    """``POST /hooks/alert``, end to end: parse, decide, record, sweep. ``live_for``
    answers a ``decide.LiveState`` (``kube.LiveCanary``) and ``revision_for`` a
    ``kube.LiveKustomization`` — factories, not instances, because each caches its own
    reads and one evaluation must be one snapshot."""

    def __init__(self, store: Any, live_for: Callable[[str, str], Any],
                 revision_for: Callable[[], Any], *, clock: Callable[[], str] = now) -> None:
        self._store, self._live_for, self._revision_for, self._clock = store, live_for, revision_for, clock
        self.started_at = clock()  # this process's start, not the first sweep's
        self._run_written = False
        self.interruption_window_seconds: Optional[float] = None

    def handle(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """The route body, after ``Receiver`` admitted and authenticated it. A grouped
        notification splits here; every outcome is 202, a hold being a decision made
        and not a delivery Alertmanager should retry."""
        try:
            group_key, alerts = parse(payload)
        except MalformedAlert as exc:
            return 400, {"result": "MalformedAlert", "detail": str(exc)}
        return 202, {"result": "Accepted", "alerts": [self._one(alert, group_key) for alert in alerts]}

    def _one(self, alert: Alert, group_key: str) -> dict[str, Any]:
        answer = {"fingerprint": alert.fingerprint, "alert": alert.key}
        if self._store.get_document(alert.kind, alert.key) is not None:
            self._log(alert, DUPLICATE, "")  # the ladder already ran on this exact alert
            return {**answer, "result": DUPLICATE}
        if alert.status != STATUS_FIRING:
            # AC1: a resolution is evidence the symptom stopped, never about a revision.
            self._record(alert, group_key, Decision(kind=RECORDED, reason=STATUS_RESOLVED), {})
            self._log(alert, RECORDED, STATUS_RESOLVED)
            return {**answer, "result": RECORDED}
        decision, evidence = self._evaluate(alert)
        # Before the alert is marked seen, as ``Receiver._register`` writes its record
        # first: a failed proposal write must reach the handler's 500 and leave the alert
        # unrecorded, so a redelivery re-runs the ladder rather than answering
        # ``Duplicate`` for a proposal that never landed.
        if decision.proposal is not None:
            self._store.put_document(decision.proposal.to_document())
        self._record(alert, group_key, decision, evidence)
        key = "" if decision.proposal is None else decision.proposal.key
        self._log(alert, decision.kind, decision.reason, key)
        return {**answer, "result": decision.kind, "detail": decision.reason, "proposal": key}

    def _evaluate(self, alert: Alert) -> tuple[Decision, dict[str, Any]]:
        """The ladder. Every live read sits inside the one ``try``, so "unreadable is
        ``Hold(health-unknown)``, never a proposal" is provable by inspection."""
        evidence: dict[str, Any] = {"functional_check": FUNCTIONAL_NOT_CONSULTED}
        target = alert.canary
        if target is None:
            return Hold(HOLD_UNATTRIBUTED), evidence
        namespace, canary_name = target
        try:
            live = self._live_for(namespace, canary_name)
            status = live.canary_status() or {}
            applied = status.get("lastAppliedSpec") or ""
            promoted_spec = status.get("lastPromotedSpec") or ""
            if not applied or not promoted_spec:
                return Hold(HOLD_HEALTH_UNKNOWN), evidence
            if applied != promoted_spec:  # a rollout is in flight, and Flagger owns the
                return Hold(HOLD_ROLLOUT_IN_PROGRESS), evidence  # Deployment until it ends
            promoted = self._store.get(make_key_parts(namespace, canary_name, promoted_spec, PHASE_PROMOTED))
            if promoted is None:
                return Hold(HOLD_NO_PROMOTED_RECORD), evidence
            evidence["promoted_template_hash"] = promoted_spec
            evidence["promoted_source_sha"] = promoted.identity.source_sha
            applied_sha = self._revision_for().source_sha()
            evidence["applied_source_sha"] = applied_sha or ""
            if not applied_sha:
                return Hold(HOLD_HEALTH_UNKNOWN), evidence
            if applied_sha != promoted.identity.source_sha:
                # Something moved unseen, so which revision the symptom belongs to is
                # not establishable — and guessing is what AC2 forbids.
                return Hold(HOLD_REVISION_MISMATCH), evidence
            previous = self._previous_promoted(namespace, canary_name, promoted)
            if previous is None:
                return Hold(HOLD_NO_PRIOR_PROMOTED), evidence
            evidence["restore_source_sha"] = previous.identity.source_sha
            # Recorded, never gating: ``LiveCanary`` answers ``None`` (the apex's own
            # check has no path back here), which is neither a pass nor a failure.
            check = live.functional_check()
            evidence["functional_check"] = (FUNCTIONAL_NOT_CONSULTED if check is None else
                                            FUNCTIONAL_PASSING if check else FUNCTIONAL_FAILING)
        except _UNREADABLE as exc:
            # The class only: the message carries the request path and response body.
            LOG.warning("alert %s: %s (%s)", _log_field(alert.fingerprint),
                        HOLD_HEALTH_UNKNOWN, type(exc).__name__)
            return Hold(HOLD_HEALTH_UNKNOWN), evidence

        # Causal corroboration: the alert must name the expression that fired and be
        # severe enough to correct. Both come from the rule, never the clock (AC2).
        expression = alert.annotations.get(ANNOTATION_EVIDENCE, "").strip()
        severity = alert.labels.get(LABEL_SEVERITY, "")
        evidence["recovery_evidence"], evidence["severity"] = expression, severity
        if not expression or severity != REQUIRED_SEVERITY:
            return Hold(HOLD_INSUFFICIENT_EVIDENCE), evidence
        # The promoted revision is the failed one here; restore the one before it.
        proposal = build_proposal(promoted.identity, previous.identity, phase=PHASE_ALERT_PROPOSAL)
        return Decision(kind=PROPOSE_CORRECTION, reason=proposal.summary, proposal=proposal), evidence

    def _previous_promoted(self, namespace: str, canary_name: str,
                           current: DeploymentRecord) -> Optional[DeploymentRecord]:
        """The promotion before ``current`` — what a correction restores to. By the
        records' own ``created_at``, never "the newest record of any phase": a candidate
        registered after this promotion is newer, and is exactly what must not win."""
        earlier = [record for record in self._store.list(canary_label(namespace, canary_name))
                   if record.phase == PHASE_PROMOTED
                   and record.identity.template_hash != current.identity.template_hash
                   and record.created_at < current.created_at and record.identity.source_sha]
        return max(earlier, key=lambda record: record.created_at, default=None)

    def _record(self, alert: Alert, group_key: str, decision: Decision,
                evidence: Mapping[str, Any]) -> PutResult:
        canary = alert.canary
        return self._store.put_document(Document(
            kind=alert.kind, key=alert.key,
            # ``decision.kind`` is this module's vocabulary, clamped anyway: a label the
            # API server rejects would turn a recordable alert into a 500.
            labels={"flagger-recovery/canary": canary_label(*canary) if canary else UNATTRIBUTED,
                    "flagger-recovery/alert-status": alert.status,
                    "flagger-recovery/decision": label_value(decision.kind)},
            payload={"fingerprint": alert.fingerprint, "starts_at": alert.starts_at,
                     "status": alert.status, "group_key": group_key,
                     "labels": dict(alert.labels), "annotations": dict(alert.annotations),
                     "received_at": self._clock(), "decision": decision.kind,
                     "reason": decision.reason, "evidence": dict(evidence),
                     "proposal": "" if decision.proposal is None else decision.proposal.key}))

    def sweep(self) -> AlertSweepReport:
        """Re-runs the ladder over every held alert: a hold's reason can clear with
        nothing arriving here (a rollout finishes, a record lands, Flux catches up). One
        that still holds is counted and left alone — nothing escalates a hold for being
        old — and a cleared one writes through the in-band key, so one revision still
        yields one proposal (AC3)."""
        self._begin_run()
        counts = {"alerts_held": 0, "alerts_resolved": 0, "proposals_written": 0}
        for document in self._store.list_documents(KIND_ALERT):
            if document.payload.get("decision") != HOLD:
                continue
            alert = _alert_from(document)
            decision = Hold(HOLD_HEALTH_UNKNOWN) if alert is None else self._evaluate(alert)[0]
            if decision.proposal is None:
                counts["alerts_held"] += 1
                continue
            counts["alerts_resolved"] += 1
            if self._store.put_document(decision.proposal.to_document()) is PutResult.CREATED:
                counts["proposals_written"] += 1
                LOG.info("alert sweep: hold %s cleared for %s; proposal %s",
                         _log_field(str(document.payload.get("reason") or "")),
                         _log_field(str(document.payload.get("fingerprint") or "")),
                         _log_field(decision.proposal.key))
        return AlertSweepReport(interruption_window_seconds=self.interruption_window_seconds, **counts)

    def _begin_run(self) -> None:
        """The ``receiver-run`` document, written once per process on the first sweep
        (which the loop runs at startup). The store is create-only, so no per-tick
        document exists: the last moment this receiver is *known* to have run is the
        newest timestamp it left behind, and the window is the gap to this start."""
        if self._run_written:
            return
        self._run_written = True  # before the write: a store that refuses it must not
        # make every later sweep re-attempt it, and the window is this start either way.
        last_tick = self._last_tick()
        self.interruption_window_seconds = _seconds_between(last_tick, self.started_at)
        self._store.put_document(Document(
            kind=KIND_RECEIVER_RUN, labels={},
            key=hashlib.sha256(self.started_at.encode("utf-8")).hexdigest()[:32],
            payload={"started_at": self.started_at, "last_tick_at": last_tick or "",
                     "interruption_window_seconds": self.interruption_window_seconds}))

    def _last_tick(self) -> Optional[str]:
        stamps = [str(document.payload.get("started_at") or "")
                  for document in self._store.list_documents(KIND_RECEIVER_RUN)]
        for kind in (KIND_ALERT, KIND_ALERT_RESOLVED):
            stamps += [str(document.payload.get("received_at") or "")
                       for document in self._store.list_documents(kind)]
        return max((stamp for stamp in stamps if stamp), default=None)

    def _log(self, alert: Alert, result: str, reason: str, proposal: str = "") -> None:
        """One line per alert, mirroring ``Receiver._log_decision``. Every field comes
        from the payload, so every one is clamped and JSON-quoted: a forged ``result=``
        inside a label cannot read as a second field."""
        LOG.info("hook=alert fingerprint=%s status=%s canary=%s result=%s reason=%s proposal=%s",
                 _log_field(alert.fingerprint), _log_field(alert.status),
                 _log_field("/".join(alert.canary) if alert.canary else ""),
                 _log_field(result), _log_field(reason), _log_field(proposal))
