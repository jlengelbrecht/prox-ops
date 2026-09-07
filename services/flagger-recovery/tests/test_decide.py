import dataclasses
import unittest

# As modules: the reason constants below are the vocabulary under test.
from flagger_recovery import decide as rules
from flagger_recovery import proposal as proposals
from flagger_recovery.identity import resolve
from flagger_recovery.inbox import KIND_EVENT, STATUS_ATTRIBUTION_PENDING, STATUS_RECEIVED, WebhookEvent
from flagger_recovery.record import DeploymentRecord, InMemoryStore, PutResult, canary_label, make_key_parts
from flagger_recovery.server import PHASE_CANDIDATE, PHASE_PROMOTED, Receiver
from tests.test_server import CHECKSUM, TOKEN, FakeCandidates, _body

NAMESPACE = "flagger-pilot"
CANARY = "podinfo"
FAILED_HASH = "759f9fb7bd"  # the fixtures' lastAppliedSpec, so FakeCandidates resolves to it
PROMOTED_HASH = "5b86bd6879"
OTHER_HASH = "cafed00d99"
# Every hook below carries the payload checksum, never a template hash: the two
# are different hash spaces, and the record is what maps one to the other.
FAILED_CHECKSUM = CHECKSUM
PROMOTED_SHA = "4e2f0b1a" + "c" * 32
_SAME = object()  # "whatever the recorded hash is", so a FakeLive names only what it changes

# The real identity resolved from the recorded pilot objects.
BASE = resolve(**FakeCandidates().read(NAMESPACE, CANARY))
FAILED_SHA = BASE.source_sha

def identity(**changes):
    changes.setdefault("template_hash", FAILED_HASH)
    return dataclasses.replace(BASE, **changes)

def record(*, phase=PHASE_CANDIDATE, checksum=FAILED_CHECKSUM, **changes):
    return DeploymentRecord(phase=phase, identity=identity(**changes), created_at="2026-09-07T00:00:00Z",
                            checksum=checksum)

def promoted_identity(**changes):
    return identity(**{"template_hash": PROMOTED_HASH, "source_sha": PROMOTED_SHA, **changes})

def promoted_record(**changes):
    return record(phase=PHASE_PROMOTED, **{"template_hash": PROMOTED_HASH, "source_sha": PROMOTED_SHA, **changes})

def event(*, hook="post-rollout", phase="Failed", checksum=FAILED_CHECKSUM):
    return WebhookEvent(hook=hook, name=CANARY, namespace=NAMESPACE, phase=phase, checksum=checksum, metadata={})

class FakeLive:
    """A ``decide.LiveState`` in four attributes. The defaults are the state
    AC2 asks for: the target carries the failed spec, the primary the promoted."""

    def __init__(self, *, applied=FAILED_HASH, promoted=PROMOTED_HASH, target=_SAME, primary=_SAME,
                 functional=None, phase="Failed"):
        self.status = {"lastAppliedSpec": applied, "lastPromotedSpec": promoted, "phase": phase}
        self.target = applied if target is _SAME else target
        self.primary = promoted if primary is _SAME else primary
        self.functional = functional

    def canary_status(self): return self.status
    def deployment_template_hash(self): return self.target
    def primary_template_hash(self): return self.primary
    def functional_check(self): return self.functional

