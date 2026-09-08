"""The Alertmanager path: ``POST /hooks/alert``, hold-first.

A failure *during* an analysis arrives as a ``post-rollout`` hook (``decide.py``); one
*after* promotion gets no hook at all, so the only witness is monitoring — a weak one,
since an alert says a symptom is present and never that a revision caused it. Hence a
closed set of ``Hold`` reasons with one allow at the bottom, and attribution to the
**promoted revision**, never to what changed most recently; the README's "The Alertmanager
path" says what each rung is for. Nothing in an alert is executed, and no field of one
reaches a URL, a header, a correction's bound or an unencoded log line.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Optional, Sequence

from .inbox import MAX_FIELD_LENGTH, MAX_METADATA_ENTRIES
from .kube import ApiError
from .proposal import PHASE_ALERT_PROPOSAL, build_proposal
from .record import (ApiWriteError, DeploymentRecord, Document, MalformedRecord, PutResult,
                     canary_label, label_value, make_key_parts)
from .server import PHASE_PROMOTED, Decision, log_field, now

LOG = logging.getLogger("flagger_recovery.alerts")

ALERT_VERSION = "4"
STATUS_FIRING, STATUS_RESOLVED = "firing", "resolved"
_ALERT_STATUSES = (STATUS_FIRING, STATUS_RESOLVED)
MAX_ALERTS = 64  # one notification may not become an unbounded number of writes

# Three kinds through the same create-only store. ``alert-resolved`` is its own kind,
# indexed by fingerprint: a firing alert asks it whether the symptom it names has already
# stopped, which is how a resolution prevents work rather than reversing it (AC1).
KIND_ALERT, KIND_ALERT_RESOLVED, KIND_RECEIVER_RUN = "alert", "alert-resolved", "receiver-run"
FINGERPRINT_LABEL = "flagger-recovery/fingerprint"

# The identity FRP-008b's PrometheusRule stamps on every pilot alert: label *names*,
# constants rather than configuration, because a rule that could rename them could
# attribute somebody else's symptom to this pilot. The *values* are checked against the
# configured canary, since they say whose symptom a notification carries.
LABEL_NAMESPACE, LABEL_CANARY, LABEL_SEVERITY = "namespace", "canary", "severity"
LABEL_ALERTNAME, REQUIRED_SEVERITY, ANNOTATION_EVIDENCE = "alertname", "critical", "recovery_evidence"
# What the entry cap may never evict: Go emits map keys sorted, so wire order is
# alphabetical, and a production alert carries every metric label as well as the rule's.
KEPT_LABELS = (LABEL_ALERTNAME, LABEL_NAMESPACE, LABEL_CANARY, LABEL_SEVERITY)
KEPT_ANNOTATIONS = (ANNOTATION_EVIDENCE,)

HOLD, RECORDED, DUPLICATE, PROPOSE_CORRECTION = "Hold", "Recorded", "Duplicate", "ProposeCorrection"

HOLD_UNATTRIBUTED = "unattributed"  # no identity labels, or not this pilot's canary
HOLD_INCOMPLETE_NOTIFICATION = "incomplete-notification"  # Alertmanager truncated the group
HOLD_RESOLVED = "resolved"  # a recorded resolution says the symptom already stopped
HOLD_ROLLOUT_IN_PROGRESS = "rollout-in-progress"  # Flagger owns a rollout while it runs
HOLD_NO_PROMOTED_RECORD = "no-promoted-record"  # nothing recorded the promotion
HOLD_REVISION_MISMATCH = "revision-mismatch"  # the record and Flux name different revisions
HOLD_NO_PRIOR_PROMOTED = "no-prior-promoted"  # there is nothing to restore to
HOLD_INSUFFICIENT_EVIDENCE = "insufficient-evidence"  # the alert corroborates nothing (AC2)
HOLD_REQUIRES_DECISION = "requires-decision"  # the proposal names no correction at all
HOLD_HEALTH_UNKNOWN = "health-unknown"  # something could not be read at all (AC4)
HOLD_REASONS = (HOLD_UNATTRIBUTED, HOLD_INCOMPLETE_NOTIFICATION, HOLD_RESOLVED,
                HOLD_ROLLOUT_IN_PROGRESS, HOLD_NO_PROMOTED_RECORD, HOLD_REVISION_MISMATCH,
                HOLD_NO_PRIOR_PROMOTED, HOLD_INSUFFICIENT_EVIDENCE, HOLD_REQUIRES_DECISION,
                HOLD_HEALTH_UNKNOWN)
# The only reasons a sweep re-evaluates: each can clear with nothing arriving here, and
# every other reason can only re-derive itself.
CLEARABLE_HOLDS = (HOLD_ROLLOUT_IN_PROGRESS, HOLD_NO_PROMOTED_RECORD, HOLD_REVISION_MISMATCH,
                   HOLD_HEALTH_UNKNOWN)
SWEEP_WINDOW_SECONDS = 24 * 3600
# How far past this receiver's clock a resolution's ``endsAt`` may sit and still be read as
# cover. A real one is the instant the symptom stopped, never meaningfully in the future —
# only skew is. Unbounded, one ``endsAt: 2099-01-01`` gags that fingerprint for ever.
RESOLUTION_HORIZON_SECONDS = 300
FUNCTIONAL_NOT_CONSULTED, FUNCTIONAL_PASSING, FUNCTIONAL_FAILING = "not-consulted", "passing", "failing"

# "Could not be read", enumerated rather than a bare ``Exception``: the API client's error,
# the store's and the transport's (``URLError``/``TimeoutError`` are ``OSError``). A bug is
# none of these — an ``AttributeError`` or a ``KeyError`` reaches the handler's 500, where
# Alertmanager retries safely, rather than a permanent hold that looks like an outage.
_UNREADABLE = (ApiError, ApiWriteError, OSError)

class MalformedAlert(Exception):
    """The request was JSON but not an Alertmanager v4 webhook notification."""

def Hold(reason: str) -> Decision:  # noqa: N802 - a Decision constructor, as in decide.py
    return Decision(kind=HOLD, reason=reason)

def _at(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):  # a naive year-1 stamp overflows the shift
        return None

def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_FIELD_LENGTH:
        raise MalformedAlert(f"{field!r} must be a non-empty string of <= {MAX_FIELD_LENGTH} chars")
    return value.strip()

def _timestamp(value: Any, field: str, *, required: bool = True) -> str:
    """An RFC3339 stamp, refused rather than stored: ``startsAt`` is part of the alert's
    identity and ``endsAt`` decides whether a resolution covers a firing."""
    if value is None and not required:
        return ""
    text = _text(value, field)
    if _at(text) is None:
        raise MalformedAlert(f"{field!r} must be an RFC3339 timestamp")
    return text

def _string_map(raw: Any, field: str, keep: Sequence[str] = ()) -> dict[str, str]:
    """Bounded as ``inbox._metadata`` bounds metadata, except that here the cap decides
    *attribution* too: ``keep`` is taken first, then the remainder sorted before it is cut. So
    unlike ``_metadata`` it reads the map in full — a kept name may sort anywhere — which
    ``MAX_BODY_BYTES`` bounds, an oversized notification being refused before this runs."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise MalformedAlert(f"{field!r} must be an object")
    items = {str(key)[:MAX_FIELD_LENGTH]: str(value)[:MAX_FIELD_LENGTH] for key, value in raw.items()}
    bounded = {name: items[name] for name in keep if name in items}
    for key, value in sorted(items.items()):
        if len(bounded) >= MAX_METADATA_ENTRIES:
            break
        bounded.setdefault(key, value)
    return dict(sorted(bounded.items()))

