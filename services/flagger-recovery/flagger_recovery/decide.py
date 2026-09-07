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
import logging
from typing import Any, Callable, Mapping, Optional, Protocol

from .identity import is_manual_rollback
from .inbox import KIND_EVENT, STATUS_ATTRIBUTION_PENDING, WebhookEvent
from .policy import KIND_CORRECTION, STATUS_IN_PROGRESS, is_stale
from .proposal import KIND_PROPOSAL, Proposal, build_proposal
from .record import DeploymentRecord, PutResult, canary_label, make_key_parts
from .server import PHASE_PROMOTED, Decision, Ignore, now, write_promoted_record

LOG = logging.getLogger("flagger_recovery.decide")

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

    # Decided first: at promotion time lastPromotedSpec has just become the
    # candidate's own hash, so the F8 test below would read a fresh success as
    # a manual rollback.
    if event.hook == "post-rollout" and event.phase == "Succeeded":
        return Register()
    if not event.checksum:
        return Ignore(REASON_NO_CHECKSUM)
    if event.hook != "post-rollout" or event.phase != "Failed":
        return Ignore(REASON_NOT_A_TERMINAL_FAILURE)
    if record is None:
        return Refuse(REASON_NO_CANDIDATE_RECORD)

    # From here on the comparisons are all in the template-hash space
    # (``status.lastAppliedSpec``). ``event.checksum`` is a hash *of* that value
    # (``record.checksum_label``) and never equals it, so it is only ever used
    # to find the record — the record is what says which spec failed.
    template_hash = record.identity.template_hash
    # F8. A hook about a spec that is already the promoted one describes a
    # release that is serving again, whatever status.phase still says.
    if is_manual_rollback(status, template_hash):
        return Ignore(REASON_MANUAL_ROLLBACK)
    # Superseded: something newer has been applied since, so correcting this
    # failure would revert work that has nothing to do with it.
    if status.get("lastAppliedSpec") != template_hash:
        return Ignore(REASON_SUPERSEDED)
    live_hash = live.deployment_template_hash()
    if not live_hash:
        return Refuse(REASON_TARGET_SPEC_UNKNOWN)
    if live_hash != template_hash:
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
    events_unattributable: int = 0
    proposals_written: int = 0
    proposals_open: int = 0
    proposals_superseded: int = 0
    corrections_stale: int = 0

def reconcile(store: Any, live: LiveState, *, canary: Optional[str] = None,
              corrector: Optional[Callable[[Mapping[str, Any]], Any]] = None) -> ReconcileReport:
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
            record = store.find_candidate(canary_label(event.namespace, event.name), event.checksum)
        if record is None:
            # A ``pre-rollout`` is retried in band — Flagger redelivers it, the
            # receiver re-runs resolution and writes the record, and the next
            # pass finds it here. Any other hook is a one-shot: the rollout it
            # belongs to is over, nothing will create a candidate record for it
            # now, and re-checking it every five minutes forever is not
            # progress. Refuse it out loud instead, once per pass.
            if event.hook == "pre-rollout":
                counts["events_pending"] += 1
            else:
                counts["events_unattributable"] += 1
                # %r on phase and checksum, not %s: unlike ``hook`` they are
                # payload fields the inbox only strips and bounds, so repr's
                # escaping is what stops a caller forging a log line.
                LOG.warning(
                    "reconcile: refusing %s hook, phase %r, checksum %r: no candidate record is "
                    "indexed under that checksum, and none can be created for a finished rollout",
                    event.hook, event.phase, event.checksum,
                )
            continue
        counts["events_resolved"] += 1
        decision = decide(record, event, live, promoted=promoted_record(store, event, live))
        # A ``Register`` here is a delayed promotion: the candidate record
        # only just landed, so nothing wrote the ``promoted`` record when the
        # event first arrived (canary-matrix.md F8's promotion path is the
        # only other writer, and it never saw this event). Without this, the
        # next failure's ``promoted_record()`` lookup finds nothing and its
        # proposal degrades to ``requires_decision``.
        if decision.kind == "Register":
            write_promoted_record(store, record.identity, now, checksum=record.checksum)
        elif decision.proposal is not None:
            if store.put_document(decision.proposal.to_document()) is PutResult.CREATED:
                counts["proposals_written"] += 1
                if corrector is not None:
                    corrector(decision.proposal.to_payload())

    for document in store.list_documents(KIND_PROPOSAL, canary=canary):
        failed_hash = str(document.payload.get("template_hash") or "")
        settled = status.get("lastAppliedSpec") != failed_hash or is_manual_rollback(status, failed_hash)
        counts["proposals_superseded" if settled else "proposals_open"] += 1

    # Create-only storage means a writer that died mid-write leaves its
    # ``in-progress`` document forever. Report those and stop: a retry would re-enter
    # a write whose outcome nobody established, which wants a human on the branch.
    for document in store.list_documents(KIND_CORRECTION, canary=canary):
        if document.payload.get("status") == STATUS_IN_PROGRESS and is_stale(document.payload, now()):
            counts["corrections_stale"] += 1

    return ReconcileReport(**counts)
