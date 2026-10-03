"""Gateway unit and ASGI bounds fixtures."""

import asyncio
import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval import Preview, Snapshot, StateEntry  # noqa: E402
from bridge import BridgeClient, BridgeError  # noqa: E402
from gateway import (CapabilityFilter, RequestBodyLimit, _ExecutionPool, _MAX_VAULTS,
                     _MAX_REGISTRY_BYTES, _image_content, _note_with_images,
                     create_gateway_from_env)  # noqa: E402
from fastmcp.exceptions import ToolError


TOKENS = {name: base64.urlsafe_b64encode(bytes([index]) * 32).decode().rstrip("=")
          for index, name in enumerate(("codex", "claude", "opencode", "antigravity"), 1)}
SAFE = frozenset({"list_notes", "read_note", "search", "create_note", "append_note", "read_media"})


class BridgeFixture:
    def __init__(self):
        self.notes = {"iam": {"note.md": ("IAM", "sha256:" + "a" * 64)},
                      "homelab": {"note.md": ("Homelab", "sha256:" + "b" * 64)}}
        self.calls = []
        self.closed = set()
        image = Image.new("RGB", (2, 2), (20, 40, 60))
        stream = io.BytesIO()
        image.save(stream, "PNG")
        self.image = stream.getvalue()

    def ready(self, vault, *, deadline=None):
        if vault in self.closed:
            raise BridgeError("unavailable")
        return {"capabilities": ["health", "list", "read", "search", "create", "append", "embed"]}

    def list_entries(self, vault, prefix="", limit=1000):
        self.ready(vault)
        self.calls.append((vault, "list"))
        return {"entries": [{"path": path, "kind": "file"} for path in self.notes[vault]][:limit]}

    def read_note(self, vault, path):
        self.ready(vault)
        self.calls.append((vault, "read"))
        content, revision = self.notes[vault][path]
        return {"path": path, "content": content, "revision": revision}

    def search(self, vault, query, limit=50):
        self.ready(vault)
        self.calls.append((vault, "search"))
        return {"matches": []}

    def create_note(self, vault, path, content, *, client_id="bridge"):
        self.ready(vault)
        self.calls.append((vault, "create", client_id))
        if path in self.notes[vault]:
            raise BridgeError("conflict")
        revision = "sha256:" + "c" * 64
        self.notes[vault][path] = (content, revision)
        return {"path": path, "revision": revision}

    def append_note(self, vault, path, content, expected_revision, *, client_id="bridge"):
        self.ready(vault)
        self.calls.append((vault, "append", client_id))
        old, revision = self.notes[vault][path]
        if revision != expected_revision:
            raise BridgeError("conflict")
        revision = "sha256:" + "d" * 64
        self.notes[vault][path] = (old + content, revision)
        return {"path": path, "revision": revision}

    def resolve_embed(self, vault, source, target):
        self.ready(vault)
        self.calls.append((vault, "embed"))
        if target == "bad.png":
            payload = b"not a png"
        else:
            payload = self.image
        return {"path": "images/picture.png", "mime": "image/png",
                "data": base64.b64encode(payload).decode()}

    def mutation_receipt(self, vault, operation, mutation, receipt, client_id):
        self.ready(vault)
        self.calls.append((vault, "receipt", client_id))
        return {"receipt": receipt, "status": "indeterminate"}


class GuardedFixture:
    def __init__(self):
        self.revision = "r1"
        self.writes = []

    def preview(self, action):
        return Preview(Snapshot((StateEntry("note", action.arguments["path"], self.revision),), True, 1),
                       "<script>alert(1)</script> exact diff")

    def observe(self, action, expected):
        return Snapshot((StateEntry("note", action.arguments["path"], self.revision),), True, 1)

    def execute(self, action, *, package=None):
        self.writes.append(action)
        return {"ok": True}


class ImageDecodeTests(unittest.TestCase):
    def test_validated_image_strips_metadata_and_rejects_bad_content(self):
        bridge = BridgeFixture()
        metadata = PngInfo()
        metadata.add_text("owner", "private fixture marker")
        stream = io.BytesIO()
        Image.new("RGB", (2, 2), (20, 40, 60)).save(stream, "PNG", pnginfo=metadata)
        bridge.image = stream.getvalue()
        result = _image_content(bridge, "iam", "note.md", "picture.png")
        content = base64.b64decode(result.content[1].data)
        self.assertEqual(result.content[1].mimeType, "image/png")
        self.assertNotIn(b"private fixture marker", content)
        with Image.open(io.BytesIO(content)) as decoded:
            self.assertEqual(decoded.getpixel((0, 0)), (20, 40, 60))
        with self.assertRaises(Exception):
            _image_content(bridge, "iam", "note.md", "bad.png")
        with self.assertRaises(Exception):
            _image_content(bridge, "iam", "note.md", "https://remote.invalid/image.png")
        with self.assertRaises(Exception):
            _image_content(bridge, "iam", "note.md", "../outside.png")


