"""Byte-exact attachment import: content checks, the upload route and the receipt tool."""

import asyncio
import hashlib
import io
import sys
import threading
import unittest
from pathlib import Path

import httpx
from fastmcp import Client
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval import VaultEnrollment  # noqa: E402
from bridge import BridgeError  # noqa: E402
from gateway import create_gateway  # noqa: E402
from owner_auth import OwnerAuthConfig, OwnerAuthenticator  # noqa: E402
from test_gateway import SAFE, TOKENS  # noqa: E402
from test_gateway_routes import running  # noqa: E402
from test_transfer import AttachmentBridge  # noqa: E402
from transfer import content_matches  # noqa: E402


def png(size=(64, 32)):
    stream = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(stream, "PNG")
    return stream.getvalue()


class ImportBridge(AttachmentBridge):
    def __init__(self):
        super().__init__()
        self.extra_capabilities = ["file_info", "stage_begin", "state"]
        self.writes = []
        self.outcome = "committed"
        self.stages = {}

    def path_state(self, vault, path):
        if path in self.files[vault]:
            return {"path": path, "kind": "file", "revision": "sha256:" + "0" * 64, "count": 1}
        return super().path_state(vault, path)

    def write_attachment(self, vault, path, chunks, size, sha256, *, client_id="bridge"):
        data = b"".join(chunks)
        assert len(data) == size and hashlib.sha256(data).hexdigest() == sha256
        if self.outcome in {"conflict", "unavailable", "limit_exceeded", "unsupported_media"}:
            raise BridgeError(self.outcome)
        self.writes.append((vault, path, client_id))
        if self.outcome == "pending":
            stage = "00000000-0000-4000-8000-000000000001"
            self.stages[stage] = (path, data, client_id)
            return {"stage": stage, "status": "pending"}
        self.files[vault][path] = data
        return {"path": path, "size": size, "sha256": sha256, "stage": "x", "status": "committed"}

    def commit_attachment(self, vault, stage, *, client_id="bridge"):
        if stage not in self.stages:
            return {"stage": stage, "status": "indeterminate"}
        path, data, owner = self.stages[stage]
        if owner != client_id:
            raise BridgeError("unauthorized")
        self.files[vault][path] = data
        return {"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                "stage": stage, "status": "committed"}


class ContentTests(unittest.TestCase):
    def test_bytes_must_match_the_extension(self):
        image = png()
        self.assertTrue(content_matches("a.png", image))
        self.assertFalse(content_matches("a.jpg", image))
        self.assertFalse(content_matches("a.png", image[:40]))
        self.assertTrue(content_matches("a.pdf", b"%PDF-1.7\n..."))
        self.assertFalse(content_matches("a.pdf", image))
        self.assertFalse(content_matches("a.png", b"%PDF-1.7"))
        webp = io.BytesIO()
        Image.new("RGB", (4, 4)).save(webp, "WEBP")
        self.assertTrue(content_matches("a.webp", webp.getvalue()))
        self.assertFalse(content_matches("a.svg", b"<svg/>"))


class ImportRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bridge = ImportBridge()
        self.check_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda call: httpx.Response(401)))
        config = OwnerAuthConfig("http://authentik.svc.cluster.local:9000/outpost.goauthentik.io/auth/nginx",
                                 "https://obsidian.homelab0.org", frozenset({"owner"}), frozenset({"iam"}))
        self.auth = OwnerAuthenticator(config, self.check_client)
        grants = {name: {"iam": SAFE | {"import_media"}} for name in TOKENS}
        self.app = create_gateway(self.bridge, TOKENS, grants, self.auth,
                                  {"iam": VaultEnrollment(frozenset({"owner"}), {})},
                                  transfer_origin="https://obsidian-mcp.homelab0.org")

    async def asyncTearDown(self):
        await self.check_client.aclose()

    async def link(self, client, path, data, sha=None):
        result = (await client.call_tool("import_attachment", {
            "vault": "iam", "path": path, "size": len(data),
            "sha256": sha or hashlib.sha256(data).hexdigest()})).data
        self.assertEqual((result["method"], result["url"]), ("PUT", "https://obsidian-mcp.homelab0.org/transfer"))
        token = result["header"].split(": ", 1)[1]
        self.assertIn(token, result["command"])
        return {"X-Obsidian-Transfer": token}

    async def test_upload_lands_byte_exact_once(self):
        image = png((3840, 2160))
        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["claude"]) as client, \
                httpx.AsyncClient(base_url=url) as http:
            self.assertTrue({"import_attachment", "import_receipt"} <= {t.name for t in await client.list_tools()})
            path = await self.link(client, "Design/4k.png", image)
            response = await http.put("/transfer", headers=path, content=image)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["sha256"], hashlib.sha256(image).hexdigest())
            self.assertEqual(self.bridge.files["iam"]["Design/4k.png"], image)
            self.assertEqual(self.bridge.writes, [("iam", "Design/4k.png", "claude")])
            self.assertEqual((await http.put("/transfer", headers=path, content=image)).status_code, 404)
            existing = await client.call_tool("import_attachment", {
                "vault": "iam", "path": "Design/4k.png", "size": 1, "sha256": "0" * 64}, raise_on_error=False)
            self.assertTrue(existing.is_error)

    async def test_uploads_that_do_not_match_are_refused_before_writing(self):
        image = png()
        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["claude"]) as client, \
                httpx.AsyncClient(base_url=url) as http:
            cases = [
                (await self.link(client, "a.png", image), image[:-1], 400, "size_mismatch"),
                (await self.link(client, "b.png", image), image + b"x", 400, "size_mismatch"),
                (await self.link(client, "c.png", image, "0" * 64), image, 400, "sha256_mismatch"),
                (await self.link(client, "d.pdf", image), image, 415, "content_type_mismatch"),
            ]
            for path, body, status, error in cases:
                response = await http.put("/transfer", headers=path, content=body)
                self.assertEqual((response.status_code, response.json()["error"]), (status, error))
            # Without a declared length the body is cut off as soon as it passes the size.
            async def chunked():
                yield image
                yield b"x"
            response = await http.put("/transfer", headers=await self.link(client, "g.png", image), content=chunked())
            self.assertEqual((response.status_code, response.json()["error"]), (413, "size_mismatch"))

            async def short():
                yield image[:-1]
            response = await http.put("/transfer", headers=await self.link(client, "h.png", image), content=short())
            self.assertEqual((response.status_code, response.json()["error"]), (400, "size_mismatch"))
            self.assertEqual(self.bridge.writes, [])
            wrong_host = await self.link(client, "e.png", image)
            self.assertEqual((await http.put("/transfer", content=image, headers={**wrong_host, "host": "evil.example"})).status_code, 421)
            export_link = await self.link(client, "f.png", image)
            self.assertEqual((await http.get("/transfer", headers=export_link)).status_code, 404)
            self.assertEqual((await http.put("/transfer", content=image)).status_code, 404)
            for bad in ({"path": "x.svg"}, {"path": "../x.png"}, {"size": 0}, {"size": 51 * 1024 * 1024},
                        {"sha256": "ABC"}):
                args = {"vault": "iam", "path": "ok.png", "size": 10, "sha256": "0" * 64, **bad}
                self.assertTrue((await client.call_tool("import_attachment", args, raise_on_error=False)).is_error)

    async def test_conflict_and_pending_outcomes(self):
        image = png()
        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["claude"]) as client, \
                httpx.AsyncClient(base_url=url) as http:
            self.bridge.outcome = "conflict"
            response = await http.put("/transfer", headers=await self.link(client, "race.png", image), content=image)
            self.assertEqual((response.status_code, response.json()), (409, {"error": "conflict", "written": False}))
            for outcome, status, error in (("unavailable", 503, "unavailable"), ("limit_exceeded", 503, "app_busy"),
                                           ("unsupported_media", 415, "unsupported_media")):
                self.bridge.outcome = outcome
                response = await http.put("/transfer", headers=await self.link(client, f"{outcome}.png", image), content=image)
                self.assertEqual((response.status_code, response.json()), (status, {"error": error, "written": False}))
            self.bridge.outcome = "pending"
            response = await http.put("/transfer", headers=await self.link(client, "slow.png", image), content=image)
            self.assertEqual(response.status_code, 202)
            stage = response.json()["stage"]
            receipt = (await client.call_tool("import_receipt", {"vault": "iam", "stage": stage})).data
            self.assertEqual((receipt["status"], self.bridge.files["iam"]["slow.png"]), ("committed", image))
        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["codex"]) as other:
            denied = await other.call_tool("import_receipt", {"vault": "iam", "stage": stage}, raise_on_error=False)
            self.assertTrue(denied.is_error)

    async def test_busy_answer_keeps_the_link(self):
        image = png()
        started = threading.Semaphore(0)
        release = threading.Event()
        original = self.bridge.write_attachment

        def blocked(*args, **kwargs):
            started.release()
            release.wait(10)
            return original(*args, **kwargs)

        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["claude"]) as client, \
                httpx.AsyncClient(base_url=url) as http:
            link = await self.link(client, "later.png", image)
            self.bridge.write_attachment = blocked
            uploads = [asyncio.create_task(http.put("/transfer", headers=await self.link(client, f"{n}.png", image),
                                                    content=image)) for n in ("one", "two")]
            for _ in range(2):
                self.assertTrue(await asyncio.to_thread(started.acquire, True, 10))
            busy = await http.put("/transfer", headers=link, content=image)
            self.assertEqual((busy.status_code, busy.json()["error"]), (429, "busy"))
            release.set()
            self.assertEqual([(await task).status_code for task in uploads], [201, 201])
            self.bridge.write_attachment = original
            self.assertEqual((await http.put("/transfer", headers=link, content=image)).status_code, 201)

    async def test_tools_hidden_without_plugin_support(self):
        self.bridge.extra_capabilities = ["file_info"]
        async with running(self.app) as url, Client(url + "/mcp", auth=TOKENS["claude"]) as client:
            names = {t.name for t in await client.list_tools()}
            self.assertFalse({"import_attachment", "import_receipt"} & names)


if __name__ == "__main__":
    unittest.main()
