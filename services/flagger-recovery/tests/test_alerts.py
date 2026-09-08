import copy
import dataclasses
import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from flagger_recovery import alerts as rules
from flagger_recovery.auth import BEARER_HEADER, TOKEN_HEADER
from flagger_recovery.identity import resolve
from flagger_recovery.kube import ApiError
from flagger_recovery.proposal import KIND_PROPOSAL, PHASE_ALERT_PROPOSAL
from flagger_recovery.record import (ApiWriteError, ConfigMapStore, DeploymentRecord, Document,
                                     InMemoryStore, MalformedRecord, canary_label, label_value,
                                     make_key_parts)
from flagger_recovery.server import (ALERT_PATH, PHASE_PROMOTED, Receiver, build_server,
                                     combined_pass)
from tests.test_record import FakeTransport
from tests.test_server import TOKEN, FakeCandidates

FIXTURES = Path(__file__).parent / "fixtures"
NAMESPACE, CANARY = "flagger-pilot", "podinfo"
# The pilot's real cycle-2 and cycle-3 promotions, from live-candidate-records.json:
# cycle 3 is the revision serving and failing, cycle 2 is what a correction restores to.
PROMOTED_HASH, PROMOTED_SHA = "576f8b8d6", "c7123c0b6c4d26bfb9bc7007abe3ed1d7173b8ce"
PRIOR_HASH, PRIOR_SHA = "6d4d7b659", "8e0d74e80f34afdc5f838d09fe40c476273d6097"
OTHER_HASH, OTHER_SHA = "cafed00d99", "4e2f0b1a" + "c" * 32
PROMOTED_AT, PRIOR_AT = "2026-09-07T19:09:42Z", "2026-09-07T18:58:42Z"
RESTORE = f"restore-file kubernetes/pilot/flagger-pilot/helmrelease.yaml to {PRIOR_SHA}"
BASE = resolve(**FakeCandidates().read(NAMESPACE, CANARY))
SIGNED = {BEARER_HEADER: f"Bearer {TOKEN}"}

def promoted_record(template_hash, source_sha, created_at, source_branch=BASE.source_branch):
    """A promoted record as the store really holds one: written out through
    ``to_configmap`` and read back, so these tests run against the real promotion stamp."""
    identity = dataclasses.replace(BASE, template_hash=template_hash, source_sha=source_sha,
                                   source_branch=source_branch)
    return DeploymentRecord.from_configmap(DeploymentRecord(
        phase=PHASE_PROMOTED, identity=identity, created_at=created_at).to_configmap())

def _raising(exception):
    def _raise(*args, **kwargs):
        raise exception
    return _raise

class FakeLive:
    """``kube.LiveCanary``'s two methods; the defaults are the post-promotion state."""

    def __init__(self, *, applied=PROMOTED_HASH, promoted=PROMOTED_HASH, functional=None, failure=None):
        self.status = {"lastAppliedSpec": applied, "lastPromotedSpec": promoted, "phase": "Succeeded"}
        self.functional, self.failure = functional, failure

    def canary_status(self):
        if self.failure is not None:
            raise self.failure
        return self.status

    def functional_check(self):
        return self.functional

class FakeRevision:
    """``kube.LiveKustomization``: the revision Flux says it has applied."""

    def __init__(self, sha=PROMOTED_SHA, failure=None):
        self.sha, self.failure = sha, failure

    def source_sha(self):
        if self.failure is not None:
            raise self.failure
        return self.sha

class AlertTestCase(unittest.TestCase):
    def setUp(self):
        self.now = "2026-09-08T09:15:00Z"
        self.store, self.live, self.revision = InMemoryStore(), FakeLive(), FakeRevision()
        self.router = self.build_router()

    def build_router(self, **changes):
        """A router for the configured pilot: the canary it serves is a constructor
        argument, so an alert naming anything else is refused."""
        return rules.AlertRouter(self.store, lambda namespace, name: self.live,
                                 lambda: self.revision, clock=lambda: self.now,
                                 canary_namespace=NAMESPACE, canary_name=CANARY, **changes)

    def seed(self, *, prior=True, current=True):
        if prior:
            self.store.put(promoted_record(PRIOR_HASH, PRIOR_SHA, PRIOR_AT))
        if current:
            self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))

    def notification(self, *, alerts=None, **changes):
        with open(FIXTURES / "alertmanager-webhook-v4.json", encoding="utf-8") as handle:
            payload = json.load(handle)
        payload.update(changes)
        return payload if alerts is None else {**payload, "alerts": alerts}

    def alert(self, **changes):
        """The fixture's alert. A dict override merges into the field it names (a
        ``None`` value drops that key); anything else replaces the field outright."""
        entry = copy.deepcopy(self.notification()["alerts"][0])
        for field, value in changes.items():
            if isinstance(value, dict) and isinstance(entry.get(field), dict):
                entry[field] = {key: item for key, item in {**entry[field], **value}.items()
                                if item is not None}
            else:
                entry[field] = value
        return entry

    def fire(self, *, notification=None, **changes):
        return self.router.handle(self.notification(alerts=[self.alert(**changes)],
                                                    **(notification or {})))

    def resolution(self, **changes):
        return self.router.handle(self.notification(
            status=rules.STATUS_RESOLVED, alerts=[self.alert(status=rules.STATUS_RESOLVED, **changes)]))

    def documents(self, kind):
        return self.store.list_documents(kind)

    def proposals(self):
        return self.documents(KIND_PROPOSAL)

