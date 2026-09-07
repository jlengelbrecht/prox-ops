import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from flagger_recovery.auth import TOKEN_HEADER
from flagger_recovery.identity import AttributionRefused
from flagger_recovery.inbox import KIND_EVENT, STATUS_ATTRIBUTION_PENDING, STATUS_RECEIVED
from flagger_recovery.kube import ApiError
from flagger_recovery.record import DeploymentRecord, InMemoryStore, make_key_parts
from flagger_recovery.server import (
    DECIDER_NOT_INSTALLED,
    MAX_BODY_BYTES,
    PHASE_CANDIDATE,
    PHASE_PROMOTED,
    Decision,
    Ignore,
    Receiver,
    _reconcile_startup_note,
    build_server,
    reconcile_interval,
    start_reconcile_loop,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "t" * 32
TEMPLATE_HASH = "759f9fb7bd"

def _load(name: str):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)

class FakeCandidates:
    """A ``CandidateSource`` over the recorded live objects. ``failure`` makes
    it raise, which is how the attribution-refused paths are driven."""

    def __init__(self, failure: Exception = None) -> None:
        self.failure = failure
        self.reads: list[tuple[str, str]] = []

    def read(self, namespace: str, canary_name: str):
        self.reads.append((namespace, canary_name))
        if self.failure is not None:
            raise self.failure
        return {
            "canary": _load("canary.json"),
            "deployment": _load("deployment.json"),
            "candidate_pods": _load("pods.json")["items"],
            "candidate_replicasets": _load("replicasets.json")["items"],
            "helmrelease": _load("helmrelease.json"),
            "ocirepository": _load("ocirepository.json"),
            "kustomization": _load("kustomization.json"),
        }

def _payload(**overrides):
    payload = {
        "name": "podinfo",
        "namespace": "flagger-pilot",
        "phase": "Progressing",
        "checksum": TEMPLATE_HASH,
        "metadata": {"token": TOKEN},
    }
    payload.update(overrides)
    return payload

def _body(**overrides) -> bytes:
    return json.dumps(_payload(**overrides)).encode("utf-8")

class ReceiverTestCase(unittest.TestCase):
    def setUp(self):
        self.store = InMemoryStore()
        self.candidates = FakeCandidates()
        self.receiver = Receiver(
            token=TOKEN, store=self.store, candidates=self.candidates, clock=lambda: "2026-09-07T00:00:00Z"
        )

    def post(self, hook, body=None, headers=None):
        return self.receiver.handle(hook, headers or {}, _body() if body is None else body)

class PreRolloutTests(ReceiverTestCase):
    def test_registers_the_candidate_and_returns_202(self):
        status, payload = self.post("pre-rollout")

        self.assertEqual(status, 202)
        self.assertEqual(payload["result"], "Registered")
        self.assertEqual(self.candidates.reads, [("flagger-pilot", "podinfo")])

        record = self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE))
        self.assertIsNotNone(record)
        self.assertEqual(record.phase, PHASE_CANDIDATE)
        self.assertEqual(record.identity.template_hash, TEMPLATE_HASH)
        self.assertEqual(len(self.store.list_documents(KIND_EVENT)), 1)

    def test_second_identical_hook_is_a_duplicate_that_creates_nothing(self):
        self.post("pre-rollout")
        writes_after_first = self.store.writes

        status, payload = self.post("pre-rollout")

        self.assertEqual((status, payload["result"]), (202, "Duplicate"))
        self.assertEqual(self.store.writes, writes_after_first)
        self.assertEqual(len(self.candidates.reads), 1, "a known duplicate must not re-read live state")

    def test_attribution_refusal_is_202_with_a_pending_event_and_a_retry_hint(self):
        self.candidates.failure = AttributionRefused("no candidate pods observed yet")
        status, payload = self.post("pre-rollout")

        self.assertEqual(status, 202)
        self.assertEqual(payload["result"], "AttributionPending")
        self.assertGreater(payload["retry_after_seconds"], 0)

        events = self.store.list_documents(KIND_EVENT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["status"], STATUS_ATTRIBUTION_PENDING)
        self.assertIn("no candidate pods observed yet", events[0].payload["detail"])
        self.assertIsNone(self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE)))

    def test_a_live_read_failure_is_also_pending_never_a_5xx(self):
        self.candidates.failure = ApiError(503, "https://kubernetes.default.svc/apis/flagger.app/v1beta1")
        status, payload = self.post("pre-rollout")
        self.assertEqual((status, payload["result"]), (202, "AttributionPending"))

