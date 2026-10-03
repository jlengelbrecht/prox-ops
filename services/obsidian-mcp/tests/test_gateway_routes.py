"""Real local HTTP gateway route and MCP integration fixtures."""

import asyncio
import base64
import io
import re
import socket
import sys
import threading
import time
import unittest
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from fastmcp import Client
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval import ApprovalCoordinator, VaultEnrollment  # noqa: E402
from gateway import create_gateway  # noqa: E402
from owner_auth import OwnerAuthConfig, OwnerAuthenticator  # noqa: E402
from test_gateway import BridgeFixture, GuardedFixture, SAFE, TOKENS  # noqa: E402


@asynccontextmanager
async def running(app):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           lifespan="on", log_config=None, access_log=False))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.01)
    if not server.started:
        raise AssertionError("local HTTP server did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = BridgeFixture()
        self.guarded = GuardedFixture()
        config = OwnerAuthConfig(
            "http://authentik.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx",
            "https://obsidian.homelab0.org", frozenset({"owner"}), frozenset({"iam", "homelab"}))

        self.auth_checks = []

        def check(call):
            self.auth_checks.append(call)
            cookies = SimpleCookie()
            cookies.load(call.headers.get("cookie", ""))
            return (httpx.Response(200, headers={"X-authentik-uid": "owner"})
                    if cookies.get("session") and cookies["session"].value == "fixture"
                    else httpx.Response(401))

        self.check_client = httpx.AsyncClient(transport=httpx.MockTransport(check))
        self.auth = OwnerAuthenticator(config, self.check_client)
        self.grants = {
            "codex": {"iam": SAFE | {"replace_note"}, "homelab": SAFE},
            "claude": {"iam": SAFE},
            "opencode": {"homelab": SAFE},
            "antigravity": {"iam": frozenset({"read_note", "read_media"})},
        }
        self.app = create_gateway(
            self.bridge, TOKENS, self.grants, self.auth,
            {vault: VaultEnrollment(frozenset({"owner"}), {}) for vault in ("iam", "homelab")},
            destructive_adapter=self.guarded, destructive_verbs=frozenset({"replace_note"}),
        )

    async def asyncTearDown(self):
        await self.check_client.aclose()

    async def test_mcp_host_and_origin_boundary_over_http(self):
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "host-origin-test", "version": "1"}}}
        base = {"Authorization": "Bearer " + TOKENS["codex"],
                "Accept": "application/json, text/event-stream"}
        deployed = "obsidian-mcp.homelab0.org"
        async with running(self.app) as url:
            async with httpx.AsyncClient() as browser:
                for host, origin in ((deployed, None), (deployed + ":443", None),
                                     (deployed, "https://" + deployed),
                                     (deployed + ":443", "https://" + deployed + ":443")):
                    headers = {**base, "Host": host}
                    if origin:
                        headers["Origin"] = origin
                    with self.subTest(host=host, origin=origin):
                        response = await browser.post(url + "/mcp", headers=headers, json=initialize)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("result", response.json())

                local = await browser.post(url + "/mcp", headers={**base,
                    "Origin": url}, json=initialize)
                self.assertEqual(local.status_code, 200)
                self.assertIn("result", local.json())

                denied = (
                    ({"Host": "attacker.invalid"}, 421),
                    ({"Host": deployed + ":8443"}, 421),
                    ({"Host": "127.0.0.2"}, 421),
                    ({"Host": "localhost:99999"}, 421),
                    ({"Host": "attacker.invalid", "X-Forwarded-Host": deployed,
                      "X-Forwarded-Proto": "https", "Forwarded": "host=" + deployed}, 421),
                    ({"Host": deployed, "Origin": "https://attacker.invalid"}, 403),
                    ({"Host": deployed, "Origin": "https://" + deployed + ":8443"}, 403),
                    ({"Host": deployed, "Origin": self.auth.config.origin}, 403),
                    ({"Host": deployed, "Origin": "https://attacker.invalid",
                      "X-Forwarded-Host": deployed, "X-Forwarded-Proto": "https"}, 403),
                )
                for extra, status in denied:
                    with self.subTest(extra=extra):
                        response = await browser.post(url + "/mcp", headers={**base, **extra},
                                                      json=initialize)
                        self.assertEqual(response.status_code, status)

                for extra, status in (
                    ([("Host", deployed), ("Origin", "https://" + deployed),
                      ("Origin", "https://attacker.invalid")], 403),
                ):
                    with self.subTest(extra=extra):
                        response = await browser.post(url + "/mcp", headers=[*base.items(), *extra],
                                                      json=initialize)
                        self.assertEqual(response.status_code, status)

                probe = await browser.get(url + "/health", headers={"Host": "attacker.invalid"})
                self.assertEqual(probe.json(), {"status": "ok"})

    async def test_duplicate_host_rejected_at_asgi_boundary(self):
        # HTTP/1.1 clients reject duplicate Host before transmitting it; test the
        # guard against an ASGI server that supplies such a malformed scope.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                     base_url="http://127.0.0.1") as browser:
            response = await browser.post("/mcp", headers=[
                ("Host", "obsidian-mcp.homelab0.org"), ("Host", "attacker.invalid")])
        self.assertEqual(response.status_code, 421)

    async def test_owner_post_stalled_body_times_out_before_decision(self):
        pending_id = "a" * 20
        messages = []
        first = True
        never = asyncio.Event()

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": b"decision=approve", "more_body": True}
            await never.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "method": "POST", "path": "/owner/" + pending_id,
                 "root_path": "", "headers": [(b"cookie", b"session=fixture"),
                                            (b"content-type", b"application/x-www-form-urlencoded"),
                                            (b"origin", b"https://approval.example.test")],
                 "http_version": "1.1", "scheme": "http", "server": ("test", 80),
                 "client": ("127.0.0.1", 1), "query_string": b""}
        view = type("View", (), {"digest": "fixture-digest"})()
        with (patch.object(ApprovalCoordinator, "owner_view", return_value=view),
              patch.object(ApprovalCoordinator, "approve") as approve,
              patch("gateway._HTTP_BODY_TIMEOUT", 0.08)):
            await asyncio.wait_for(self.app(scope, receive, send), 0.3)
            self.assertEqual(messages[0]["status"], 408)
            messages.clear()

            async def oversized_receive():
                return {"type": "http.request", "body": b"x" * 4097, "more_body": False}

            await asyncio.wait_for(self.app(scope, oversized_receive, send), 0.3)
            self.assertEqual(messages[0]["status"], 403)
        approve.assert_not_called()
        self.assertEqual(self.guarded.writes, [])

    async def test_four_identities_scopes_and_app_mutations_over_http(self):
        async with running(self.app) as url:
            expected = {"codex": {"iam", "homelab"}, "claude": {"iam"},
                        "opencode": {"homelab"}, "antigravity": {"iam"}}
            for name, vaults in expected.items():
                async with Client(url + "/mcp", auth=TOKENS[name]) as client:
                    tools = {tool.name for tool in await client.list_tools()}
                    base = {"list_vaults", "read_note", "read_embedded_image"}
                    if name != "antigravity":
                        base |= {"list_entries", "search_notes", "create_note", "append_note", "mutation_receipt"}
                    if name == "codex":
                        base |= {"prepare_action", "commit_action"}
                    self.assertEqual(tools, base)
                    listed = await client.call_tool("list_vaults")
                    self.assertEqual(set(listed.structured_content["vaults"]), vaults)
                    vault = next(iter(vaults))
                    result = await client.call_tool("read_note", {"vault": vault, "path": "note.md"})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content["path"], "note.md")
                    if name == "codex":
                        root = await client.call_tool("list_entries", {"vault": "iam"})
                        self.assertFalse(root.is_error)
                        self.assertIn({"path": "note.md", "kind": "file"}, root.structured_content["entries"])
                        await client.call_tool("create_note", {"vault": "iam", "path": "new.md", "content": "new"})
                        await client.call_tool("append_note", {"vault": "iam", "path": "new.md", "content": " more", "expected_revision": "sha256:" + "c" * 64})
                        self.assertEqual(self.bridge.notes["iam"]["new.md"][0], "new more")
                        receipt = await client.call_tool("mutation_receipt", {"vault": "iam", "operation": "create",
                            "path": "new.md", "content": "new", "receipt": "00000000-0000-4000-8000-000000000001"})
                        self.assertEqual(receipt.structured_content["status"], "indeterminate")
                        self.assertIn(("iam", "create", "codex"), self.bridge.calls)
                        self.assertIn(("iam", "append", "codex"), self.bridge.calls)
                        self.assertIn(("iam", "receipt", "codex"), self.bridge.calls)
                    denied = await client.call_tool("read_note", {"vault": "ungranted", "path": "note.md"}, raise_on_error=False)
                    self.assertTrue(denied.is_error)
            self.assertNotIn(("ungranted", "read"), self.bridge.calls)
            async with httpx.AsyncClient() as browser:
                health = await browser.get(url + "/health")
                self.assertEqual(health.json(), {"status": "ok"})
                self.assertNotIn("iam", health.text)
                denied = await browser.post(url + "/mcp", headers={"Authorization": "Bearer invalid"}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(denied.status_code, 401)
                non_ascii = await browser.post(url + "/mcp", headers={"Authorization": "Bearer caf\u00e9".encode("latin-1")},
                                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(non_ascii.status_code, 401)

    async def test_media_native_block_and_failure_cases(self):
        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["codex"]) as client:
                result = await client.call_tool("read_embedded_image", {"vault": "iam", "source": "note.md", "target": "picture.png"})
                images = [block for block in result.content if block.type == "image"]
                self.assertEqual(len(images), 1)
                self.assertEqual(images[0].mimeType, "image/png")
                with Image.open(io.BytesIO(base64.b64decode(images[0].data))) as decoded:
                    self.assertEqual(decoded.getpixel((0, 0)), (20, 40, 60))
                bad = await client.call_tool("read_embedded_image", {"vault": "iam", "source": "note.md", "target": "bad.png"}, raise_on_error=False)
                self.assertTrue(bad.is_error)
                remote = await client.call_tool("read_embedded_image", {"vault": "iam", "source": "note.md", "target": "https://evil.invalid/image.png"}, raise_on_error=False)
                self.assertTrue(remote.is_error)

    async def test_owner_approval_is_browser_only_and_replay_or_changed_state_fails(self):
        action = {"vault": "iam", "verb": "replace_note", "arguments": {"path": "note.md", "content": "new"}}
        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["codex"]) as client:
                prepared = await client.call_tool("prepare_action", action)
                pending = prepared.structured_content
                self.assertEqual(self.guarded.writes, [])
                self.assertTrue(pending["url"].startswith(self.auth.config.origin + "/owner/"))
                async with httpx.AsyncClient() as browser:
                    path = "/owner/" + pending["id"]
                    forged = await browser.post(url + path, headers={"Authorization": "Bearer " + TOKENS["codex"], "X-authentik-uid": "owner", "Origin": self.auth.config.origin}, data={"decision": "approve", "digest": pending["digest"]})
                    self.assertEqual(forged.status_code, 401)
                    page = await browser.get(url + path, headers={"Cookie": "session=fixture"})
                    self.assertEqual(page.status_code, 200)
                    self.assertIn("&lt;script&gt;", page.text)
                    self.assertNotIn("<script>", page.text)
                    nonce = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                    cookie = page.cookies.get("__Host-obsidian_csrf")
                    payload = {"decision": "approve", "digest": pending["digest"], "csrf": nonce}
                    missing_origin = await browser.post(url + path, headers={"Cookie": f"session=fixture; __Host-obsidian_csrf={cookie}"}, data=payload)
                    self.assertEqual(missing_origin.status_code, 403)
                    self.assertIn("__Host-obsidian_csrf=", self.auth_checks[-1].headers["cookie"])
                    wrong_nonce = await browser.post(url + path, headers={"Cookie": f"session=fixture; __Host-obsidian_csrf={cookie}", "Origin": self.auth.config.origin}, data={**payload, "csrf": "wrong"})
                    self.assertEqual(wrong_nonce.status_code, 403)
                    approved = await browser.post(url + path, headers={"Cookie": f"session=fixture; __Host-obsidian_csrf={cookie}", "Origin": self.auth.config.origin}, data=payload)
                    self.assertEqual(approved.status_code, 200)
                self.guarded.revision = "r2"
                stale = await client.call_tool("commit_action", {**action, "pending_id": pending["id"]}, raise_on_error=False)
                self.assertTrue(stale.is_error)
                self.assertEqual(self.guarded.writes, [])
                replay = await client.call_tool("commit_action", {**action, "pending_id": pending["id"]}, raise_on_error=False)
                self.assertTrue(replay.is_error)
                self.guarded.revision = "r3"
                fresh = (await client.call_tool("prepare_action", action)).structured_content
                async with httpx.AsyncClient() as browser:
                    path = "/owner/" + fresh["id"]
                    page = await browser.get(url + path, headers={"Cookie": "session=fixture"})
                    nonce = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
                    cookie = page.cookies.get("__Host-obsidian_csrf")
                    accepted = await browser.post(url + path, headers={
                        "Cookie": f"session=fixture; __Host-obsidian_csrf={cookie}",
                        "Origin": self.auth.config.origin,
                    }, data={"decision": "approve", "digest": fresh["digest"], "csrf": nonce})
                    self.assertEqual(accepted.status_code, 200)
                committed = await client.call_tool("commit_action", {**action, "pending_id": fresh["id"]})
                self.assertFalse(committed.is_error)
                self.assertEqual(len(self.guarded.writes), 1)
                duplicate = await client.call_tool("commit_action", {**action, "pending_id": fresh["id"]}, raise_on_error=False)
                self.assertTrue(duplicate.is_error)
                self.assertEqual(len(self.guarded.writes), 1)

    async def test_closed_app_fails_without_fallback(self):
        self.bridge.closed.add("iam")
        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                self.assertEqual({tool.name for tool in await client.list_tools()}, {"list_vaults"})
                result = await client.call_tool("read_note", {"vault": "iam", "path": "note.md"}, raise_on_error=False)
                self.assertTrue(result.is_error)
                self.assertEqual(self.bridge.calls, [])

    async def test_active_tool_worker_keeps_health_and_owner_routes_responsive(self):
        entered = threading.Event()
        release = threading.Event()
        original = self.bridge.read_note

        def slow_read(vault, path):
            entered.set()
            release.wait(2)
            return original(vault, path)

        self.bridge.read_note = slow_read
        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                call = asyncio.create_task(client.call_tool("read_note", {"vault": "iam", "path": "note.md"}))
                try:
                    for _ in range(100):
                        if entered.is_set():
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(entered.is_set())
                    async with httpx.AsyncClient() as browser:
                        health = await asyncio.wait_for(browser.get(url + "/health"), 0.5)
                        owner = await asyncio.wait_for(browser.get(url + "/owner/" + "a" * 20), 0.5)
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(owner.status_code, 401)
                finally:
                    release.set()
                result = await asyncio.wait_for(call, 1)
                self.assertFalse(result.is_error)

    async def test_read_and_image_deadlines_include_readiness_and_worker_result(self):
        original_ready = self.bridge.ready
        original_read = self.bridge.read_note
        original_embed = self.bridge.resolve_embed
        entered = threading.Event()
        release = threading.Event()

        def slow_ready(vault, *, deadline=None):
            time.sleep(0.04)
            return original_ready(vault, deadline=deadline)

        def slow_read(vault, path):
            entered.set()
            release.wait(1)
            return original_read(vault, path)

        def slow_embed(vault, source, target):
            entered.set()
            release.wait(1)
            return original_embed(vault, source, target)

        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                self.bridge.ready = slow_ready
                try:
                    for name, args, method in (
                        ("read_note", {"vault": "iam", "path": "note.md"}, "read_note"),
                        ("read_embedded_image", {"vault": "iam", "source": "note.md",
                                                 "target": "picture.png"}, "resolve_embed"),
                    ):
                        entered.clear()
                        release.clear()
                        setattr(self.bridge, method, slow_read if method == "read_note" else slow_embed)
                        with patch("gateway._EXECUTION_ADMISSION_TIMEOUT", 0.16):
                            start = time.monotonic()
                            result = await asyncio.wait_for(client.call_tool(name, args,
                                                          raise_on_error=False), 0.6)
                        self.assertTrue(entered.is_set())
                        self.assertTrue(result.is_error)
                        self.assertLess(time.monotonic() - start, 0.45)
                        async with httpx.AsyncClient() as browser:
                            health = await asyncio.wait_for(browser.get(url + "/health"), 0.5)
                            owner = await asyncio.wait_for(browser.get(url + "/owner/" + "a" * 20), 0.5)
                        self.assertEqual(health.status_code, 200)
                        self.assertEqual(owner.status_code, 401)
                        release.set()
                        await asyncio.sleep(0.02)
                finally:
                    release.set()
                    self.bridge.ready = original_ready
                    self.bridge.read_note = original_read
                    self.bridge.resolve_embed = original_embed

    async def test_slow_discovery_keeps_routes_responsive_and_hides_unsupported_tools(self):
        started = threading.Event()
        release = threading.Event()
        original_ready = self.bridge.ready

        def slow_ready(vault, *, deadline=None):
            if vault == "iam":
                started.set()
                release.wait(2)
                return original_ready(vault)
            return {"capabilities": ["health", "read"]}

        async with running(self.app) as url:
            async with Client(url + "/mcp", auth=TOKENS["codex"]) as client:
                self.bridge.ready = slow_ready
                async with httpx.AsyncClient() as browser:
                    listing = asyncio.create_task(client.list_tools())
                    try:
                        self.assertTrue(await asyncio.to_thread(started.wait, 1))
                        health = await asyncio.wait_for(browser.get(url + "/health"), 0.5)
                        owner = await asyncio.wait_for(browser.get(url + "/owner/" + "a" * 20), 0.5)
                        self.assertEqual(health.status_code, 200)
                        self.assertEqual(owner.status_code, 401)
                        tools = {tool.name for tool in await asyncio.wait_for(listing, 1.5)}
                        self.assertEqual(tools, {"list_vaults", "read_note"})
                        listed = await asyncio.wait_for(client.call_tool("list_vaults"), 1.5)
                        self.assertEqual(listed.structured_content["vaults"], ["homelab"])
                    finally:
                        release.set()
                        if not listing.done():
                            await listing


if __name__ == "__main__":
    unittest.main()