class NoteWithImagesTests(unittest.TestCase):
    def test_pages_images_in_reading_order_and_marks_failures(self):
        bridge = BridgeFixture()
        text = ("Intro ![[one.png]] middle ![alt](two%20b.jpg) ![[bad.png|200]] "
                "![remote](https://remote.invalid/x.png) ![[doc.pdf]] end ![[four.webp#c]]")
        bridge.notes["iam"]["pics.md"] = (text, "sha256:" + "c" * 64)
        first = _note_with_images(bridge, "iam", "pics.md", 0, 2)
        self.assertEqual(first.structured_content["images_total"], 4)
        self.assertEqual(first.structured_content["next_start"], 2)
        self.assertEqual(first.content[1].text, text)
        captions = [c.text for c in first.content if c.type == "text"][2:]
        self.assertEqual(captions, ["Image 1 of 4: one.png", "Image 2 of 4: two b.jpg"])
        self.assertEqual(sum(1 for c in first.content if c.type == "image"), 2)
        rest = _note_with_images(bridge, "iam", "pics.md", 2, 10)
        self.assertIsNone(rest.structured_content["next_start"])
        self.assertNotIn(text, [c.text for c in rest.content if c.type == "text"])
        captions = [c.text for c in rest.content if c.type == "text"][1:]
        self.assertEqual(captions, ["Image 3 of 4: bad.png (unavailable)", "Image 4 of 4: four.webp"])
        self.assertEqual(sum(1 for c in rest.content if c.type == "image"), 1)

    def test_hostile_embed_text_is_scanned_in_linear_time(self):
        bridge = BridgeFixture()
        for hostile in ("![[" * 85_000, "![a](" * 50_000, "![" + "]" * 200_000):
            bridge.notes["iam"]["hostile.md"] = (hostile, "sha256:" + "d" * 64)
            started = time.monotonic()
            result = _note_with_images(bridge, "iam", "hostile.md", 0, 10)
            self.assertLess(time.monotonic() - started, 1.0)
            self.assertEqual(result.structured_content["images_total"], 0)

    def test_note_without_images_returns_text_only(self):
        bridge = BridgeFixture()
        result = _note_with_images(bridge, "iam", "note.md", 0, 10)
        self.assertEqual(result.structured_content["images_total"], 0)
        self.assertIsNone(result.structured_content["next_start"])
        self.assertEqual([c.type for c in result.content], ["text", "text"])


class RequestBodyTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, chunks, *, stalled=False, downstream_delay=0):
        delivered = []
        wait = asyncio.Event()
        async def downstream(scope, receive, send):
            from starlette.requests import Request
            delivered.append(await Request(scope, receive).body())
            await asyncio.sleep(downstream_delay)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def send(message):
            messages.append(message)

        async def receive():
            if chunks:
                return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks) or stalled}
            await wait.wait()
            return {"type": "http.disconnect"}

        messages = []
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [],
                 "http_version": "1.1", "scheme": "http", "server": ("test", 80)}
        with patch("gateway._HTTP_BODY_TIMEOUT", 0.08):
            await asyncio.wait_for(RequestBodyLimit(downstream)(scope, receive, send), 0.3)
        return messages[0]["status"], delivered

    async def test_stalled_prefix_has_absolute_deadline_and_no_downstream_call(self):
        start = time.monotonic()
        status, delivered = await self._request([b'{"jsonrpc":'], stalled=True)
        self.assertEqual(status, 408)
        self.assertEqual(delivered, [])
        self.assertLess(time.monotonic() - start, 0.3)

    async def test_complete_body_replays_and_oversize_rejects(self):
        self.assertEqual(await self._request([b"one", b"two"]), (200, [b"onetwo"]))
        self.assertEqual(await self._request([b"complete"], downstream_delay=0.12),
                         (200, [b"complete"]))
        status, delivered = await self._request([b"x" * (1024 * 1024 + 1)])
        self.assertEqual(status, 413)
        self.assertEqual(delivered, [])

    async def test_slow_progress_cannot_extend_absolute_deadline(self):
        async def downstream(scope, receive, send):
            self.fail("incomplete body reached downstream")
        async def receive():
            await asyncio.sleep(0.03)
            return {"type": "http.request", "body": b"x", "more_body": True}
        messages = []
        async def send(message):
            messages.append(message)
        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [],
                 "http_version": "1.1", "scheme": "http", "server": ("test", 80)}
        with patch("gateway._HTTP_BODY_TIMEOUT", 0.08):
            await asyncio.wait_for(RequestBodyLimit(downstream)(scope, receive, send), 0.3)
        self.assertEqual(messages[0]["status"], 408)