class PostRolloutTests(ReceiverTestCase):
    def test_succeeded_promotes_the_recorded_identity_without_re_resolving(self):
        self.post("pre-rollout")
        self.candidates.failure = AssertionError("promotion must not re-resolve; the candidate pods are gone")

        status, payload = self.post("post-rollout", _body(phase="Succeeded"))

        self.assertEqual((status, payload["result"]), (202, "Promoted"))
        promoted = self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_PROMOTED))
        self.assertIsNotNone(promoted)
        self.assertEqual(promoted.phase, PHASE_PROMOTED)
        candidate = self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE))
        self.assertEqual(promoted.identity, candidate.identity)

    def test_succeeded_without_a_candidate_record_is_pending_not_a_guess(self):
        status, payload = self.post("post-rollout", _body(phase="Succeeded"))
        self.assertEqual((status, payload["result"]), (202, "AttributionPending"))
        self.assertEqual(self.store.list_documents(KIND_EVENT)[0].payload["status"], STATUS_ATTRIBUTION_PENDING)

    def test_failed_is_accepted_durably_and_creates_no_record(self):
        status, payload = self.post("post-rollout", _body(phase="Failed"))

        self.assertEqual((status, payload["result"]), (202, "Ignore"))
        self.assertEqual(payload["detail"], DECIDER_NOT_INSTALLED)
        events = self.store.list_documents(KIND_EVENT)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["status"], STATUS_RECEIVED)
        self.assertEqual(events[0].payload["phase"], "Failed")
        self.assertIn(DECIDER_NOT_INSTALLED, events[0].payload["detail"])
        self.assertIsNone(self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE)))

    def test_event_hook_is_accepted_and_deduplicated(self):
        self.assertEqual(self.post("event")[1]["result"], "Ignore")
        self.assertEqual(self.post("event")[1]["result"], "Duplicate")
        self.assertEqual(self.store.writes, 1)

class DeciderTests(ReceiverTestCase):
    """The hook slice 3's ``decide.py`` plugs into, without a route changing."""

    def test_default_decider_ignores_and_records_the_reason_on_the_event(self):
        status, payload = self.post("post-rollout", _body(phase="Failed"))
        self.assertEqual((status, payload["result"]), (202, "Ignore"))
        self.assertEqual(payload["detail"], DECIDER_NOT_INSTALLED)

    def test_an_injected_decider_is_called_with_the_stored_candidate_record_and_the_event(self):
        self.post("pre-rollout")  # stores the candidate record the decider should see
        calls = []

        def fake_decider(record, event):
            calls.append((record, event))
            return Ignore("manual-rollback-restored")

        receiver = Receiver(
            token=TOKEN, store=self.store, candidates=self.candidates, decider=fake_decider,
        )
        status, payload = receiver.handle("post-rollout", {}, _body(phase="Failed"))

        self.assertEqual((status, payload["result"]), (202, "Ignore"))
        self.assertEqual(payload["detail"], "manual-rollback-restored")
        self.assertEqual(len(calls), 1)
        record, event = calls[0]
        self.assertIsInstance(record, DeploymentRecord)
        self.assertEqual(record.phase, PHASE_CANDIDATE)
        self.assertEqual(event.phase, "Failed")

    def test_an_injected_decider_can_report_a_kind_other_than_ignore(self):
        receiver = Receiver(
            token=TOKEN, store=self.store, candidates=self.candidates,
            decider=lambda record, event: Decision(kind="Refuse", reason="not implemented yet"),
        )
        status, payload = receiver.handle("event", {}, _body())
        self.assertEqual((status, payload["result"]), (202, "Refuse"))

