import json
import unittest

from flagger_recovery.inbox import (
    KIND_EVENT,
    MAX_FIELD_LENGTH,
    MAX_METADATA_ENTRIES,
    STATUS_ATTRIBUTION_PENDING,
    STATUS_RECEIVED,
    Inbox,
    MalformedPayload,
    WebhookEvent,
)
from flagger_recovery.record import InMemoryStore, PutResult, canary_label

PAYLOAD = {
    "name": "podinfo",
    "namespace": "flagger-pilot",
    "phase": "Progressing",
    "checksum": "5b86bd6879",
    "metadata": {"token": "token-must-not-be-stored", "type": "pre-rollout"},
}

def _event(**overrides) -> WebhookEvent:
    payload = dict(PAYLOAD)
    payload.update(overrides.pop("payload", {}))
    return WebhookEvent.parse(overrides.pop("hook", "pre-rollout"), payload)

class ParseTests(unittest.TestCase):
    def test_parses_the_flagger_payload_shape(self):
        event = _event()
        self.assertEqual(event.hook, "pre-rollout")
        self.assertEqual(event.name, "podinfo")
        self.assertEqual(event.namespace, "flagger-pilot")
        self.assertEqual(event.phase, "Progressing")
        self.assertEqual(event.checksum, "5b86bd6879")

    def test_token_never_survives_into_the_event(self):
        event = _event()
        self.assertEqual(event.metadata, {"type": "pre-rollout"})
        self.assertNotIn("token-must-not-be-stored", str(event.to_document(status=STATUS_RECEIVED, received_at="t")))

    def test_phase_and_checksum_are_optional(self):
        event = WebhookEvent.parse("event", {"name": "podinfo", "namespace": "flagger-pilot"})
        self.assertEqual((event.phase, event.checksum), ("", ""))

    def test_missing_or_blank_required_fields_are_malformed(self):
        for payload in ({"namespace": "flagger-pilot"}, {"name": " ", "namespace": "flagger-pilot"}, {"name": "p"}):
            with self.subTest(payload=payload):
                with self.assertRaises(MalformedPayload):
                    WebhookEvent.parse("event", payload)

    def test_required_fields_are_stripped_not_stored_with_surrounding_whitespace(self):
        event = WebhookEvent.parse("event", {"name": " podinfo ", "namespace": "flagger-pilot"})
        self.assertEqual(event.name, "podinfo")

    def test_required_fields_with_inner_whitespace_or_control_chars_are_malformed(self):
        for name in ("pod info", "podinfo\tx", "pod\ninfo", "pod\x00info"):
            with self.subTest(name=name):
                with self.assertRaises(MalformedPayload):
                    WebhookEvent.parse("event", {"name": name, "namespace": "flagger-pilot"})

    def test_wrongly_typed_fields_are_malformed(self):
        with self.assertRaises(MalformedPayload):
            WebhookEvent.parse("event", {"name": 1, "namespace": "flagger-pilot"})
        with self.assertRaises(MalformedPayload):
            WebhookEvent.parse("event", {"name": "p", "namespace": "n", "metadata": ["not", "a", "map"]})

    def test_unknown_hook_is_malformed(self):
        with self.assertRaises(MalformedPayload):
            WebhookEvent.parse("rollback", PAYLOAD)

    def test_oversized_fields_and_metadata_are_bounded(self):
        with self.assertRaises(MalformedPayload):
            WebhookEvent.parse("event", {"name": "x" * (MAX_FIELD_LENGTH + 1), "namespace": "n"})
        crowded = {"name": "p", "namespace": "n", "metadata": {f"k{i:03d}": "v" * 900 for i in range(200)}}
        event = WebhookEvent.parse("event", crowded)
        self.assertEqual(len(event.metadata), MAX_METADATA_ENTRIES)
        self.assertTrue(all(len(value) <= MAX_FIELD_LENGTH for value in event.metadata.values()))

    def test_a_token_hidden_behind_a_crowded_metadata_map_is_still_never_stored(self):
        metadata = {f"k{i:03d}": "v" for i in range(MAX_METADATA_ENTRIES * 4)}
        metadata["token"] = "token-must-not-be-stored"
        event = WebhookEvent.parse("event", {"name": "p", "namespace": "n", "metadata": metadata})
        self.assertNotIn("token", event.metadata)
        self.assertNotIn("token-must-not-be-stored", json.dumps(event.metadata))

