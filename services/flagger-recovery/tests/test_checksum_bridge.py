"""The checksum bridge, driven by the pilot's first real hooks.

Every payload below is read out of a ConfigMap the receiver itself wrote in
``flagger-system`` on 2026-09-07 — the first rollouts to reach it with the hooks
wired (``kubectl get cm -n flagger-system -l flagger-recovery/hook=...``, copied
verbatim into ``fixtures/live-*.json``). Two releases are in there: healthy,
payload checksum ``5f5697644f``, candidate record keyed by template hash
``6d4d7b659``; and faulty, checksum ``687b888d46``, hash ``576f8b8d6``.

Neither checksum equals its template hash, and that is the whole bug this file
guards: the receiver looked candidate records up *by the payload checksum*, so
the healthy release's promotion was stored ``attribution-pending`` and the faulty
release's failure was refused. Both refusals are in the fixtures.
"""

import dataclasses
import json
import unittest
from pathlib import Path

from flagger_recovery import decide as rules
from flagger_recovery.inbox import KIND_EVENT, STATUS_ATTRIBUTION_PENDING, WebhookEvent
from flagger_recovery.proposal import KIND_PROPOSAL
from flagger_recovery.record import (
    CHECKSUM_LABEL,
    DeploymentRecord,
    Document,
    InMemoryStore,
    canary_label,
    make_key_parts,
)
from flagger_recovery.server import PHASE_CANDIDATE, PHASE_PROMOTED, Receiver
from tests.test_server import TOKEN, FakeCandidates

FIXTURES = Path(__file__).parent / "fixtures"
NAMESPACE = "flagger-pilot"
CANARY = "podinfo"
CANARY_LABEL = canary_label(NAMESPACE, CANARY)

HEALTHY_CHECKSUM = "5f5697644f"
HEALTHY_HASH = "6d4d7b659"
HEALTHY_SHA = "8e0d74e80f34afdc5f838d09fe40c476273d6097"
FAULTY_CHECKSUM = "687b888d46"
FAULTY_HASH = "576f8b8d6"
FAULTY_SHA = "c7123c0b6c4d26bfb9bc7007abe3ed1d7173b8ce"
# What the canary carried before the healthy release: the spec 6d4d7b659 promoted over.
EARLIER_HASH = "759f9fb7bd"

def live_documents(name: str) -> list[dict]:
    """The stored ``document.json`` payloads from one recorded ``kubectl get``."""
    listing = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return [json.loads(item["data"]["document.json"]) for item in listing["items"]]