class RejectionTests(ReceiverTestCase):
    def assertNothingStored(self):
        self.assertEqual(self.store.writes, 0)
        self.assertEqual(self.candidates.reads, [])

    def test_missing_wrong_and_mistyped_tokens_are_401(self):
        for body in (
            _body(metadata={}),
            _body(metadata={"token": "wrong"}),
            _body(metadata={"token": TOKEN[:-1]}),
            json.dumps({"name": "podinfo", "namespace": "flagger-pilot"}).encode("utf-8"),
        ):
            with self.subTest(body=body):
                status, payload = self.post("pre-rollout", body)
                self.assertEqual((status, payload["result"]), (401, "Unauthorised"))
        self.assertNothingStored()
        self.assertEqual(self.receiver.unauthorised, 4)

    def test_a_valid_token_in_a_header_is_accepted(self):
        status, payload = self.post("pre-rollout", _body(metadata={}), headers={TOKEN_HEADER: TOKEN})
        self.assertEqual((status, payload["result"]), (202, "Registered"))

    def test_non_json_and_non_object_bodies_are_400(self):
        for body in (b"not json", b"", b"[1, 2, 3]", b'"a string"', b"\xff\xfe"):
            with self.subTest(body=body):
                status, payload = self.post("pre-rollout", body)
                self.assertEqual((status, payload["result"]), (400, "MalformedJSON"))
        self.assertNothingStored()

    def test_an_authenticated_but_malformed_payload_is_400(self):
        status, payload = self.post("pre-rollout", json.dumps({"metadata": {"token": TOKEN}}).encode())
        self.assertEqual((status, payload["result"]), (400, "MalformedPayload"))
        self.assertNothingStored()

    def test_an_oversized_body_is_413_and_is_never_parsed(self):
        body = b"x" * (MAX_BODY_BYTES + 1)
        status, payload = self.post("pre-rollout", body)
        self.assertEqual((status, payload["result"]), (413, "PayloadTooLarge"))
        self.assertNothingStored()

    def test_a_receiver_without_a_token_cannot_be_built(self):
        with self.assertRaises(ValueError):
            Receiver(token="", store=self.store, candidates=self.candidates)

class DurabilityTests(ReceiverTestCase):
    """A failure to write the record itself must not be masked as a
    duplicate on retry: the inbox is only marked seen once the write it
    describes has actually landed."""

    class _FlakyStore:
        """Wraps a real store and raises once from ``put()``, then delegates."""

        def __init__(self, inner):
            self._inner = inner
            self.put_calls = 0

        def put(self, record):
            self.put_calls += 1
            if self.put_calls == 1:
                raise RuntimeError("simulated store outage")
            return self._inner.put(record)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def test_a_failed_record_write_leaves_the_event_unseen_so_a_retry_can_still_register(self):
        flaky = self._FlakyStore(self.store)
        receiver = Receiver(
            token=TOKEN, store=flaky, candidates=self.candidates, clock=lambda: "2026-09-07T00:00:00Z"
        )

        with self.assertRaises(RuntimeError):
            receiver.handle("pre-rollout", {}, _body())

        self.assertEqual(
            len(self.store.list_documents(KIND_EVENT)), 0,
            "the inbox must not be marked seen when the record write failed",
        )

        status, payload = receiver.handle("pre-rollout", {}, _body())
        self.assertEqual((status, payload["result"]), (202, "Registered"))

