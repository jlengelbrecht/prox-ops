"""The decision function, and the reconcile pass that re-runs it.

``decide()`` is pure: no I/O, no clock, no store. Everything live arrives
through the ``LiveState`` protocol, so every branch below is reachable from a
test with a four-method fake; ``decider()`` binds a real reader and store to it
for the server. ``status.phase`` is never the success signal (canary-matrix.md
F8): a revert to the last promoted spec runs no analysis and leaves the Canary
``Failed`` forever, so phase-driven recovery would propose a second correction
for a failure that no longer exists. ``is_manual_rollback`` is therefore checked
before any failure handling, and a failure is only proposed against once the live
serving state is confirmed on both sides — the target still carries the failed
spec, and the primary is serving the promoted one.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Mapping, Optional, Protocol

from .identity import is_manual_rollback
from .inbox import KIND_EVENT, STATUS_ATTRIBUTION_PENDING, WebhookEvent
from .proposal import KIND_PROPOSAL, Proposal, build_proposal
from .record import DeploymentRecord, make_key_parts
from .server import PHASE_CANDIDATE, PHASE_PROMOTED, Decision, Ignore

REASON_MANUAL_ROLLBACK = "manual-rollback-restored"
REASON_NOT_A_TERMINAL_FAILURE = "not-a-terminal-failure"
REASON_NO_CHECKSUM = "no-checksum"
REASON_NO_CANDIDATE_RECORD = "no-candidate-record"
REASON_SUPERSEDED = "superseded"
REASON_TARGET_SPEC_UNKNOWN = "target-spec-unknown"
REASON_NO_PROMOTED_SPEC = "no-promoted-spec"
REASON_PRIMARY_NOT_SERVING = "primary-not-serving-promoted"
REASON_FUNCTIONAL_CHECK_FAILING = "functional-check-failing"

class LiveState(Protocol):
    """The live view of one canary (``kube.LiveCanary`` in production). A hash
    method answers ``None`` for "not being served right now", which ``decide``
    reads as a refusal, never as a licence to reuse the recorded value."""

    def canary_status(self) -> Mapping[str, Any]: ...
    def deployment_template_hash(self) -> Optional[str]: ...
    def primary_template_hash(self) -> Optional[str]: ...
    def functional_check(self) -> Optional[bool]: ...

def Register(reason: str = "promoted") -> Decision:  # noqa: N802
    return Decision(kind="Register", reason=reason)

def ProposeCorrection(proposal: Proposal) -> Decision:  # noqa: N802
    return Decision(kind="ProposeCorrection", reason=proposal.summary, proposal=proposal)

def Refuse(reason: str) -> Decision:  # noqa: N802
    return Decision(kind="Refuse", reason=reason)

def decide(record: Optional[DeploymentRecord], event: WebhookEvent, live: LiveState, *,
           promoted: Optional[DeploymentRecord] = None) -> Decision:
    """Decide what a webhook means. ``record`` is the stored ``candidate``
    record for the event's checksum (``None`` when there is none) and
    ``promoted`` the stored ``promoted`` record for the canary's last promoted
    spec — the revision a correction points back to."""
    status = live.canary_status() or {}
    checksum = event.checksum

    # Decided first: at promotion time lastPromotedSpec has just become the
    # candidate's own hash, so the F8 test below would read a fresh success as
    # a manual rollback.
    if event.hook == "post-rollout" and event.phase == "Succeeded":
        return Register()
    if not checksum:
        return Ignore(REASON_NO_CHECKSUM)
    # F8. A hook about a hash that is already the promoted spec describes a
    # release that is serving again, whatever status.phase still says.
    if is_manual_rollback(status, checksum):
        return Ignore(REASON_MANUAL_ROLLBACK)
    if event.hook != "post-rollout" or event.phase != "Failed":
        return Ignore(REASON_NOT_A_TERMINAL_FAILURE)

    if record is None:
        return Refuse(REASON_NO_CANDIDATE_RECORD)
    # Superseded: something newer has been applied since, so correcting this
    # failure would revert work that has nothing to do with it.
    if checksum != record.identity.template_hash or status.get("lastAppliedSpec") != checksum:
        return Ignore(REASON_SUPERSEDED)
    live_hash = live.deployment_template_hash()
    if not live_hash:
        return Refuse(REASON_TARGET_SPEC_UNKNOWN)
    if live_hash != checksum:
        return Ignore(REASON_SUPERSEDED)

    last_promoted = status.get("lastPromotedSpec")
    if not last_promoted:
        return Refuse(REASON_NO_PROMOTED_SPEC)
    if live.primary_template_hash() != last_promoted:
        return Refuse(REASON_PRIMARY_NOT_SERVING)
    # False means the apex is answering wrongly right now, which is a bigger
    # problem than one bad candidate. None means no prober is wired in, which
    # is the receiver's own case and is not a reason to refuse.
    if live.functional_check() is False:
        return Refuse(REASON_FUNCTIONAL_CHECK_FAILING)

    return ProposeCorrection(
        build_proposal(record.identity, None if promoted is None else promoted.identity)
    )

def promoted_record(store: Any, event: WebhookEvent, live: LiveState) -> Optional[DeploymentRecord]:
    """The stored ``promoted`` record for the canary's current lastPromotedSpec."""
    last_promoted = (live.canary_status() or {}).get("lastPromotedSpec")
    if not last_promoted:
        return None
    return store.get(make_key_parts(event.namespace, event.name, last_promoted, PHASE_PROMOTED))

