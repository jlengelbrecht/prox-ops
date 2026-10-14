"""Byte-exact attachment export: links, zip stream, export planning and the HTTP route."""

import hashlib
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import httpx
from starlette.requests import ClientDisconnect
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval import VaultEnrollment  # noqa: E402
from bridge import BridgeError  # noqa: E402
from gateway import _SlotResponse, _plan_export, create_gateway  # noqa: E402
from owner_auth import OwnerAuthConfig, OwnerAuthenticator  # noqa: E402
from test_gateway import BridgeFixture, SAFE, TOKENS  # noqa: E402
from test_gateway_routes import running  # noqa: E402
from transfer import CHECKSUMS, MANIFEST, TransferLedger, export_stream  # noqa: E402


class AttachmentBridge(BridgeFixture):
    """BridgeFixture plus an in-memory attachment store served in small chunks."""

    def __init__(self):
        super().__init__()
        self.extra_capabilities = ["file_info"]
        self.files = {"iam": {"Shots/a.png": os.urandom(5000), "Shots/deep/b.jpg": os.urandom(7000),
                              "Shots/doc.pdf": b"%PDF-1.7 x", "Shots/vector.svg": b"<svg/>",
                              "Shots/notes.md": b"# not an attachment"}}
        self.notes["iam"]["pack.md"] = ("![[a.png]] ![x](deep/b.jpg) ![[missing.png]] ![[doc.pdf]]",
                                        "sha256:" + "c" * 64)
        self.changed = set()

    def list_entries(self, vault, prefix="", limit=1000):
        names = set()
        for path in self.files[vault]:
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1:] if prefix else path
            head = rest.split("/", 1)[0]
            names.add(((prefix + "/" if prefix else "") + head, "folder" if "/" in rest else "file"))
        return {"entries": [{"path": path, "kind": kind} for path, kind in sorted(names)]}

    def resolve_link(self, vault, source, target):
        path = "Shots/" + target
        if path not in self.files[vault]:
            raise BridgeError("not_found")
        return path

    def attachment_info(self, vault, path):
        if os.path.splitext(path)[1] not in {".png", ".jpg", ".pdf"}:
            raise BridgeError("unsupported_media")
        if path not in self.files[vault]:
            raise BridgeError("not_found")
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf"}[os.path.splitext(path)[1]]
        return {"path": path, "mime": mime, "size": len(self.files[vault][path]), "mtime": 1_760_000_000_000}

    def iter_attachment(self, vault, info, chunk=4 * 1024 * 1024):
        data = self.files[vault][info["path"]]
        if info["path"] in self.changed:
            data = data[:-1]
        for start in range(0, len(data), 1024):
            yield data[start:start + 1024]


def unzip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


class LedgerTests(unittest.TestCase):
    def test_tokens_are_single_use_typed_and_expire(self):
        ledger = TransferLedger(ttl=600, limit=2)
        token, ttl = ledger.issue("export", "claude", "iam", ["x"])
        self.assertEqual((len(token), ttl), (43, 600))
        self.assertIsNone(ledger.claim(token, "import"))
        self.assertEqual(ledger.claim(token, "export").payload, ["x"])
        self.assertIsNone(ledger.claim(token, "export"))
        ledger.issue("export", "claude", "iam", [])
        ledger.issue("export", "claude", "iam", [])
        with self.assertRaises(OverflowError):
            ledger.issue("export", "claude", "iam", [])
        with patch("transfer.time.monotonic", return_value=10**9):
            self.assertIsNotNone(ledger.issue("export", "claude", "iam", []))
        for bad in (None, "", "x" * 42, "x" * 44):
            self.assertIsNone(ledger.claim(bad, "export"))