class RetryableAttributionPendingTests(ReceiverTestCase):
    """``attribution-pending`` is not a terminal outcome: the document store
    is create-only, so the stored event can never be flipped to a finished
    status in place. A redelivery of the same hook must re-run resolution,
    not answer ``Duplicate`` for a candidate that was never recorded."""

    class _FlakyDocumentStore:
        """Wraps a real store and raises once from ``put_document()``, then delegates."""

        def __init__(self, inner):
            self._inner = inner
            self.put_document_calls = 0

        def put_document(self, document):
            self.put_document_calls += 1
            if self.put_document_calls == 1:
                raise RuntimeError("simulated event store outage")
            return self._inner.put_document(document)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def test_pending_then_success_on_retry_registers_the_candidate(self):
        self.candidates.failure = AttributionRefused("no candidate pods observed yet")
        status, payload = self.post("pre-rollout")
        self.assertEqual((status, payload["result"]), (202, "AttributionPending"))
        self.assertIsNone(self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE)))

        self.candidates.failure = None
        status, payload = self.post("pre-rollout")

        self.assertEqual((status, payload["result"]), (202, "Registered"))
        self.assertEqual(len(self.candidates.reads), 2, "a pending event must re-run resolution on retry")
        record = self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE))
        self.assertIsNotNone(record)

    def test_record_written_but_event_missing_on_retry_creates_no_second_record(self):
        flaky = self._FlakyDocumentStore(self.store)
        receiver = Receiver(
            token=TOKEN, store=flaky, candidates=self.candidates, clock=lambda: "2026-09-07T00:00:00Z"
        )

        with self.assertRaises(RuntimeError):
            receiver.handle("pre-rollout", {}, _body())

        record_key = make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE)
        self.assertIsNotNone(self.store.get(record_key), "the record must have landed before the event write failed")
        self.assertEqual(len(self.store.list_documents(KIND_EVENT)), 0)
        writes_after_first = self.store.writes

        status, payload = receiver.handle("pre-rollout", {}, _body())

        self.assertEqual((status, payload["result"]), (202, "Duplicate"))
        self.assertEqual(self.store.writes, writes_after_first + 1, "only the event should be written on retry")
        self.assertEqual(len(self.store.list_documents(KIND_EVENT)), 1)

    def test_processed_event_short_circuits_with_zero_writes(self):
        self.post("pre-rollout")
        writes_after_first = self.store.writes
        reads_after_first = len(self.candidates.reads)

        status, payload = self.post("pre-rollout")

        self.assertEqual((status, payload["result"]), (202, "Duplicate"))
        self.assertEqual(self.store.writes, writes_after_first)
        self.assertEqual(len(self.candidates.reads), reads_after_first, "a processed event must not re-resolve")

class HttpTests(unittest.TestCase):
    """Drives the real handler over a loopback socket on an ephemeral port."""

    def setUp(self):
        self.store = InMemoryStore()
        receiver = Receiver(token=TOKEN, store=self.store, candidates=FakeCandidates())
        self.server = build_server(receiver, host="127.0.0.1", port=0)
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        request = urllib.request.Request(
            self.base_url + path, data=body, method=method, headers=headers or {}
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read())

    def test_health_endpoints(self):
        for path in ("/healthz", "/readyz"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path), (200, {"result": "ok"}))

    def test_a_signed_pre_rollout_hook_is_accepted_over_http(self):
        """The integration proof the PR body cites: a real socket, a real
        ``InMemoryStore`` and a fake reader, one signed request in, one
        candidate record out."""
        status, payload = self.request("POST", "/hooks/pre-rollout", _body())
        self.assertEqual(status, 202)
        self.assertEqual(payload["result"], "Registered")

        record = self.store.get(make_key_parts("flagger-pilot", "podinfo", TEMPLATE_HASH, PHASE_CANDIDATE))
        self.assertIsNotNone(record)
        self.assertEqual(record.phase, PHASE_CANDIDATE)
        self.assertEqual(record.identity.template_hash, TEMPLATE_HASH)

    def test_bad_token_is_401_over_http_and_stores_nothing(self):
        status, payload = self.request("POST", "/hooks/pre-rollout", _body(metadata={"token": "wrong"}))
        self.assertEqual((status, payload["result"]), (401, "Unauthorised"))
        self.assertEqual(self.store.writes, 0)

    def test_unknown_paths_are_404_and_health_paths_reject_post(self):
        self.assertEqual(self.request("GET", "/nope"), (404, {"result": "NotFound"}))
        self.assertEqual(self.request("POST", "/hooks/rollback", b"{}"), (404, {"result": "NotFound"}))
        self.assertEqual(self.request("POST", "/healthz", b"{}"), (405, {"result": "MethodNotAllowed"}))
        self.assertEqual(self.request("GET", "/hooks/pre-rollout"), (405, {"result": "MethodNotAllowed"}))
        self.assertEqual(self.store.writes, 0)

    def test_an_oversized_body_is_413_over_http(self):
        status, _ = self.request("POST", "/hooks/pre-rollout", b"x" * (MAX_BODY_BYTES + 1))
        self.assertEqual(status, 413)
        self.assertEqual(self.store.writes, 0)

    def test_non_json_body_is_400_over_http(self):
        self.assertEqual(self.request("POST", "/hooks/event", b"not json")[0], 400)
        self.assertEqual(self.store.writes, 0)

