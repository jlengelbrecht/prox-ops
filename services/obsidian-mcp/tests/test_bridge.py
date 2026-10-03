"""Isolated Unix socket tests for the typed bridge adapter."""

import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
import sys
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "bridge.py"
spec = importlib.util.spec_from_file_location("obsidian_bridge", SOURCE)
bridge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bridge
spec.loader.exec_module(bridge)


class Fixture:
    instances = {}

    def __init__(self, vault, reply=None, delay=0):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.root = self.home / "vault"
        self.root.mkdir()
        runtime = self.home / "runtime"
        runtime.mkdir(mode=0o700)
        self.directory = runtime / vault
        self.directory.mkdir(mode=0o700)
        self.credential = self.directory / "credential"
        self.credential.write_text("a" * 43)
        self.credential.chmod(0o600)
        self.endpoint = bridge.VaultEndpoint(vault, vault.upper(), self.root,
                                              self.directory / f"{vault}.sock", self.credential)
        self.instances[bridge._socket_address(self.endpoint, "a" * 43)] = self
        self.requests = []
        self.reply = reply
        self.delay = delay

    def close(self):
        self.instances.pop(bridge._socket_address(self.endpoint, "a" * 43), None)
        self.tmp.cleanup()


class SocketDouble:
    """Byte-level local transport used where AF_UNIX bind is sandboxed."""
    def __init__(self, *args):
        SocketDouble.last = self
        self.timeout = None
        self.fixture = None
        self.response = b""
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, filename):
        self.fixture = Fixture.instances[filename]

    def sendall(self, raw):
        request = json.loads(raw)
        self.fixture.requests.append(request)
        reply = self.fixture.reply
        if callable(reply):
            reply = reply(request)
        elif request["op"] == "health":
            reply = None
        if reply is None:
            mutation = request["args"].get("mutation", {})
            results = {
                "health": {"protocol": 1, "vault": request["vault"], "app_name": self.fixture.endpoint.app_name,
                           "app_root": str(self.fixture.root), "capabilities": list(bridge.RESULT_KEYS)},
                "list": {"entries": []}, "read": {"path": request["args"].get("path", ""),
                                                   "content": "", "revision": "sha256:" + "0" * 64},
                "search": {"matches": []}, "reserve": {"receipt": "00000000-0000-4000-8000-000000000001"},
                "create": {"receipt": request["args"].get("receipt"), "status": "committed",
                           "result": {"path": mutation.get("path", ""), "revision": "sha256:" + "0" * 64},
                           "error": None},
                "append": {"receipt": request["args"].get("receipt"), "status": "committed",
                           "result": {"path": mutation.get("path", ""), "revision": "sha256:" + "0" * 64},
                           "error": None},
                "receipt": {"receipt": request["args"].get("receipt"), "status": "committed",
                            "result": {"path": mutation.get("path", ""), "revision": "sha256:" + "0" * 64},
                            "error": None},
                "embed": {"path": "image.png", "mime": "image/png", "data": ""},
            }
            reply = {"v": 1, "vault": request["vault"], "ok": True,
                     "result": results[request["op"]]}
        self.response = json.dumps(reply).encode() + b"\n"

    def recv(self, count):
        if self.fixture.delay > self.timeout:
            raise socket.timeout()
        time.sleep(self.fixture.delay)
        count = min(count, self.fixture.chunk_size) if hasattr(self.fixture, "chunk_size") else count
        output, self.response = self.response[:count], self.response[count:]
        return output


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.socket_patch = patch.object(bridge.socket, "socket", SocketDouble)
        cls.socket_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.socket_patch.stop()

    def test_exact_vault_selection_and_fixed_calls(self):
        a, b, c = Fixture("iam"), Fixture("homelab"), Fixture("third")
        try:
            client = bridge.BridgeClient({"iam": a.endpoint, "homelab": b.endpoint,
                                          "third": c.endpoint})
            self.assertEqual(client.read_note("iam", "Private/note.md")["path"], "Private/note.md")
            self.assertEqual(client.create_note("homelab", "x.md", "test")["path"], "x.md")
            self.assertEqual(client.ready("third")["vault"], "third")
            self.assertEqual([r["op"] for r in a.requests], ["health", "read"])
            self.assertEqual([r["op"] for r in b.requests], ["health", "reserve", "health", "create"])
            self.assertEqual(a.requests[0]["vault"], "iam")
            with self.assertRaisesRegex(bridge.BridgeError, "unknown_vault"):
                client.read_note("missing", "x.md")
            with self.assertRaisesRegex(ValueError, "Ambiguous"):
                bridge.BridgeClient({"iam": a.endpoint, "duplicate": bridge.VaultEndpoint(
                    "duplicate", "Other", a.root, c.endpoint.socket_path, c.credential)})
        finally:
            a.close(); b.close(); c.close()

    def test_readiness_requires_exact_app_identity(self):
        a = Fixture("iam", lambda req: {"v": 1, "vault": "iam", "ok": True,
                                         "result": {"protocol": 1, "vault": "iam", "app_name": "WRONG",
                                                    "app_root": str(a.root), "capabilities": list(bridge.RESULT_KEYS)}})
        try:
            client = bridge.BridgeClient({"iam": a.endpoint})
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.ready("iam")
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "note.md")
            self.assertTrue(all(request["op"] == "health" for request in a.requests))
        finally:
            a.close()

    def test_readiness_and_result_paths_are_bound_to_registry_and_request(self):
        a = Fixture("iam")
        try:
            client = bridge.BridgeClient({"iam": a.endpoint})
            self.assertEqual(client.ready("iam")["app_root"], str(a.root))
            self.assertEqual(client.list_entries("iam")["entries"], [])
            self.assertEqual(client.resolve_embed("iam", "note.md", "image.png")["mime"], "image/png")
            a.reply = {"v": 1, "vault": "iam", "ok": True,
                       "result": {"path": "other.md", "content": "", "revision": "sha256:" + "0" * 64}}
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "note.md")
            a.reply = {"v": 1, "vault": "iam", "ok": True,
                       "result": {"entries": [{"path": ".obsidian/config", "kind": "file"}]}}
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.list_entries("iam")
        finally:
            a.close()

    def test_response_mismatch_remote_error_and_token_never_echoed(self):
        a = Fixture("iam", {"v": 1, "vault": "homelab", "ok": True, "result": {}})
        try:
            client = bridge.BridgeClient({"iam": a.endpoint})
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.search("iam", "needle")
            a.reply = {"v": 1, "vault": "iam", "ok": False, "error": "conflict"}
            with self.assertRaisesRegex(bridge.BridgeError, "conflict"):
                client.append_note("iam", "n.md", "x", "sha256:" + "0" * 64)
            a.reply = {"v": 1, "vault": "iam", "ok": False, "error": "secret:" + "a" * 43}
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "n.md")
        finally:
            a.close()

    def test_limits_timeout_and_private_credential_mode(self):
        a = Fixture("iam")
        try:
            client = bridge.BridgeClient({"iam": a.endpoint}, timeout=0.05)
            with self.assertRaisesRegex(bridge.BridgeError, "limit_exceeded"):
                client.create_note("iam", "x.md", "x" * bridge.MAX_REQUEST)
            a.delay = 0.2
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "x.md")
            a.credential.chmod(0o666)
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "x.md")
        finally:
            a.close()

    def test_whole_request_deadline_stops_slow_drip_and_bounds_health_plus_write(self):
        a = Fixture("iam", delay=0.003)
        try:
            client = bridge.BridgeClient({"iam": a.endpoint}, timeout=0.035)
            a.chunk_size = 1
            start = time.monotonic()
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.create_note("iam", "new.md", "text")
            self.assertLess(time.monotonic() - start, 0.15)
            self.assertTrue(SocketDouble.last.closed)
            self.assertEqual([r["op"] for r in a.requests], ["health"])
            a.chunk_size = 100
            a.delay = 0.001
            self.assertEqual(client.create_note("iam", "new.md", "text")["path"], "new.md")
            self.assertEqual([r["op"] for r in a.requests[-4:]], ["health", "reserve", "health", "create"])
        finally:
            a.close()

    def test_mutation_timeout_returns_receipt_without_retrying_write(self):
        a = Fixture("iam")
        try:
            def delayed(request):
                if request["op"] == "append":
                    a.delay = 0.08
                return None
            a.reply = delayed
            client = bridge.BridgeClient({"iam": a.endpoint}, timeout=0.04)
            original = "sha256:" + "0" * 64
            result = client.append_note("iam", "n.md", "+", original)
            self.assertEqual(result["status"], "pending")
            self.assertRegex(result["receipt"], bridge.RECEIPT_RE)
            self.assertEqual([r["op"] for r in a.requests].count("append"), 1)
            a.delay = 0
            reconciled = client.mutation_receipt("iam", "append", {"path": "n.md", "content": "+",
                "expected_revision": original}, result["receipt"], "bridge")
            self.assertEqual(reconciled["status"], "committed")
            self.assertEqual([r["op"] for r in a.requests].count("append"), 1)
        finally:
            a.close()

    def test_unexpected_results_and_response_bound(self):
        a = Fixture("iam", {"v": 1, "vault": "iam", "ok": True, "result": {"content": "secret"}})
        try:
            client = bridge.BridgeClient({"iam": a.endpoint})
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "n.md")
            a.reply = {"v": 1, "vault": "iam", "ok": True, "result": {"path": "n.md", "content": "x" * bridge.MAX_RESPONSE,
                                                                 "revision": "sha256:" + "0" * 64}}
            with self.assertRaisesRegex(bridge.BridgeError, "limit_exceeded"):
                client.read_note("iam", "n.md")
            a.credential.chmod(0o644)
            with self.assertRaisesRegex(bridge.BridgeError, "unavailable"):
                client.read_note("iam", "n.md")
        finally:
            a.close()