class StreamTests(unittest.TestCase):
    def test_zip_holds_exact_bytes_manifest_and_checksums(self):
        bridge = AttachmentBridge()
        infos = [bridge.attachment_info("iam", path) for path in ("Shots/a.png", "Shots/deep/b.jpg")]
        files = unzip(b"".join(export_stream(bridge, "iam", infos)))
        for path in ("Shots/a.png", "Shots/deep/b.jpg"):
            self.assertEqual(files[path], bridge.files["iam"][path])
        manifest = json.loads(files[MANIFEST])
        self.assertEqual([item["sha256"] for item in manifest["files"]],
                         [hashlib.sha256(bridge.files["iam"][p]).hexdigest() for p in ("Shots/a.png", "Shots/deep/b.jpg")])
        self.assertEqual(files[CHECKSUMS].decode().splitlines()[0],
                         f"{hashlib.sha256(bridge.files['iam']['Shots/a.png']).hexdigest()}  Shots/a.png")
        with zipfile.ZipFile(io.BytesIO(b"".join(export_stream(bridge, "iam", infos)))) as archive:
            self.assertTrue(all(item.compress_type == zipfile.ZIP_STORED for item in archive.infolist()))

    def test_a_file_that_changes_mid_export_breaks_the_stream(self):
        bridge = AttachmentBridge()
        infos = [bridge.attachment_info("iam", "Shots/a.png")]
        bridge.changed.add("Shots/a.png")
        sent = []
        with self.assertRaisesRegex(ValueError, "changed during export"):
            for chunk in export_stream(bridge, "iam", infos):
                sent.append(chunk)
        with self.assertRaises(zipfile.BadZipFile):
            zipfile.ZipFile(io.BytesIO(b"".join(sent)))


class PlanTests(unittest.TestCase):
    def test_folder_note_and_paths_selection(self):
        bridge = AttachmentBridge()
        infos, total, skipped = _plan_export(bridge, "iam", [], "Shots", "")
        self.assertEqual([i["path"] for i in infos], ["Shots/a.png", "Shots/deep/b.jpg", "Shots/doc.pdf"])
        self.assertEqual(total, 5000 + 7000 + 10)
        self.assertEqual(skipped, [])
        infos, _, skipped = _plan_export(bridge, "iam", [], None, "pack.md")
        self.assertEqual([i["path"] for i in infos], ["Shots/a.png", "Shots/deep/b.jpg", "Shots/doc.pdf"])
        self.assertEqual(skipped, [{"path": "missing.png", "reason": "not_found"}])
        infos, _, skipped = _plan_export(bridge, "iam", ["Shots/a.png", "Shots/a.png", "Shots/vector.svg"], None, "")
        self.assertEqual([i["path"] for i in infos], ["Shots/a.png"])
        self.assertEqual(skipped, [{"path": "Shots/vector.svg", "reason": "unsupported_media"}])

    def test_caps_skip_what_does_not_fit(self):
        bridge = AttachmentBridge()
        with patch("gateway.MAX_EXPORT_BYTES", 6000):
            infos, total, skipped = _plan_export(bridge, "iam", [], "Shots", "")
        self.assertEqual(([i["path"] for i in infos], total), (["Shots/a.png", "Shots/doc.pdf"], 5010))
        self.assertEqual(skipped, [{"path": "Shots/deep/b.jpg", "reason": "limit_exceeded"}])
        with patch("gateway._EXPORT_WALK", 2), self.assertRaises(BridgeError):
            _plan_export(bridge, "iam", [], "", "")


class SlotTests(unittest.IsolatedAsyncioTestCase):
    async def test_slot_is_released_when_the_client_leaves_before_the_body(self):
        released = []

        async def body():
            yield b"never sent"

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise OSError("client went away")

        response = _SlotResponse(body(), lambda: released.append(True), media_type="application/zip")
        with self.assertRaises((OSError, ClientDisconnect)):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        self.assertEqual(released, [True])

    def test_planning_stops_at_its_deadline(self):
        with self.assertRaisesRegex(BridgeError, "limit_exceeded"):
            _plan_export(AttachmentBridge(), "iam", [], "Shots", "", deadline=0.0)


async def client_export(url):
    async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
        result = (await client.call_tool("export_attachments", {"vault": "iam", "paths": ["Shots/a.png"]})).data
    return {"X-Obsidian-Transfer": result["header"].split(": ", 1)[1]}


class ExportRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = AttachmentBridge()
        self.check_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda call: httpx.Response(401)))
        config = OwnerAuthConfig("http://authentik.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx",
                                 "https://obsidian.homelab0.org", frozenset({"owner"}), frozenset({"iam"}))
        self.auth = OwnerAuthenticator(config, self.check_client)

    async def asyncTearDown(self):
        await self.check_client.aclose()

    def app(self, origin, grants=None):
        grants = grants or {name: {"iam": SAFE | {"export_media"}} for name in TOKENS}
        return create_gateway(self.bridge, TOKENS, grants, self.auth,
                              {"iam": VaultEnrollment(frozenset({"owner"}), {})}, transfer_origin=origin)

    async def test_export_link_serves_the_exact_files_once(self):
        async with running(self.app("https://obsidian-mcp.homelab0.org")) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                names = {tool.name for tool in await client.list_tools()}
                self.assertIn("export_attachments", names)
                result = (await client.call_tool("export_attachments", {"vault": "iam", "folder": "Shots"})).data
                self.assertEqual((result["files"], result["bytes"]), (3, 12010))
                bad = await client.call_tool("export_attachments", {"vault": "iam", "folder": "Shots", "note": "pack.md"},
                                             raise_on_error=False)
                self.assertTrue(bad.is_error)
            self.assertEqual(result["url"], "https://obsidian-mcp.homelab0.org/transfer")
            self.assertTrue(result["header"].startswith("X-Obsidian-Transfer: "))
            token = {"X-Obsidian-Transfer": result["header"].split(": ", 1)[1]}
            self.assertIn(token["X-Obsidian-Transfer"], result["command"])
            async with httpx.AsyncClient(base_url=url) as http:
                self.assertEqual((await http.get("/transfer", headers={**token, "host": "evil.example"})).status_code, 421)
                # A HEAD probe neither serves nor uses up the link.
                self.assertEqual((await http.head("/transfer", headers=token)).status_code, 405)
                self.assertEqual((await http.get("/transfer")).status_code, 404)
                response = await http.get("/transfer", headers=token)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "application/zip")
                self.assertEqual(response.headers["cache-control"], "no-store")
                files = unzip(response.content)
                self.assertEqual(files["Shots/deep/b.jpg"], self.bridge.files["iam"]["Shots/deep/b.jpg"])
                self.assertEqual((await http.get("/transfer", headers=token)).status_code, 404)
                # Both download slots are free again after the finished stream.
                for _ in range(3):
                    again = (await client_export(url))
                    self.assertEqual((await http.get("/transfer", headers=again)).status_code, 200)
                self.assertEqual((await http.get("/transfer", headers={"X-Obsidian-Transfer": "A" * 43})).status_code, 404)
                # Old-style URLs carrying the token in the path are not served and do not use it up.
                fresh = await client_export(url)
                self.assertEqual((await http.get("/transfer/" + fresh["X-Obsidian-Transfer"])).status_code, 404)
                self.assertEqual((await http.get("/transfer", headers=fresh)).status_code, 200)

    async def test_tool_hidden_without_plugin_support_or_grant(self):
        self.bridge.extra_capabilities = []
        async with running(self.app("http://127.0.0.1")) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                self.assertNotIn("export_attachments", {tool.name for tool in await client.list_tools()})
        self.bridge.extra_capabilities = ["file_info"]
        async with running(self.app("http://127.0.0.1", {name: {"iam": SAFE} for name in TOKENS})) as url:
            async with Client(url + "/mcp", auth=TOKENS["claude"]) as client:
                self.assertNotIn("export_attachments", {tool.name for tool in await client.list_tools()})
                denied = await client.call_tool("export_attachments", {"vault": "iam", "paths": ["Shots/a.png"]},
                                                raise_on_error=False)
                self.assertTrue(denied.is_error)


if __name__ == "__main__":
    unittest.main()