class ReconcileIntervalTests(unittest.TestCase):
    """``nan`` and ``inf`` parse without raising and defeat both ``<= 0`` and
    ``max()``, and ``Event.wait(nan)`` returns at once — a hot loop."""

    def test_every_env_value_is_disabled_or_a_finite_interval_above_the_floor(self):
        for raw, expected in (("", 300.0), ("abc", 300.0), ("nan", 300.0), ("inf", 300.0), ("-inf", 300.0),
                              ("0", 0.0), ("-5", 0.0), ("5", 10.0), ("600", 600.0)):
            with self.subTest(raw=raw):
                interval = reconcile_interval({"RECONCILE_INTERVAL_SECONDS": raw})
                self.assertEqual(interval, expected)
                self.assertTrue(interval == 0.0 or interval >= 10.0)

class ReconcileLoopTests(unittest.TestCase):
    def test_start_returns_before_the_first_pass_completes(self):
        started = threading.Event()
        release = threading.Event()
        finished = []
        def pass_fn():
            started.set()
            release.wait(timeout=5)
            finished.append(1)
            return "ok"

        stop = start_reconcile_loop(pass_fn, 0.0)
        self.addCleanup(stop.set)
        try:
            self.assertTrue(started.wait(timeout=5), "the pass must start promptly")
            self.assertEqual(finished, [], "start_reconcile_loop must not block on the pass")
        finally:
            release.set()

    def test_the_pass_runs_exactly_once_when_the_timer_is_disabled(self):
        calls = []
        done = threading.Event()
        def pass_fn():
            calls.append(1)
            done.set()
            return "ok"

        stop = start_reconcile_loop(pass_fn, 0.0)
        self.addCleanup(stop.set)
        self.assertTrue(done.wait(timeout=5))
        # A disabled timer must never schedule a second call; give a wrongly
        # re-armed loop a moment to prove it stayed off before asserting.
        time.sleep(0.1)
        self.assertEqual(calls, [1])

class ReconcileStartupNoteTests(unittest.TestCase):
    def test_a_disabled_timer_reads_as_startup_only(self):
        self.assertEqual(
            _reconcile_startup_note("flagger-pilot", "podinfo", 0.0),
            "reconcile: startup pass only, periodic timer disabled",
        )

    def test_a_positive_interval_keeps_the_every_n_seconds_wording(self):
        self.assertEqual(
            _reconcile_startup_note("flagger-pilot", "podinfo", 300.0),
            "reconciling flagger-pilot/podinfo every 300s",
        )

class RestartTests(unittest.TestCase):
    def test_a_restarted_receiver_reads_prior_records_and_never_duplicates(self):
        store = InMemoryStore()
        first = Receiver(token=TOKEN, store=store, candidates=FakeCandidates())
        first.handle("pre-rollout", {}, _body())
        writes_before_restart = store.writes

        # A new process over the same durable store: the pre-rollout redelivery
        # Flagger sends after a restart must not create a second record.
        second = Receiver(token=TOKEN, store=store, candidates=FakeCandidates())
        status, payload = second.handle("pre-rollout", {}, _body())

        self.assertEqual((status, payload["result"]), (202, "Duplicate"))
        self.assertEqual(store.writes, writes_before_restart)
        self.assertEqual(len(store.list_documents(KIND_EVENT)), 1)

if __name__ == "__main__":
    unittest.main()
