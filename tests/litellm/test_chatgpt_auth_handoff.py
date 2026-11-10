"""Synthetic-only checks for the fixed ChatGPT staging protocol."""

import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
import io
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/litellm/chatgpt_auth_handoff.py"
spec = importlib.util.spec_from_file_location("handoff", SCRIPT)
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def record(**changes):
    claims = base64.urlsafe_b64encode(json.dumps({"chatgpt_account_id": "acct"}).encode()).rstrip(b"=").decode()
    data = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
            "id_token": "x." + claims + ".x", "account_id": "acct",
            "expires_at": int(time.time()) + 3600}
    data.update(changes)
    return json.dumps(data).encode()


class HandoffTest(unittest.TestCase):
    def test_schema_and_size(self):
        self.assertTrue(h.parse_record(record()))
        for raw in (b"", b"x", b" " * (h.MAX + 1), record(expires_at=0),
                    record(expires_at="tomorrow"), record(account_id="other"),
                    record(access_token=""), record(extra="x")):
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                h.parse_record(raw)

    def test_malformed_claim_envelope(self):
        for payload in ("[]", "3", "null", "{}", '{"chatgpt_account_id":3}',
                        '{"chatgpt_account_id":"other"}'):
            claim = base64.urlsafe_b64encode(payload.encode()).rstrip(b"=").decode()
            raw = record(id_token="x." + claim + ".x")
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
                h.parse_record(raw)
            with tempfile.TemporaryDirectory() as d:
                root = Path(d) / "root"; root.mkdir(); self.fixture(root)
                self.assertEqual(self.run_receiver(root, True, raw).stdout, b"READY\nSTAGE_FAILED\n")
        for token in ("x", "x..x", ".e30.x", "x.e30."):
            with self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
                h.parse_record(record(id_token=token))

    def test_source_private_tmpfs(self):
        with tempfile.TemporaryDirectory(dir="/dev/shm") as d:
            p = Path(d) / "auth.json"
            p.write_bytes(record()); p.chmod(0o600)
            self.assertEqual(h.source_bytes(p), record_from_file(p))
            p.chmod(0o644)
            with self.assertRaises(ValueError): h.source_bytes(p)
            p.chmod(0o600)
            link = Path(d) / "link"
            link.symlink_to(p)
            with self.assertRaises(ValueError): h.source_bytes(link)
            p.write_bytes(b"x" * (h.MAX + 1))
            with self.assertRaises(ValueError): h.source_bytes(p)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as d:
            p = Path(d) / "auth.json"; p.write_bytes(record()); p.chmod(0o600)
            with self.assertRaises(ValueError): h.source_bytes(p)

    def run_receiver(self, root, stage, raw=b""):
        code = h.remote_program(stage).replace(h.ROOT, str(root))
        return subprocess.run([sys.executable, "-u", "-c", code], input=raw,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)

    def fixture(self, root):
        root.chmod(0o700)
        (root / "auth.json").write_bytes(b"old-chatgpt")
        (root / "auth.json").chmod(0o600)
        (root / "xai").mkdir(mode=0o700)
        (root / "xai" / "auth.json").write_bytes(b"old-xai")

    def test_remote_success_readback_and_old_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"; root.mkdir(); self.fixture(root)
            self.assertEqual(self.run_receiver(root, False).stdout, b"SAFE\n")
            r = self.run_receiver(root, True, record())
            self.assertEqual(r.stdout, b"READY\nSTAGED\n")
            g = root / h.GEN
            self.assertEqual(h.parse_record((g / "auth.json").read_bytes())["account_id"], "acct")
            self.assertEqual((g / "auth.json").stat().st_mode & 0o777, 0o600)
            self.assertEqual(g.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "auth.json").read_bytes(), b"old-chatgpt")
            self.assertEqual((root / "xai" / "auth.json").read_bytes(), b"old-xai")
            self.assertEqual(self.run_receiver(root, True, record()).stdout, b"GENERATION_EXISTS\n")

    def test_umask_and_atomic_publication(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"; root.mkdir(); self.fixture(root)
            for mask in (0o000, 0o077, 0o777):
                r = subprocess.run([sys.executable, "-u", "-c",
                                    "import os,sys;os.umask(int(sys.argv[1],8));exec(sys.argv[2])",
                                    oct(mask), h.remote_program(True).replace(h.ROOT, str(root))],
                                   input=record(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
                self.assertEqual(r.stdout, b"READY\nSTAGED\n")
                g = root / h.GEN
                self.assertEqual(g.stat().st_mode & 0o777, 0o700)
                self.assertEqual((g / "auth.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual((root / "auth.json").read_bytes(), b"old-chatgpt")
                self.assertEqual((root / "xai" / "auth.json").read_bytes(), b"old-xai")
                (g / "auth.json").unlink(); g.rmdir()
            # Failed readback/write/fsync cannot publish the final directory.
            code = h.remote_program(True).replace(h.ROOT, str(root))
            for needle, replacement in (("os.fsync(g)", "raise ValueError()"),
                                        ("os.fsync(f)\n            libc", "raise ValueError()\n            libc")):
                r = subprocess.run([sys.executable, "-u", "-c", code.replace(needle, replacement)],
                                   input=record(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
                self.assertEqual(r.stdout, b"READY\nSTAGE_FAILED\n")
                self.assertFalse((root / h.GEN).exists())
                staged = root / ("." + h.GEN + ".staging")
                self.assertTrue(staged.exists())
                self.assertEqual(self.run_receiver(root, True, record()).stdout, b"GENERATION_EXISTS\n")
                (staged / "auth.json").unlink(); staged.rmdir()
            r = subprocess.run([sys.executable, "-u", "-c", code.replace(
                "published=True\n            os.fsync(f)", "published=True\n            raise ValueError()")],
                input=record(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
            self.assertEqual(r.stdout, b"READY\nSTAGE_UNCERTAIN\n")
            self.assertEqual((root / h.GEN / "auth.json").read_bytes(), record())
            self.assertEqual(self.run_receiver(root, True, record()).stdout, b"GENERATION_EXISTS\n")
            self.assertEqual((root / "auth.json").read_bytes(), b"old-chatgpt")
            self.assertEqual((root / "xai" / "auth.json").read_bytes(), b"old-xai")

    def test_remote_rejects_unsafe_and_partial(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"; root.mkdir(); self.fixture(root)
            for raw in (b"bad", b"x" * (h.MAX + 1), record(expires_at=0)):
                self.assertEqual(self.run_receiver(root, True, raw).stdout, b"READY\nSTAGE_FAILED\n")
                self.assertFalse((root / h.GEN).exists())
            (root / h.GEN).mkdir()
            self.assertEqual(self.run_receiver(root, True, record()).stdout, b"GENERATION_EXISTS\n")
            (root / h.GEN).rmdir()
            (root / "auth.json").chmod(0o644)
            self.assertEqual(self.run_receiver(root, False).stdout, b"DESTINATION_UNSAFE\n")
            (root / "auth.json").chmod(0o600)
            (root / "xai").rename(root / "xai-old")
            (root / "xai").symlink_to(root / "xai-old")
            self.assertEqual(self.run_receiver(root, False).stdout, b"DESTINATION_UNSAFE\n")

    def test_root_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "root"; root.mkdir(); self.fixture(root)
            root.chmod(0o722)
            self.assertEqual(self.run_receiver(root, False).stdout, b"DESTINATION_UNSAFE\n")
            root.chmod(0o755)
            self.assertEqual(self.run_receiver(root, False).stdout, b"SAFE\n")
            link = Path(d) / "link"; link.symlink_to(root)
            self.assertEqual(self.run_receiver(link, False).stdout, b"DESTINATION_UNSAFE\n")

    def test_uid_change_prevents_transfer(self):
        with mock.patch.object(h, "call", return_value={"metadata": {"uid": "new"}}):
            with self.assertRaisesRegex(ValueError, "POD_IDENTITY_CHANGED"):
                h.uid_still("pod", "old")

    def test_metadata_owner_image_mount_and_other_writer(self):
        dep = {"metadata": {"name": "litellm", "namespace": "ai", "uid": "d"},
               "spec": {"replicas": 1, "strategy": {"type": "Recreate"}}}
        rs = {"metadata": {"uid": "r", "ownerReferences": [{"uid": "d", "kind": "Deployment", "controller": True}]}}
        pod = {"metadata": {"name": "p", "uid": "u", "ownerReferences": [{"uid": "r", "kind": "ReplicaSet", "controller": True}]},
               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                          "containerStatuses": [{"name": "litellm", "ready": True, "imageID": "x@sha256:" + h.IMAGE_DIGEST}]},
               "spec": {"volumes": [{"name": "auth", "persistentVolumeClaim": {"claimName": "litellm-data"}}],
                        "containers": [{"name": "litellm", "image": h.IMAGE,
                                        "volumeMounts": [{"name": "auth", "mountPath": h.ROOT}],
                                        "env": [{"name": "CHATGPT_TOKEN_DIR", "value": h.ROOT},
                                                {"name": "XAI_OAUTH_TOKEN_DIR", "value": h.ROOT + "/xai"}]}]}}
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, {"items": [rs]}]):
            self.assertEqual(h.metadata(), ("p", "u", "litellm"))
        other = {"metadata": {"uid": "other"}, "status": {"phase": "Running"},
                 "spec": {"volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "litellm-data"}}],
                          "containers": [{"volumeMounts": [{"name": "v", "mountPath": "/other"}]}]}}
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod, other]}, {"items": [rs]}]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()
        other["status"]["phase"] = "Succeeded"
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod, other]}, {"items": [rs]}]):
            self.assertEqual(h.metadata(), ("p", "u", "litellm"))
        for kind in ("initContainers", "ephemeralContainers"):
            pod["spec"][kind] = [{"name": "extra", "volumeMounts": [{"name": "auth", "mountPath": "/other"}]}]
            with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, {"items": [rs]}]):
                with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()
            pod["spec"][kind][0]["volumeMounts"][0]["readOnly"] = True
            with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, {"items": [rs]}]):
                self.assertEqual(h.metadata(), ("p", "u", "litellm"))
            del pod["spec"][kind]
        pod["status"]["containerStatuses"][0]["imageID"] = "wrong"
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, {"items": [rs]}]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()

    def test_no_live_default(self):
        with mock.patch.object(h.subprocess, "Popen") as popen, mock.patch.object(h.subprocess, "run") as run:
            with mock.patch.object(sys, "argv", ["handoff", "preflight"]):
                self.assertEqual(h.main(), 1)
            popen.assert_not_called(); run.assert_not_called()

    def test_unexpected_output_and_timeout(self):
        for output in (b"READY\nleak", b"secret", b"READY\nSTAGED\n", b""):
            proc = mock.Mock()
            proc.stdout.fileno.return_value = 0
            with mock.patch.object(h.select, "select", return_value=([proc.stdout], [], [])), \
                 mock.patch.object(h.os, "read", return_value=output):
                if output == b"READY\nSTAGED\n":
                    with self.assertRaises(ValueError): h.marker(proc, 1)
                elif output != b"READY\nleak":
                    with self.assertRaises(ValueError): h.marker(proc, 1)
                else:
                    with self.assertRaises(ValueError): h.marker(proc, 1)
        with mock.patch.object(h.select, "select", return_value=([], [], [])):
            with self.assertRaises(ValueError): h.marker(mock.Mock(), 1)

    def test_stage_timeout_reaps_child(self):
        children = []
        real_popen = subprocess.Popen
        def fake_popen(*_args, **kwargs):
            child = real_popen([sys.executable, "-u", "-c", "import time; print('READY',flush=True); time.sleep(5)"], **kwargs)
            children.append(child)
            return child
        start = time.monotonic()
        with mock.patch.object(h.subprocess, "Popen", side_effect=fake_popen), \
             mock.patch.object(h, "uid_still"):
            with self.assertRaisesRegex(ValueError, "STAGE_UNCERTAIN"):
                h.remote("pod", "litellm", True, "uid", record(), 0.1)
        self.assertLess(time.monotonic() - start, 1)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        def stage_overflow(*_args, **kwargs):
            proc = real_popen([sys.executable, "-u", "-c",
                               "import sys,time;print('READY',flush=True);sys.stdin.buffer.read();sys.stdout.buffer.write(b'K'*100000);sys.stdout.flush();time.sleep(5)"], **kwargs)
            children.append(proc); return proc
        start = time.monotonic()
        with mock.patch.object(h.subprocess, "Popen", side_effect=stage_overflow), mock.patch.object(h, "uid_still"):
            with self.assertRaisesRegex(ValueError, "STAGE_UNCERTAIN"):
                h.remote("p", "litellm", True, "u", record(), 1)
        self.assertLess(time.monotonic() - start, 1)
        self.assertIsNotNone(children[-1].returncode)

    def test_cancellation_reaps_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(5)"],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        start = time.monotonic()
        with mock.patch.object(h.subprocess, "Popen", return_value=child), \
             mock.patch.object(h, "marker", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                h.remote("pod", "litellm", False, "uid", None, 1)
        self.assertIsNotNone(child.returncode)
        self.assertLess(time.monotonic() - start, 1)

    def test_actual_signals_reap_owned_child_and_preserve_source(self):
        for command in ("preflight", "stage"):
            for sig in (signal.SIGTERM, signal.SIGHUP):
                with self.subTest(command=command, signal=sig), tempfile.TemporaryDirectory() as d:
                    source = Path(d) / "synthetic-source"
                    original = record()
                    source.write_bytes(original)
                    pid_file = Path(d) / "child.pid"
                    runner = f'''
import importlib.util, os, signal, subprocess, sys
from unittest import mock
spec=importlib.util.spec_from_file_location("handoff", {str(SCRIPT)!r})
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)
signal.signal(signal.SIGTERM,h.cancelled); signal.signal(signal.SIGHUP,h.cancelled)
real=subprocess.Popen
def synthetic(args,**kwargs):
    stage="-i" in args
    if {command!r}=="stage" and not stage:
        code="print('SAFE',flush=True)"
    else:
        code="import time;print('READY' if " + str(stage) + " else 'SAFE',flush=True);time.sleep(30)"
    p=real([sys.executable,"-u","-c",code],**kwargs)
    if {command!r}=="preflight" or stage:
        with open({str(pid_file)!r},"w") as f: f.write(str(p.pid))
    return p
sys.argv=["handoff",{command!r},"--live"] + (["--source",{str(source)!r}] if {command!r}=="stage" else [])
with mock.patch.object(h,"metadata",return_value=("p","u","litellm")),mock.patch.object(h,"uid_still"),mock.patch.object(h,"source_bytes",return_value={record()!r}),mock.patch.object(h.subprocess,"Popen",side_effect=synthetic):
    sys.exit(h.main())
'''
                    parent = subprocess.Popen([sys.executable, "-u", "-c", runner],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        deadline = time.monotonic() + 3
                        while not pid_file.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(pid_file.exists(), "synthetic child did not start")
                        child_pid = int(pid_file.read_text())
                        os.kill(parent.pid, sig)
                        out, err = parent.communicate(timeout=3)
                        self.assertEqual(parent.returncode, 1, err.decode(errors="replace"))
                        self.assertEqual(out, b"STAGE_UNCERTAIN\n" if command == "stage" else b"PREFLIGHT_FAILED\n")
                        self.assertEqual(err, b"")
                        with self.assertRaises(ProcessLookupError):
                            os.kill(child_pid, 0)
                        self.assertEqual(source.read_bytes(), original)
                    finally:
                        if parent.poll() is None: parent.kill(); parent.wait()

    def test_bounded_capture_and_stage_cli(self):
        real_popen = subprocess.Popen
        children = []
        def overflowing(*_args, **kwargs):
            proc = real_popen([sys.executable, "-u", "-c",
                               "import sys,time;sys.stdout.buffer.write(b'Z'*1100000);sys.stdout.flush();time.sleep(5)"], **kwargs)
            children.append(proc); return proc
        start = time.monotonic()
        with mock.patch.object(h.subprocess, "Popen", side_effect=overflowing):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"):
                h.call(["synthetic"], 1)
        self.assertLess(time.monotonic() - start, 1)
        self.assertIsNotNone(children[0].returncode)
        with mock.patch.object(h, "source_bytes", return_value=record()), \
             mock.patch.object(h, "metadata", side_effect=[("p", "u", "litellm"), ("p", "u", "litellm")]) as md, \
             mock.patch.object(h, "uid_still") as uid, \
             mock.patch.object(h, "remote", side_effect=["SAFE", "STAGED"]) as remote:
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["handoff", "stage", "--live", "--source", "/dev/shm/synthetic"]), redirect_stdout(out):
                self.assertEqual(h.main(), 0)
            self.assertEqual(out.getvalue(), "STAGED\n")
            self.assertEqual(remote.call_count, 2)
            self.assertEqual(md.call_count, 2)
            uid.assert_called_once_with("p", "u")
            self.assertFalse(remote.call_args_list[0].args[2])
            self.assertTrue(remote.call_args_list[1].args[2])
        with mock.patch.object(h, "source_bytes", return_value=record()), \
             mock.patch.object(h, "metadata", side_effect=[("p", "u", "litellm"), ("p", "new", "litellm")]), \
             mock.patch.object(h, "uid_still"), \
             mock.patch.object(h, "remote", side_effect=["SAFE", "STAGED"]):
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["handoff", "stage", "--live", "--source", "/dev/shm/synthetic"]), redirect_stdout(out):
                self.assertEqual(h.main(), 1)
            self.assertEqual(out.getvalue(), "STAGE_UNCERTAIN\n")
        for replies in (["SAFE", None], ["SAFE", ValueError("STAGE_UNCERTAIN")]):
            with mock.patch.object(h, "source_bytes", return_value=record()), \
                 mock.patch.object(h, "metadata", return_value=("p", "u", "litellm")), \
                 mock.patch.object(h, "uid_still"), \
                 mock.patch.object(h, "remote", side_effect=replies):
                out = io.StringIO()
                with mock.patch.object(sys, "argv", ["handoff", "stage", "--live", "--source", "/dev/shm/synthetic"]), redirect_stdout(out):
                    self.assertEqual(h.main(), 1)
                self.assertNotIn("STAGED", out.getvalue())



def record_from_file(path):
    return path.read_bytes()


if __name__ == "__main__":
    unittest.main()
