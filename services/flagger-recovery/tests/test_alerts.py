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
from flagger_recovery.record import (ApiWriteError, DeploymentRecord, Document, InMemoryStore,
                                     canary_label, make_key_parts)
from flagger_recovery.server import ALERT_PATH, PHASE_PROMOTED, Receiver, build_server
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

def promoted_record(template_hash, source_sha, created_at):
    """A promoted record as the store really holds one: written out through
    ``to_configmap`` and read back, so these tests run against the promotion stamp a
    live promotion leaves behind rather than a hand-built dataclass."""
    identity = dataclasses.replace(BASE, template_hash=template_hash, source_sha=source_sha)
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
        self.router = rules.AlertRouter(self.store, lambda namespace, name: self.live,
                                        lambda: self.revision, clock=lambda: self.now)

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

    def fire(self, **changes):
        return self.router.handle(self.notification(alerts=[self.alert(**changes)]))

    def resolution(self, **changes):
        return self.router.handle(self.notification(
            status=rules.STATUS_RESOLVED, alerts=[self.alert(status=rules.STATUS_RESOLVED, **changes)]))

    def documents(self, kind):
        return self.store.list_documents(kind)

    def proposals(self):
        return self.documents(KIND_PROPOSAL)

class ParseTests(AlertTestCase):
    def test_the_documented_shape_parses_into_one_attributable_alert(self):
        group_key, alerts = rules.parse(self.notification())
        self.assertIn(NAMESPACE, group_key)
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0].status, alerts[0].canary, alerts[0].kind),
                         (rules.STATUS_FIRING, (NAMESPACE, CANARY), rules.KIND_ALERT))
        self.assertIn(rules.ANNOTATION_EVIDENCE, alerts[0].annotations)

    def test_every_rejected_shape_is_400_and_stores_nothing(self):
        """Anything but the published v4 contract: this reads five fields of it and must
        not guess at a payload that is not it."""
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
        }.items():
            with self.subTest(label):
                with self.assertRaises(rules.MalformedAlert):
                    rules.parse(payload)
                self.assertEqual(self.router.handle(payload)[0], 400)
        self.assertEqual(self.store.writes, 0)

    def test_hostile_labels_are_bounded_and_claim_nothing(self):
        oversized = {f"k{index}": "v" * 4000 for index in range(rules.MAX_METADATA_ENTRIES + 20)}
        _, alerts = rules.parse(self.notification(alerts=[{**self.alert(), "labels": oversized}]))
        self.assertEqual(len(alerts[0].labels), rules.MAX_METADATA_ENTRIES)
        self.assertTrue(all(len(value) <= rules.MAX_FIELD_LENGTH for value in alerts[0].labels.values()))
        self.assertIsNone(alerts[0].canary, "no identity labels survived, so nothing is claimed")

class DecisionTests(AlertTestCase):
    def test_a_firing_alert_proposes_restoring_the_promotion_before_the_serving_one(self):
        self.seed()
        status, body = self.fire()
        answer = body["alerts"][0]
        self.assertEqual((status, answer["result"]), (202, rules.PROPOSE_CORRECTION))
        self.assertEqual(len(self.proposals()), 1)
        payload = self.proposals()[0].payload
        # Attribution is to the promoted revision; the correction points at the promotion
        # before it, never at "whatever changed most recently".
        self.assertEqual(
            (payload["template_hash"], payload["phase"], payload["failed_source_sha"],
             payload["last_promoted_source_sha"], payload["correction"], payload["requires_decision"]),
            (PROMOTED_HASH, PHASE_ALERT_PROPOSAL, PROMOTED_SHA, PRIOR_SHA, RESTORE, False))
        self.assertEqual(self.proposals()[0].key,
                         make_key_parts(NAMESPACE, CANARY, PROMOTED_HASH, PHASE_ALERT_PROPOSAL))
        self.assertEqual(answer["proposal"], self.proposals()[0].key)

    def test_the_alert_document_records_the_decision_and_the_evidence_behind_it(self):
        self.seed()
        self.fire()
        stored = self.documents(rules.KIND_ALERT)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].payload["decision"], rules.PROPOSE_CORRECTION)
        self.assertEqual(stored[0].payload["received_at"], self.now)
        self.assertEqual(stored[0].payload["labels"][rules.LABEL_NAMESPACE], NAMESPACE)
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
                self.assertEqual(stored["evidence"]["functional_check"], expected)
                self.assertEqual(stored["decision"], rules.PROPOSE_CORRECTION)

    def test_a_resolution_is_recorded_under_its_own_kind_and_decides_nothing(self):
        self.seed()
        status, body = self.resolution(endsAt="2026-09-08T09:20:00Z")
        self.assertEqual((status, body["alerts"][0]["result"]), (202, rules.RECORDED))
        self.assertEqual(self.proposals(), [])
        self.assertEqual(self.documents(rules.KIND_ALERT), [], "a resolution is not a held alert")
        recorded = self.documents(rules.KIND_ALERT_RESOLVED)
        self.assertEqual((len(recorded), recorded[0].payload["decision"], recorded[0].payload["status"]),
                         (1, rules.RECORDED, rules.STATUS_RESOLVED))

    def test_a_resolution_never_undoes_a_proposal_already_written(self):
        self.seed()
        self.fire()
        writes = self.store.writes
        self.resolution()
        self.assertEqual(len(self.proposals()), 1)
        self.assertEqual(self.store.writes, writes + 1, "only the resolution document")

    def test_a_repeated_firing_alert_is_a_duplicate_that_writes_nothing(self):
        self.seed()
        self.fire()
        writes = self.store.writes
        self.live.failure = AssertionError("a duplicate must not re-read live state")
        status, body = self.fire()
        self.assertEqual((status, body["alerts"][0]["result"]), (202, rules.DUPLICATE))
        self.assertEqual(self.store.writes, writes)

    def test_three_firing_alerts_on_one_revision_produce_exactly_one_proposal(self):
        self.seed()
        grouped = [self.alert(fingerprint=f"aaaa000000000{index}",
                              labels={"alertname": f"FlaggerPilotApexAlert{index}"})
                   for index in range(3)]
        status, body = self.router.handle(self.notification(alerts=grouped))
        self.assertEqual(status, 202)
        self.assertEqual([answer["result"] for answer in body["alerts"]], [rules.PROPOSE_CORRECTION] * 3)
        self.assertEqual(len(self.documents(rules.KIND_ALERT)), 3, "each alert is its own record")
        self.assertEqual(len(self.proposals()), 1, "one proposal per failed promoted revision")
        self.assertEqual({answer["proposal"] for answer in body["alerts"]}, {self.proposals()[0].key})

