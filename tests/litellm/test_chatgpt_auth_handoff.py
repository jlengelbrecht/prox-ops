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


def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "x." + payload + ".x"


def record(**changes):
    data = {"access_token": "synthetic-access", "refresh_token": "synthetic-refresh",
            "id_token": jwt({"chatgpt_account_id": "acct"}), "account_id": "acct",
            "expires_at": int(time.time()) + 3600}
    data.update(changes)
    return json.dumps(data).encode()


def metadata_fixture():
    dep = {"metadata": {"name": "litellm", "namespace": "ai", "uid": "d"},
           "spec": {"replicas": 1, "strategy": {"type": "Recreate"}}}
    rs = {"metadata": {"name": "litellm-abc", "namespace": "ai", "uid": "r",
           "ownerReferences": [{"uid": "d", "kind": "Deployment", "controller": True}]}}
    pod = {"metadata": {"name": "p", "uid": "u", "ownerReferences": [
           {"name": "litellm-abc", "uid": "r", "kind": "ReplicaSet", "controller": True}]},
           "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                      "containerStatuses": [{"name": "litellm", "ready": True, "imageID": "x@sha256:" + h.IMAGE_DIGEST}]},
           "spec": {"volumes": [{"name": "auth", "persistentVolumeClaim": {"claimName": "litellm-data"}}],
                    "containers": [{"name": "litellm", "image": h.IMAGE,
                                    "volumeMounts": [{"name": "auth", "mountPath": h.ROOT}],
                                    "env": [{"name": "CHATGPT_TOKEN_DIR", "value": h.ROOT},
                                            {"name": "XAI_OAUTH_TOKEN_DIR", "value": h.ROOT + "/xai"}]}]}}
    return dep, pod, rs