class ParseTests(AlertTestCase):
    def test_the_documented_shape_parses_into_one_attributable_alert(self):
        group_key, alerts, truncated = rules.parse(self.notification())
        self.assertIn(NAMESPACE, group_key)
        self.assertEqual((len(alerts), truncated, alerts[0].status, alerts[0].canary, alerts[0].kind),
                         (1, False, rules.STATUS_FIRING, (NAMESPACE, CANARY), rules.KIND_ALERT))
        self.assertIn(rules.ANNOTATION_EVIDENCE, alerts[0].annotations)

    def test_every_rejected_shape_is_400_and_stores_nothing(self):
        """Anything but the published v4 contract: it must not guess at what it is not."""
        for label, payload in {
            "v3": self.notification(version="3"),
            "a numeric version": self.notification(version=4),
            "no version": {key: value for key, value in self.notification().items() if key != "version"},
            "an unknown top-level status": self.notification(status="silenced"),
            "no alerts": self.notification(alerts=[]),
            "alerts is not a list": self.notification(alerts={"status": "firing"}),
            "an entry is not an object": self.notification(alerts=["firing"]),
            "an unknown alert status": self.notification(alerts=[self.alert(status="pending")]),
            "no fingerprint": self.notification(alerts=[self.alert(fingerprint="")]),
            "no startsAt": self.notification(alerts=[self.alert(startsAt=None)]),
            "labels is not an object": self.notification(alerts=[self.alert(labels="namespace")]),
            "too many alerts": self.notification(alerts=[self.alert()] * (rules.MAX_ALERTS + 1)),
            "a startsAt that is not a time": self.notification(alerts=[self.alert(startsAt="soon")]),
            "an endsAt that is not a time": self.notification(alerts=[self.alert(endsAt="later")]),
            "a resolution with no end time": self.notification(status=rules.STATUS_RESOLVED,
                alerts=[self.alert(status=rules.STATUS_RESOLVED, endsAt=None)]),
            # Alertmanager's group status is firing when *any* member fires, so a firing
            # notification carrying resolved members is ordinary; the reverse is a shape
            # only a forger holding the token can produce.
            "resolved at the top, firing per alert": self.notification(status=rules.STATUS_RESOLVED),
        }.items():
            with self.subTest(label):
                with self.assertRaises(rules.MalformedAlert):
                    rules.parse(payload)
                self.assertEqual(self.router.handle(payload)[0], 400)
        self.assertEqual(self.store.writes, 0)
        # The detail is a response body, not a place to reflect an oversized field back.
        reflected = self.router.handle(self.notification(version="4" + "A" * 5000))[1]["detail"]
        self.assertLess(len(reflected), 2 * rules.MAX_FIELD_LENGTH)

    def test_hostile_labels_are_bounded_and_claim_nothing(self):
        oversized = {f"k{index}": "v" * 4000 for index in range(rules.MAX_METADATA_ENTRIES + 20)}
        _, alerts, _ = rules.parse(self.notification(alerts=[{**self.alert(), "labels": oversized}]))
        self.assertTrue(all(len(value) <= rules.MAX_FIELD_LENGTH for value in alerts[0].labels.values()))
        self.assertEqual((len(alerts[0].labels), alerts[0].canary),
                         (rules.MAX_METADATA_ENTRIES, None), "nothing survived, so nothing is claimed")

    def test_the_entry_cap_cannot_evict_the_labels_that_decide_attribution(self):
        """The order Alertmanager really sends: Go emits map keys sorted, and a production
        alert carries every metric label too, so the identity ones sort past the cap."""
        junk = {f"a{index:02d}": "junk" for index in range(40)}  # all sort before "alertname"
        entry = self.alert()
        entry["labels"] = dict(sorted({**entry["labels"], **junk}.items()))
        entry["annotations"] = dict(sorted({**entry["annotations"], **junk}.items()))
        _, alerts, _ = rules.parse(self.notification(alerts=[entry]))
        self.assertEqual(len(alerts[0].labels), rules.MAX_METADATA_ENTRIES)
        self.assertEqual((alerts[0].canary, alerts[0].labels[rules.LABEL_SEVERITY]),
                         ((NAMESPACE, CANARY), rules.REQUIRED_SEVERITY))
        self.assertIn(rules.ANNOTATION_EVIDENCE, alerts[0].annotations)