class HoldTests(AlertTestCase):
    """The closed hold set. Every one stores the alert with its reason and stores no
    proposal: a hold is a decision on the record, not a silence."""

    def _hold(self, reason, *, prior=True, current=True, seed=True, live=None, revision=None, **changes):
        self.live, self.revision = live or self.live, revision or self.revision
        if seed:
            self.seed(prior=prior, current=current)
        status, body = self.fire(**changes)
        answer = body["alerts"][0]
        self.assertEqual((status, answer["result"], answer["detail"]), (202, rules.HOLD, reason))
        stored = self.documents(rules.KIND_ALERT)
        self.assertEqual(len(stored), 1)
        self.assertEqual((stored[0].payload["decision"], stored[0].payload["reason"],
                          stored[0].payload["proposal"]), (rules.HOLD, reason, ""))
        self.assertEqual(self.proposals(), [], "a hold never writes a proposal")

    # AC4's last two: a status that says nothing about which revision is serving, and a
    # Flux revision that cannot be read, are unknown health -- never a success decision.
    CASES = (
        ("no identity labels", rules.HOLD_UNATTRIBUTED,
         dict(labels={rules.LABEL_NAMESPACE: None, rules.LABEL_CANARY: None})),
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

    def test_an_unattributed_alert_is_labelled_as_such_rather_than_as_some_canary(self):
        self._hold(rules.HOLD_UNATTRIBUTED, **self.CASES[0][2])
        self.assertEqual(self.documents(rules.KIND_ALERT)[0].labels["flagger-recovery/canary"],
                         rules.UNATTRIBUTED)

    def test_a_promotion_newer_than_the_serving_one_is_not_something_to_restore_back_to(self):
        self.store.put(promoted_record(PROMOTED_HASH, PROMOTED_SHA, PROMOTED_AT))
        self.store.put(promoted_record(OTHER_HASH, OTHER_SHA, "2026-09-08T09:00:00Z"))
        self._hold(rules.HOLD_NO_PRIOR_PROMOTED, seed=False)

    def test_temporal_proximity_is_never_an_input(self):
        """AC2. The alert that proposes with its evidence holds without it, whatever the
        clock says: only the corroboration changed."""
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
                    self._hold(rules.HOLD_HEALTH_UNKNOWN, live=setup.get("live"),
                               revision=setup.get("revision"))
                self.assertNotIn("api.invalid", logs.output[0])

    def test_a_bug_in_the_ladder_is_not_laundered_into_a_hold(self):
        """``_UNREADABLE`` is a named set, not ``Exception``: a programming error still
        reaches the handler's 500 rather than being recorded as unknown health."""
        self.seed()
        self.live.functional_check = _raising(RuntimeError("a bug, not an outage"))
        with self.assertRaises(RuntimeError):
            self.fire()
        self.assertEqual(self.documents(rules.KIND_ALERT), [])

class SweepTests(AlertTestCase):
    """AC3: durable decisions converge after an interruption, one action per failed
    update, with the window recorded."""

    def test_a_hold_whose_reason_has_cleared_is_re_evaluated_into_one_proposal(self):
        self.seed()
        self.live = FakeLive(applied=OTHER_HASH)  # mid-rollout
        self.fire()
        self.assertEqual(self.documents(rules.KIND_ALERT)[0].payload["reason"],
                         rules.HOLD_ROLLOUT_IN_PROGRESS)

        self.live = FakeLive()  # the rollout finished
        report = self.router.sweep()
        self.assertEqual((report.alerts_held, report.alerts_resolved, report.proposals_written), (0, 1, 1))
        self.assertEqual(len(self.proposals()), 1)

        # A second pass re-derives the same answer and writes nothing: the proposal key is
        # create-only, so convergence is one action, not one per tick.
        writes = self.store.writes
        second = self.router.sweep()
        self.assertEqual((second.alerts_resolved, second.proposals_written), (1, 0))
        self.assertEqual(self.store.writes, writes)

    def test_what_the_sweep_leaves_alone(self):
        self.seed(current=False)
        self.fire()  # held: no promoted record
        self.resolution()  # a resolution, which no sweep reads
        self.store.put_document(Document(  # a held document from before a field existed
            kind=rules.KIND_ALERT, key="0" * 32,
            labels={"flagger-recovery/canary": canary_label(NAMESPACE, CANARY)},
            payload={"decision": rules.HOLD, "reason": rules.HOLD_HEALTH_UNKNOWN, "fingerprint": "abc"}))

        report = self.router.sweep()
        self.assertEqual((report.alerts_held, report.alerts_resolved, report.proposals_written), (2, 0, 0))
        self.assertEqual(self.proposals(), [], "nothing escalates a hold for being old")

    def test_the_first_run_records_a_null_window_never_a_zero(self):
        report = self.router.sweep()
        self.assertIsNone(report.interruption_window_seconds)
        runs = self.documents(rules.KIND_RECEIVER_RUN)
        self.assertEqual((len(runs), runs[0].payload["started_at"], runs[0].payload["last_tick_at"]),
                         (1, self.now, ""))
        writes = self.store.writes
        self.router.sweep()  # once per process, not once per pass
        self.assertEqual((self.store.writes, len(self.documents(rules.KIND_RECEIVER_RUN))), (writes, 1))

    def test_a_restart_records_the_window_between_the_last_evidence_and_this_start(self):
        self.seed(current=False)
        self.fire()  # a held alert at 09:15:00, the last thing this receiver did
        self.router.sweep()

        self.now = "2026-09-08T09:47:30Z"  # a new process over the same durable store
        restarted = rules.AlertRouter(self.store, lambda namespace, name: self.live,
                                      lambda: self.revision, clock=lambda: self.now)
        report = restarted.sweep()

        self.assertEqual(report.interruption_window_seconds, 1950.0)
        runs = self.documents(rules.KIND_RECEIVER_RUN)
        self.assertEqual(len(runs), 2)
        latest = max(runs, key=lambda document: document.payload["started_at"])
        self.assertEqual((latest.payload["last_tick_at"], latest.payload["interruption_window_seconds"]),
                         ("2026-09-08T09:15:00Z", 1950.0))
        self.assertEqual(report.alerts_held, 1, "the pass still converges over what it missed")

class RouteAndAuthTests(AlertTestCase):
    """The receiver's own gate: an alert reaches the router only once it has been
    size-checked, parsed and authenticated."""

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
            ("the scheme name alone", {BEARER_HEADER: "Bearer"}),
        ):
            with self.subTest(label):
                self.assertEqual(self.receiver.handle_alert(headers, self.body()),
                                 (401, {"result": "Unauthorised"}))
        self.assertEqual((self.store.writes, self.receiver.unauthorised), (writes, 6))

    def test_bodies_the_router_never_sees(self):
        for label, body, expected in (("not json", b"not json", "MalformedJSON"),
                                      ("not an object", b"[1,2]", "MalformedJSON"),
                                      ("a v3 payload", self.body(version="3"), "MalformedAlert")):
            with self.subTest(label):
                status, answer = self.receiver.handle_alert(SIGNED, body)
                self.assertEqual((status, answer["result"]), (400, expected))
        self.assertEqual(self.proposals(), [])

    def test_the_default_handler_holds_rather_than_answering_a_success(self):
        receiver = Receiver(token=TOKEN, store=self.store, candidates=FakeCandidates())
        status, answer = receiver.handle_alert({TOKEN_HEADER: TOKEN}, self.body())
        self.assertEqual((status, answer["result"], answer["detail"]),
                         (202, "Hold", "alerts-not-installed"))

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
