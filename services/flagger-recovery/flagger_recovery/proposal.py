"""The correction proposal: what a failed pilot release should be changed back
to, and by which of exactly two moves.

The bounds are hard-coded, per ``design.md``'s "Branch and path bounds": ref
``flagger-pilot``, path prefix ``kubernetes/pilot/flagger-pilot/``, one file. A
situation that cannot be expressed inside them sets ``requires_decision`` and
names no correction at all — FRP-007 (the Git writer) refuses such a proposal
rather than trimming it to fit. Building one writes nothing; the caller stores it
through the same create-only ``Document`` path as every other record, keyed on the
candidate identity, so a repeated ``Failed`` hook produces only one.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Optional

from .identity import CandidateIdentity
from .policy import ALLOWED_REPOSITORY
from .record import Document, canary_label, label_value, make_key_parts

# The one repository a correction may name, so ``policy.evaluate`` can cross-check the
# proposal's own field rather than only the writer's constructor argument.
REPOSITORY = ALLOWED_REPOSITORY
BRANCH = "flagger-pilot"
PATH_PREFIX = "kubernetes/pilot/flagger-pilot/"
HELMRELEASE_PATH = PATH_PREFIX + "helmrelease.yaml"
KIND_PROPOSAL = "proposal"
# The key's last part. A rollout failure is keyed ``proposal`` under the *failed
# candidate's* template hash; the post-promotion alert path (FRP-008a) is keyed
# ``alert-proposal`` under the *promoted* hash. Both are ``kind=proposal``
# documents of one shape, so the writer path and ``policy.evaluate`` see one
# thing; only the key differs, which keeps the two from ever colliding.
PHASE_ALERT_PROPOSAL = "alert-proposal"

# Why no correction can be established, and a human has to decide instead.
NEEDS_PROMOTED_REVISION = "no last promoted source revision is recorded"
NEEDS_ALLOWED_BRANCH = f"a source revision is not on {BRANCH}"
NEEDS_USABLE_SHA = "a source revision is not a full 40-character sha"
NEEDS_DISTINCT_REVISIONS = "the failed and promoted source revisions are identical"

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

@dataclasses.dataclass(frozen=True)
class Proposal:
    """One correction, for one candidate identity. ``correction`` is the whole
    instruction FRP-007 carries out — exactly one of ``revert-commit <sha>`` or
    ``restore-file <path> to <sha>``, and ``None`` when ``requires_decision``
    is set. The two are never both populated."""

    namespace: str
    canary_name: str
    template_hash: str  # the failed candidate's Canary status.lastAppliedSpec
    failed_source_sha: str
    last_promoted_source_sha: Optional[str]
    failed_identity: CandidateIdentity
    promoted_identity: Optional[CandidateIdentity]
    correction: Optional[str]
    requires_decision: bool
    decision_reason: str
    ahead_by: Optional[int]  # commits the failed revision is ahead of the promoted one
    phase: str = KIND_PROPOSAL  # which path built this; see PHASE_ALERT_PROPOSAL

    @property
    def key(self) -> str:
        """One per candidate identity and path, so every repeated or delayed
        hook — or alert — about the same template hash lands on one document."""
        return make_key_parts(self.namespace, self.canary_name, self.template_hash, self.phase)

    @property
    def summary(self) -> str:
        return self.correction or f"requires-decision: {self.decision_reason}"

    def to_payload(self) -> dict[str, Any]:
        # asdict() recurses through both identities, so the payload carries
        # every field of each, as the record ConfigMaps store them.
        return {"repository": REPOSITORY, "branch": BRANCH, "path_prefix": PATH_PREFIX,
                **dataclasses.asdict(self)}

    def to_document(self) -> Document:
        return Document(
            kind=KIND_PROPOSAL,
            key=self.key,
            labels={
                "flagger-recovery/canary": canary_label(self.namespace, self.canary_name),
                "flagger-recovery/template-hash": label_value(self.template_hash or "none"),
                "flagger-recovery/requires-decision": "true" if self.requires_decision else "false",
            },
            payload=self.to_payload(),
        )

def _blocked(failed: CandidateIdentity, promoted: Optional[CandidateIdentity]) -> Optional[str]:
    """Why no correction can be established, or ``None`` when one can."""
    if promoted is None or not promoted.source_sha:
        return NEEDS_PROMOTED_REVISION
    if failed.source_branch != BRANCH or promoted.source_branch != BRANCH:
        return NEEDS_ALLOWED_BRANCH
    if not _SHA_RE.match(failed.source_sha or "") or not _SHA_RE.match(promoted.source_sha):
        return NEEDS_USABLE_SHA
    if failed.source_sha == promoted.source_sha:
        return NEEDS_DISTINCT_REVISIONS
    return None

def build_proposal(failed: CandidateIdentity, promoted: Optional[CandidateIdentity] = None, *,
                   ahead_by: Optional[int] = None, phase: str = KIND_PROPOSAL) -> Proposal:
    """Build the proposal for ``failed``, correcting back to ``promoted``.
    ``ahead_by`` is how many commits the failed source revision is ahead of the
    promoted one, when the caller can establish it. A revert is only a safe
    single move at a distance of exactly 1; at any other distance — and when it
    is unknown, the receiver's own case, since it holds no Git credential — the
    establishable correction is to restore the one file in bounds. Only when
    neither can be established is ``requires_decision`` set."""
    blocked = _blocked(failed, promoted)
    correction = None
    if blocked is None and promoted is not None:  # _blocked() rejects a None promoted identity
        correction = (
            f"revert-commit {failed.source_sha}"
            if ahead_by == 1
            else f"restore-file {HELMRELEASE_PATH} to {promoted.source_sha}"
        )
    return Proposal(
        namespace=failed.namespace, canary_name=failed.canary_name, template_hash=failed.template_hash,
        failed_source_sha=failed.source_sha, failed_identity=failed, promoted_identity=promoted,
        last_promoted_source_sha=None if promoted is None else promoted.source_sha,
        correction=correction, requires_decision=blocked is not None, decision_reason=blocked or "",
        ahead_by=ahead_by, phase=phase,
    )
