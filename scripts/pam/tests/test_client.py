"""Runs the real pam client against the dev server, with the test playing the approver."""
import os
import pathlib
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock

import lab

PAM = str(lab.ROOT / "scripts/pam/pam")
DENIED, TIMED_OUT, REFUSED, DOWN = 3, 4, 5, 6
CANARY = "S3cretCanaryValue"


class Client(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = lab.Lab()
        cls.addClassCleanup(cls.lab.close)
        cls.approver = cls.lab.approver()
        cls.dir = tempfile.TemporaryDirectory(prefix="pam-client-")
        cls.addClassCleanup(cls.dir.cleanup)
        jwt_file = pathlib.Path(cls.dir.name, "jwt")
        jwt_file.write_text(cls.lab.keys.jwt("agent-claude"))
        cls.env = {
            "PATH": os.environ["PATH"], "PAM_ADDR": cls.lab.addr, "PAM_HARNESS": "claude",
            "PAM_POLL": "0.1", "PAM_JWT_CMD": f"cat {jwt_file}",
        }
        cls.lab.seed("pam/live/demo", value=CANARY)
        cls.lab.seed("pam/live/multi", user="u", password=CANARY)

    def start(self, *args, **env):
        return subprocess.Popen([PAM, *args], env={**self.env, **env}, text=True, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_pam(self, *args, stdin=None, **env):
        return subprocess.run([PAM, *args], env={**self.env, **env}, input=stdin, text=True,
                              capture_output=True, timeout=30)

    def held(self):
        keys = self.lab.api("LIST", "auth/token/accessors/", token=self.approver)[1]["data"]["keys"]
        return [key for key in keys if self.lab.request(key, self.approver)[0] == 200]

    def decide(self, action):
        """Waits for the client's request to appear, then acts on it, the way the page would."""
        before = set(self.held())

        def worker():
            for _ in range(100):
                new = set(self.held()) - before
                if new:
                    action(new.pop())
                    return
                time.sleep(0.05)

        thread = threading.Thread(target=worker)
        thread.start()
        self.addCleanup(thread.join)

    def approve(self):
        self.decide(lambda accessor: self.lab.approve(accessor, self.approver))

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def test_get_to_a_file(self):
        self.approve()
        target = self.path("single")
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", target)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(pathlib.Path(target).read_text(), CANARY)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertNotIn(CANARY, result.stdout + result.stderr)

    def test_several_keys_become_json(self):
        self.approve()
        target = self.path("multi")
        self.assertEqual(self.run_pam("get", "multi", "--reason", "testing", "--to", target).returncode, 0)
        self.assertIn(CANARY, pathlib.Path(target).read_text())

    def test_get_exec_passes_values_only_in_the_childs_environment(self):
        self.approve()
        code = "import os,sys; sys.exit(0 if os.environ['PAM_SECRET_PASSWORD'] == os.environ['EXPECT'] else 9)"
        result = self.run_pam("get", "multi", "--reason", "testing", "--exec", "--", sys.executable, "-c", code, EXPECT=CANARY)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(CANARY, result.stdout + result.stderr)

    def child(self, code, **env):
        """Runs code in an approved --exec child and returns the result."""
        self.approve()
        return self.run_pam("get", "demo", "--reason", "testing", "--exec", "--", sys.executable, "-c", code, **env)

    def test_child_output_is_masked_on_stdout_and_stderr(self):
        result = self.child("import os,sys; v=os.environ['PAM_SECRET_VALUE']; print('a', v, 'b'); print(v, file=sys.stderr)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("a [pam: masked] b", result.stdout)
        self.assertIn("[pam: masked]", result.stderr)
        self.assertNotIn(CANARY, result.stdout + result.stderr)

    def test_a_value_split_across_writes_is_masked(self):
        result = self.child(
            "import os,sys,time\nv=os.environ['PAM_SECRET_VALUE']\n"
            "for c in v:\n sys.stdout.write(c); sys.stdout.flush(); time.sleep(0.01)\n")
        self.assertEqual(result.stdout, "[pam: masked]")

    def test_common_encodings_are_masked(self):
        code = ("import base64,json,os,urllib.parse as u\nv=os.environ['PAM_SECRET_VALUE']\n"
                "print(base64.b64encode(v.encode()).decode()); print(v.encode().hex()); print(u.quote(v, safe=''))")
        result = self.child(code)
        self.assertEqual(result.stdout.splitlines(), ["[pam: masked]"] * 3)

    def test_the_limit_a_transformed_value_gets_through(self):
        result = self.child("import os; print(os.environ['PAM_SECRET_VALUE'][::-1])")
        self.assertIn(CANARY[::-1], result.stdout)

    def test_a_partial_match_comes_through_at_the_end(self):
        result = self.child(f"print('{CANARY[:5]}', end='')")
        self.assertEqual(result.stdout, CANARY[:5])

    def test_a_child_that_cant_start_is_refused(self):
        self.approve()
        result = self.run_pam("get", "demo", "--reason", "testing", "--exec", "--", "/nonexistent/cmd")
        self.assertEqual(result.returncode, REFUSED)

    def test_the_approver_sees_the_command(self):
        seen = []

        def look(accessor):
            seen.append(self.lab.request(accessor, self.approver)[1]["data"]["request_data"])
            self.lab.approve(accessor, self.approver)

        self.decide(look)
        self.run_pam("get", "demo", "--reason", "testing", "--exec", "--", "kubectl", "get", "pods", "-n", "my ns")
        self.assertEqual(seen[0]["delivery"], "run kubectl get pods -n 'my ns'")

    def test_the_approver_sees_the_file(self):
        seen = []
        target = self.path("shown")

        def look(accessor):
            seen.append(self.lab.request(accessor, self.approver)[1]["data"]["request_data"])
            self.lab.approve(accessor, self.approver)

        self.decide(look)
        self.run_pam("get", "demo", "--reason", "testing", "--to", target)
        self.assertEqual(seen[0]["delivery"], f"write {target}")

    def test_a_stored_key_cant_set_the_childs_environment(self):
        self.lab.seed("pam/live/hostile", PATH="/evil", LD_PRELOAD="/evil.so")
        self.approve()
        code = "import os,sys; sys.exit(0 if '/evil' not in os.environ['PATH'] and 'LD_PRELOAD' not in os.environ else 9)"
        result = self.run_pam("get", "hostile", "--reason", "testing", "--exec", "--", sys.executable, "-c", code)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rate_limited_is_refused_with_a_clear_message(self):
        self.lab.ok("POST", "sys/quotas/rate-limit/tight", {"path": "kv/data/pam/*", "rate": 1, "interval": "1h"})
        self.addCleanup(self.lab.api, "DELETE", "sys/quotas/rate-limit/tight")
        self.lab.api("GET", "kv/data/pam/live/demo")
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", self.path("limited"))
        self.assertEqual(result.returncode, REFUSED)
        self.assertIn("too many requests", result.stderr)

    def test_child_exit_code_is_the_result(self):
        self.approve()
        result = self.run_pam("get", "demo", "--reason", "testing", "--exec", "--", sys.executable, "-c", "raise SystemExit(7)")
        self.assertEqual(result.returncode, 7)

    def test_the_window_is_closed_afterwards(self):
        accessors = []

        def approve(accessor):
            accessors.append(accessor)
            self.lab.approve(accessor, self.approver)

        self.decide(approve)
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", self.path("closed"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("could not revoke", result.stderr)
        self.assertEqual(self.lab.request(accessors[0], self.approver)[0], 400)

    def test_denied(self):
        self.decide(lambda accessor: self.lab.api("POST", "auth/token/revoke-accessor", token=self.approver,
                                                  body={"accessor": accessor}))
        target = self.path("denied")
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", target)
        self.assertEqual(result.returncode, DENIED, result.stderr)
        self.assertFalse(os.path.exists(target))
        self.assertNotIn("could not revoke", result.stderr)

    def test_timed_out(self):
        short = lab.AGENT_POLICY.replace('ttl = "15m"', 'ttl = "2s"')
        self.lab.policy("pam-agent", short)
        self.addCleanup(self.lab.policy, "pam-agent", lab.AGENT_POLICY)
        target = self.path("timeout")
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", target)
        self.assertEqual(result.returncode, TIMED_OUT, result.stderr)
        self.assertFalse(os.path.exists(target))

    def test_client_refuses_to_deliver_an_ungated_read(self):
        self.lab.policy("pam-agent", 'path "kv/data/pam/live/*" {\n  capabilities = ["read"]\n}\n')
        self.addCleanup(self.lab.policy, "pam-agent", lab.AGENT_POLICY)
        target = self.path("ungated")
        result = self.run_pam("get", "demo", "--reason", "testing", "--to", target)
        self.assertEqual(result.returncode, REFUSED)
        self.assertFalse(os.path.exists(target))

    def test_interrupted_get_withdraws_the_request(self):
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGHUP, 129), (signal.SIGQUIT, 131),
                          (signal.SIGINT, 130)):
            with self.subTest(signal=sig.name):
                before = set(self.held())
                process = self.start("get", "demo", "--reason", "testing", "--to", self.path("interrupted"))
                new = set()
                for _ in range(100):
                    new = set(self.held()) - before
                    if new:
                        break
                    time.sleep(0.05)
                self.assertTrue(new)
                process.send_signal(sig)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, code)
                self.assertFalse(set(self.held()) - before)

    def test_usage_errors_are_caught_before_asking(self):
        before = set(self.held())
        existing = self.path("existing")
        pathlib.Path(existing).touch()
        cases = [
            ("get", "demo", "--reason", "testing"),
            ("get", "demo", "--to", self.path("noreason")),
            ("get", "demo", "--reason", "x" * 201, "--to", self.path("long")),
            ("get", "demo", "--reason", "testing", "--to", existing),
            ("get", "demo", "--reason", "testing", "--exec"),
            ("get", "../demo", "--reason", "testing", "--to", self.path("traversal")),
            ("get", "demo", "--reason", "password=hunter2", "--to", self.path("shape1")),
            ("get", "demo", "--reason", "use ghp_abcdefghijklmnop", "--to", self.path("shape2")),
            ("get", "demo", "--reason", "a" * 40, "--to", self.path("shape3")),
            ("get", "demo", "--reason", "testing", "--exec", "--", "curl", "--header", "password=hunter2"),
            ("get", "demo", "--reason", "testing", "--exec", "--", "echo", "x " * 300),
        ]
        for case in cases:
            self.assertEqual(self.run_pam(*case).returncode, 2, case)
        self.assertEqual(set(self.held()), before)

    def test_put_stores_and_wont_overwrite(self):
        self.assertEqual(self.run_pam("put", "new-value", stdin=CANARY + "\n").returncode, 0)
        stored = self.lab.ok("GET", "kv/data/pam/inbox/agent-claude/new-value")["data"]["data"]
        self.assertEqual(stored, {"value": CANARY})
        result = self.run_pam("put", "new-value", stdin="other")
        self.assertEqual(result.returncode, REFUSED)
        self.assertNotIn("other", result.stdout + result.stderr)
        self.assertEqual(self.lab.ok("GET", "kv/data/pam/inbox/agent-claude/new-value")["data"]["metadata"]["version"], 1)

    def test_put_needs_a_value(self):
        self.assertEqual(self.run_pam("put", "empty", stdin="").returncode, 2)

    def test_get_reads_its_own_inbox_with_approval(self):
        self.lab.seed("pam/inbox/agent-claude/shared", value=CANARY)
        self.approve()
        target = self.path("inbox")
        self.assertEqual(self.run_pam("get", "inbox/agent-claude/shared", "--reason", "testing", "--to", target).returncode, 0)
        self.assertEqual(pathlib.Path(target).read_text(), CANARY)

    def test_another_agents_inbox_is_refused(self):
        self.lab.seed("pam/inbox/agent-codex/shared", value=CANARY)
        result = self.run_pam("get", "inbox/agent-codex/shared", "--reason", "testing", "--to", self.path("other"))
        self.assertEqual(result.returncode, REFUSED)

    def test_a_missing_secret_is_refused_after_approval(self):
        self.approve()
        result = self.run_pam("get", "does-not-exist", "--reason", "testing", "--to", self.path("missing"))
        self.assertEqual(result.returncode, REFUSED, result.stderr)

    def test_status(self):
        result = self.run_pam("status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("identity agent-claude", result.stdout)
        self.assertIn("demo", result.stdout)
        self.assertNotIn(CANARY, result.stdout)

    def test_unknown_harness_is_a_usage_error(self):
        self.assertEqual(self.run_pam("status", PAM_HARNESS="").returncode, 2)

    def test_bad_token_is_refused(self):
        self.assertEqual(self.run_pam("status", PAM_JWT_CMD="echo not-a-jwt").returncode, REFUSED)

    def test_execcredential_output_is_accepted(self):
        token = self.lab.keys.jwt("agent-claude")
        doc = pathlib.Path(self.path("credential"))
        doc.write_text('{"status": {"token": "%s"}}' % token)
        self.assertEqual(self.run_pam("status", PAM_JWT_CMD=f"cat {doc}").returncode, 0)

    def test_openbao_down_fails_closed(self):
        result = self.run_pam("status", PAM_ADDR=f"http://127.0.0.1:{lab.free_port()}")
        self.assertEqual(result.returncode, DOWN)



def load_client():
    import importlib.machinery
    import importlib.util
    path = str(pathlib.Path(__file__).resolve().parent.parent / "pam")
    spec = importlib.util.spec_from_loader("pam_client", importlib.machinery.SourceFileLoader("pam_client", path))
    pam = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pam)
    return pam