class DecideTests(unittest.TestCase):
    def test_every_branch_that_stops_short_of_a_proposal(self):
        cases = [
            ("no checksum", record(), event(checksum=""), FakeLive(), "Ignore", rules.REASON_NO_CHECKSUM),
            ("F8 manual rollback restored", record(template_hash=PROMOTED_HASH), event(), FakeLive(), "Ignore", rules.REASON_MANUAL_ROLLBACK),
            ("a plain event hook", record(), event(hook="event", phase="Progressing"), FakeLive(), "Ignore", rules.REASON_NOT_A_TERMINAL_FAILURE),
            ("neither failed nor succeeded", record(), event(phase="Progressing"), FakeLive(), "Ignore", rules.REASON_NOT_A_TERMINAL_FAILURE),
            ("no candidate record", None, event(), FakeLive(), "Refuse", rules.REASON_NO_CANDIDATE_RECORD),
            ("the record is for a spec the canary no longer has applied", record(template_hash=OTHER_HASH), event(), FakeLive(), "Ignore", rules.REASON_SUPERSEDED),
            ("the canary moved on", record(), event(), FakeLive(applied=OTHER_HASH), "Ignore", rules.REASON_SUPERSEDED),
            ("the target's spec is unreadable", record(), event(), FakeLive(target=None), "Refuse", rules.REASON_TARGET_SPEC_UNKNOWN),
            ("the target serves something else", record(), event(), FakeLive(target=OTHER_HASH), "Ignore", rules.REASON_SUPERSEDED),
            ("nothing was ever promoted", record(), event(), FakeLive(promoted=""), "Refuse", rules.REASON_NO_PROMOTED_SPEC),
            ("the primary is degraded", record(), event(), FakeLive(primary=None), "Refuse", rules.REASON_PRIMARY_NOT_SERVING),
            ("the primary serves another spec", record(), event(), FakeLive(primary=OTHER_HASH), "Refuse", rules.REASON_PRIMARY_NOT_SERVING),
            ("the apex answers wrongly", record(), event(), FakeLive(functional=False), "Refuse", rules.REASON_FUNCTIONAL_CHECK_FAILING),
        ]
        for label, stored, hook, live, kind, reason in cases:
            with self.subTest(label):
                decision = rules.decide(stored, hook, live, promoted=promoted_record())
                self.assertEqual((decision.kind, decision.reason), (kind, reason))
                self.assertIsNone(decision.proposal, "nothing short of a proposal carries one")

    def test_succeeded_post_rollout_registers_a_promotion_and_proposes_nothing(self):
        # lastPromotedSpec is already this hash at promotion time, so the F8
        # rule would otherwise misread a fresh success as a manual rollback.
        live = FakeLive(applied=FAILED_HASH, promoted=FAILED_HASH, phase="Succeeded")
        decision = rules.decide(record(), event(phase="Succeeded"), live)
        self.assertEqual(decision.kind, "Register")
        self.assertIsNone(decision.proposal)

    def test_a_confirmed_failure_proposes_a_restore_back_to_the_promoted_revision(self):
        decision = rules.decide(record(), event(), FakeLive(), promoted=promoted_record())
        self.assertEqual(decision.kind, "ProposeCorrection")
        proposal = decision.proposal
        self.assertEqual(proposal.correction, f"restore-file {proposals.HELMRELEASE_PATH} to {PROMOTED_SHA}")
        self.assertFalse(proposal.requires_decision)
        self.assertEqual(proposal.failed_source_sha, FAILED_SHA)
        self.assertEqual(proposal.last_promoted_source_sha, PROMOTED_SHA)
        self.assertEqual(decision.reason, proposal.correction)

    def test_without_a_promoted_record_it_still_proposes_but_needs_a_human(self):
        decision = rules.decide(record(), event(), FakeLive())
        self.assertEqual((decision.kind, decision.proposal.requires_decision), ("ProposeCorrection", True))
        self.assertIsNone(decision.proposal.correction)
        self.assertIn("requires-decision", decision.reason)