class DecisionTests(AlertTestCase):
    def test_a_firing_alert_proposes_restoring_the_promotion_before_the_serving_one(self):
        self.seed()
        status, body = self.fire()
        answer = body["alerts"][0]
        self.assertEqual((status, answer["result"]), (202, rules.PROPOSE_CORRECTION))
        self.assertEqual(len(self.proposals()), 1)
        payload = self.proposals()[0].payload
        # Attribution is to the promoted revision; the correction points at the one before
        # it, never at "whatever changed most recently".
        self.assertEqual(
            (payload["template_hash"], payload["phase"], payload["failed_source_sha"],
             payload["last_promoted_source_sha"], payload["correction"], payload["requires_decision"]),
            (PROMOTED_HASH, PHASE_ALERT_PROPOSAL, PROMOTED_SHA, PRIOR_SHA, RESTORE, False))
        self.assertEqual((self.proposals()[0].key, answer["proposal"]),
                         (make_key_parts(NAMESPACE, CANARY, PROMOTED_HASH, PHASE_ALERT_PROPOSAL),) * 2)

    def test_the_alert_document_records_the_decision_and_the_evidence_behind_it(self):
        self.seed()
        self.fire()
        stored = self.documents(rules.KIND_ALERT)
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0].payload["decision"], stored[0].payload["received_at"],
                          stored[0].payload["labels"][rules.LABEL_NAMESPACE]),
                         (rules.PROPOSE_CORRECTION, self.now, NAMESPACE))
        self.assertEqual(stored[0].payload["evidence"] | {"recovery_evidence": ""},
                         {"functional_check": rules.FUNCTIONAL_NOT_CONSULTED, "recovery_evidence": "",
                          "promoted_template_hash": PROMOTED_HASH, "promoted_source_sha": PROMOTED_SHA,
                          "applied_source_sha": PROMOTED_SHA, "restore_source_sha": PRIOR_SHA,
                          "severity": rules.REQUIRED_SEVERITY})
        self.assertEqual(stored[0].labels["flagger-recovery/canary"], canary_label(NAMESPACE, CANARY))
        self.assertEqual(stored[0].labels["flagger-recovery/alert-status"], rules.STATUS_FIRING)

    def test_a_reported_functional_check_is_recorded_but_never_the_gate(self):
        for reported, expected in ((True, rules.FUNCTIONAL_PASSING), (False, rules.FUNCTIONAL_FAILING)):
            with self.subTest(reported=reported):
                self.setUp()
                self.seed()
                self.live.functional = reported
                self.fire()
                stored = self.documents(rules.KIND_ALERT)[0].payload
                self.assertEqual((stored["evidence"]["functional_check"], stored["decision"]),
                                 (expected, rules.PROPOSE_CORRECTION))

    def test_a_resolution_is_recorded_under_its_own_kind_and_undoes_nothing(self):
        self.seed()
        self.fire()
        writes = self.store.writes
        status, body = self.resolution(endsAt="2026-09-08T09:20:00Z")
        self.assertEqual((status, body["alerts"][0]["result"]), (202, rules.RECORDED))
        self.assertEqual((len(self.proposals()), self.store.writes), (1, writes + 1))
        recorded = self.documents(rules.KIND_ALERT_RESOLVED)
        self.assertEqual((len(recorded), recorded[0].payload["decision"], recorded[0].payload["status"]),
                         (1, rules.RECORDED, rules.STATUS_RESOLVED))

    def test_a_resolution_delivered_before_its_firing_prevents_the_proposal(self):
        """A resolution has to *prevent* work, not merely fail to reverse it; delivery order is
        not ours to choose. One that ended before this firing began is not cover, though."""
        self.seed()
        self.resolution(startsAt="2026-09-08T08:00:00Z", endsAt="2026-09-08T08:30:00Z")
        self.assertEqual(self.fire()[1]["alerts"][0]["result"], rules.PROPOSE_CORRECTION)

        self.setUp()
        self.seed()
        self.resolution(endsAt="2026-09-08T09:20:00Z")
        writes = self.store.writes
        answer = self.fire()[1]["alerts"][0]
        self.assertEqual((answer["result"], answer["detail"]), (rules.HOLD, rules.HOLD_RESOLVED))
        self.assertEqual(self.proposals(), [])
        self.assertEqual(self.store.writes, writes + 1, "the held alert, and no proposal")

    def test_a_resolution_dated_beyond_the_clock_cannot_suppress_a_fingerprint(self):
        """Unbounded, one ``endsAt: 2099`` gags that fingerprint for ever in a create-only store
        nothing prunes; a real one is the instant the symptom stopped, so only skew puts it ahead
        of the clock — and the test above covers at ``09:20``, the allowance exactly."""
        self.seed()
        self.resolution(endsAt="2099-01-01T00:00:00Z")
        self.assertEqual(self.fire()[1]["alerts"][0]["result"], rules.PROPOSE_CORRECTION)

    def test_a_resolution_landing_while_the_ladder_runs_still_stops_the_proposal(self):
        """The cover check and the write are not atomic, and this is a threading server: asking
        again immediately before the write narrows that window to the store write. This pins
        the narrowing, not atomicity."""
        self.seed()
        listing = self.store.list_documents
        def racing(kind, **selectors):  # a resolution lands between the check and the write
            answer = listing(kind, **selectors)
            if kind == rules.KIND_ALERT_RESOLVED and not answer:
                self.resolution(endsAt="2026-09-08T09:20:00Z")
            return answer
        self.store.list_documents = racing
        self.assertEqual((self.fire()[1]["alerts"][0]["detail"], self.proposals()), (rules.HOLD_RESOLVED, []))

    def test_an_alert_this_pilot_cannot_claim_is_counted_and_never_stored(self):
        """The key is caller-supplied: one request must not grow the store by fingerprints."""
        grouped = [self.alert(fingerprint=f"aaaa000000000{index}",
                              labels={rules.LABEL_CANARY: f"invented-{index}"}) for index in range(3)]
        status, body = self.router.handle(self.notification(alerts=grouped))
        self.assertEqual((status, body["not_stored"]), (202, 3))
        self.assertEqual((self.store.writes, self.documents(rules.KIND_ALERT)), (0, []))

    def test_a_repeated_firing_alert_is_a_duplicate_that_writes_nothing(self):
        self.seed()
        self.fire()
        writes = self.store.writes
        self.live.failure = AssertionError("a duplicate must not re-read live state")
        status, body = self.fire()
        self.assertEqual((status, body["alerts"][0]["result"], self.store.writes),
                         (202, rules.DUPLICATE, writes))

    def test_three_firing_alerts_on_one_revision_produce_exactly_one_proposal(self):
        self.seed()
        grouped = [self.alert(fingerprint=f"aaaa000000000{index}",
                              labels={"alertname": f"FlaggerPilotApexAlert{index}"})
                   for index in range(3)]
        status, body = self.router.handle(self.notification(alerts=grouped))
        self.assertEqual((status, [answer["result"] for answer in body["alerts"]]),
                         (202, [rules.PROPOSE_CORRECTION] * 3))
        self.assertEqual(len(self.documents(rules.KIND_ALERT)), 3, "each alert is its own record")
        self.assertEqual(len(self.proposals()), 1, "one proposal per failed promoted revision")
        self.assertEqual({answer["proposal"] for answer in body["alerts"]}, {self.proposals()[0].key})

