"""What a correction is allowed to be, decided before anything is written.

Every bound is a literal here: the repository, the ref, the path prefix and the one
file the pilot may correct. ``evaluate`` is pure — proposal payload, a live view of
the canary, and (once the Git reads are done) a ``TreeState`` — and answers a
``Verdict``: an allow, or a refusal carrying a reason from the closed set below,
which the README lists. Nothing here opens a socket, reads a file, starts a program
or touches a credential. It is a ladder of refusals with the one allow at the
bottom, run twice: with ``tree=None`` before any network call, so a proposal out of
bounds on its face costs zero GitHub requests, and again with the snapshot, so
every freshness bound is re-read at write time.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from .identity import is_manual_rollback

ALLOWED_REPOSITORY = "jlengelbrecht/prox-ops"
ALLOWED_REF = "refs/heads/flagger-pilot"
ALLOWED_PATH_PREFIX = "kubernetes/pilot/flagger-pilot/"
ALLOWED_TARGETS = ("kubernetes/pilot/flagger-pilot/helmrelease.yaml",)

# The ``branch`` a proposal carries, and the segment GitHub's ref endpoints take.
ALLOWED_BRANCH = ALLOWED_REF[len("refs/heads/"):]
ALLOWED_REF_PATH = ALLOWED_REF[len("refs/"):]

KIND_CORRECTION, KIND_CORRECTION_RESULT = "correction", "correction-result"
STATUS_IN_PROGRESS, STATUS_WRITTEN = "in-progress", "written"
# How long an ``in-progress`` marker may sit before the reconcile pass calls it
# stale. Create-only storage means a writer that dies leaves its marker forever.
STALE_AFTER_SECONDS = 600.0
RESTORE_FILE, REVERT_COMMIT = "restore-file", "revert-commit"

# The closed refusal set; the README says what each one means for an operator.
DISABLED, REQUIRES_DECISION = "corrections-disabled", "requires-decision"
UNPARSABLE, WRONG_REPOSITORY, WRONG_REF = "unparsable-correction", "wrong-repository", "wrong-ref"
PATH_OUTSIDE_PREFIX, NOT_AN_ALLOWED_TARGET = "path-outside-prefix", "not-an-allowed-target"
UNUSABLE_SHA, MIXED_SCOPE, NO_CHANGE = "unusable-sha", "mixed-scope", "no-change"
SUPERSEDED, ALREADY_RESTORED = "superseded", "already-restored"
TARGET_CHANGED, BRANCH_MOVED, ALREADY_CORRECTED = "target-changed", "branch-moved", "already-corrected"
REFUSAL_REASONS = (DISABLED, REQUIRES_DECISION, UNPARSABLE, WRONG_REPOSITORY, WRONG_REF,
                   PATH_OUTSIDE_PREFIX, NOT_AN_ALLOWED_TARGET, UNUSABLE_SHA, MIXED_SCOPE, NO_CHANGE,
                   SUPERSEDED, ALREADY_RESTORED, TARGET_CHANGED, BRANCH_MOVED, ALREADY_CORRECTED)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RESTORE_RE = re.compile(r"^restore-file (\S+) to (\S+)$")
_REVERT_RE = re.compile(r"^revert-commit (\S+)$")

@dataclasses.dataclass(frozen=True)
class Verdict:
    """Allowed, or refused with a reason. ``target_path``/``restore_sha`` are the
    correction reduced to one file and one revision; ``blob_sha`` is that revision's
    blob for that path, which only the second pass can know."""

    allowed: bool
    reason: str = ""
    form: str = ""
    target_path: str = ""
    restore_sha: str = ""
    blob_sha: str = ""

    def __bool__(self) -> bool:
        return self.allowed

def Refuse(reason: str) -> Verdict:  # noqa: N802 - a Verdict constructor, not a function
    return Verdict(allowed=False, reason=reason)

@dataclasses.dataclass(frozen=True)
class TreeState:
    """The Git side of the freshness check, read at write time: every field is a fact
    about the repository now, the proposal being a statement about the past."""

    head_sha: str  # the branch head commit
    head_tree_sha: str  # its tree, the base for the tree we build
    blob_at_head: str  # the target file's blob at the head
    blob_at_failed: str  # ... at the revision the failed release was built from
    blob_at_restore: str  # ... at the revision being restored: the blob we install
    head_is_descendant: bool  # the head is the failed revision, or ahead of it
    paths_since_failed: tuple[str, ...] = ()  # files changed between the two
    paths_undone: tuple[str, ...] = ()  # files the correction reverses

def evaluate(proposal: Mapping[str, Any], live: Any, tree: Optional[TreeState] = None) -> Verdict:
    """Whether ``proposal`` may be written. ``live`` is ``decide``'s four-method
    ``LiveState``; ``tree`` is ``None`` pre-network, a snapshot just before the write."""
    if proposal.get("requires_decision") or not proposal.get("correction"):
        return Refuse(REQUIRES_DECISION)
    if proposal.get("branch") != ALLOWED_BRANCH:
        return Refuse(WRONG_REF)
    if proposal.get("path_prefix") != ALLOWED_PATH_PREFIX:
        return Refuse(PATH_OUTSIDE_PREFIX)
    failed_sha = str(proposal.get("failed_source_sha") or "")
    promoted_sha = str(proposal.get("last_promoted_source_sha") or "")
    if not _SHA_RE.match(failed_sha) or not _SHA_RE.match(promoted_sha):
        return Refuse(UNUSABLE_SHA)

    correction = str(proposal["correction"])
    restore, revert = _RESTORE_RE.match(correction), _REVERT_RE.match(correction)
    if restore is not None:
        form, path, sha = RESTORE_FILE, restore.group(1), restore.group(2)
    elif revert is not None:
        # Undoing the failed commit is restoring the one file in bounds to the
        # revision the proposal already names; the path comes from the allowlist
        # and not from the string, and the commit's own scope is checked below.
        form, path, sha = REVERT_COMMIT, ALLOWED_TARGETS[0], promoted_sha
        if revert.group(1) != failed_sha:
            return Refuse(UNPARSABLE if _SHA_RE.match(revert.group(1)) else UNUSABLE_SHA)
    else:
        return Refuse(UNPARSABLE)
    if not _SHA_RE.match(sha):
        return Refuse(UNUSABLE_SHA)
    if sha != promoted_sha:
        return Refuse(UNPARSABLE)
    if not path.startswith(ALLOWED_PATH_PREFIX):
        return Refuse(PATH_OUTSIDE_PREFIX)
    if path not in ALLOWED_TARGETS:
        return Refuse(NOT_AN_ALLOWED_TARGET)

    status = live.canary_status() or {}
    template_hash = str(proposal.get("template_hash") or "")
    if not template_hash or status.get("lastAppliedSpec") != template_hash:
        return Refuse(SUPERSEDED)
    # F8: the primary already serves this spec, so there is nothing to undo and a
    # commit here would revert whatever restored it.
    if is_manual_rollback(status, template_hash):
        return Refuse(ALREADY_RESTORED)

    allowed = Verdict(allowed=True, form=form, target_path=path, restore_sha=sha)
    if tree is None:
        return allowed
    if not _SHA_RE.match(tree.head_sha) or not _SHA_RE.match(tree.head_tree_sha):
        return Refuse(UNUSABLE_SHA)
    if not tree.blob_at_head or tree.blob_at_head != tree.blob_at_failed:
        return Refuse(TARGET_CHANGED)
    if tree.head_sha != failed_sha and (
        not tree.head_is_descendant
        or any(changed.startswith(ALLOWED_PATH_PREFIX) for changed in tree.paths_since_failed)
    ):
        return Refuse(BRANCH_MOVED)
    if form == REVERT_COMMIT and sorted(tree.paths_undone) != [path]:
        return Refuse(MIXED_SCOPE)
    if not tree.blob_at_restore:
        return Refuse(UNUSABLE_SHA)
    if tree.blob_at_restore == tree.blob_at_head:
        return Refuse(NO_CHANGE)
    return dataclasses.replace(allowed, blob_sha=tree.blob_at_restore)

def commit_message(proposal: Mapping[str, Any], verdict: Verdict) -> str:
    """The commit subject; the file is its basename, the path being prefix plus it."""
    return (f"fix(flagger-pilot): restore {verdict.target_path.rsplit('/', 1)[-1]} "
            f"to {verdict.restore_sha[:8]} after failed rollout {proposal.get('template_hash')}")

def _parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None

def is_stale(payload: Mapping[str, Any], at: str, *, after_seconds: float = STALE_AFTER_SECONDS) -> bool:
    """True when an ``in-progress`` marker written at ``payload["started_at"]`` is older
    than ``after_seconds`` as of ``at``. One with no readable timestamp is stale at
    once — exactly a case a human needs to see."""
    started, now = _parse_time(str(payload.get("started_at") or "")), _parse_time(at)
    if now is None:
        return False
    return started is None or (now - started).total_seconds() > after_seconds