def decider(store: Any, live_for: Callable[[str, str], LiveState]) -> Callable[..., Decision]:
    """Bind a store and a per-canary live view into ``server.Decider``'s
    two-argument shape, so installing this needs no route change."""

    def _decide(record: Optional[DeploymentRecord], event: WebhookEvent) -> Decision:
        live = live_for(event.namespace, event.name)
        return decide(record, event, live, promoted=promoted_record(store, event, live))

    return _decide

@dataclasses.dataclass(frozen=True)
class ReconcileReport:
    """What one pass saw. It writes at most the proposals that could not be written in-band."""

    events_pending: int = 0
    events_resolved: int = 0
    proposals_written: int = 0
    proposals_open: int = 0
    proposals_superseded: int = 0

def reconcile(store: Any, live: LiveState, *, canary: Optional[str] = None) -> ReconcileReport:
    """Re-evaluate, once, everything the receiver could not finish in-band.
    An **attribution-pending event** whose candidate record has since landed is
    decided here, because nothing decided it when it arrived; one still missing
    its record stays pending, and the retry for that is in-band (a redelivered
    ``pre-rollout`` re-runs resolution rather than answering ``Duplicate``). A
    **proposal** is acknowledged once the failure it describes is no longer live
    — the canary moved on, or F8's manual rollback restored the promoted spec.
    That is derived, never stored, because the document store is create-only.
    Every write reuses its in-band key, so a second pass writes nothing."""
    status = live.canary_status() or {}
    counts = {field.name: 0 for field in dataclasses.fields(ReconcileReport)}

    for document in store.list_documents(KIND_EVENT, canary=canary):
        payload = document.payload
        if payload.get("status") != STATUS_ATTRIBUTION_PENDING:
            continue
        event = WebhookEvent(metadata={}, **{name: str(payload.get(name, ""))
                                             for name in ("hook", "name", "namespace", "phase", "checksum")})
        record = None
        if event.checksum and event.namespace and event.name:
            record = store.get(make_key_parts(event.namespace, event.name, event.checksum, PHASE_CANDIDATE))
        if record is None:
            counts["events_pending"] += 1
            continue
        counts["events_resolved"] += 1
        decision = decide(record, event, live, promoted=promoted_record(store, event, live))
        if decision.proposal is not None:
            store.put_document(decision.proposal.to_document())
            counts["proposals_written"] += 1

    for document in store.list_documents(KIND_PROPOSAL, canary=canary):
        failed_hash = str(document.payload.get("template_hash") or "")
        settled = status.get("lastAppliedSpec") != failed_hash or is_manual_rollback(status, failed_hash)
        counts["proposals_superseded" if settled else "proposals_open"] += 1

    return ReconcileReport(**counts)