def real_ipc_client(manifest: Path) -> None:
    """Exercise actual Node listeners from the Python adapter; never mock sockets."""
    records = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(records) == 2
    endpoints = {item["vault_id"]: bridge.VaultEndpoint(
        item["vault_id"], item["app_name"], Path(item["app_root"]),
        Path(item["socket"]), Path(item["credential_file"])) for item in records}
    client = bridge.BridgeClient(endpoints)
    assert client.ready("iam")["vault"] == "iam"
    assert client.ready("homelab")["vault"] == "homelab"
    assert client.read_note("iam", "Private/note.md")["content"] == "from iam"
    assert client.read_note("homelab", "Private/note.md")["content"] == "from homelab"
    short = bridge.BridgeClient(endpoints, timeout=0.05)
    created = short.create_note("iam", "Private/late.md", "late")
    assert created["status"] == "pending"
    time.sleep(0.18)
    create_args = {"path": "Private/late.md", "content": "late"}
    assert short.mutation_receipt("iam", "create", create_args, created["receipt"], "bridge")["status"] == "committed"
    assert client._transport("iam", "create", {"client": "bridge", "receipt": created["receipt"],
           "mutation": create_args})["status"] == "committed"
    assert client.read_note("iam", "Private/late.md")["content"] == "late"
    original = client.read_note("iam", "Private/note.md")["revision"]
    appended = short.append_note("iam", "Private/note.md", "+", original)
    assert appended["status"] == "pending"
    time.sleep(0.18)
    append_args = {"path": "Private/note.md", "content": "+", "expected_revision": original}
    final = short.mutation_receipt("iam", "append", append_args, appended["receipt"], "bridge")
    assert final["status"] == "committed"
    assert client._transport("iam", "append", {"client": "bridge", "receipt": appended["receipt"],
           "mutation": append_args})["result"]["revision"] == final["revision"]
    assert client.read_note("iam", "Private/note.md")["content"] == "from iam+"
    for vault in ("iam", "homelab"):
        entry = endpoints[vault]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(bridge._socket_address(entry, entry.credential_file.read_text().strip()))
            connection.sendall(json.dumps({"v": 1, "vault": vault, "token": "wrong", "op": "read",
                                           "args": {"path": "Private/note.md"}}).encode() + b"\n")
            assert json.loads(connection.recv(4096))["error"] == "unauthorized"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(bridge._socket_address(entry, entry.credential_file.read_text().strip()))
            connection.sendall(json.dumps({"v": 1, "vault": "other", "token": entry.credential_file.read_text().strip(),
                                           "op": "read", "args": {"path": "Private/note.md"}}).encode() + b"\n")
            assert json.loads(connection.recv(4096))["error"] == "unauthorized"
    try:
        client.read_note("other", "Private/note.md")
    except bridge.BridgeError as error:
        assert error.code == "unknown_vault"
    else:
        raise AssertionError("unknown vault was accepted")
    print("real IPC passed", flush=True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--real-ipc-client":
        real_ipc_client(Path(sys.argv[2]))
    else:
        unittest.main()
