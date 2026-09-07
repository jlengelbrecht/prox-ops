import dataclasses
import json
import re
import unittest
import urllib.parse
from pathlib import Path

from flagger_recovery.identity import CandidateIdentity, ContainerImage
from flagger_recovery.record import (
    CHECKSUM_LABEL,
    ConfigMapStore,
    DeploymentRecord,
    ForeignConfigMap,
    InMemoryStore,
    PutResult,
    canary_label,
    canary_label_full,
    checksum_label,
    label_value,
    make_key,
)

FIXTURES = Path(__file__).parent / "fixtures"

def _identity(**overrides) -> CandidateIdentity:
    fields = dict(
        namespace="flagger-pilot",
        canary_name="podinfo",
        deployment_uid="556b57bc-ee18-45bb-8d8d-9ccfb483e07d",
        template_hash="5b86bd6879",
        replicaset_hash="675556c6fd",
        images=(
            ContainerImage(
                name="app",
                repository="ghcr.io/stefanprodan/podinfo",
                tag="6.15.0",
                digest="ghcr.io/stefanprodan/podinfo@sha256:" + "e" * 64,
            ),
        ),
        chart_name="app-template",
        chart_version="4.4.0",
        oci_digest="sha256:" + "0" * 64,
        source_branch="flagger-pilot",
        source_sha="9" * 40,
        last_promoted_spec="759f9fb7bd",
        is_promoted=False,
        pr_number=None,
        sources={"namespace": "Canary.metadata.namespace"},
    )
    fields.update(overrides)
    return CandidateIdentity(**fields)

class FakeTransport:
    """A namespaced ConfigMap store keyed by name, logging every request so
    tests can assert exact shapes. Raises on anything but GET or POST."""

    def __init__(self) -> None:
        self.by_namespace: dict[str, dict[str, dict]] = {}
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, *, headers, body, ca_file, timeout):
        self.requests.append((method, url))
        if method not in ("GET", "POST"):
            raise AssertionError(f"unexpected HTTP method {method} for a record store")

        parsed = urllib.parse.urlparse(url)
        parts = parsed.path.split("/")
        # /api/v1/namespaces/<ns>/configmaps[/<name>]
        namespace = parts[4]
        store = self.by_namespace.setdefault(namespace, {})

        if method == "POST":
            configmap = json.loads(body)
            name = configmap["metadata"]["name"]
            if name in store:
                return 409, json.dumps({"reason": "AlreadyExists"}).encode()
            store[name] = configmap
            return 201, json.dumps(configmap).encode()

        if len(parts) >= 7 and parts[6]:
            name = parts[6]
            if name not in store:
                return 404, b"{}"
            return 200, json.dumps(store[name]).encode()

        query = urllib.parse.parse_qs(parsed.query)
        selector = query.get("labelSelector", [""])[0]
        items = list(store.values())
        # Every term must match, as the API server does it: find_candidate()
        # selects on canary, phase and checksum at once.
        for term in filter(None, selector.split(",")):
            key, _, value = term.partition("=")
            items = [item for item in items if item["metadata"]["labels"].get(key) == value]
        return 200, json.dumps({"items": items}).encode()

class ScriptedTransport:
    """Returns a fixed sequence of (status, body) responses, for the 409/404
    races a consistent backing dict (``FakeTransport``) can't produce on its
    own -- the ConfigMap changing shape between our create and our
    verification GET."""

    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str]] = []

    def request(self, method, url, *, headers, body, ca_file, timeout):
        self.requests.append((method, url))
        if method not in ("GET", "POST"):
            raise AssertionError(f"unexpected HTTP method {method} for a record store")
        return self._responses.pop(0)

class MakeKeyTests(unittest.TestCase):
    def test_deterministic_and_32_hex_chars(self):
        identity = _identity()
        key_a = make_key(identity, "Failed")
        key_b = make_key(identity, "Failed")
        self.assertEqual(key_a, key_b)
        self.assertEqual(len(key_a), 32)
        int(key_a, 16)  # raises if not hex

    def test_changes_with_template_hash(self):
        identity_a = _identity(template_hash="aaaaaaaaaa")
        identity_b = _identity(template_hash="bbbbbbbbbb")
        self.assertNotEqual(make_key(identity_a, "Failed"), make_key(identity_b, "Failed"))

    def test_changes_with_phase(self):
        identity = _identity()
        self.assertNotEqual(make_key(identity, "Failed"), make_key(identity, "Succeeded"))

    def test_unaffected_by_image_tag_or_timestamp(self):
        identity_a = _identity(
            images=(ContainerImage(name="app", repository="r", tag="1.0", digest="r@sha256:" + "a" * 64),)
        )
        identity_b = _identity(
            images=(ContainerImage(name="app", repository="r", tag="2.0", digest="r@sha256:" + "a" * 64),)
        )
        self.assertEqual(make_key(identity_a, "Failed"), make_key(identity_b, "Failed"))