@dataclasses.dataclass(frozen=True)
class Alert:
    """One bounded entry of a v4 notification's ``alerts[]``."""

    status: str
    fingerprint: str
    starts_at: str
    labels: Mapping[str, str]
    annotations: Mapping[str, str]
    ends_at: str = ""

    @property
    def key(self) -> str:
        """``(fingerprint, startsAt, status)``: one fingerprint covers a firing alert and
        its own resolution, and the fingerprint alone would collapse the two."""
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

def parse(payload: Mapping[str, Any]) -> tuple[str, tuple[Alert, ...], bool]:
    """A v4 notification as ``(groupKey, alerts, truncated)``. ``version`` must be the
    *string* ``"4"``, not something that stringifies to it; any other shape raises."""
    status = payload.get("status")
    if payload.get("version") != ALERT_VERSION or status not in _ALERT_STATUSES:
        # Clamped and encoded like every other caller-supplied value here: a 400's detail
        # is a response body, not a place to reflect an oversized field back.
        raise MalformedAlert(f"not a v4 {_ALERT_STATUSES} notification: "
                             f"version={log_field(str(payload.get('version')))} "
                             f"status={log_field(str(status))}")
    entries = payload.get("alerts")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_ALERTS:
        raise MalformedAlert(f"'alerts' must be a list of 1..{MAX_ALERTS} objects")
    alerts = []
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("status") not in _ALERT_STATUSES:
            raise MalformedAlert(f"every alert must be an object with a {_ALERT_STATUSES} status")
        if status == STATUS_RESOLVED and entry.get("status") != STATUS_RESOLVED:
            # A group is firing when *any* member fires, so a firing notification carrying
            # resolved members is ordinary; the reverse only a forger can produce.
            raise MalformedAlert("a resolved notification may not carry a firing alert")
        alerts.append(Alert(status=str(entry["status"]),
                            fingerprint=_text(entry.get("fingerprint"), "fingerprint"),
                            starts_at=_timestamp(entry.get("startsAt"), "startsAt"),
                            # Required on a resolution: an absent end time must not cover a
                            ends_at=_timestamp(entry.get("endsAt"), "endsAt",  # firing for ever
                                               required=entry["status"] == STATUS_RESOLVED),
                            labels=_string_map(entry.get("labels"), "labels", KEPT_LABELS),
                            annotations=_string_map(entry.get("annotations"), "annotations",
                                                    KEPT_ANNOTATIONS)))
    return (str(payload.get("groupKey") or "")[:MAX_FIELD_LENGTH], tuple(alerts),
            bool(payload.get("truncatedAlerts")))