class HandoffTest(unittest.TestCase):
    def test_synthetic_native_contract_fixture_admission(self):
        # Flat records model the approved native producer's output contract;
        # the producer itself is not part of this checkout.
        namespace = "https://api.openai.com/auth"
        expiry = int(time.time()) + 3600
        account = lambda value: {namespace: {"chatgpt_account_id": value}}
        cases = (
            ({"exp": expiry, **account("acct")}, account("acct")),
            ({"exp": expiry, **account("acct")}, {}),
            ({"exp": expiry}, account("acct")),
            ({"exp": expiry}, {"chatgpt_account_id": "acct"}),
        )
        for access, identity in cases:
            with self.subTest(access=access, identity=identity):
                raw = record(access_token=jwt(access), id_token=jwt(identity))
                self.assertEqual(h.parse_record(raw)["account_id"], "acct")
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d) / "root"; root.mkdir(); self.fixture(root)
                    self.assertEqual(self.run_receiver(root, True, raw).stdout, b"READY\nSTAGED\n")
        # Conflicting, malformed, absent, and near-expiry claims fail admission.
        for access, identity in (({"exp": expiry, **account("other")}, account("acct")),
                                 ({"exp": expiry, **account(3)}, account("acct")),
                                 ({"exp": expiry, namespace: "bad"}, account("acct")),
                                 ({"exp": expiry}, {})):
            with self.subTest(flat_access=access, flat_identity=identity):
                raw = record(access_token=jwt(access), id_token=jwt(identity))
                with self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
                    h.parse_record(raw)
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d) / "root"; root.mkdir(); self.fixture(root)
                    self.assertEqual(self.run_receiver(root, True, raw).stdout, b"READY\nSTAGE_FAILED\n")
        for remaining in (0, 299, 300):
            with self.subTest(remaining=remaining):
                raw = record(expires_at=time.time() + remaining)
                with self.assertRaisesRegex(ValueError, "SCHEMA_INVALID"):
                    h.parse_record(raw)
                with tempfile.TemporaryDirectory() as d:
                    root = Path(d) / "root"; root.mkdir(); self.fixture(root)
                    self.assertEqual(self.run_receiver(root, True, raw).stdout, b"READY\nSTAGE_FAILED\n")

    def test_optimized_metadata_refuses_before_kubectl(self):
        code = ("import importlib.util,sys,unittest.mock as mock;"
                "spec=importlib.util.spec_from_file_location('handoff',sys.argv[1]);"
                "h=importlib.util.module_from_spec(spec);spec.loader.exec_module(h);"
                "p=mock.patch.object(h,'call',side_effect=RuntimeError('kubectl called'));p.start();"
                "\ntry: h.metadata()\nexcept ValueError as e:"
                " sys.exit(0 if str(e)=='PREFLIGHT_FAILED' else 2)\nsys.exit(3)")
        result = subprocess.run([sys.executable, "-O", "-c", code, str(SCRIPT)],
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

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

    def repair_code(self, root):
        # Change only the fixed production identity/path constants for a local fixture.
        code = h.repair_program()
        code = code.replace('("var","lib","litellm")', repr(tuple(root.parent.parts[1:])))
        code = code.replace('ancestor.st_uid!=0 or stat.S_IMODE(ancestor.st_mode)&0o022', 'False')
        code = code.replace('os.geteuid()!=0 or os.getegid()!=0', 'False')
        code = code.replace('st_uid!=0', f'st_uid!={os.geteuid()}')
        code = code.replace('st_gid!=65534', f'st_gid!={os.getegid()}')
        code = code.replace('(0,65534', f'({os.geteuid()},{os.getegid()}')
        return code

    def run_repair(self, root, code=None):
        return subprocess.run([sys.executable, '-u', '-c', code or self.repair_code(root)],
                              input=b'1', stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=3)

    def test_repair_exact_fixture_and_idempotence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'chatgpt'; root.mkdir(); self.fixture(root)
            auth = root / 'auth.json'; xai = root / 'xai'
            root.chmod(0o2775); auth.chmod(0o644)
            before = (auth.read_bytes(), (xai / 'auth.json').read_bytes(),
                      auth.stat().st_ino, xai.stat().st_ino, xai.stat().st_mode)
            self.assertEqual(self.run_repair(root).stdout, b'READY\nREPAIRED\n')
            self.assertEqual(root.stat().st_mode & 0o7777, 0o700)
            self.assertEqual(auth.stat().st_mode & 0o7777, 0o600)
            self.assertEqual((auth.read_bytes(), (xai / 'auth.json').read_bytes(),
                              auth.stat().st_ino, xai.stat().st_ino, xai.stat().st_mode), before)
            self.assertEqual(self.run_repair(root).stdout, b'READY\nREPAIRED\n')
            self.assertEqual(self.run_receiver(root, False).stdout, b'SAFE\n')

    def test_repair_refuses_symlinks_hardlinks_and_wrong_identity(self):
        for problem in ('root_link', 'auth_link', 'auth_hardlink', 'xai_link', 'wrong_owner'):
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as d:
                root = Path(d) / 'chatgpt'; root.mkdir(); self.fixture(root)
                root.chmod(0o2775); (root / 'auth.json').chmod(0o644)
                target = root
                code = self.repair_code(root)
                if problem == 'root_link':
                    target = Path(d) / 'link'; target.symlink_to(root)
                    code = self.repair_code(target).replace('root=opened(parent,"chatgpt")', 'root=opened(parent,"link")').replace('parent,"chatgpt",root', 'parent,"link",root')
                elif problem == 'auth_link':
                    (root / 'auth.json').rename(root / 'old'); (root / 'auth.json').symlink_to(root / 'old')
                elif problem == 'auth_hardlink':
                    os.link(root / 'auth.json', root / 'other')
                elif problem == 'xai_link':
                    (root / 'xai').rename(root / 'old-xai'); (root / 'xai').symlink_to(root / 'old-xai')
                else:
                    code = code.replace('rs.st_uid!=' + str(os.geteuid()), 'rs.st_uid!=-1')
                self.assertEqual(self.run_repair(target, code).stdout, b'REPAIR_REFUSED\n')
                self.assertEqual(root.stat().st_mode & 0o7777, 0o2775)
                if problem != 'auth_link':
                    self.assertEqual((root / 'auth.json').stat().st_mode & 0o7777, 0o644)

    def test_repair_partial_error_is_uncertain(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'chatgpt'; root.mkdir(); self.fixture(root)
            root.chmod(0o2775); (root / 'auth.json').chmod(0o644)
            code = self.repair_code(root).replace('os.fchmod(auth,0o600)', 'raise OSError()')
            self.assertEqual(self.run_repair(root, code).stdout, b'READY\nREPAIR_UNCERTAIN\n')
            self.assertEqual(root.stat().st_mode & 0o7777, 0o700)
            self.assertEqual((root / 'auth.json').stat().st_mode & 0o7777, 0o644)
            self.assertEqual((root / 'auth.json').read_bytes(), b'old-chatgpt')
            self.assertEqual((root / 'xai' / 'auth.json').read_bytes(), b'old-xai')

    def test_repair_timeout_cancellation_and_identity_reap_child(self):
        real_popen = subprocess.Popen
        children = []
        def stalled(*_args, **kwargs):
            child = real_popen([sys.executable, '-u', '-c',
                                "import time;print('READY',flush=True);time.sleep(5)"], **kwargs)
            children.append(child)
            return child
        for identity_check in (None, ValueError('POD_IDENTITY_CHANGED')):
            with mock.patch.object(h.subprocess, 'Popen', side_effect=stalled), \
                 mock.patch.object(h, 'metadata', return_value=('p', 'u', 'litellm')), \
                 mock.patch.object(h, 'uid_still', side_effect=identity_check):
                with self.assertRaisesRegex(ValueError,
                                            'REPAIR_UNCERTAIN' if identity_check is None else 'REPAIR_REFUSED'):
                    h.remote_repair('p', 'litellm', 'u', 0.1)
            self.assertIsNotNone(children[-1].returncode)
        with mock.patch.object(h.subprocess, 'Popen', side_effect=stalled), \
             mock.patch.object(h, 'marker', side_effect=KeyboardInterrupt):
            with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN'):
                h.remote_repair('p', 'litellm', 'u', 1)
        self.assertIsNotNone(children[-1].returncode)

    def test_harden_cli_requires_live_and_verifies_after_repair(self):
        with mock.patch.object(h.subprocess, 'Popen') as popen:
            with mock.patch.object(sys, 'argv', ['handoff', 'harden']):
                self.assertEqual(h.main(), 1)
            popen.assert_not_called()
        with mock.patch.object(h, 'metadata', return_value=('p', 'u', 'litellm')) as md, \
             mock.patch.object(h, 'uid_still') as uid, \
             mock.patch.object(h, 'remote_repair', return_value='REPAIRED') as repair, \
             mock.patch.object(h, 'remote', return_value='SAFE') as remote:
            out = io.StringIO()
            with mock.patch.object(sys, 'argv', ['handoff', 'harden', '--live', '--timeout', '300']), redirect_stdout(out):
                self.assertEqual(h.main(), 0)
            self.assertEqual(out.getvalue(), 'REPAIRED\n')
            self.assertEqual(md.call_count, 2)
            self.assertEqual(uid.call_count, 2)
            repair.assert_called_once_with('p', 'litellm', 'u', 300)
            remote.assert_called_once_with('p', 'litellm', False, 'u', None, 300)
        with mock.patch.object(h, 'metadata', side_effect=[('p', 'u', 'litellm'), ('p', 'new', 'litellm')]), \
             mock.patch.object(h, 'uid_still'), mock.patch.object(h, 'remote_repair', return_value='REPAIRED'):
            out = io.StringIO()
            with mock.patch.object(sys, 'argv', ['handoff', 'harden', '--live']), redirect_stdout(out):
                self.assertEqual(h.main(), 1)
            self.assertEqual(out.getvalue(), 'REPAIR_UNCERTAIN\n')

    def test_repair_handshake_checks_workload_before_chmod(self):
        real_popen = subprocess.Popen
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / 'chatgpt'; root.mkdir(); self.fixture(root)
            root.chmod(0o2775); (root / 'auth.json').chmod(0o644)
            def local_process(_args, **kwargs):
                return real_popen([sys.executable, '-u', '-c', self.repair_code(root)], **kwargs)
            with mock.patch.object(h.subprocess, 'Popen', side_effect=local_process), \
                 mock.patch.object(h, 'metadata', return_value=('p', 'wrong', 'litellm')), \
                 mock.patch.object(h, 'uid_still') as uid:
                with self.assertRaisesRegex(ValueError, 'REPAIR_REFUSED'):
                    h.remote_repair('p', 'litellm', 'u', 1)
                uid.assert_not_called()
            self.assertEqual(root.stat().st_mode & 0o7777, 0o2775)
            self.assertEqual((root / 'auth.json').stat().st_mode & 0o7777, 0o644)
            with mock.patch.object(h.subprocess, 'Popen', side_effect=local_process), \
                 mock.patch.object(h, 'metadata', return_value=('p', 'u', 'litellm')), \
                 mock.patch.object(h, 'uid_still'):
                self.assertEqual(h.remote_repair('p', 'litellm', 'u', 1), 'REPAIRED')
            self.assertEqual(root.stat().st_mode & 0o7777, 0o700)

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
        dep, pod, rs = metadata_fixture()
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
            self.assertEqual(h.metadata(), ("p", "u", "litellm"))
        other = {"metadata": {"uid": "other"}, "status": {"phase": "Running"},
                 "spec": {"volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "litellm-data"}}],
                          "containers": [{"volumeMounts": [{"name": "v", "mountPath": "/other"}]}]}}
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod, other]}, rs]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()
        other["status"]["phase"] = "Succeeded"
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod, other]}, rs]):
            self.assertEqual(h.metadata(), ("p", "u", "litellm"))
        for kind in ("initContainers", "ephemeralContainers"):
            pod["spec"][kind] = [{"name": "extra", "volumeMounts": [{"name": "auth", "mountPath": "/other"}]}]
            with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
                with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()
            pod["spec"][kind][0]["volumeMounts"][0]["readOnly"] = True
            with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
                self.assertEqual(h.metadata(), ("p", "u", "litellm"))
            del pod["spec"][kind]
        pod["status"]["containerStatuses"][0]["imageID"] = "wrong"
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"): h.metadata()

    def test_oversized_replicaset_namespace_uses_named_lookup(self):
        dep, pod, rs = metadata_fixture()
        historical = [{"metadata": {"name": f"old-{i}", "padding": "x" * 14000}}
                      for i in range(88)]
        self.assertGreater(len(json.dumps({"items": historical})), 1024 * 1024)
        seen = []
        def selected(args, timeout):
            seen.append((args, timeout))
            if args[-4:] == ["get", "replicaset", "-o", "json"]:
                raise ValueError("oversized namespace list")
            if "replicaset" in args:
                self.assertEqual(args[-5:], ["get", "replicaset", "litellm-abc", "-o", "json"])
                return rs
            return dep if "deployment" in args else {"items": [pod]}
        with mock.patch.object(h, "call", side_effect=selected):
            self.assertEqual(h.metadata(300), ("p", "u", "litellm"))
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(0 < timeout <= 300 for _, timeout in seen))

    def test_metadata_approval_budget_and_reaping(self):
        dep, pod, rs = metadata_fixture()
        real_popen = subprocess.Popen
        children = []
        def delayed(args, **kwargs):
            payload = dep if "deployment" in args else ({"items": [pod]} if "pods" in args else rs)
            code = "import sys,time;time.sleep(0.12);sys.stdout.write(" + repr(json.dumps(payload)) + ")"
            child = real_popen([sys.executable, "-c", code], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(h.subprocess, "Popen", side_effect=delayed):
            self.assertEqual(h.metadata(1), ("p", "u", "litellm"))
        self.assertTrue(all(c.returncode == 0 for c in children))
        self.assertEqual(len(children), 3)
        def stalled(args, **kwargs):
            child = real_popen([sys.executable, "-c", "import time;time.sleep(5)"], **kwargs)
            children.append(child)
            return child
        with mock.patch.object(h.subprocess, "Popen", side_effect=stalled):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"):
                h.metadata(0.1)
        self.assertIsNotNone(children[-1].returncode)

    def test_replicaset_identity_and_owner_must_match(self):
        for field, value in (("name", "wrong"), ("namespace", "other"), ("uid", "wrong")):
            with self.subTest(field=field):
                dep, pod, rs = metadata_fixture()
                rs["metadata"][field] = value
                with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
                    with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"):
                        h.metadata()
        dep, pod, rs = metadata_fixture()
        rs["metadata"]["ownerReferences"][0]["uid"] = "wrong"
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod]}, rs]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"):
                h.metadata()
        dep, pod, rs = metadata_fixture()
        impostor = {"metadata": {"uid": "other", "ownerReferences": [
            {"name": "litellm-abc", "uid": "wrong", "kind": "ReplicaSet", "controller": True}]},
            "status": {"phase": "Running"}, "spec": {"volumes": [], "containers": []}}
        with mock.patch.object(h, "call", side_effect=[dep, {"items": [pod, impostor]}, rs]):
            with self.assertRaisesRegex(ValueError, "PREFLIGHT_FAILED"):
                h.metadata()

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
            self.assertEqual([call.args for call in md.call_args_list], [(30,), (30,)])
            uid.assert_called_once_with("p", "u", 30)
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

    def test_manifest_switch_only(self):
        path = SCRIPT.parents[2] / "kubernetes/apps/ai/litellm/app/helmrelease.yaml"
        data = path.read_text()
        self.assertIn("CHATGPT_TOKEN_DIR: " + h.ROOT + "/" + h.GEN, data)
        self.assertIn("XAI_OAUTH_TOKEN_DIR: /var/lib/litellm/chatgpt/xai", data)
        self.assertIn("type: Recreate", data)
        self.assertIn("claimName: litellm-data", data)
        self.assertIn("replicaCount: 1", data)


def record_from_file(path):
    return path.read_bytes()


if __name__ == "__main__":
    unittest.main()