class LabelValueTests(unittest.TestCase):
    _LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")

    def test_ascii_value_within_limit_is_used_verbatim(self):
        self.assertEqual(label_value("Progressing"), "Progressing")

    def test_non_ascii_input_falls_back_to_an_ascii_only_stem(self):
        # str.isalnum() accepts Unicode ("échec" would pass it), which would
        # let a non-ASCII stem back into the label value and violate the
        # Kubernetes label grammar. The stem here must be ASCII-only.
        value = label_value("échec")
        self.assertRegex(value, self._LABEL_VALUE_RE)
        self.assertNotIn("é", value)
        self.assertEqual(value, label_value("échec"))  # deterministic

    def test_oversized_value_is_clamped_and_deterministic(self):
        value = label_value("x" * 64)
        self.assertLessEqual(len(value), 63)
        self.assertRegex(value, self._LABEL_VALUE_RE)
        self.assertEqual(value, label_value("x" * 64))

class CanaryLabelTests(unittest.TestCase):
    def test_short_name_is_used_verbatim_with_no_annotation(self):
        label = canary_label("flagger-pilot", "podinfo")
        self.assertEqual(label, "flagger-pilot.podinfo")
        self.assertLessEqual(len(label), 63)
        self.assertIsNone(canary_label_full("flagger-pilot", "podinfo"))

    def test_long_name_falls_back_to_a_stable_short_hash(self):
        namespace = "a" * 40
        canary_name = "b" * 40
        label_a = canary_label(namespace, canary_name)
        label_b = canary_label(namespace, canary_name)

        self.assertLessEqual(len(label_a), 63)
        self.assertEqual(label_a, label_b)  # deterministic
        self.assertEqual(canary_label_full(namespace, canary_name), f"{namespace}.{canary_name}")

        # A different pair of long names must not collide on the short form.
        other_label = canary_label("c" * 40, "d" * 40)
        self.assertNotEqual(label_a, other_label)