class HoldTests(AlertTestCase):
    """The closed hold set: every reason paired with an input that produces it, none of
    them writing a proposal — a hold is a decision, not a silence."""

    def _hold(self, reason, *, prior=True, current=True, seed=True, live=None, revision=None,
              stored=True, before=None, **changes):
        self.live, self.revision = live or self.live, revision or self.revision
        if seed:
            self.seed(prior=prior, current=current)
        if before is not None:
            before(self)
        status, body = self.fire(**changes)
        answer = body["alerts"][0]
        self.assertEqual((status, answer["result"], answer["detail"]), (202, rules.HOLD, reason))
        documents = self.documents(rules.KIND_ALERT)
        # A hold this pilot can claim is on the record; one it cannot is refused first.
        self.assertEqual(len(documents), 1 if stored else 0)
        if stored:
            self.assertEqual((documents[0].payload["decision"], documents[0].payload["reason"],
                              documents[0].payload["proposal"]), (rules.HOLD, reason, ""))
        self.assertEqual(self.proposals(), [], "a hold never writes a proposal")

    # AC4's last two -- an unreadable status, an unreadable revision -- are unknown health.
    CASES = (
        ("no identity labels", rules.HOLD_UNATTRIBUTED,
         dict(labels={rules.LABEL_NAMESPACE: None, rules.LABEL_CANARY: None}, stored=False)),
        ("labels naming another canary", rules.HOLD_UNATTRIBUTED,
         dict(labels={rules.LABEL_CANARY: "somebody-elses"}, stored=False,
              live=FakeLive(failure=AssertionError("another canary is never read")))),
        ("a notification Alertmanager truncated", rules.HOLD_INCOMPLETE_NOTIFICATION,
         dict(notification={"truncatedAlerts": 9000}, stored=False)),
        ("a recorded resolution says the symptom stopped", rules.HOLD_RESOLVED,
         dict(before=lambda case: case.resolution(endsAt="2026-09-08T09:20:00Z"))),
        ("the recorded promotion is not on the pilot branch", rules.HOLD_REQUIRES_DECISION,
         dict(seed=False, before=lambda case: [case.store.put(promoted_record(*seed)) for seed in (
             (PRIOR_HASH, PRIOR_SHA, PRIOR_AT, "main"), (PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))])),
        ("a rollout in flight belongs to Flagger", rules.HOLD_ROLLOUT_IN_PROGRESS,
         dict(live=FakeLive(applied=OTHER_HASH))),
        ("no stored promotion for the serving spec", rules.HOLD_NO_PROMOTED_RECORD, dict(current=False)),
        ("flux applied a revision the record does not name", rules.HOLD_REVISION_MISMATCH,
         dict(revision=FakeRevision(sha=OTHER_SHA))),
        ("nothing to restore to", rules.HOLD_NO_PRIOR_PROMOTED, dict(prior=False)),
        ("no evidence annotation", rules.HOLD_INSUFFICIENT_EVIDENCE,
         dict(annotations={rules.ANNOTATION_EVIDENCE: None})),
        ("blank evidence annotation", rules.HOLD_INSUFFICIENT_EVIDENCE,
         dict(annotations={rules.ANNOTATION_EVIDENCE: "   "})),
        ("severity is not critical", rules.HOLD_INSUFFICIENT_EVIDENCE,
         dict(labels={rules.LABEL_SEVERITY: "warning"})),
        ("no severity at all", rules.HOLD_INSUFFICIENT_EVIDENCE,
         dict(labels={rules.LABEL_SEVERITY: None})),
        ("the canary has no applied spec", rules.HOLD_HEALTH_UNKNOWN, dict(live=FakeLive(applied=""))),
        ("the canary has never promoted", rules.HOLD_HEALTH_UNKNOWN, dict(live=FakeLive(promoted=""))),
        ("flux reports no readable revision", rules.HOLD_HEALTH_UNKNOWN,
         dict(revision=FakeRevision(sha=None))),
    )

    def test_every_hold_reason_with_an_input_that_produces_it(self):
        for label, reason, setup in self.CASES:
            with self.subTest(label):
                self.setUp()
                self._hold(reason, **setup)
        self.assertEqual({reason for _, reason, _ in self.CASES}, set(rules.HOLD_REASONS))

    def test_an_alert_this_pilot_cannot_claim_is_logged_with_what_it_claimed(self):
        """Refused with no document, so the log line is the only record it leaves."""
        with self.assertLogs("flagger_recovery.alerts", level="INFO") as logs:
            self._hold(rules.HOLD_UNATTRIBUTED, **self.CASES[1][2])
        self.assertIn(f'canary="{NAMESPACE}/somebody-elses"', logs.output[-1])

    def test_a_promotion_newer_than_the_serving_one_is_not_something_to_restore_back_to(self):
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        self.store.put(promoted_record(OTHER_HASH, OTHER_SHA, "2026-09-08T09:00:00Z"))
        self._hold(rules.HOLD_NO_PRIOR_PROMOTED, seed=False)

    def test_temporal_proximity_is_never_an_input(self):
        """AC2: the alert that proposes with its evidence holds without it, whatever the clock
        says — only the corroboration changed."""
        self.seed()
        self.now = PROMOTED_AT  # firing at the very instant of the promotion
        _, body = self.fire(annotations={rules.ANNOTATION_EVIDENCE: None})
        self.assertEqual(body["alerts"][0]["detail"], rules.HOLD_INSUFFICIENT_EVIDENCE)

    def test_every_read_that_never_answered_holds_health_unknown(self):
        """AC4, the transport side. The warning names the exception's class and nothing
        else: an ``ApiError``'s message carries the request URL and the response body."""
        for label, setup in {
            "the canary API refuses": dict(live=FakeLive(failure=ApiError(503, "https://api.invalid/c"))),
            "the canary read times out": dict(live=FakeLive(failure=TimeoutError("timed out"))),
            "the kustomization API refuses": dict(
                revision=FakeRevision(failure=ApiError(500, "https://api.invalid/k"))),
            "the store refuses": dict(store=ApiWriteError("GET", "https://api.invalid", 503, b"")),
        }.items():
            with self.subTest(label):
                self.setUp()
                if "store" in setup:
                    self.store.get = _raising(setup["store"])
                with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
                    self._hold(rules.HOLD_HEALTH_UNKNOWN, live=setup.get("live"), revision=setup.get("revision"))
                self.assertNotIn("api.invalid", logs.output[0])

    def test_a_bug_in_the_ladder_is_not_laundered_into_a_hold(self):
        """``_UNREADABLE`` is the transport and API set, not ``Exception`` and not the two
        commonest bug signatures: a renamed status key must not turn every alert into a
        permanent hold that looks like an outage. The 500 is safe against this store."""
        for error in (RuntimeError("a bug, not an outage"), KeyError("lastPromotedSpec"),
                      AttributeError("'NoneType' object has no attribute 'identity'"),
                      ValueError("invalid literal")):
            with self.subTest(type(error).__name__):
                self.setUp()
                self.seed()
                self.live.functional_check = _raising(error)
                with self.assertRaises(type(error)):
                    self.fire()
                self.assertEqual(self.documents(rules.KIND_ALERT), [])

    def test_the_duplicate_probe_is_as_much_a_live_read_as_the_ladder_s(self):
        """``ConfigMapStore.get_document`` raises on a non-200, and unreadable is a decision
        this path makes, not a 500 Alertmanager should retry."""
        self.seed()
        self.store.get_document = _raising(ApiWriteError("GET", "https://api.invalid", 503, b""))
        writes = self.store.writes
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            status, body = self.fire()
        self.assertEqual((status, body["alerts"][0]["detail"]), (202, rules.HOLD_HEALTH_UNKNOWN))
        self.assertEqual((self.store.writes, self.proposals()), (writes, []))
        self.assertNotIn("api.invalid", logs.output[0])