class ProposalTests(unittest.TestCase):
    def test_the_correction_turns_on_the_commit_distance(self):
        restore = f"restore-file {proposals.HELMRELEASE_PATH} to {PROMOTED_SHA}"
        for ahead_by, correction in ((1, f"revert-commit {FAILED_SHA}"), (None, restore), (2, restore)):
            with self.subTest(ahead_by=ahead_by):
                proposal = proposals.build_proposal(identity(), promoted_identity(), ahead_by=ahead_by)
                self.assertEqual((proposal.correction, proposal.requires_decision), (correction, False))

    def test_everything_that_leaves_the_call_to_a_human(self):
        cases = [
            ("no promoted identity", identity(), None, proposals.NEEDS_PROMOTED_REVISION),
            ("a promotion with no revision", identity(), promoted_identity(source_sha=""), proposals.NEEDS_PROMOTED_REVISION),
            ("the failure is off-branch", identity(source_branch="main"), promoted_identity(), proposals.NEEDS_ALLOWED_BRANCH),
            ("the promotion is off-branch", identity(), promoted_identity(source_branch="main"), proposals.NEEDS_ALLOWED_BRANCH),
            ("a short failed sha", identity(source_sha="9abc123"), promoted_identity(), proposals.NEEDS_USABLE_SHA),
            ("a non-hex promoted sha", identity(), promoted_identity(source_sha="Z" * 40), proposals.NEEDS_USABLE_SHA),
            ("identical revisions", identity(source_sha=PROMOTED_SHA), promoted_identity(), proposals.NEEDS_DISTINCT_REVISIONS),
        ]
        for label, failed, promoted, reason in cases:
            with self.subTest(label):
                proposal = proposals.build_proposal(failed, promoted)
                self.assertTrue(proposal.requires_decision)
                self.assertIsNone(proposal.correction, "never a partially usable proposal")
                self.assertEqual(proposal.decision_reason, reason)
                self.assertIn("requires-decision", proposal.summary)
                labels = proposal.to_document().labels
                self.assertEqual(labels["flagger-recovery/requires-decision"], "true")

    def test_the_document_carries_both_identities_and_the_bounds(self):
        document = proposals.build_proposal(identity(), promoted_identity()).to_document()
        payload = document.payload
        self.assertEqual((payload["branch"], document.kind), (proposals.BRANCH, proposals.KIND_PROPOSAL))
        self.assertEqual(payload["failed_source_sha"], FAILED_SHA)
        self.assertEqual(payload["last_promoted_source_sha"], PROMOTED_SHA)
        self.assertEqual(payload["failed_identity"]["template_hash"], FAILED_HASH)
        self.assertEqual(payload["promoted_identity"]["template_hash"], PROMOTED_HASH)

        configmap = document.to_configmap()
        self.assertEqual(configmap["metadata"]["name"], f"flagger-recovery-proposal-{document.key}")
        self.assertEqual(configmap["metadata"]["labels"], {
            "app.kubernetes.io/part-of": "flagger-recovery", "flagger-recovery/kind": "proposal",
            "flagger-recovery/canary": "flagger-pilot.podinfo", "flagger-recovery/template-hash": FAILED_HASH,
            "flagger-recovery/requires-decision": "false"})

    def test_the_key_is_the_candidate_identity_so_the_store_deduplicates(self):
        first = proposals.build_proposal(identity(), promoted_identity())
        second = proposals.build_proposal(identity(), promoted_identity(), ahead_by=1)  # same candidate, resolved later
        self.assertEqual(first.key, make_key_parts(NAMESPACE, CANARY, FAILED_HASH, proposals.KIND_PROPOSAL))
        self.assertEqual(first.key, second.key)
        self.assertNotEqual(first.key, proposals.build_proposal(identity(template_hash=OTHER_HASH)).key)

        store = InMemoryStore()
        self.assertEqual(store.put_document(first.to_document()), PutResult.CREATED)
        self.assertEqual(store.put_document(second.to_document()), PutResult.DUPLICATE)
        self.assertEqual(store.writes, 1)

    def test_the_bound_decider_finds_the_promoted_record_for_the_last_promoted_spec(self):
        """The two-argument shape ``server.Receiver`` calls."""
        store = InMemoryStore()
        store.put(promoted_record())
        seen = []

        def live_for(namespace, canary_name):
            seen.append((namespace, canary_name))
            return FakeLive()

        decision = rules.decider(store, live_for)(record(), event())

        self.assertEqual(seen, [(NAMESPACE, CANARY)])
        self.assertEqual(decision.proposal.last_promoted_source_sha, PROMOTED_SHA)

class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.canary = canary_label(NAMESPACE, CANARY)

    def pending(self, **kwargs):
        self.store.put_document(event(**kwargs).to_document(
            status=STATUS_ATTRIBUTION_PENDING, received_at="2026-09-07T00:00:00Z", detail="no pods yet"
        ))