class Revoke(unittest.TestCase):
    def setUp(self):
        self.pam = load_client()
        patcher = unittest.mock.patch.object(self.pam.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def outcome(self, *replies, gone=None):
        calls = []

        def api(*_):
            calls.append(1)
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply, {}

        self.pam.api = api
        with unittest.mock.patch("sys.stderr") as err:
            ok = self.pam.revoke("t", "approval window", gone)
        return ok, len(calls), err.write.called

    def test_success_counts_as_closed(self):
        self.assertEqual(self.outcome(204), (True, 1, False))

    def test_a_403_counts_only_when_confirmed(self):
        self.assertEqual(self.outcome(403, gone=lambda: True), (True, 1, False))
        self.assertEqual(self.outcome(403, gone=lambda: False), (False, 1, True))
        self.assertEqual(self.outcome(403), (False, 1, True))

    def test_transient_errors_are_retried(self):
        down = self.pam.Stop(self.pam.DOWN, "down")
        self.assertEqual(self.outcome(down, down, 204), (True, 3, False))

    def test_a_failure_is_reported_not_hidden(self):
        down = self.pam.Stop(self.pam.DOWN, "down")
        self.assertEqual(self.outcome(down, down, down), (False, 3, True))
        self.assertEqual(self.outcome(400), (False, 1, True))


class EnvNames(unittest.TestCase):
    def test_keys_that_map_to_the_same_name_are_refused(self):
        pam = load_client()
        with self.assertRaises(pam.Stop):
            pam.env_for({"api-key": "a", "api_key": "b"})
        self.assertEqual(pam.env_for({"api-key": "a"}), {pam.ENV_PREFIX + "API_KEY": "a"})


if __name__ == "__main__":
    unittest.main()