class SweepTests(AlertTestCase):
    """AC3: durable decisions converge after an interruption, one action per failed update,
    with the window recorded."""

    def test_a_hold_whose_reason_has_cleared_is_re_evaluated_into_one_proposal(self):
        self.seed()
        self.live = FakeLive(applied=OTHER_HASH)  # mid-rollout
        self.fire()
        self.assertEqual(self.documents(rules.KIND_ALERT)[0].payload["reason"],
                         rules.HOLD_ROLLOUT_IN_PROGRESS)
        self.live = FakeLive()  # the rollout finished
        report = self.router.sweep()
        self.assertEqual((report.alerts_held, report.alerts_resolved, report.proposals_written,
                          len(self.proposals())), (0, 1, 1, 1))

        # A second pass re-derives it and writes nothing: the key is create-only, so
        # convergence is one action, not one per tick.
        writes = self.store.writes
        second = self.router.sweep()
        self.assertEqual((second.alerts_resolved, second.proposals_written), (1, 0))
        self.assertEqual(self.store.writes, writes)

    def test_a_hold_that_resolved_before_the_sweep_reached_it_writes_no_proposal(self):
        """The reason cleared, but a resolution for the fingerprint is on record: this would
        correct a failure the store says is over."""
        self.seed()
        self.live = FakeLive(applied=OTHER_HASH)  # mid-rollout
        self.fire()
        self.resolution(endsAt="2026-09-08T09:20:00Z")
        self.live = FakeLive()  # the rollout finished
        report = self.router.sweep()
        self.assertEqual((report.alerts_held, report.proposals_written, self.proposals()), (1, 0, []))

    def test_what_a_sweep_never_reads_again(self):
        """A terminal reason (``insufficient-evidence`` judges the alert itself) can only
        re-derive itself; a hold outside the window is nothing to escalate; one missing the
        fields the ladder reads stays held rather than being guessed at."""
        self.seed()
        self.fire(annotations={rules.ANNOTATION_EVIDENCE: None})  # terminal
        self.live.failure = AssertionError("none of these is ever re-read")
        self.assertEqual(self.router.sweep().alerts_held, 1)

        self.setUp()
        self.seed(current=False)
        self.fire()  # held on no-promoted-record, which can clear — but not a day later
        self.resolution()  # a resolution is not a held alert
        self.store.put_document(Document(  # a held document from before a field existed
            kind=rules.KIND_ALERT, key="0" * 32,
            labels={"flagger-recovery/canary": canary_label(NAMESPACE, CANARY)},
            payload={"decision": rules.HOLD, "reason": rules.HOLD_HEALTH_UNKNOWN, "fingerprint": "abc"}))
        self.now = "2026-09-09T09:15:01Z"
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        self.live.failure = AssertionError("none of these is ever re-read")
        report = self.build_router().sweep()
        self.assertEqual((report.alerts_held, report.alerts_resolved, report.proposals_written,
                          self.proposals()), (2, 0, 0, []), "nothing escalates a hold for being old")

    def test_a_non_mapping_alert_payload_does_not_cost_the_other_holds_their_sweep(self):
        """R28: the held alert's *own* document can carry a payload that is not an object too —
        not just a nested resolution read, which R19b already guards — and reading its
        ``decision`` must sit inside the same per-alert guard rather than aborting the pass."""
        self.seed(current=False)  # every alert holds on no-promoted-record, which can clear
        [self.fire(fingerprint=f"dddd000000000{index}") for index in range(3)]
        self.store.put_document(Document(
            kind=rules.KIND_ALERT, key="f" * 32,
            labels={"flagger-recovery/canary": canary_label(NAMESPACE, CANARY)},
            payload=["not", "an", "object"]))
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            report = self.router.sweep()
        self.assertEqual((report.alerts_unreadable, report.alerts_resolved, report.alerts_held,
                          report.proposals_written, len(self.proposals())), (1, 3, 0, 1, 1))
        self.assertIn("flagger-recovery-alert-" + "f" * 32, logs.output[0])

    def test_a_failing_sweep_still_lets_the_rollout_path_reconcile(self):
        """One pass covers both paths, and an alert-side read failure must not take the rollout
        path's reconcile — delayed promotions, re-offered corrections — with it."""
        self.store.list_documents_tolerant = _raising(ApiWriteError("GET", "https://api.invalid", 503, b""))
        ran = []
        with self.assertLogs("flagger_recovery.alerts", level="ERROR"):
            result = combined_pass(self.router.sweep_safely, lambda: ran.append("reconciled"))
        self.assertEqual((result["alerts"].alerts_sweep_failed, ran), (1, ["reconciled"]))

    def test_the_first_run_records_a_null_window_never_a_zero(self):
        # Clock skew between nodes, or a second replica writing while this one starts, is
        # exactly the unmeasurable case — and ``0.0`` reads as "nothing was missed".
        self.assertIsNone(rules._seconds_between("2026-09-08T09:20:00Z", self.now))
        self.assertIsNone(rules._seconds_between("not-a-time", self.now))
        self.assertEqual(rules._seconds_between(self.now, self.now), 0.0)
        report = self.router.sweep()
        self.assertIsNone(report.seconds_since_previous_start)
        runs = self.documents(rules.KIND_RECEIVER_RUN)
        self.assertEqual((len(runs), runs[0].payload["started_at"],
                          runs[0].payload["previous_started_at"]), (1, self.now, ""))
        writes = self.store.writes
        self.router.sweep()  # once per process, not once per pass
        self.assertEqual((self.store.writes, len(self.documents(rules.KIND_RECEIVER_RUN))), (writes, 1))

    def test_a_restart_records_the_gap_to_the_previous_run_and_says_so(self):
        """Not downtime, and not "time since the last thing that happened": the gap to the
        previous run's start is all a create-only store can honestly hold — hence the name.
        Two receivers starting in the same second still get a document each, because a rolling
        update starts the new pod as the old one stops and ``now()`` resolves to seconds; the
        holder is in the key material as ``Lease`` has it."""
        self.seed(current=False)
        self.fire()  # a held alert at 09:15:00
        self.build_router(holder="recovery-receiver-a").sweep()
        self.build_router(holder="recovery-receiver-b").sweep()
        self.now = "2026-09-08T09:47:30Z"  # a new process over the same durable store
        report = self.build_router(holder="recovery-receiver-c").sweep()

        self.assertEqual(report.seconds_since_previous_start, 1950.0)
        runs = self.documents(rules.KIND_RECEIVER_RUN)
        self.assertEqual(sorted(document.payload["pod"] for document in runs),
                         ["recovery-receiver-a", "recovery-receiver-b", "recovery-receiver-c"])
        latest = max(runs, key=lambda document: document.payload["started_at"])
        self.assertEqual((latest.payload["previous_started_at"],
                          latest.payload["seconds_since_previous_start"]),
                         ("2026-09-08T09:15:00Z", 1950.0))
        self.assertEqual(report.alerts_held, 1, "the pass still converges over what it missed")