class ReconcileEventTests(ReconcileTestCase):
    def test_a_pending_event_whose_record_has_since_landed_is_resolved(self):
        self.pending()
        self.store.put(record())
        report = rules.reconcile(self.store, FakeLive(promoted=""), canary=self.canary)
        self.assertEqual((report.events_resolved, report.events_pending), (1, 0))

    def test_a_delayed_candidate_lets_a_pending_promotion_write_its_promoted_record(self):
        """CodeRabbit on #1304: an event stored ``attribution-pending`` at
        post-rollout/Succeeded because the candidate record was not there yet
        must still get its ``promoted`` record written once that record
        lands, or the next failure's correction degrades to
        ``requires_decision``."""
        self.pending(phase="Succeeded")
        self.store.put(record())

        report = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual((report.events_resolved, report.events_pending), (1, 0))

        promoted = self.store.get(make_key_parts(NAMESPACE, CANARY, FAILED_HASH, PHASE_PROMOTED))
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.identity.source_sha, FAILED_SHA)

        writes_after_first = self.store.writes
        rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual(self.store.writes, writes_after_first, "a second pass writes nothing")

        next_hash = OTHER_HASH
        self.store.put(record(template_hash=next_hash, source_sha=PROMOTED_SHA))
        failure = event(checksum=next_hash)
        live = FakeLive(applied=next_hash, promoted=FAILED_HASH)
        decision = rules.decide(
            self.store.get(make_key_parts(NAMESPACE, CANARY, next_hash, PHASE_CANDIDATE)),
            failure, live, promoted=rules.promoted_record(self.store, failure, live),
        )
        self.assertEqual(decision.kind, "ProposeCorrection")
        self.assertFalse(decision.proposal.requires_decision)
        self.assertEqual(decision.proposal.last_promoted_source_sha, FAILED_SHA)

    def test_a_pending_pre_rollout_stays_pending_because_the_retry_is_in_band(self):
        self.pending(hook="pre-rollout", phase="Progressing")
        report = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual((report.events_pending, report.events_resolved), (1, 0))
        self.assertEqual(report.events_unattributable, 0)
        self.assertEqual(self.store.writes, 1, "only the pending event itself is stored")

    def test_a_pending_post_rollout_with_no_indexed_record_is_refused_out_loud(self):
        """The one already in the cluster: it was stored before candidate
        records carried a checksum, so no lookup will ever find its record and
        Flagger will not send that hook again. Counting it as pending forever
        hides that; it is refused with a reason instead."""
        self.pending(phase="Succeeded")
        with self.assertLogs("flagger_recovery.decide", level="WARNING") as logs:
            report = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual((report.events_unattributable, report.events_pending, report.events_resolved), (1, 0, 0))
        self.assertEqual(self.store.writes, 1, "only the pending event itself is stored")
        self.assertIn(FAILED_CHECKSUM, logs.output[0])

    def test_a_hostile_phase_cannot_forge_a_line_in_that_refusal(self):
        """``phase`` is only stripped and bounded by the inbox, never clamped."""
        self.pending(phase="Succeeded\nWARNING:root:forged")
        with self.assertLogs("flagger_recovery.decide", level="WARNING") as logs:
            rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual(len(logs.records), 1)
        self.assertNotIn("\n", logs.output[0])
        self.assertIn("forged", logs.output[0], "escaped, not dropped")

    def test_a_settled_event_is_never_re_evaluated(self):
        self.store.put_document(event().to_document(status=STATUS_RECEIVED, received_at="2026-09-07T00:00:00Z"))
        self.assertEqual(rules.reconcile(self.store, FakeLive(), canary=self.canary), rules.ReconcileReport())

    def test_documents_for_another_canary_are_not_touched(self):
        self.pending()
        self.store.put(record())
        report = rules.reconcile(self.store, FakeLive(), canary=canary_label(NAMESPACE, "other"))
        self.assertEqual(report, rules.ReconcileReport())
        self.assertEqual(len(self.store.list_documents(KIND_EVENT)), 1)

class ReconcileProposalTests(ReconcileTestCase):
    def test_a_pending_failure_produces_one_proposal_however_often_it_runs(self):
        self.pending()
        self.store.put(record())
        self.store.put(promoted_record())

        first = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        writes_after_first = self.store.writes
        second = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        self.assertEqual((first.proposals_written, second.proposals_written), (1, 0))
        self.assertEqual(self.store.writes, writes_after_first, "it re-decides, but the key is the guard")
        self.assertEqual(len(self.store.list_documents(proposals.KIND_PROPOSAL)), 1)

    def test_a_proposal_is_open_until_the_failure_it_describes_is_gone(self):
        self.store.put(record())
        self.pending()
        rules.reconcile(self.store, FakeLive(), canary=self.canary)

        still_failed = rules.reconcile(self.store, FakeLive(), canary=self.canary)
        moved_on = rules.reconcile(self.store, FakeLive(applied=OTHER_HASH), canary=self.canary)
        # F8: a revert makes lastPromotedSpec equal the failed hash, and the
        # proposal is settled even though status.phase is still Failed.
        reverted = rules.reconcile(self.store, FakeLive(promoted=FAILED_HASH), canary=self.canary)
        self.assertEqual((still_failed.proposals_open, still_failed.proposals_superseded), (1, 0))
        self.assertEqual((moved_on.proposals_open, moved_on.proposals_superseded), (0, 1))
        self.assertEqual((reverted.proposals_open, reverted.proposals_superseded), (0, 1))

    def test_a_restart_writes_the_missed_proposal_and_the_redelivery_adds_nothing(self):
        """AC5's restart: the durable store carries a candidate, a promoted
        record and a ``Failed`` hook that was accepted but never decided. The
        startup pass writes the proposal; the hook Flagger redelivers to the
        new process afterwards decides the same thing and stores nothing."""
        self.store.put(record())
        self.store.put(promoted_record())
        self.pending()
        self.assertEqual(rules.reconcile(self.store, FakeLive(), canary=self.canary).proposals_written, 1)
        writes_after_startup = self.store.writes

        receiver = Receiver(token=TOKEN, store=self.store, candidates=FakeCandidates(),
                            decider=rules.decider(self.store, lambda namespace, name: FakeLive()))
        status, payload = receiver.handle("post-rollout", {}, _body(phase="Failed"))
        self.assertEqual((status, payload["result"]), (202, "ProposeCorrection"))
        self.assertEqual(self.store.writes, writes_after_startup, "no second proposal, no second event")
        self.assertEqual(len(self.store.list_documents(proposals.KIND_PROPOSAL)), 1)

if __name__ == "__main__":
    unittest.main()
