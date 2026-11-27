import io
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from stubs import OTHER_VAULT_ID, SECRET, VAULT, VAULT_ID, StubConnect

import access
from access import AGENT_TAG, Broker, ConnectClient, ConnectError, Rejected

TOKEN = "connect-token-" + "t" * 40


class RecordingConsumer:
    def __init__(self):
        self.values = []

    def __call__(self, value):
        self.values.append(value)
        return {"accepted": True}


class BrokerCreateTest(unittest.TestCase):
    def setUp(self):
        self.connect = StubConnect()
        self.broker = Broker(self.connect, VAULT)

    def test_create_generates_server_side_and_returns_metadata_only(self):
        with self.assertLogs("onepassword_agent_mcp", "INFO") as logs:
            result = self.broker.create_generated_item(VAULT, "omarchy disposable")

        self.assertTrue(result["created"])
        self.assertEqual(result["item"]["vault"], {"id": VAULT_ID, "name": VAULT})
        self.assertEqual(result["item"]["tags"], [AGENT_TAG])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(result["item"]["fields"],
                         [{"id": "password", "label": "password", "type": "CONCEALED",
                           "purpose": "PASSWORD"}])

        posts = [call for call in self.connect.calls if call[0] == "POST"]
        self.assertEqual(len(posts), 1)
        method, path, _, body = posts[0]
        self.assertEqual(path, f"/v1/vaults/{VAULT_ID}/items")
        self.assertEqual(body["vault"], {"id": VAULT_ID})
        (field,) = body["fields"]
        self.assertTrue(field["generate"])
        self.assertNotIn("value", field, "the broker must never supply the secret itself")

    def test_retry_reports_the_existing_item_instead_of_duplicating_it(self):
        first = self.broker.create_generated_item(VAULT, "retry me")
        second = self.broker.create_generated_item(VAULT, "retry me")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertIn("already exists", second["reason"])
        self.assertEqual(second["item"]["id"], first["item"]["id"])
        self.assertEqual(sum(1 for call in self.connect.calls if call[0] == "POST"), 1)
        self.assertNotIn(SECRET, json.dumps(second))

    def test_any_vault_but_the_bound_name_is_refused_before_connect(self):
        for vault in ("Private", "ai-access", "AI Access", "AI-Access ", " AI-Access",
                      VAULT_ID, "Automation", "", None, ["AI-Access"], {"name": VAULT}):
            with self.subTest(vault=vault):
                with self.assertRaises(Rejected):
                    self.broker.create_generated_item(vault, "title")
        self.assertEqual(self.connect.calls, [])

    def test_titles_that_could_alter_the_connect_filter_are_refused_before_connect(self):
        for title in ('x" or title eq "y', "", "a" * 101, "-leading", "new\nline",
                      "tab\there", "quote'", None, 7):
            with self.subTest(title=title):
                with self.assertRaises(Rejected):
                    self.broker.create_generated_item(VAULT, title)
        self.assertEqual(self.connect.calls, [])


class BrokerDeliverTest(unittest.TestCase):
    def setUp(self):
        self.connect = StubConnect()
        self.consumer = RecordingConsumer()
        self.broker = Broker(self.connect, VAULT, {"test-consumer": self.consumer})
        self.item_id = self.broker.create_generated_item(VAULT, "deliver me")["item"]["id"]
        self.connect.calls.clear()

    def test_value_reaches_the_consumer_and_never_the_response_or_logs(self):
        with self.assertLogs("onepassword_agent_mcp", "INFO") as logs:
            result = self.broker.deliver_item_to_consumer(VAULT, self.item_id, "test-consumer")

        self.assertEqual(self.consumer.values, [SECRET])
        self.assertTrue(result["delivered"])
        self.assertEqual(result["receipt"], {"accepted": True})
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(self.connect.calls,
                         [("GET", f"/v1/vaults/{VAULT_ID}/items/{self.item_id}", {}, None)])

    def test_unapproved_destinations_are_refused_before_connect(self):
        for destination in ("connectivity-check", "test-consumer ", "Test-Consumer",
                            "https://example.com/hook", "", None, ["test-consumer"]):
            with self.subTest(destination=destination):
                with self.assertRaises(Rejected):
                    self.broker.deliver_item_to_consumer(VAULT, self.item_id, destination)
        self.assertEqual(self.connect.calls, [])
        self.assertEqual(self.consumer.values, [])

    def test_wrong_vault_or_malformed_item_is_refused_before_connect(self):
        for vault, item_id in ((OTHER_VAULT_ID, self.item_id), ("Private", self.item_id),
                               (VAULT, "../" + self.item_id), (VAULT, self.item_id.upper()),
                               (VAULT, self.item_id + "/files"), (VAULT, ""), (VAULT, None)):
            with self.subTest(vault=vault, item_id=item_id):
                with self.assertRaises(Rejected):
                    self.broker.deliver_item_to_consumer(vault, item_id, "test-consumer")
        self.assertEqual(self.connect.calls, [])
        self.assertEqual(self.consumer.values, [])

    def test_item_reported_from_another_vault_is_not_delivered(self):
        self.connect.item_vault_override = OTHER_VAULT_ID
        with self.assertRaises(ConnectError):
            self.broker.deliver_item_to_consumer(VAULT, self.item_id, "test-consumer")
        self.assertEqual(self.consumer.values, [])

    def test_builtin_connectivity_check_confirms_presence_only(self):
        broker = Broker(self.connect, VAULT)
        result = broker.deliver_item_to_consumer(VAULT, self.item_id, "connectivity-check")
        self.assertEqual(result["receipt"], {"value_present": True})
        self.assertNotIn(SECRET, json.dumps(result))