@dataclasses.dataclass(frozen=True)
class AlertSweepReport:
    """What one pass saw, derived from live state and never stored — a held alert's record
    is create-only, so a cleared hold is found by re-running the ladder."""

    alerts_held: int = 0
    alerts_resolved: int = 0
    proposals_written: int = 0
    alerts_unreadable: int = 0  # this pass could not read one alert's inputs; the rest ran
    alerts_sweep_failed: int = 0
    seconds_since_previous_start: Optional[float] = None

def _seconds_between(earlier: Optional[str], later: str) -> Optional[float]:
    """``None`` when either end is missing or unreadable, and when the difference comes out
    negative (clock skew between nodes, a second replica writing while this one starts) —
    never ``0.0``, which reads as "nothing was missed" for a window nobody could measure."""
    start, end = _at(earlier), _at(later)
    if start is None or end is None:
        return None
    seconds = (end - start).total_seconds()
    return None if seconds < 0 else seconds

def _alert_from(document: Document) -> Optional[Alert]:
    """The alert a held document recorded, so the sweep re-runs one ladder over the same
    inputs. ``None`` if a field is missing: stay held, never guess."""
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
    """``POST /hooks/alert``, end to end: parse, decide, record, sweep. ``live_for`` answers
    a ``decide.LiveState`` (``kube.LiveCanary``) and ``revision_for`` a
    ``kube.LiveKustomization`` — factories, since each caches its own reads."""

    def __init__(self, store: Any, live_for: Callable[[str, str], Any],
                 revision_for: Callable[[], Any], *, canary_namespace: str, canary_name: str,
                 clock: Callable[[], str] = now, holder: str = "") -> None:
        self._store, self._live_for, self._revision_for, self._clock = store, live_for, revision_for, clock
        # The one canary this receiver serves: an alert whose labels name anything else is
        # unattributed by definition and reaches no live read. Those values are caller-supplied.
        self._pilot = (canary_namespace, canary_name)
        self.holder = holder or os.environ.get("HOSTNAME") or f"recovery-receiver-pid-{os.getpid()}"
        self.started_at = clock()  # this process's start, not the first sweep's
        self._run_written = False
        self.alerts_sweep_failed = 0
        self.seconds_since_previous_start: Optional[float] = None

    def handle(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """The route body, after ``Receiver`` admitted and authenticated it. A grouped
        notification splits here; every outcome is 202, a hold being a decision, not a retry."""
        try:
            group_key, alerts, truncated = parse(payload)
        except MalformedAlert as exc:
            return 400, {"result": "MalformedAlert", "detail": str(exc)}
        answers = [self._one(alert, group_key, truncated) for alert in alerts]
        return 202, {"result": "Accepted", "alerts": answers,
                     "not_stored": sum(1 for answer in answers if not answer["stored"])}

    def _one(self, alert: Alert, group_key: str, truncated: bool = False) -> dict[str, Any]:
        answer = {"fingerprint": alert.fingerprint, "alert": alert.key, "stored": False}
        if alert.canary != self._pilot:
            return self._refuse(answer, alert, HOLD_UNATTRIBUTED)
        if truncated and alert.status == STATUS_FIRING:
            # Alertmanager dropped members of this group, so the corroboration the ladder
            # rests on is partial. A resolution is still recorded below: withholding one
            # could only let a later proposal through, which is the wrong direction.
            return self._refuse(answer, alert, HOLD_INCOMPLETE_NOTIFICATION)
        try:  # the duplicate probe and the resolution index are live reads like any other
            seen = self._store.get_document(alert.kind, alert.key) is not None
            covered = not seen and alert.status == STATUS_FIRING and self._resolution_covers(alert)
        except _UNREADABLE as exc:
            return self._refuse(answer, alert, self._unreadable(alert, exc))
        if seen:
            self._log(alert, DUPLICATE, "")  # the ladder already ran on this exact alert
            return {**answer, "result": DUPLICATE}
        if alert.status != STATUS_FIRING:
            # AC1: a resolution is evidence the symptom stopped, never about a revision.
            self._record(alert, group_key, Decision(kind=RECORDED, reason=STATUS_RESOLVED), {})
            self._log(alert, RECORDED, STATUS_RESOLVED)
            return {**answer, "stored": True, "result": RECORDED}
        decision, evidence = ((Hold(HOLD_RESOLVED), {"functional_check": FUNCTIONAL_NOT_CONSULTED})
                              if covered else self._evaluate(alert, self._snapshot()))
        # The ladder took time and this is a threading server, so ask once more: it narrows
        # the race to the store write (see ``_resolution_covers``). An unreadable store
        # raises here, which is the 500 a failed write would have been anyway.
        if decision.proposal is not None and self._resolution_covers(alert):
            decision, evidence = Hold(HOLD_RESOLVED), {"functional_check": FUNCTIONAL_NOT_CONSULTED}
        # Before the alert is marked seen, as ``Receiver._register`` writes its record first:
        # a failed proposal write must reach the 500 and leave the alert unrecorded, so a
        # redelivery re-runs the ladder instead of answering ``Duplicate`` for nothing.
        if decision.proposal is not None:
            self._store.put_document(decision.proposal.to_document())
        self._record(alert, group_key, decision, evidence)
        key = "" if decision.proposal is None else decision.proposal.key
        self._log(alert, decision.kind, decision.reason, key)
        return {**answer, "stored": True, "result": decision.kind,
                "detail": decision.reason, "proposal": key}

    def _refuse(self, answer: dict[str, Any], alert: Alert, reason: str) -> dict[str, Any]:
        """A hold that stores nothing. Only alerts this pilot can claim become documents: the
        key is caller-supplied, and nothing else may grow the store."""
        self._log(alert, HOLD, reason)
        return {**answer, "result": HOLD, "detail": reason, "proposal": ""}

    def _unreadable(self, alert: Alert, exc: Exception) -> str:
        # The class only: the message carries the request path and response body.
        LOG.warning("alert %s: %s (%s)", log_field(alert.fingerprint),
                    HOLD_HEALTH_UNKNOWN, type(exc).__name__)
        return HOLD_HEALTH_UNKNOWN

    def _snapshot(self) -> tuple[Callable[[], Any], Callable[[], Any]]:
        """One live view: each reader caches its own read, so a decision is made against one
        snapshot. ``_one`` builds one per request; the sweep one per *pass*, not per alert."""
        return (functools.lru_cache(maxsize=1)(lambda: self._live_for(*self._pilot)),
                functools.lru_cache(maxsize=1)(self._revision_for))

    def _resolution_covers(self, alert: Alert) -> bool:
        """Whether a recorded resolution says this firing's symptom has already stopped: an
        ``alert-resolved`` document for the same fingerprint whose ``endsAt`` is not before
        this alert's ``startsAt`` and not more than ``RESOLUTION_HORIZON_SECONDS`` past this
        receiver's own clock. The firing looks, rather than the resolution arriving to
        undo something — out-of-order delivery is ordinary, since Alertmanager retries a
        notification whose POST timed out. An unreadable ``endsAt`` counts as covering.
        ``_one`` asks twice, the second time immediately before the proposal write: that
        **narrows** the window to the store write, and does not close it. Nothing is atomic."""
        starts_at, horizon = _at(alert.starts_at), _at(self._clock())
        for document in self._store.list_documents(
                KIND_ALERT_RESOLVED, labels={FINGERPRINT_LABEL: label_value(alert.fingerprint)}):
            if str(document.payload.get("fingerprint") or "") != alert.fingerprint:
                continue  # ``label_value`` clamps, so the label is an index, never the proof
            ends_at = _at(document.payload.get("ends_at"))
            if (horizon is not None and ends_at is not None
                    and (ends_at - horizon).total_seconds() > RESOLUTION_HORIZON_SECONDS):
                continue  # dated past anything a real resolution can mean
            if starts_at is None or ends_at is None or ends_at >= starts_at:
                return True
        return False

    def _evaluate(self, alert: Alert,
                  live_view: tuple[Callable[[], Any], Callable[[], Any]]) -> tuple[Decision, dict[str, Any]]:
        """The ladder. Every live read sits inside the one ``try``, so "unreadable is
        ``Hold(health-unknown)``, never a proposal" is provable by inspection."""
        evidence: dict[str, Any] = {"functional_check": FUNCTIONAL_NOT_CONSULTED}
        if alert.canary != self._pilot:
            return Hold(HOLD_UNATTRIBUTED), evidence
        canary_of, revision_of = live_view
        namespace, canary_name = self._pilot
        try:
            live = canary_of()
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
            # ``None`` for an absent or malformed ``lastAppliedRevision`` (one parser of
            # that field, ``identity.revision_sha``): unreadable is unknown health, not a
            # mismatch against a string nobody validated.
            applied_sha = revision_of().source_sha()
            evidence["applied_source_sha"] = applied_sha or ""
            if not applied_sha:
                return Hold(HOLD_HEALTH_UNKNOWN), evidence
            if applied_sha != promoted.identity.source_sha:
                # Something moved unseen: which revision the symptom belongs to is not
                # establishable, and guessing is what AC2 forbids.
                return Hold(HOLD_REVISION_MISMATCH), evidence
            previous = self._previous_promoted(namespace, canary_name, promoted)
            if previous is None:
                return Hold(HOLD_NO_PRIOR_PROMOTED), evidence
            evidence["restore_source_sha"] = previous.identity.source_sha
            # Recorded, never gating: ``LiveCanary`` answers ``None`` (the apex's own check
            # has no path back here), which is neither a pass nor a failure.
            check = live.functional_check()
            evidence["functional_check"] = (FUNCTIONAL_NOT_CONSULTED if check is None else
                                            FUNCTIONAL_PASSING if check else FUNCTIONAL_FAILING)
        except _UNREADABLE as exc:
            return Hold(self._unreadable(alert, exc)), evidence

        # Causal corroboration: the alert must name the expression that fired and be
        # severe enough to correct. Both come from the rule, never the clock (AC2).
        expression = alert.annotations.get(ANNOTATION_EVIDENCE, "").strip()
        severity = alert.labels.get(LABEL_SEVERITY, "")
        evidence["recovery_evidence"], evidence["severity"] = expression, severity
        if not expression or severity != REQUIRED_SEVERITY:
            return Hold(HOLD_INSUFFICIENT_EVIDENCE), evidence
        # The promoted revision is the failed one here; restore the one before it.
        proposal = build_proposal(promoted.identity, previous.identity, phase=PHASE_ALERT_PROPOSAL)
        if proposal.requires_decision:
            # The ladder has already proved the promoted record and the applied revision, so
            # a proposal naming no correction means the record is malformed — a hold, not a
            # success-shaped answer nobody can act on.
            evidence["requires_decision"] = proposal.decision_reason
            return Hold(HOLD_REQUIRES_DECISION), evidence
        return Decision(kind=PROPOSE_CORRECTION, reason=proposal.summary, proposal=proposal), evidence

    def _previous_promoted(self, namespace: str, canary_name: str,
                           current: DeploymentRecord) -> Optional[DeploymentRecord]:
        """The promotion before ``current`` — what a correction restores to. By the records'
        own ``created_at``, never "the newest of any phase": a candidate registered after
        this promotion is newer, and is exactly what must not win."""
        earlier = [record for record in self._store.list(canary_label(namespace, canary_name))
                   if record.phase == PHASE_PROMOTED
                   and record.identity.template_hash != current.identity.template_hash
                   and record.created_at < current.created_at and record.identity.source_sha]
        return max(earlier, key=lambda record: record.created_at, default=None)

    def _record(self, alert: Alert, group_key: str, decision: Decision,
                evidence: Mapping[str, Any]) -> PutResult:
        return self._store.put_document(Document(
            kind=alert.kind, key=alert.key,
            # The canary is the configured one: only an alert naming it gets this far. The
            # rest are clamped — a label the API server rejects would 500 a recordable alert.
            labels={"flagger-recovery/canary": canary_label(*self._pilot),
                    "flagger-recovery/alert-status": alert.status,
                    # The index a firing alert finds its own resolution by.
                    FINGERPRINT_LABEL: label_value(alert.fingerprint),
                    "flagger-recovery/decision": label_value(decision.kind)},
            payload={"fingerprint": alert.fingerprint, "starts_at": alert.starts_at,
                     "ends_at": alert.ends_at, "status": alert.status, "group_key": group_key,
                     "labels": dict(alert.labels), "annotations": dict(alert.annotations),
                     "received_at": self._clock(), "decision": decision.kind,
                     "reason": decision.reason, "evidence": dict(evidence),
                     "proposal": "" if decision.proposal is None else decision.proposal.key}))

    def sweep(self) -> AlertSweepReport:
        """Re-runs the ladder over the holds that can still change: reason in
        ``CLEARABLE_HOLDS`` (a rollout finishes, a record lands, Flux catches up), received
        within ``SWEEP_WINDOW_SECONDS``, and not since resolved. A terminal reason re-derives
        only itself and nothing escalates a hold for being old, so neither is read again, and
        one live view serves the pass. A cleared hold writes through the in-band key (AC3).

        Per alert, not per pass: inputs nothing here can read — a malformed stored object,
        a store that stops answering — cost that one alert its re-evaluation and are
        counted, never the other held alerts theirs."""
        self._begin_run()
        counts = dict.fromkeys(("alerts_held", "alerts_resolved", "proposals_written",
                                "alerts_unreadable"), 0)
        live_view, at = self._snapshot(), self._clock()
        # The pilot's own alerts: the selector bounds what one pass lists, rather than every
        # ``alert`` document ever written reaching the filters below. Tolerant: one document
        # that never decoded must not stop the rest from being re-evaluated (R19's residual).
        documents, unreadable_names = self._store.list_documents_tolerant(
            KIND_ALERT, canary=canary_label(*self._pilot))
        for name in unreadable_names:
            counts["alerts_unreadable"] += 1
            LOG.warning("alert sweep: %s is unreadable (MalformedRecord); the pass continues",
                        log_field(name))
        for document in documents:
            try:  # one alert's reads, and nothing else, under this guard
                payload = document.payload
                if not isinstance(payload, Mapping):
                    # Live only for a store that hands a document back undecoded
                    # (``InMemoryStore``): ``ConfigMapStore``'s tolerant listing above already
                    # proved every document here is a mapping, via ``Document.from_configmap``.
                    raise MalformedRecord(document.name, TypeError("alert payload is not an object"))
                if payload.get("decision") != HOLD:
                    continue
                alert = _alert_from(document)
                age = _seconds_between(str(payload.get("received_at") or ""), at)
                if (payload.get("reason") not in CLEARABLE_HOLDS or alert is None or age is None
                        or age > SWEEP_WINDOW_SECONDS or self._resolution_covers(alert)):
                    counts["alerts_held"] += 1
                    continue
                decision = self._evaluate(alert, live_view)[0]
                if decision.proposal is None:
                    counts["alerts_held"] += 1
                    continue
                counts["alerts_resolved"] += 1
                if self._store.put_document(decision.proposal.to_document()) is PutResult.CREATED:
                    counts["proposals_written"] += 1
                    LOG.info("alert sweep: hold %s cleared for %s; proposal %s",
                             log_field(str(payload.get("reason") or "")),
                             log_field(str(payload.get("fingerprint") or "")),
                             log_field(decision.proposal.key))
            except _UNREADABLE as exc:
                counts["alerts_unreadable"] += 1
                # Names and a class, never a message: an ``ApiError``'s carries URL and body.
                LOG.warning("alert sweep: %s is unreadable (%s on %s); the pass continues",
                            log_field(document.name), type(exc).__name__,
                            log_field(str(getattr(exc, "name", ""))))
        return AlertSweepReport(seconds_since_previous_start=self.seconds_since_previous_start,
                                alerts_sweep_failed=self.alerts_sweep_failed, **counts)

    def sweep_safely(self) -> AlertSweepReport:
        """The sweep as the reconcile loop runs it. One pass covers both paths, and an
        alert-side read failure must not take the rollout path's down with it: that is where
        delayed promotions land and where a refused correction is re-offered."""
        try:
            return self.sweep()
        except Exception:  # noqa: BLE001 - the rollout path must not lose a tick to this one
            self.alerts_sweep_failed += 1
            LOG.exception("alert sweep failed")
            return AlertSweepReport(alerts_sweep_failed=self.alerts_sweep_failed,
                                    seconds_since_previous_start=self.seconds_since_previous_start)

    def _begin_run(self) -> None:
        """The ``receiver-run`` document, written once per process on the first sweep (which
        the loop runs at startup). The store is create-only, so there is no per-tick document
        and no honest measure of downtime: this reports the gap to the *previous run's start*,
        which is why the field says that and not "interruption"."""
        if self._run_written:
            return
        self._run_written = True  # before the write: a store that refuses it must not
        # make every later sweep re-attempt it, and the window is this start either way.
        previous = self._previous_start()
        self.seconds_since_previous_start = _seconds_between(previous, self.started_at)
        self._store.put_document(Document(
            kind=KIND_RECEIVER_RUN, labels={},
            # The holder is in the key material as ``Lease`` puts it there: a rolling update
            # starts the new pod as the old one stops, and ``now()`` resolves to seconds.
            key=hashlib.sha256(f"{self.started_at}|{self.holder}".encode("utf-8")).hexdigest()[:32],
            payload={"started_at": self.started_at, "pod": self.holder,
                     "previous_started_at": previous or "",
                     "seconds_since_previous_start": self.seconds_since_previous_start}))

    def _previous_start(self) -> Optional[str]:
        """The newest start this receiver recorded before this one. Runs only — never an
        alert's ``received_at``, which says when work arrived, not when a process was alive."""
        stamps = [str(document.payload.get("started_at") or "")
                  for document in self._store.list_documents(KIND_RECEIVER_RUN)]
        return max((stamp for stamp in stamps if stamp and stamp < self.started_at), default=None)

    def _log(self, alert: Alert, result: str, reason: str, proposal: str = "") -> None:
        """One line per alert, mirroring ``Receiver._log_decision``. Every field comes from the
        payload, so every one is clamped and JSON-quoted: a forged ``result=`` cannot split it."""
        LOG.info("hook=alert fingerprint=%s status=%s canary=%s result=%s reason=%s proposal=%s",
                 log_field(alert.fingerprint), log_field(alert.status),
                 log_field("/".join(alert.canary) if alert.canary else ""),
                 log_field(result), log_field(reason), log_field(proposal))