class DiscoveryBoundTests(unittest.IsolatedAsyncioTestCase):
    async def _settle(self, predicate, timeout=3.0):
        # Worker threads start asynchronously; on a loaded host they may lag the event loop.
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            await asyncio.sleep(0.01)
    async def test_cancelled_callers_do_not_release_active_worker_slots(self):
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0
        class BlockedBridge:
            def ready(self, vault, *, deadline=None):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    release.wait(5)
                    return {"capabilities": ["read"]}
                finally:
                    with lock:
                        active -= 1
        middleware = CapabilityFilter({}, BlockedBridge(), frozenset())
        try:
            for _ in range(3):
                tasks = [asyncio.create_task(middleware._probe("iam", time.monotonic() + 1))
                         for _ in range(8)]
                await asyncio.sleep(0.03)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._settle(lambda: active == 8)
            self.assertLessEqual(peak, 8)
            self.assertEqual(active, 8)
        finally:
            release.set()
        await asyncio.wait_for(middleware._probe("iam", time.monotonic() + 1), 1)
        self.assertEqual(active, 0)
        for _ in range(100):
            if middleware._probe_slots._value == 8:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(middleware._probe_slots._value, 8)
        async def keep_loop_awake():
            while True:
                await asyncio.sleep(0.01)
        ticker = asyncio.create_task(keep_loop_awake())
        try:
            await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 1)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)

    async def test_listing_and_discovery_share_actual_worker_limit_and_deadline(self):
        release = threading.Event()
        lock = threading.Lock()
        active = 0
        peak = 0

        class SlowBridge:
            def ready(self, vault, *, deadline=None):
                nonlocal active, peak
                if vault == "ready":
                    return {"capabilities": ["read"]}
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    release.wait(5)
                    raise BridgeError("unavailable")
                finally:
                    with lock:
                        active -= 1

        middleware = CapabilityFilter({}, SlowBridge(), frozenset())
        grants = {"ready": SAFE, **{f"slow{index}": SAFE for index in range(31)}}
        try:
            with patch("gateway._DISCOVERY_TIMEOUT", 0.4):
                start = time.monotonic()
                self.assertEqual(await middleware.ready_vaults(grants), ["ready"])
                for _ in range(2):
                    self.assertEqual(await middleware.ready_vaults(grants), [])
                    self.assertEqual(await middleware.probe_grants({"ready": SAFE}), {})
                self.assertLess(time.monotonic() - start, 3.0)
            await self._settle(lambda: active == 8)
            self.assertEqual(active, 8)
            self.assertLessEqual(peak, 8)
        finally:
            release.set()
        await asyncio.wait_for(asyncio.to_thread(lambda: None), 1)
        for _ in range(100):
            if active == 0 and middleware._probe_slots._value == 8:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(active, 0)
        self.assertEqual(middleware._probe_slots._value, 8)
        async def keep_loop_awake():
            while True:
                await asyncio.sleep(0.01)
        ticker = asyncio.create_task(keep_loop_awake())
        try:
            await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 1)
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)


class ExecutionBoundTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_read_and_image_have_result_deadlines_and_retain_workers(self):
        pool = _ExecutionPool(("iam",))
        release = threading.Event()
        entered = threading.Event()

        def stalled():
            entered.set()
            release.wait(1)
            return "late result"

        try:
            for kind in ("read", "image"):
                entered.clear()
                start = time.monotonic()
                with self.assertRaises(ToolError):
                    await asyncio.wait_for(pool.run(stalled, deadline=start + 0.08,
                                                    vault="iam" if kind == "read" else None), 0.3)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - start, 0.2)
                self.assertEqual(pool._slots._value, 7)
                self.assertEqual(pool._admitted._value, 15)
                if kind == "read":
                    self.assertTrue(pool._vault_locks["iam"].locked())
                    with self.assertRaises(ToolError):
                        await pool.run(lambda: "late mutation", vault="iam",
                                       deadline=time.monotonic() + 0.03, preserve_result=True)
                release.set()
                for _ in range(100):
                    if pool._slots._value == 8:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(pool._slots._value, 8)
                release.clear()
        finally:
            release.set()
            pool.close()

    async def test_readiness_and_execution_share_one_deadline(self):
        class SlowReady:
            def ready(self, vault, *, deadline=None):
                time.sleep(0.06)
                return {"capabilities": ["read"]}

        readiness = CapabilityFilter({}, SlowReady(), frozenset())
        pool = _ExecutionPool()
        release = threading.Event()
        async def ticker():
            while True:
                await asyncio.sleep(0.005)
        tick = asyncio.create_task(ticker())
        try:
            start = time.monotonic()
            deadline = start + 0.09
            self.assertIn("iam", await readiness.probe_grants({"iam": SAFE}, deadline=deadline))
            with self.assertRaises(ToolError):
                await pool.run(lambda: release.wait(1), deadline=deadline)
            self.assertLess(time.monotonic() - start, 0.18)
            self.assertEqual(pool._slots._value, 7)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.get_running_loop().shutdown_default_executor(), 1)
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            pool.close()

    async def test_same_vault_mutation_wait_is_before_dispatch_and_bounded(self):
        pool = _ExecutionPool(("iam", "homelab"))
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def first():
            entered.set()
            release.wait(1)
            writes.append("first")
            return "recorded"

        try:
            first_deadline = time.monotonic() + 0.3
            task = asyncio.create_task(pool.run(first, vault="iam",
                                                deadline=first_deadline,
                                                preserve_result=True))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(entered.is_set())
            task.cancel()
            with self.assertRaises(ToolError):
                await pool.run(lambda: writes.append("late"), vault="iam",
                               deadline=time.monotonic() + 0.35, preserve_result=True)
            self.assertEqual(writes, [])
            self.assertGreater(time.monotonic(), first_deadline)
            self.assertEqual(await pool.run(lambda: "other", vault="homelab",
                                            deadline=time.monotonic() + 0.1), "other")
            release.set()
            self.assertEqual(await asyncio.wait_for(task, 1), "recorded")
            self.assertEqual(writes, ["first"])
        finally:
            release.set()
            pool.close()

    def test_bridge_mutation_uses_one_finite_health_and_operation_budget(self):
        bridge = BridgeClient.__new__(BridgeClient)
        bridge._timeout = 0.08
        deadlines = []

        def transport(vault, operation, args, deadline=None):
            deadlines.append(deadline)
            if operation == "health":
                time.sleep(0.05)
                return {"capabilities": ["create"]}
            time.sleep(max(0, deadline - time.monotonic()))
            raise BridgeError("unavailable")

        bridge._transport = transport
        bridge.ready = lambda vault, *, deadline=None: transport(vault, "health", {}, deadline)
        start = time.monotonic()
        with self.assertRaises(BridgeError):
            bridge._call("iam", "create", {"path": "note.md"})
        self.assertEqual(len(deadlines), 2)
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertLess(time.monotonic() - start, 0.16)

    async def test_same_vault_burst_has_finite_running_and_waiting_work(self):
        pool = _ExecutionPool()
        release = threading.Event()
        started = threading.Event()
        lock = threading.Lock()
        active = peak = 0
        late_starts = []

        def blocked():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 8:
                    started.set()
            try:
                release.wait(2)
                return "recorded"
            finally:
                with lock:
                    active -= 1

        try:
            first = [asyncio.create_task(pool.run(blocked, deadline=time.monotonic() + 1))
                     for _ in range(8)]
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started.is_set())
            queued = [asyncio.create_task(pool.run(lambda: late_starts.append("mutated"),
                                                   deadline=time.monotonic() + 0.1,
                                                   preserve_result=True))
                      for _ in range(8)]
            await asyncio.sleep(0.02)
            self.assertEqual(pool._admitted._value, 0)
            with self.assertRaises(ToolError):
                await pool.run(blocked, deadline=time.monotonic() + 0.02)
            for task in first:
                task.cancel()
            await asyncio.gather(*first, return_exceptions=True)
            self.assertEqual(active, 8)
            self.assertEqual(pool._slots._value, 0)
            results = await asyncio.gather(*queued, return_exceptions=True)
            self.assertTrue(all(isinstance(result, ToolError) for result in results))
            self.assertEqual(peak, 8)
        finally:
            release.set()
        for _ in range(100):
            if pool._admitted._value == 16:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(pool._slots._value, 8)
        self.assertEqual(pool._admitted._value, 16)
        self.assertEqual(late_starts, [])
        release.clear()
        second = [asyncio.create_task(pool.run(blocked, deadline=time.monotonic() + 1))
                  for _ in range(8)]
        for _ in range(100):
            if active == 8:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(active, 8)
        for task in second:
            task.cancel()
        await asyncio.gather(*second, return_exceptions=True)
        self.assertEqual(pool._slots._value, 0)
        release.set()
        for _ in range(100):
            if pool._slots._value == 8:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(peak, 8)
        self.assertEqual(await pool.run(lambda: "healthy", deadline=time.monotonic() + 1), "healthy")
        pool.close()

    async def test_cancelled_mutation_finishes_once_and_late_work_never_starts(self):
        pool = _ExecutionPool()
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def mutate():
            entered.set()
            release.wait(1)
            writes.append("committed")
            return {"ok": True}

        task = asyncio.create_task(pool.run(mutate, deadline=time.monotonic() + 1,
                                            preserve_result=True))
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        self.assertTrue(entered.is_set())
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(writes, [])
        with self.assertRaises(ToolError):
            await pool.run(lambda: writes.append("late"), deadline=time.monotonic() - 1)
        release.set()
        self.assertEqual(await asyncio.wait_for(task, 1), {"ok": True})
        self.assertEqual(writes, ["committed"])
        pool.close()