class VaultResolutionTest(unittest.TestCase):
    def test_ambiguous_vault_fails_closed(self):
        connect = StubConnect(vaults=[{"id": VAULT_ID, "name": VAULT},
                                      {"id": OTHER_VAULT_ID, "name": VAULT}])
        with self.assertRaises(ConnectError):
            Broker(connect, VAULT).create_generated_item(VAULT, "title")
        self.assertEqual([call[1] for call in connect.calls], ["/v1/vaults"])

    def test_additional_vault_fails_closed(self):
        connect = StubConnect(vaults=[{"id": VAULT_ID, "name": VAULT},
                                      {"id": OTHER_VAULT_ID, "name": "Private"}])
        with self.assertRaises(ConnectError):
            Broker(connect, VAULT).create_generated_item(VAULT, "title")
        self.assertEqual(connect.calls, [("GET", "/v1/vaults", {}, None)])

    def test_missing_vault_fails_closed(self):
        connect = StubConnect(vaults=[])
        with self.assertRaises(ConnectError):
            Broker(connect, VAULT).create_generated_item(VAULT, "title")
        self.assertEqual([call[1] for call in connect.calls], ["/v1/vaults"])

    def test_vault_lookup_is_cached(self):
        connect = StubConnect()
        broker = Broker(connect, VAULT)
        broker.create_generated_item(VAULT, "one")
        broker.create_generated_item(VAULT, "two")
        self.assertEqual(sum(1 for call in connect.calls if call[1] == "/v1/vaults"), 1)

    def test_bound_vault_name_must_be_plain(self):
        for name in ("", 'AI"Access', None):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Broker(StubConnect(), name)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class FakeOpener:
    def __init__(self, response=b"[]", error=None):
        self.requests = []
        self.response = response
        self.error = error

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error:
            raise self.error
        return FakeResponse(self.response)


class ConnectClientTest(unittest.TestCase):
    def test_only_bare_https_origins_are_accepted(self):
        for origin in ("http://op.example", "https://op.example/v1", "https://user@op.example",
                       "https://op.example?x=1", "https://", "op.example"):
            with self.subTest(origin=origin):
                with self.assertRaises(ValueError):
                    ConnectClient(origin, TOKEN)
        with self.assertRaises(ValueError):
            ConnectClient("https://op.example", "")

    def test_requests_go_to_the_pinned_origin_with_the_bearer_token(self):
        opener = FakeOpener(response=b'[{"id": "x"}]')
        client = ConnectClient("https://op.example/", TOKEN, opener=opener)

        self.assertEqual(client.request("GET", "/v1/vaults", {"filter": 'name eq "AI-Access"'}),
                         [{"id": "x"}])
        (request, _), = opener.requests
        self.assertEqual(request.full_url,
                         "https://op.example/v1/vaults?filter=name%20eq%20%22AI-Access%22")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_method(), "GET")

    def test_error_bodies_and_transport_details_are_not_surfaced(self):
        error = urllib.error.HTTPError("https://op.example/v1/vaults", 500, "boom", {},
                                       io.BytesIO(SECRET.encode()))
        client = ConnectClient("https://op.example", TOKEN, opener=FakeOpener(error=error))
        with self.assertRaises(ConnectError) as raised:
            client.request("GET", "/v1/vaults")
        self.assertEqual(str(raised.exception), "Connect answered HTTP 500")
        self.assertIsNone(raised.exception.__cause__)

        client = ConnectClient("https://op.example", TOKEN,
                               opener=FakeOpener(error=urllib.error.URLError(SECRET)))
        with self.assertRaises(ConnectError) as raised:
            client.request("GET", "/v1/vaults")
        self.assertNotIn(SECRET, str(raised.exception))

    def test_default_opener_neither_follows_redirects_nor_uses_proxies(self):
        with patch("urllib.request.getproxies", return_value={"https": "http://proxy.example"}):
            handlers = ConnectClient("https://op.example", TOKEN)._opener.handlers
        self.assertFalse(any(type(h) is urllib.request.HTTPRedirectHandler for h in handlers))
        self.assertTrue(any(isinstance(h, access._NoRedirect) for h in handlers))
        proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertTrue(all(h.proxies == {} for h in proxies))
        self.assertIsNone(access._NoRedirect().redirect_request(None, None, 302, "", {},
                                                                "https://elsewhere.example"))


if __name__ == "__main__":
    unittest.main()
