import http.client
import json
import threading
import unittest

from stubs import SECRET, VAULT, StubConnect

from access import Broker
from server import McpApp, build_server

CALLER_TOKEN = "caller-" + "c" * 57
PILOT_KEY = "pilot-" + "p" * 58


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.connect = StubConnect()
        self.app = McpApp(Broker(self.connect, VAULT), CALLER_TOKEN)
        self.server = build_server(self.app, host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method="POST", path="/mcp", body=None, token=CALLER_TOKEN, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        try:
            conn.request(method, path, body=data, headers=sent)
            response = conn.getresponse()
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), raw
        finally:
            conn.close()

    def rpc(self, method, params=None, msg_id=1, **kwargs):
        message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            message["params"] = params
        return self.request(body=message, **kwargs)

    def call(self, name, arguments, **kwargs):
        return self.rpc("tools/call", {"name": name, "arguments": arguments}, **kwargs)

    def test_initialize_and_list_expose_exactly_the_two_broker_tools(self):
        status, body, _ = self.rpc("initialize", {"protocolVersion": "2025-06-18",
                                                  "capabilities": {},
                                                  "clientInfo": {"name": "test"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", body["result"]["capabilities"])

        status, body, _ = self.rpc("tools/list", {})
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        self.assertEqual(set(tools), {"create_generated_item", "deliver_item_to_consumer"})
        self.assertEqual(tools["create_generated_item"]["inputSchema"]["properties"]["vault"]["enum"],
                         [VAULT])
        self.assertEqual(
            tools["deliver_item_to_consumer"]["inputSchema"]["properties"]["destination"]["enum"],
            ["connectivity-check"])

    def test_notifications_are_accepted_without_a_body(self):
        status, body, _ = self.request(body={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_missing_wrong_or_alternate_bearer_is_refused_before_connect(self):
        for token in (None, "", PILOT_KEY, CALLER_TOKEN[:-1], CALLER_TOKEN + "x"):
            with self.subTest(token=token):
                status, _, _ = self.call("create_generated_item",
                                         {"vault": VAULT, "title": "x"}, token=token)
                self.assertEqual(status, 401)
        status, _, _ = self.request(body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                    token=None, headers={"Authorization": f"Basic {CALLER_TOKEN}"})
        self.assertEqual(status, 401)
        self.assertEqual(self.connect.calls, [])

    def test_browser_origins_are_refused(self):
        status, _, _ = self.rpc("tools/list", {}, headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_generic_update_delete_and_alias_tools_do_not_exist(self):
        for name in ("get_item", "update_item", "delete_item", "proxy", "execute_action",
                     "create_item", "ai-access_create_generated_item", "Create_Generated_Item",
                     "create_generated_item ", None):
            with self.subTest(name=name):
                status, body, _ = self.call(name, {"vault": VAULT, "title": "x"})
                self.assertEqual(status, 200)
                self.assertEqual(body["error"]["code"], -32602)
        self.assertEqual(self.connect.calls, [])

    def test_wrong_vault_and_extra_arguments_are_tool_errors_before_connect(self):
        cases = (
            {"vault": "Private", "title": "x"},
            {"vault": VAULT, "title": "x", "fields": [{"value": "chosen"}]},
            {"vault": VAULT},
            ["AI-Access", "x"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                status, body, _ = self.call("create_generated_item", arguments)
                self.assertEqual(status, 200)
                self.assertTrue(body["result"]["isError"])
        self.assertEqual(self.connect.calls, [])

    def test_create_then_deliver_keeps_the_value_server_side(self):
        with self.assertLogs("onepassword_agent_mcp", "INFO") as logs:
            status, created, raw_create = self.call("create_generated_item",
                                                    {"vault": VAULT, "title": "omarchy check"})
            item_id = created["result"]["structuredContent"]["item"]["id"]
            status_retry, retried, raw_retry = self.call("create_generated_item",
                                                         {"vault": VAULT, "title": "omarchy check"})
            status_deliver, delivered, raw_deliver = self.call(
                "deliver_item_to_consumer",
                {"vault": VAULT, "item_id": item_id, "destination": "connectivity-check"})

        self.assertEqual((status, status_retry, status_deliver), (200, 200, 200))
        self.assertTrue(created["result"]["structuredContent"]["created"])
        self.assertFalse(retried["result"]["structuredContent"]["created"])
        receipt = delivered["result"]["structuredContent"]["receipt"]
        self.assertEqual(receipt, {"value_present": True})
        for raw in (raw_create, raw_retry, raw_deliver):
            self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(SECRET, "\n".join(logs.output))
        self.assertEqual(sum(1 for call in self.connect.calls if call[0] == "POST"), 1)

    def test_batches_and_malformed_messages_are_refused(self):
        status, _, _ = self.request(body=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(status, 400)
        status, body, _ = self.request(body=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32700)
        status, body, _ = self.rpc("resources/list", {})
        self.assertEqual(body["error"]["code"], -32601)

    def test_oversized_bodies_are_refused(self):
        status, _, _ = self.request(body=b"{" + b" " * (64 * 1024) + b"}")
        self.assertEqual(status, 413)

    def test_chunked_bodies_are_read(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        payload = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}).encode()
        try:
            conn.request("POST", "/mcp", body=iter([payload[:10], payload[10:]]),
                         headers={"Authorization": f"Bearer {CALLER_TOKEN}",
                                  "Content-Type": "application/json"})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())["id"], 9)
        finally:
            conn.close()

    def test_health_is_open_and_other_methods_are_refused(self):
        status, body, _ = self.request("GET", "/health", token=None)
        self.assertEqual((status, body), (200, {"status": "ok"}))
        for method in ("GET", "DELETE", "PUT", "PATCH"):
            with self.subTest(method=method):
                status, _, _ = self.request(method, "/mcp")
                self.assertEqual(status, 405)
        status, _, _ = self.request("POST", "/v1/vaults", body={})
        self.assertEqual(status, 404)

    def test_short_caller_tokens_are_refused_at_startup(self):
        with self.assertRaises(ValueError):
            McpApp(Broker(StubConnect(), VAULT), "short")


if __name__ == "__main__":
    unittest.main()