class RegistryBoundsTests(unittest.TestCase):
    def test_rejects_over_limit_and_malformed_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            base = {"clients": {}, "owner": {"check_url": "fixture", "origin": "fixture",
                                               "owner_ids": []}}
            for vaults in ({f"v{index}": {} for index in range(_MAX_VAULTS + 1)},
                           {"iam": []}):
                path.write_text(json.dumps({**base, "vaults": vaults}))
                with (patch.dict(os.environ, {"OBSIDIAN_GATEWAY_REGISTRY": str(path)}),
                      patch("gateway.os.statvfs", return_value=type("Stat", (),
                             {"f_flag": os.ST_RDONLY})())):
                    with self.assertRaisesRegex(ValueError, "vault"):
                        create_gateway_from_env()

    def test_registry_rejects_oversize_and_invalid_arrays_before_factory(self):
        valid = {
            "vaults": {"iam": {"app_name": "IAM", "app_root": "/vault/iam",
                               "socket_path": "/run/bridge/iam/iam.sock",
                               "credential_file": "/run/bridge/iam/credential",
                               "owner_ids": ["owner"]}},
            "clients": {name: {"iam": ["read_note"]} for name in TOKENS},
            "owner": {"check_url": "http://auth.security.svc.cluster.local/outpost.goauthentik.io/auth/nginx",
                      "origin": "https://approval.example.test", "owner_ids": ["owner"]},
        }
        bad = []
        for section in ("owner", "vaults"):
            for values in ("owner", [], ["owner", "owner"], [""], [1], ["x" * 129],
                           [f"o{i}" for i in range(33)]):
                item = json.loads(json.dumps(valid))
                target = item["owner"] if section == "owner" else item["vaults"]["iam"]
                target["owner_ids"] = values
                bad.append(item)
        for values in ("read_note", [], ["read_note", "read_note"], [1], [""],
                       ["read_note"] * 33):
            item = json.loads(json.dumps(valid))
            item["clients"]["codex"]["iam"] = values
            bad.append(item)
        for section, key in (("vaults", "app_name"), ("owner", "origin")):
            item = json.loads(json.dumps(valid))
            target = item["vaults"]["iam"] if section == "vaults" else item["owner"]
            target[key] = "x" * 1025
            bad.append(item)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            with (patch.dict(os.environ, {"OBSIDIAN_GATEWAY_REGISTRY": str(path),
                                         **{f"OBSIDIAN_MCP_TOKEN_{name.upper()}": token
                                            for name, token in TOKENS.items()}}),
                  patch("gateway.os.statvfs", return_value=type("Stat", (),
                         {"f_flag": os.ST_RDONLY})()),
                  patch("gateway.create_gateway", return_value="accepted") as factory):
                path.write_text(json.dumps(valid))
                self.assertEqual(create_gateway_from_env(), "accepted")
                self.assertEqual(factory.call_count, 1)
                for item in bad:
                    path.write_text(json.dumps(item))
                    with self.subTest(item=item), self.assertRaises(ValueError):
                        create_gateway_from_env()
                path.write_bytes(b" " * (_MAX_REGISTRY_BYTES + 1))
                with self.assertRaisesRegex(ValueError, "size"):
                    create_gateway_from_env()
                self.assertEqual(factory.call_count, 1)


if __name__ == "__main__":
    unittest.main()