class MalformedStoredObjectTests(AlertTestCase):
    """A stored object nothing can decode is *unreadable* — a decision this path makes, never a
    500 on every Alertmanager retry, and never one document costing a whole sweep. Over the real
    ``ConfigMapStore``, so the decode is the production one."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        self.store = ConfigMapStore("http://127.0.0.1:8001", transport=self.transport)
        self.router = self.build_router()

    def poison(self, fingerprint):
        """One of our own resolution ConfigMaps whose payload will not decode: a truncated write."""
        configmap = Document(kind=rules.KIND_ALERT_RESOLVED, key="1" * 32, payload={},
                             labels={rules.FINGERPRINT_LABEL: label_value(fingerprint)}).to_configmap()
        configmap["data"] = {"document.json": "{not json"}
        self.transport.by_namespace.setdefault("flagger-system", {})[configmap["metadata"]["name"]] = configmap

    def test_a_malformed_stored_object_holds_health_unknown_rather_than_500ing(self):
        self.seed()
        self.poison(self.alert()["fingerprint"])
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            status, body = self.fire()
        self.assertEqual((status, body["alerts"][0]["detail"]), (202, rules.HOLD_HEALTH_UNKNOWN))
        # Refused before anything is stored, as every unreadable read is: a redelivery re-runs it.
        self.assertEqual((self.proposals(), self.documents(rules.KIND_ALERT)), ([], []))
        self.assertIn("MalformedRecord", logs.output[0])

    def test_one_unreadable_alert_does_not_cost_the_other_holds_their_sweep(self):
        """Per alert, not per pass: three holds still clear around the one that cannot."""
        self.seed(current=False)  # every alert holds on no-promoted-record, which can clear
        [self.fire(fingerprint=f"aaaa000000000{index}") for index in range(4)]
        self.poison("aaaa0000000001")
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            report = self.router.sweep()
        self.assertEqual((report.alerts_unreadable, report.alerts_resolved, report.alerts_held,
                          report.proposals_written, len(self.proposals())), (1, 3, 0, 1, 1))
        self.assertIn("flagger-recovery-alert-resolved-" + "1" * 32, logs.output[0])  # what to look at

    def poison_shape(self, fingerprint):
        """One of our own resolution ConfigMaps whose payload decodes cleanly but is not an
        object: a list, unlike ``poison``'s truncated write, ``json.loads`` accepts without
        complaint, so only the shape check catches it."""
        configmap = Document(kind=rules.KIND_ALERT_RESOLVED, key="2" * 32, payload={},
                             labels={rules.FINGERPRINT_LABEL: label_value(fingerprint)}).to_configmap()
        configmap["data"] = {"document.json": "[]"}
        self.transport.by_namespace.setdefault("flagger-system", {})[configmap["metadata"]["name"]] = configmap

    def test_a_non_object_stored_payload_holds_health_unknown_rather_than_500ing(self):
        """R19's remaining half: a ``document.json`` that is valid JSON but not an object must
        not reach ``AttributeError`` the first time a caller treats it as a mapping."""
        self.seed()
        self.poison_shape(self.alert()["fingerprint"])
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            status, body = self.fire()
        self.assertEqual((status, body["alerts"][0]["detail"]), (202, rules.HOLD_HEALTH_UNKNOWN))
        self.assertEqual((self.proposals(), self.documents(rules.KIND_ALERT)), ([], []))
        self.assertIn("MalformedRecord", logs.output[0])

    def test_a_non_object_stored_payload_does_not_cost_the_other_holds_their_sweep(self):
        """The same shape defect, met during a sweep rather than a fresh request: three holds
        still clear around the one whose resolution check cannot be read."""
        self.seed(current=False)  # every alert holds on no-promoted-record, which can clear
        [self.fire(fingerprint=f"cccc000000000{index}") for index in range(4)]
        self.poison_shape("cccc0000000001")
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            report = self.router.sweep()
        self.assertEqual((report.alerts_unreadable, report.alerts_resolved, report.alerts_held,
                          report.proposals_written, len(self.proposals())), (1, 3, 0, 1, 1))
        self.assertIn("flagger-recovery-alert-resolved-" + "2" * 32, logs.output[0])

class TolerantAlertListingTests(AlertTestCase):
    """R19's last residual: the sweep's own ``list_documents(KIND_ALERT, ...)`` decoded its
    whole selection eagerly, so one malformed ``alert`` document stalled every held alert
    forever. Over the real ``ConfigMapStore``, so the tolerant decode is the production one;
    every other listing — records, proposals, ``list_documents`` itself — stays fail-closed."""

    def setUp(self):
        super().setUp()
        self.transport = FakeTransport()
        self.store = ConfigMapStore("http://127.0.0.1:8001", transport=self.transport)
        self.router = self.build_router()

    def poison(self, kind, key, data):
        """One of our own ConfigMaps of ``kind``, whose ``data`` will not decode."""
        configmap = Document(kind=kind, key=key, payload={},
                             labels={"flagger-recovery/canary": canary_label(NAMESPACE, CANARY)}).to_configmap()
        configmap["data"] = data
        self.transport.by_namespace.setdefault("flagger-system", {})[configmap["metadata"]["name"]] = configmap

    def test_a_malformed_alert_document_is_counted_and_named_while_the_rest_still_sweep(self):
        """Bad JSON, alongside three healthy held alerts: the sweep completes, counts and
        names the one it could not read, and the other three are re-evaluated."""
        self.seed(current=False)  # every alert holds on no-promoted-record, which can clear
        [self.fire(fingerprint=f"bbbb000000000{index}") for index in range(3)]
        self.poison(rules.KIND_ALERT, "2" * 32, {"document.json": "{not json"})
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        with self.assertLogs("flagger_recovery.alerts", level="WARNING") as logs:
            report = self.router.sweep()
        self.assertEqual((report.alerts_unreadable, report.alerts_resolved, report.alerts_held,
                          report.proposals_written, len(self.proposals())), (1, 3, 0, 1, 1))
        self.assertEqual(sum("flagger-recovery-alert-" + "2" * 32 in line for line in logs.output), 1)

    def test_a_missing_key_alert_document_is_unreadable_the_same_way(self):
        self.poison(rules.KIND_ALERT, "3" * 32, {})  # no "document.json" at all
        documents, unreadable = self.store.list_documents_tolerant(
            rules.KIND_ALERT, canary=canary_label(NAMESPACE, CANARY))
        self.assertEqual((documents, unreadable), ([], ["flagger-recovery-alert-" + "3" * 32]))

    def test_a_non_object_alert_payload_is_unreadable_the_same_way(self):
        self.poison(rules.KIND_ALERT, "4" * 32, {"document.json": "[]"})
        documents, unreadable = self.store.list_documents_tolerant(
            rules.KIND_ALERT, canary=canary_label(NAMESPACE, CANARY))
        self.assertEqual((documents, unreadable), ([], ["flagger-recovery-alert-" + "4" * 32]))

    def test_tolerance_does_not_reach_records_or_a_default_alert_listing(self):
        """Only ``list_documents_tolerant`` is tolerant; the identical broken shape reached
        through a record listing, or ``list_documents`` on the same alert kind, still raises."""
        record = DeploymentRecord(phase="Failed", identity=BASE, created_at="2026-09-07T00:00:00Z")
        configmap = record.to_configmap()
        configmap["data"] = {"record.json": "{not json"}
        self.transport.by_namespace.setdefault("flagger-system", {})[configmap["metadata"]["name"]] = configmap
        with self.assertRaises(MalformedRecord):
            self.store.list(canary_label(NAMESPACE, CANARY))

        self.poison(KIND_PROPOSAL, "5" * 32, {"document.json": "{not json"})
        with self.assertRaises(MalformedRecord):
            self.store.list_documents(KIND_PROPOSAL)

        self.poison(rules.KIND_ALERT, "6" * 32, {"document.json": "{not json"})
        with self.assertRaises(MalformedRecord):
            self.store.list_documents(rules.KIND_ALERT, canary=canary_label(NAMESPACE, CANARY))

class RouteAndAuthTests(AlertTestCase):
    """The receiver's gate: an alert reaches the router only once it is size-checked,
    parsed and authenticated."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.receiver = Receiver(token=TOKEN, store=self.store, candidates=FakeCandidates(),
                                 alerts=self.router.handle)

    def body(self, **changes):
        return json.dumps(self.notification(**changes)).encode("utf-8")

    def test_every_accepted_way_of_presenting_the_shared_token(self):
        for label, headers in (("Alertmanager's bearer header", SIGNED),
                               ("a lower-case scheme name", {BEARER_HEADER: f"bearer {TOKEN}"}),
                               ("the receiver's own header", {TOKEN_HEADER: TOKEN})):
            with self.subTest(label):
                self.setUp()
                status, body = self.receiver.handle_alert(headers, self.body())
                self.assertEqual((status, body["alerts"][0]["result"]), (202, rules.PROPOSE_CORRECTION))

    def test_every_unauthenticated_form_is_401_and_stores_nothing(self):
        writes = self.store.writes
        for label, headers in (
            ("no header at all", {}),
            ("a wrong bearer token", {BEARER_HEADER: f"Bearer {TOKEN[:-1]}x"}),
            ("a truncated bearer token", {BEARER_HEADER: f"Bearer {TOKEN[:8]}"}),
            ("an empty bearer credential", {BEARER_HEADER: "Bearer "}),
            ("another scheme", {BEARER_HEADER: f"Basic {TOKEN}"}),
            ("the scheme name alone", {BEARER_HEADER: "Bearer"})):
            with self.subTest(label):
                self.assertEqual(self.receiver.handle_alert(headers, self.body()),
                                 (401, {"result": "Unauthorised"}))
        self.assertEqual((self.store.writes, self.receiver.unauthorised), (writes, 6))

    def test_bodies_the_router_never_sees(self):
        for label, body, expected in (("not json", b"not json", "MalformedJSON"),
                                      ("not an object", b"[1,2]", "MalformedJSON"),
                                      ("a v3 payload", self.body(version="3"), "MalformedAlert")):
            with self.subTest(label):
                self.assertEqual(self.receiver.handle_alert(SIGNED, body)[1]["result"], expected)
        self.assertEqual(self.proposals(), [])

    def test_the_default_handler_holds_rather_than_answering_a_success(self):
        receiver = Receiver(token=TOKEN, store=self.store, candidates=FakeCandidates())
        status, answer = receiver.handle_alert({TOKEN_HEADER: TOKEN}, self.body())
        self.assertEqual((status, answer["result"], answer["detail"]), (202, "Hold", "alerts-not-installed"))

    def test_a_forged_label_cannot_split_the_decision_line(self):
        with self.assertLogs("flagger_recovery.alerts", level="INFO") as logs:
            self.fire(fingerprint='ab result=Injected\nhook=post-rollout',
                      labels={rules.LABEL_CANARY: 'podinfo" result=Injected'})
        self.assertEqual(logs.output[-1].count('result="'), 1)
        self.assertNotIn("\n", logs.output[-1])

    def test_the_route_over_a_real_socket(self):
        server = build_server(self.receiver, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}{ALERT_PATH}"

        def request(method, data=None, headers=None):
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        url, data=data, method=method, headers=headers or {}), timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                with exc:
                    return exc.code, json.loads(exc.read())
        status, body = request("POST", self.body(), {**SIGNED, "Content-Type": "application/json"})
        self.assertEqual((status, body["alerts"][0]["result"]), (202, rules.PROPOSE_CORRECTION))
        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual(request("GET")[0], 405)
        self.assertEqual(request("POST", b"{}")[0], 401)

if __name__ == "__main__":
    unittest.main()