class DeploymentRecordRoundTripTests(unittest.TestCase):
    def test_to_configmap_from_configmap_round_trip(self):
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        configmap = record.to_configmap()
        restored = DeploymentRecord.from_configmap(configmap)
        self.assertEqual(record, restored)

    def test_configmap_labels(self):
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        labels = record.to_configmap()["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/part-of"], "flagger-recovery")
        self.assertEqual(labels["flagger-recovery/canary"], canary_label("flagger-pilot", "podinfo"))
        self.assertEqual(labels["flagger-recovery/phase"], "Failed")
        self.assertEqual(labels["flagger-recovery/template-hash"], "5b86bd6879")

    def test_the_payload_checksum_is_a_label_but_never_the_key(self):
        """The bridge: the record stays keyed by the template hash, so existing
        records and every lookup by template hash keep working, and the payload
        checksum rides along as an index."""
        plain = DeploymentRecord(phase="candidate", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        indexed = dataclasses.replace(plain, checksum="5f5697644f")

        self.assertEqual(
            plain.to_configmap()["metadata"]["name"], indexed.to_configmap()["metadata"]["name"]
        )
        self.assertEqual(indexed.to_configmap()["metadata"]["labels"][CHECKSUM_LABEL], "5f5697644f")
        self.assertNotIn(CHECKSUM_LABEL, plain.to_configmap()["metadata"]["labels"])
        self.assertEqual(DeploymentRecord.from_configmap(indexed.to_configmap()), indexed)

    def test_an_unlabelable_checksum_is_clamped_the_same_way_on_both_sides(self):
        hostile = "../" + "z" * 80
        record = DeploymentRecord(phase="candidate", identity=_identity(),
                                  created_at="2026-09-07T00:00:00Z", checksum=hostile)
        label = record.to_configmap()["metadata"]["labels"][CHECKSUM_LABEL]
        self.assertRegex(label, r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
        self.assertLessEqual(len(label), 63)
        self.assertEqual(checksum_label(hostile), label)

    def test_no_annotation_when_canary_label_fits(self):
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        self.assertNotIn("annotations", record.to_configmap()["metadata"])

    def test_full_value_annotated_when_canary_label_is_shortened(self):
        identity = _identity(namespace="a" * 40, canary_name="b" * 40)
        record = DeploymentRecord(phase="Failed", identity=identity, created_at="2026-09-07T00:00:00Z")
        metadata = record.to_configmap()["metadata"]
        self.assertEqual(metadata["annotations"]["flagger-recovery/canary-full"], f"{identity.namespace}.{identity.canary_name}")
        self.assertEqual(metadata["labels"]["flagger-recovery/canary"], canary_label(identity.namespace, identity.canary_name))

class ConfigMapStoreConstructionTests(unittest.TestCase):
    def test_https_with_token_is_allowed(self):
        store = ConfigMapStore("https://kubernetes.default.svc", token="secret", transport=FakeTransport())
        self.assertEqual(store._base_url, "https://kubernetes.default.svc")

    def test_http_with_token_is_rejected(self):
        with self.assertRaises(ValueError):
            ConfigMapStore("http://kubernetes.default.svc", token="secret", transport=FakeTransport())

    def test_loopback_http_without_token_is_allowed(self):
        for base_url in ("http://127.0.0.1:8001", "http://localhost:8001"):
            with self.subTest(base_url=base_url):
                store = ConfigMapStore(base_url, transport=FakeTransport())
                self.assertEqual(store._base_url, base_url)

    def test_loopback_http_with_token_is_rejected(self):
        for base_url in ("http://127.0.0.1:8001", "http://localhost:8001"):
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    ConfigMapStore(base_url, token="secret", transport=FakeTransport())

class ConfigMapStoreTests(unittest.TestCase):
    def _store(self, transport):
        return ConfigMapStore("http://127.0.0.1:8001", transport=transport)

    def test_put_creates_then_confirms_duplicate_via_get(self):
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")

        self.assertEqual(store.put(record), PutResult.CREATED)
        self.assertEqual(store.put(record), PutResult.DUPLICATE)

        # 409 on the second create is not itself the answer -- a GET must
        # confirm the existing ConfigMap belongs to this record.
        self.assertEqual(
            transport.requests,
            [
                ("POST", transport.requests[0][1]),
                ("POST", transport.requests[1][1]),
                ("GET", transport.requests[2][1]),
            ],
        )
        self.assertTrue(transport.requests[0][1].endswith("/api/v1/namespaces/flagger-system/configmaps"))

    def test_find_candidate_selects_on_canary_phase_and_checksum(self):
        transport = FakeTransport()
        store = self._store(transport)
        wanted = DeploymentRecord(phase="candidate", identity=_identity(), created_at="2026-09-07T00:00:00Z",
                                  checksum="5f5697644f")
        store.put(wanted)
        # Same checksum and canary but promoted, not candidate; and a candidate
        # for the same canary under a different checksum. Neither may be picked.
        store.put(dataclasses.replace(wanted, phase="promoted"))
        store.put(dataclasses.replace(wanted, identity=_identity(template_hash="576f8b8d6"),
                                      checksum="687b888d46"))
        canary = canary_label("flagger-pilot", "podinfo")

        self.assertEqual(store.find_candidate(canary, "5f5697644f"), wanted)
        self.assertEqual(store.find_candidate(canary, "687b888d46").identity.template_hash, "576f8b8d6")
        self.assertIsNone(store.find_candidate(canary, "no-such-checksum"))
        self.assertIsNone(store.find_candidate(canary, ""), "an empty checksum indexes nothing")
        self.assertIsNone(store.find_candidate(canary_label("flagger-pilot", "other"), "5f5697644f"))
        selector = urllib.parse.parse_qs(urllib.parse.urlparse(transport.requests[-1][1]).query)["labelSelector"][0]
        self.assertEqual(sorted(selector.split(",")), [
            f"flagger-recovery/canary={canary_label('flagger-pilot', 'other')}",
            f"{CHECKSUM_LABEL}=5f5697644f", "flagger-recovery/phase=candidate"])

    def test_find_candidate_refuses_when_two_records_share_a_checksum(self):
        """Impossible by construction and disastrous if guessed at (NFR4), so
        the ambiguity answers ``None`` and the caller stays pending."""
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="candidate", identity=_identity(), created_at="2026-09-07T00:00:00Z",
                                  checksum="5f5697644f")
        store.put(record)
        store.put(dataclasses.replace(record, identity=_identity(template_hash="576f8b8d6")))

        with self.assertLogs("flagger_recovery.record", level="WARNING"):
            self.assertIsNone(store.find_candidate(canary_label("flagger-pilot", "podinfo"), "5f5697644f"))

    def test_put_conflict_with_a_foreign_configmap_raises(self):
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        name = record.to_configmap()["metadata"]["name"]

        # Something else already occupies our deterministic name, with a
        # different phase label -- not our record.
        foreign = {
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/part-of": "flagger-recovery",
                    "flagger-recovery/canary": canary_label("flagger-pilot", "podinfo"),
                    "flagger-recovery/phase": "Succeeded",
                    "flagger-recovery/template-hash": "5b86bd6879",
                },
            },
            "data": {"record.json": "{}"},
        }
        transport.by_namespace.setdefault("flagger-system", {})[name] = foreign

        with self.assertRaises(ForeignConfigMap) as ctx:
            store.put(record)
        self.assertEqual(ctx.exception.name, name)

    def test_put_conflict_deleted_then_still_conflicting_on_retry_gives_up(self):
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        name = record.to_configmap()["metadata"]["name"]
        transport = ScriptedTransport(
            [
                (409, json.dumps({"reason": "AlreadyExists"}).encode()),  # initial create
                (404, b"{}"),  # verification GET: deleted in between
                (409, json.dumps({"reason": "AlreadyExists"}).encode()),  # retried create: still conflicting
            ]
        )
        store = self._store(transport)

        with self.assertRaises(ForeignConfigMap) as ctx:
            store.put(record)
        self.assertEqual(ctx.exception.name, name)
        self.assertEqual([method for method, _ in transport.requests], ["POST", "GET", "POST"])

    def test_get_by_name(self):
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        store.put(record)

        key = make_key(record.identity, record.phase)
        fetched = store.get(key)
        self.assertEqual(fetched, record)
        self.assertIn(("GET", f"http://127.0.0.1:8001/api/v1/namespaces/flagger-system/configmaps/flagger-recovery-{key}"), transport.requests)

    def test_get_missing_returns_none(self):
        store = self._store(FakeTransport())
        self.assertIsNone(store.get("0" * 32))

    def test_get_rejects_a_key_that_is_not_32_hex_chars(self):
        transport = FakeTransport()
        store = self._store(transport)
        with self.assertRaises(ValueError):
            store.get("../../secrets")
        self.assertEqual(transport.requests, [])

    def test_list_with_label_selector(self):
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        store.put(record)

        results = store.list(canary_label("flagger-pilot", "podinfo"))
        self.assertEqual(results, [record])

        list_requests = [req for req in transport.requests if "labelSelector" in req[1]]
        self.assertEqual(len(list_requests), 1)
        self.assertIn("flagger-recovery%2Fcanary", list_requests[0][1])

    def test_no_configmap_request_ever_uses_patch_put_or_delete(self):
        transport = FakeTransport()
        store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        store.put(record)
        store.put(record)
        store.get(make_key(record.identity, record.phase))
        store.list(canary_label("flagger-pilot", "podinfo"))

        methods = {method for method, _ in transport.requests}
        self.assertEqual(methods, {"POST", "GET"})

    def test_get_after_simulated_restart_finds_the_record(self):
        transport = FakeTransport()
        first_process_store = self._store(transport)
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")
        first_process_store.put(record)

        second_process_store = self._store(transport)  # a fresh store instance, same backing API
        fetched = second_process_store.get(make_key(record.identity, record.phase))
        self.assertEqual(fetched, record)

class InMemoryStoreTests(unittest.TestCase):
    def test_implements_the_same_protocol(self):
        store = InMemoryStore()
        record = DeploymentRecord(phase="Failed", identity=_identity(), created_at="2026-09-07T00:00:00Z")

        self.assertEqual(store.put(record), PutResult.CREATED)
        self.assertEqual(store.put(record), PutResult.DUPLICATE)

        key = make_key(record.identity, record.phase)
        self.assertEqual(store.get(key), record)
        self.assertEqual(store.list(canary_label("flagger-pilot", "podinfo")), [record])
        self.assertIsNone(store.get("0" * 32))
        self.assertEqual(store.list("no-such-canary"), [])

if __name__ == "__main__":
    unittest.main()