class KeyTests(unittest.TestCase):
    def test_key_is_32_hex_chars_and_deterministic(self):
        self.assertEqual(_event().key, _event().key)
        self.assertEqual(len(_event().key), 32)
        int(_event().key, 16)

    def test_key_changes_with_every_component(self):
        base = _event().key
        self.assertNotEqual(base, _event(hook="post-rollout").key)
        self.assertNotEqual(base, _event(payload={"phase": "Failed"}).key)
        self.assertNotEqual(base, _event(payload={"checksum": "74bf8ddcdd"}).key)
        self.assertNotEqual(base, _event(payload={"name": "other"}).key)
        self.assertNotEqual(base, _event(payload={"namespace": "other"}).key)

    def test_key_ignores_metadata_and_arrival_time(self):
        self.assertEqual(_event(payload={"metadata": {"type": "other"}}).key, _event().key)

class DocumentTests(unittest.TestCase):
    def test_labels_index_the_event_for_a_reconcile_pass(self):
        document = _event().to_document(status=STATUS_ATTRIBUTION_PENDING, received_at="2026-09-07T00:00:00Z")
        labels = document.to_configmap()["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/part-of"], "flagger-recovery")
        self.assertEqual(labels["flagger-recovery/kind"], KIND_EVENT)
        self.assertEqual(labels["flagger-recovery/canary"], canary_label("flagger-pilot", "podinfo"))
        self.assertEqual(labels["flagger-recovery/hook"], "pre-rollout")
        self.assertEqual(labels["flagger-recovery/phase"], "Progressing")
        self.assertEqual(labels["flagger-recovery/status"], STATUS_ATTRIBUTION_PENDING)
        self.assertEqual(labels["flagger-recovery/template-hash"], "5b86bd6879")

    def test_configmap_name_is_kind_scoped(self):
        document = _event().to_document(status=STATUS_RECEIVED, received_at="t")
        self.assertEqual(document.name, f"flagger-recovery-event-{_event().key}")

    def test_hostile_label_material_is_clamped_not_rejected(self):
        event = _event(payload={"phase": "Failed\nInjected: yes", "checksum": ""})
        labels = event.to_document(status=STATUS_RECEIVED, received_at="t").to_configmap()["metadata"]["labels"]
        self.assertNotIn("\n", labels["flagger-recovery/phase"])
        self.assertEqual(labels["flagger-recovery/template-hash"], "none")

    def test_pending_events_carry_a_detail_and_a_retry_hint(self):
        document = _event().to_document(
            status=STATUS_ATTRIBUTION_PENDING, received_at="t", detail="no candidate pods yet", retry_after_seconds=30
        )
        self.assertEqual(document.payload["detail"], "no candidate pods yet")
        self.assertEqual(document.payload["retry_after_seconds"], 30)

class InboxTests(unittest.TestCase):
    def test_first_accept_creates_and_the_repeat_is_a_no_op(self):
        store = InMemoryStore()
        inbox = Inbox(store)
        event = _event()

        self.assertFalse(inbox.seen(event))
        self.assertEqual(inbox.accept(event, status=STATUS_RECEIVED, received_at="t"), PutResult.CREATED)
        self.assertTrue(inbox.seen(event))
        self.assertEqual(inbox.accept(event, status=STATUS_RECEIVED, received_at="t"), PutResult.DUPLICATE)
        self.assertEqual(store.writes, 1)

    def test_stored_events_are_listable_by_kind_and_canary(self):
        store = InMemoryStore()
        inbox = Inbox(store)
        inbox.accept(_event(), status=STATUS_RECEIVED, received_at="t")
        inbox.accept(_event(hook="event"), status=STATUS_RECEIVED, received_at="t")

        self.assertEqual(len(store.list_documents(KIND_EVENT)), 2)
        self.assertEqual(
            len(store.list_documents(KIND_EVENT, canary=canary_label("flagger-pilot", "podinfo"))), 2
        )
        self.assertEqual(store.list_documents(KIND_EVENT, canary="other.canary"), [])
        self.assertEqual(store.list_documents("proposal"), [])