def live_configmaps(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["items"]

def only(documents: list[dict], **match) -> dict:
    found = [d for d in documents if all(d.get(field) == value for field, value in match.items())]
    assert len(found) == 1, f"{match} matched {len(found)} recorded documents"
    return found[0]

def only_configmap(fixture: str, label: str, value: str) -> dict:
    found = [cm for cm in live_configmaps(fixture) if cm["metadata"]["labels"].get(label) == value]
    assert len(found) == 1, f"{label}={value} matched {len(found)} recorded ConfigMaps"
    return found[0]

def body(document: dict) -> bytes:
    """The webhook body Flagger sent: the stored document is that payload minus
    the token the inbox strips, plus the receiver's own bookkeeping."""
    payload = {field: document[field] for field in ("name", "namespace", "phase", "checksum")}
    payload["metadata"] = {"token": TOKEN}
    return json.dumps(payload).encode("utf-8")

class LiveCandidates(FakeCandidates):
    """``FakeCandidates`` moved onto one of the two releases the cluster ran, so
    ``resolve()`` produces the template hash and commit its live record carries."""

    def __init__(self, template_hash: str, promoted_hash: str, source_sha: str) -> None:
        super().__init__()
        self._status = {"lastAppliedSpec": template_hash, "lastPromotedSpec": promoted_hash}
        self._source_sha = source_sha

    def read(self, namespace: str, canary_name: str):
        objects = super().read(namespace, canary_name)
        objects["canary"]["status"].update(self._status)
        objects["kustomization"]["status"]["lastAppliedRevision"] = f"{NAMESPACE}@sha1:{self._source_sha}"
        return objects

class LiveCanaryState:
    """``decide.LiveState`` as the canary stood while the faulty release failed:
    576f8b8d6 applied, 6d4d7b659 promoted and served by the primary."""

    def __init__(self, applied: str, promoted: str) -> None:
        self.status = {"lastAppliedSpec": applied, "lastPromotedSpec": promoted, "phase": "Failed"}
        self.applied = applied
        self.promoted = promoted

    def canary_status(self): return self.status
    def deployment_template_hash(self): return self.applied
    def primary_template_hash(self): return self.promoted
    def functional_check(self): return None

class LiveEvidenceTests(unittest.TestCase):
    """What the fixtures say about the cluster, before any code runs."""

    def test_the_payload_checksum_is_never_the_recorded_template_hash(self):
        pre = live_documents("live-pre-rollout.json")
        records = [DeploymentRecord.from_configmap(cm) for cm in live_configmaps("live-candidate-records.json")]
        hashes = {record.identity.template_hash for record in records}

        self.assertEqual({d["checksum"] for d in pre}, {HEALTHY_CHECKSUM, FAULTY_CHECKSUM})
        self.assertEqual(hashes, {HEALTHY_HASH, FAULTY_HASH})
        self.assertEqual(hashes & {HEALTHY_CHECKSUM, FAULTY_CHECKSUM}, set())
        # Both hooks of one rollout carry the same checksum, which is what makes
        # it a usable index; and both refusals this story removes are on record.
        succeeded = only(live_documents("live-post-rollout.json"), phase="Succeeded")
        self.assertEqual(only(pre, checksum=HEALTHY_CHECKSUM)["checksum"], succeeded["checksum"])
        self.assertEqual(succeeded["status"], STATUS_ATTRIBUTION_PENDING)
        self.assertIn(f"no candidate record for checksum '{HEALTHY_CHECKSUM}'", succeeded["detail"])
        self.assertEqual(
            only(live_documents("live-post-rollout.json"), phase="Failed")["detail"],
            f"Refuse: {rules.REASON_NO_CANDIDATE_RECORD}",
        )

    def test_the_records_written_before_this_change_carry_no_checksum_label(self):
        """Which is what makes them unattributable below, permanently."""
        for configmap in live_configmaps("live-candidate-records.json"):
            self.assertNotIn(CHECKSUM_LABEL, configmap["metadata"]["labels"])
            self.assertEqual(DeploymentRecord.from_configmap(configmap).checksum, "")

class LiveRolloutTests(unittest.TestCase):
    """Both recorded rollouts replayed through the receiver, in order."""

    def setUp(self):
        self.store = InMemoryStore()
        self.pre = live_documents("live-pre-rollout.json")
        self.post = live_documents("live-post-rollout.json")

    def receiver(self, template_hash, promoted_hash, source_sha, decider=None):
        candidates = LiveCandidates(template_hash, promoted_hash, source_sha)
        extra = {} if decider is None else {"decider": decider}
        return Receiver(token=TOKEN, store=self.store, candidates=candidates, **extra)

    def run_healthy_release(self):
        """Pre-rollout then the ``Succeeded`` post-rollout, both live payloads."""
        receiver = self.receiver(HEALTHY_HASH, EARLIER_HASH, HEALTHY_SHA)
        registered = receiver.handle("pre-rollout", {}, body(only(self.pre, checksum=HEALTHY_CHECKSUM)))
        promoted = receiver.handle("post-rollout", {}, body(only(self.post, phase="Succeeded")))
        return registered, promoted

    def test_pre_rollout_indexes_the_candidate_under_the_payload_checksum(self):
        (status, payload), _ = self.run_healthy_release()

        self.assertEqual((status, payload["result"]), (202, "Registered"))
        record = self.store.get(make_key_parts(NAMESPACE, CANARY, HEALTHY_HASH, PHASE_CANDIDATE))
        self.assertEqual(record.checksum, HEALTHY_CHECKSUM, "the bridge is stored, not derived")
        self.assertEqual(
            record.to_configmap()["metadata"]["labels"][CHECKSUM_LABEL], HEALTHY_CHECKSUM
        )
        self.assertEqual(self.store.find_candidate(CANARY_LABEL, HEALTHY_CHECKSUM), record)
        self.assertIsNone(
            self.store.get(make_key_parts(NAMESPACE, CANARY, HEALTHY_CHECKSUM, PHASE_CANDIDATE)),
            "the lookup this replaces built a key out of the payload checksum, which keys nothing",
        )

    def test_the_succeeded_post_rollout_promotes_instead_of_going_pending(self):
        _, (status, payload) = self.run_healthy_release()

        self.assertEqual((status, payload["result"]), (202, "Promoted"))
        promoted = self.store.get(make_key_parts(NAMESPACE, CANARY, HEALTHY_HASH, PHASE_PROMOTED))
        self.assertEqual(promoted.identity.source_sha, HEALTHY_SHA)
        self.assertEqual(promoted.checksum, HEALTHY_CHECKSUM)
        self.assertEqual(
            [d.payload["status"] for d in self.store.list_documents(KIND_EVENT)], ["received", "received"]
        )

    def test_the_failed_post_rollout_proposes_a_restore_to_the_release_before_it(self):
        """Both rollouts end to end: the healthy one promotes 6d4d7b659, the
        faulty one fails on 576f8b8d6, and the proposal points back at the
        healthy release's commit. None of it works unless every checksum
        resolves to the right candidate."""
        self.run_healthy_release()
        live = LiveCanaryState(applied=FAULTY_HASH, promoted=HEALTHY_HASH)
        receiver = self.receiver(
            FAULTY_HASH, HEALTHY_HASH, FAULTY_SHA,
            decider=rules.decider(self.store, lambda namespace, name: live),
        )

        receiver.handle("pre-rollout", {}, body(only(self.pre, checksum=FAULTY_CHECKSUM)))
        status, payload = receiver.handle("post-rollout", {}, body(only(self.post, phase="Failed")))

        self.assertEqual((status, payload["result"]), (202, "ProposeCorrection"))
        proposal = self.store.list_documents(KIND_PROPOSAL)[0].payload
        self.assertEqual(proposal["template_hash"], FAULTY_HASH)
        self.assertEqual(proposal["failed_source_sha"], FAULTY_SHA)
        self.assertEqual(proposal["last_promoted_source_sha"], HEALTHY_SHA)
        self.assertFalse(proposal["requires_decision"])

    def test_a_late_hook_for_a_superseded_revision_is_never_reattributed(self):
        """NFR4. The faulty release's ``Failed`` hook arriving after the canary
        has moved on must resolve to its own candidate and be ignored as
        superseded — not to whatever ``lastAppliedSpec`` says now."""
        self.run_healthy_release()
        moved_on = LiveCanaryState(applied="9c4d1a2b3f", promoted=HEALTHY_HASH)
        receiver = self.receiver(
            FAULTY_HASH, HEALTHY_HASH, FAULTY_SHA,
            decider=rules.decider(self.store, lambda namespace, name: moved_on),
        )
        receiver.handle("pre-rollout", {}, body(only(self.pre, checksum=FAULTY_CHECKSUM)))

        status, payload = receiver.handle("post-rollout", {}, body(only(self.post, phase="Failed")))

        self.assertEqual((status, payload["result"], payload["detail"]), (202, "Ignore", rules.REASON_SUPERSEDED))
        self.assertEqual(self.store.list_documents(KIND_PROPOSAL), [])

class LiveReconcileTests(unittest.TestCase):
    """The pass that has to clean up after the bug, over what it will actually
    find in ``flagger-system``."""

    def setUp(self):
        self.store = InMemoryStore()
        self.pending = only(live_documents("live-post-rollout.json"), phase="Succeeded")
        self.store.put_document(Document.from_configmap(
            only_configmap("live-post-rollout.json", "flagger-recovery/phase", "Succeeded")
        ))
        self.live = LiveCanaryState(applied=FAULTY_HASH, promoted=HEALTHY_HASH)

    def test_the_event_already_in_the_cluster_is_refused_with_a_reason(self):
        """Its candidate record predates the index and the store is create-only,
        so no pass can ever attribute it and Flagger will not send that hook
        again. It must be refused once per pass, not counted as work pending."""
        for configmap in live_configmaps("live-candidate-records.json"):
            self.store.put(DeploymentRecord.from_configmap(configmap))

        with self.assertLogs("flagger_recovery.decide", level="WARNING") as logs:
            report = rules.reconcile(self.store, self.live, canary=CANARY_LABEL)

        self.assertEqual((report.events_unattributable, report.events_pending), (1, 0))
        self.assertIn(HEALTHY_CHECKSUM, logs.output[0])
        self.assertEqual(self.store.list_documents(KIND_PROPOSAL), [])
        # The document reconcile walked really is the hook Flagger sent: its
        # replayed key is the name of the ConfigMap sitting in the cluster.
        event = WebhookEvent(metadata={}, **{name: self.pending[name]
                                             for name in ("hook", "name", "namespace", "phase", "checksum")})
        self.assertEqual(event.key, "3e1f5a9a68e1f75c2bf034f54623f8fd")

    def test_the_same_event_resolves_once_a_checksum_indexed_record_exists(self):
        """The steady state after this change: the next promotion writes a
        record carrying its checksum, and the pending event resolves to it."""
        indexed = DeploymentRecord.from_configmap(
            only_configmap("live-candidate-records.json", "flagger-recovery/template-hash", HEALTHY_HASH)
        )
        self.store.put(dataclasses.replace(indexed, checksum=HEALTHY_CHECKSUM))

        report = rules.reconcile(self.store, self.live, canary=CANARY_LABEL)

        self.assertEqual((report.events_resolved, report.events_unattributable), (1, 0))
        promoted = self.store.get(make_key_parts(NAMESPACE, CANARY, HEALTHY_HASH, PHASE_PROMOTED))
        self.assertEqual(promoted.identity.source_sha, HEALTHY_SHA)
        self.assertEqual(rules.reconcile(self.store, self.live, canary=CANARY_LABEL).events_resolved, 1)

if __name__ == "__main__":
    unittest.main()
