"""The real scripts/openbao-configure.sh against the dev server, with kubectl and op replaced by
shims."""
import http.server
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest

import lab

KUBECTL = '''#!/bin/sh
# Stands in for: kubectl -n openbao exec -i openbao-0 -c openbao -- sh -c ... bao <args>
printf '%s\\n' "$*" >> "$KUBECTL_LOG"
case "$KUBECTL_FAIL" in
  always) echo 'cluster unavailable' >&2; exit 1 ;;
  after-preflight)
    if [ -f "$KUBECTL_PREFLIGHT_DONE" ]; then
      echo 'cluster unavailable' >&2
      exit 1
    fi
    touch "$KUBECTL_PREFLIGHT_DONE" ;;
esac
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
'''
OP = '''#!/bin/sh
printf '%s\\n' "$*" >> "$OP_LOG"
case "$1" in
  whoami) echo '{"user_type":"MEMBER"}' ;;
  read) printf %s "$LAB_ROOT_TOKEN" ;;
  *) exit 1 ;;
esac
'''
GIT = '''#!/bin/sh
[ "$1" != -C ] || shift 2
printf '%s\\n' "$*" >> "$GIT_LOG"
case "$1" in
  fetch)
    case " $* " in
      *' +refs/heads/main:refs/remotes/origin/main '*) ;;
      *) exit 2 ;;
    esac
    [ "$GIT_FETCH_FAIL" != 1 ] || exit 1
    touch "$GIT_FETCH_DONE" ;;
  rev-parse)
    [ "$GIT_MAIN_MISSING" != 1 ] || exit 1
    printf '%s\\n' '0123456789012345678901234567890123456789' ;;
  merge-base)
    [ -f "$GIT_FETCH_DONE" ] || exit 1
    [ "$GIT_BEHIND" != 1 ] || exit 1 ;;
  *) exit 2 ;;
esac
'''

RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")
if not os.access(RUNTIME_DIR, os.W_OK):
    RUNTIME_DIR = "/dev/shm"


class FakeAuthentik:
    """Discovery and keys for authentik's per-provider issuers, on loopback."""

    def __init__(self, keys):
        numbers = keys.key.public_key().public_numbers()
        jwks = {"keys": [{"kty": "RSA", "kid": "lab", "alg": "RS256", "use": "sig",
                          "n": lab.b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                          "e": lab.b64url(numbers.e.to_bytes(3, "big"))}]}
        base = None

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/jwks":
                    body = jwks
                elif self.path.endswith("/.well-known/openid-configuration"):
                    issuer = base + self.path[: -len(".well-known/openid-configuration")]
                    body = {"issuer": issuer, "jwks_uri": base + "/jwks", "authorization_endpoint": base + "/authorize",
                            "token_endpoint": base + "/token", "id_token_signing_alg_values_supported": ["RS256"],
                            "response_types_supported": ["code"], "subject_types_supported": ["public"]}
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FullRun(unittest.TestCase):
    """A whole run on a server with nothing set up, as the cluster was before agent PAM, then a
    second run. authentik is a loopback stand-in. The run stops at Kubernetes auth, which needs the
    pod's service account, so everything before it is checked."""

    def setUp(self):
        self.bao = lab.Bao()
        self.addCleanup(self.bao.close)
        self.keys = lab.Keys()
        self.idp = FakeAuthentik(self.keys)
        self.addCleanup(self.idp.close)
        self.tree = tempfile.mkdtemp(prefix="pam-full-run-")
        self.addCleanup(shutil.rmtree, self.tree)
        shims = pathlib.Path(self.tree, "shims")
        shims.mkdir()
        for name, text in (("kubectl", KUBECTL), ("op", OP), ("git", GIT)):
            (shims / name).write_text(text)
            (shims / name).chmod(stat.S_IRWXU)
        os.symlink(os.environ["BAO_BIN"], shims / "bao")
        shutil.copytree(lab.CONFIG, pathlib.Path(self.tree, "kubernetes/apps/openbao/openbao/config"))
        shutil.copytree(lab.ROOT / "scripts/pam", pathlib.Path(self.tree, "scripts/pam"), ignore=shutil.ignore_patterns("tests", "__pycache__"))
        script = (lab.ROOT / "scripts/openbao-configure.sh").read_text()
        self.assertIn("AUTHENTIK=https://authentik.homelab0.org\n", script)
        self.script = pathlib.Path(self.tree, "scripts/openbao-configure.sh")
        self.script.write_text(script.replace("AUTHENTIK=https://authentik.homelab0.org\n", f"AUTHENTIK={self.idp.base}\n", 1))
        self.script.chmod(stat.S_IRWXU)
        self.env = {"PATH": f"{shims}:{os.environ['PATH']}", "BAO_ADDR": self.bao.addr, "LAB_ROOT_TOKEN": self.bao.root,
                    "HOME": self.tree, "XDG_RUNTIME_DIR": RUNTIME_DIR,
                    "GIT_LOG": f"{self.tree}/git.log", "GIT_FETCH_DONE": f"{self.tree}/git-fetched",
                    "GIT_FETCH_FAIL": "0", "GIT_MAIN_MISSING": "0", "GIT_BEHIND": "0",
                    "KUBECTL_LOG": f"{self.tree}/kubectl.log", "KUBECTL_FAIL": "none",
                    "KUBECTL_PREFLIGHT_DONE": f"{self.tree}/kubectl-preflight", "OP_LOG": f"{self.tree}/op.log"}

    def run_configure(self):
        result = subprocess.run([str(self.script)], env=self.env, capture_output=True, text=True, timeout=120)
        steps = [line for line in result.stdout.splitlines() if line.startswith("==> ")]
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(steps[-1], "==> Configuring Kubernetes auth", result.stdout + result.stderr)
        self.assertIn("serviceaccount/ca.crt", result.stderr)
        self.assertIn("==> The agent PAM guard passed", steps)
        return result

    def agent_login(self, name="agent-claude", **claims):
        token = self.keys.sign({**{
            "iss": f"{self.idp.base}/application/o/warpgate-agents/", "sub": "hashed", "aud": "warpgate-agents",
            "iat": int(time.time()), "exp": int(time.time()) + 300, "preferred_username": name}, **claims})
        return self.bao.api("POST", "auth/jwt/login", token=None, body={"role": "agent-claude", "jwt": token})

    def test_a_fresh_server_ends_up_as_designed(self):
        self.run_configure()
        bao = self.bao
        role = bao.ok("GET", "auth/pam-oidc/role/pam-approver")["data"]
        shipped = json.loads(lab.ROLE_FILE.read_text())
        self.assertEqual(role["allowed_redirect_uris"], shipped["allowed_redirect_uris"])
        self.assertEqual(role["token_policies"], ["pam-approver"])
        self.assertTrue(role["token_no_default_policy"])
        self.assertEqual(role["bound_claims"], {"groups": ["openbao-admins"]})
        group = bao.ok("GET", "identity/group/name/pam-approvers")["data"]
        mounts = bao.ok("GET", "sys/auth")["data"]
        self.assertEqual((group["type"], group["policies"]), ("external", ["pam-approver"]))
        self.assertEqual((group["alias"]["name"], group["alias"]["mount_accessor"]), ("openbao-admins", mounts["pam-oidc/"]["accessor"]))
        self.assertEqual(bao.ok("GET", "auth/pam-oidc/config")["data"]["default_role"], "pam-approver")
        self.assertEqual(bao.ok("GET", "auth/oidc/config")["data"]["default_role"], "default")
        tune = bao.ok("GET", "sys/mounts/kv/tune")["data"]
        self.assertEqual(sorted(tune["audit_non_hmac_request_keys"]), ["delivery", "reason"])
        quota = bao.ok("GET", "sys/quotas/rate-limit/pam-requests")["data"]
        self.assertEqual((quota["path"], quota["rate"]), ("kv/data/pam/*", 60))
        entity = bao.ok("GET", "identity/entity/name/agent-claude")["data"]
        self.assertEqual([a["mount_accessor"] for a in entity["aliases"]], [mounts["jwt/"]["accessor"]])
        self.assertEqual(bao.api("GET", "identity/entity/name/agent-codex")[0], 404)
        self.assertEqual(bao.ok("GET", "kv/metadata/pam/live/poc-canary")["data"]["current_version"], 1)

        # The agent logs in as the entity configure made, and the canary waits for approval.
        status, reply = self.agent_login()
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["auth"]["entity_id"], entity["id"])
        self.assertEqual(reply["auth"]["policies"], ["pam-agent"])
        status, held = bao.api("GET", "kv/data/pam/live/poc-canary", token=reply["auth"]["client_token"])
        self.assertEqual(status, 200, held)
        self.assertIsNone(held.get("data"))
        self.assertIn("token", held["wrap_info"])

    def test_another_username_cant_use_the_role(self):
        self.run_configure()
        self.assertEqual(self.agent_login(name="agent-codex")[0], 400)
        self.assertEqual(self.agent_login(aud="something-else")[0], 400)

    def test_a_run_that_stopped_before_the_alias_is_finished_by_the_next(self):
        self.bao.ok("POST", "identity/group/name/pam-approvers", {"type": "external", "policies": ["pam-approver"]})
        self.bao.ok("POST", "sys/auth/pam-oidc", {"type": "oidc"})
        self.run_configure()
        group = self.bao.ok("GET", "identity/group/name/pam-approvers")["data"]
        accessor = self.bao.ok("GET", "sys/auth")["data"]["pam-oidc/"]["accessor"]
        self.assertEqual((group["alias"]["name"], group["alias"]["mount_accessor"]), ("openbao-admins", accessor))
        self.assertEqual(self.bao.api("GET", "auth/pam-oidc/role/pam-approver")[0], 200)

    def test_a_second_run_changes_nothing(self):
        self.run_configure()
        before = self.bao.ok("GET", "identity/entity/name/agent-claude")["data"]
        group = self.bao.ok("GET", "identity/group/name/pam-approvers")["data"]
        self.run_configure()
        after = self.bao.ok("GET", "identity/entity/name/agent-claude")["data"]
        self.assertEqual((after["id"], len(after["aliases"])), (before["id"], 1))
        self.assertEqual(self.bao.ok("GET", "identity/group/name/pam-approvers")["data"]["alias"]["id"], group["alias"]["id"])
        self.assertEqual(self.bao.ok("GET", "kv/metadata/pam/live/poc-canary")["data"]["current_version"], 1)


class NoLivePreflight(unittest.TestCase):
    """Early failure paths use only executable shims, with no server or cluster socket."""

    def setUp(self):
        self.tree = tempfile.mkdtemp(prefix="pam-preflight-")
        self.addCleanup(shutil.rmtree, self.tree)
        shims = pathlib.Path(self.tree, "shims")
        shims.mkdir()
        for name, source in (("kubectl", KUBECTL), ("op", OP), ("git", GIT)):
            (shims / name).write_text(source)
            (shims / name).chmod(stat.S_IRWXU)
        (shims / "bao").write_text('''#!/bin/sh
echo 'Error looking up token: Error making API request.' >&2
echo "URL: GET https://example.invalid/$LAB_ROOT_TOKEN" >&2
echo 'Code: 403. Errors:' >&2
echo "* permission denied for $LAB_ROOT_TOKEN" >&2
echo '{"secret":"response-data"}'
exit 2
''')
        (shims / "bao").chmod(stat.S_IRWXU)
        shutil.copytree(lab.CONFIG, pathlib.Path(self.tree, "kubernetes/apps/openbao/openbao/config"))
        shutil.copytree(lab.ROOT / "scripts/pam", pathlib.Path(self.tree, "scripts/pam"),
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copy(lab.ROOT / "scripts/openbao-configure.sh", pathlib.Path(self.tree, "scripts"))
        self.env = {"PATH": f"{shims}:{os.environ['PATH']}", "LAB_ROOT_TOKEN": "test-rejected-token-value",
                    "HOME": self.tree, "XDG_RUNTIME_DIR": RUNTIME_DIR,
                    "GIT_LOG": f"{self.tree}/git.log", "GIT_FETCH_DONE": f"{self.tree}/git-fetched",
                    "GIT_FETCH_FAIL": "0", "GIT_MAIN_MISSING": "0", "GIT_BEHIND": "0",
                    "KUBECTL_LOG": f"{self.tree}/kubectl.log", "KUBECTL_FAIL": "none",
                    "KUBECTL_PREFLIGHT_DONE": f"{self.tree}/kubectl-preflight", "OP_LOG": f"{self.tree}/op.log"}

    def configure(self):
        return subprocess.run([f"{self.tree}/scripts/openbao-configure.sh", "--check"], env=self.env,
                              capture_output=True, text=True, timeout=45)

    def calls(self, name):
        path = pathlib.Path(self.tree, f"{name}.log")
        return path.read_text().splitlines() if path.exists() else []

    def test_fetch_failure_stops_before_credentials(self):
        self.env["GIT_FETCH_FAIL"] = "1"
        result = self.configure()
        self.assertIn("origin/main", result.stderr)
        self.assertEqual(self.calls("op"), [])
        self.assertEqual(self.calls("kubectl"), [])

    def test_missing_remote_main_stops_before_credentials(self):
        self.env["GIT_MAIN_MISSING"] = "1"
        result = self.configure()
        self.assertIn("origin/main", result.stderr)
        self.assertTrue(any(call.startswith("fetch ") for call in self.calls("git")))
        self.assertEqual(self.calls("op"), [])

    def test_fetched_main_must_be_an_ancestor(self):
        self.env["GIT_BEHIND"] = "1"
        result = self.configure()
        self.assertIn("origin/main", result.stderr)
        self.assertTrue(any(call.startswith("fetch ") for call in self.calls("git")))
        self.assertTrue(any(call.startswith("merge-base ") for call in self.calls("git")))
        self.assertEqual(self.calls("op"), [])

    def test_unreachable_pod_stops_before_credentials(self):
        self.env["KUBECTL_FAIL"] = "always"
        result = self.configure()
        self.assertIn("can't reach the cluster", result.stderr)
        self.assertEqual(self.calls("op"), [])
        self.assertTrue(any("--request-timeout=" in call for call in self.calls("kubectl")))

    def test_reach_lost_after_preflight_is_reported_as_reach_failure(self):
        self.env["KUBECTL_FAIL"] = "after-preflight"
        result = self.configure()
        self.assertIn("can't reach the cluster", result.stderr)
        self.assertNotIn("token was rejected", result.stderr)
        self.assertTrue(any(call.startswith("read ") for call in self.calls("op")))

    def test_openbao_error_is_preserved_without_token_or_response_body(self):
        result = self.configure()
        self.assertIn("permission denied", result.stderr)
        self.assertNotIn(self.env["LAB_ROOT_TOKEN"], result.stdout + result.stderr)
        self.assertNotIn("response-data", result.stdout + result.stderr)
        self.assertNotIn("example.invalid", result.stdout + result.stderr)
        self.assertNotIn("Error looking up token", result.stderr)


class Configure(unittest.TestCase):
    """The first agent identity, then a plain grant on it. A separate dev server per test keeps
    each one's live state to itself."""

    def setUp(self):
        self.lab = lab.Lab()
        self.addCleanup(self.lab.close)
        self.tree = tempfile.mkdtemp(prefix="pam-configure-")
        self.addCleanup(shutil.rmtree, self.tree)
        shims = pathlib.Path(self.tree, "shims")
        shims.mkdir()
        for name, text in (("kubectl", KUBECTL), ("op", OP), ("git", GIT)):
            (shims / name).write_text(text)
            (shims / name).chmod(stat.S_IRWXU)
        os.symlink(os.environ["BAO_BIN"], shims / "bao")
        self.config = pathlib.Path(self.tree, "kubernetes/apps/openbao/openbao/config")
        shutil.copytree(lab.CONFIG, self.config)
        shutil.copytree(lab.ROOT / "scripts/pam", pathlib.Path(self.tree, "scripts/pam"), ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copy(lab.ROOT / "scripts/openbao-configure.sh", pathlib.Path(self.tree, "scripts"))
        self.env = {"PATH": f"{shims}:{os.environ['PATH']}", "BAO_ADDR": self.lab.addr, "LAB_ROOT_TOKEN": self.lab.root,
                    "HOME": self.tree, "XDG_RUNTIME_DIR": RUNTIME_DIR,
                    "GIT_LOG": f"{self.tree}/git.log", "GIT_FETCH_DONE": f"{self.tree}/git-fetched",
                    "GIT_FETCH_FAIL": "0", "GIT_MAIN_MISSING": "0", "GIT_BEHIND": "0",
                    "KUBECTL_LOG": f"{self.tree}/kubectl.log", "KUBECTL_FAIL": "none",
                    "KUBECTL_PREFLIGHT_DONE": f"{self.tree}/kubectl-preflight", "OP_LOG": f"{self.tree}/op.log"}

    def configure(self, *args):
        return subprocess.run([os.path.join(self.tree, "scripts/openbao-configure.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def policy_written(self, name):
        return self.lab.api("GET", f"sys/policies/acl/{name}")[0] == 200

    def calls(self, name):
        path = pathlib.Path(self.tree, f"{name}.log")
        return path.read_text().splitlines() if path.exists() else []

    def test_refuses_when_fetch_fails_before_token_input(self):
        self.env["GIT_FETCH_FAIL"] = "1"
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("origin/main", result.stderr)
        self.assertEqual(self.calls("op"), [])
        self.assertEqual(self.calls("kubectl"), [])
        self.assertFalse(self.policy_written("openbao-reader"))

    def test_refuses_when_remote_main_is_missing(self):
        self.env["GIT_MAIN_MISSING"] = "1"
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("origin/main", result.stderr)
        self.assertTrue(any(call.startswith("fetch ") for call in self.calls("git")))
        self.assertEqual(self.calls("op"), [])

    def test_refuses_a_stale_checkout_using_fetched_main(self):
        self.env["GIT_BEHIND"] = "1"
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("origin/main", result.stderr)
        self.assertTrue(any(call.startswith("fetch ") for call in self.calls("git")))
        self.assertTrue(any(call.startswith("merge-base ") for call in self.calls("git")))
        self.assertEqual(self.calls("op"), [])

    def test_current_feature_branch_passes_fresh_ancestry_check(self):
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(call.startswith("fetch ") for call in self.calls("git")))
        self.assertTrue(any(call.startswith("merge-base ") for call in self.calls("git")))

    def test_unreachable_pod_fails_before_1password_or_token(self):
        self.env["KUBECTL_FAIL"] = "always"
        result = self.configure("--token-stdin", "--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("can't reach the cluster", result.stderr)
        self.assertNotIn("token was rejected", result.stderr)
        self.assertEqual(self.calls("op"), [])
        self.assertTrue(any("--request-timeout=" in call and "exec" in call for call in self.calls("kubectl")))

    def test_reach_lost_after_preflight_is_not_called_a_token_rejection(self):
        self.env["KUBECTL_FAIL"] = "after-preflight"
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("can't reach the cluster", result.stderr)
        self.assertNotIn("token was rejected", result.stderr)
        self.assertTrue(any(call.startswith("read ") for call in self.calls("op")))

    def test_rejected_token_shows_openbao_error_without_echoing_token(self):
        bad = "test-rejected-token-value"
        self.env["LAB_ROOT_TOKEN"] = bad
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("permission denied", result.stderr)
        self.assertNotIn(bad, result.stdout + result.stderr)
        self.assertNotIn("can't reach the cluster", result.stderr)
        self.assertNotIn("token was rejected by OpenBao", result.stderr)

    def test_check_passes_on_a_clean_server_and_writes_nothing(self):
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard passed", result.stdout)
        self.assertIn("repo/openbao-reader", result.stdout)
        self.assertFalse(self.policy_written("openbao-reader"))

    def test_check_passes_with_no_agent_identities_or_jwt_mount(self):
        bare = lab.Bao()
        self.addCleanup(bare.close)
        self.env.update(BAO_ADDR=bare.addr, LAB_ROOT_TOKEN=bare.root)
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(bare.api("GET", "sys/policies/acl/openbao-reader")[0], 404)

    def test_token_from_stdin_also_works(self):
        result = subprocess.run([os.path.join(self.tree, "scripts/openbao-configure.sh"), "--token-stdin", "--check"],
                                env=self.env, input=self.lab.root + "\n", capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_a_plain_grant_on_a_live_identity_before_writing(self):
        self.lab.policy("a-plain", 'path "kv/data/pam/live/poc-canary" {\n  capabilities = ["read"]\n}\n')
        self.lab.ok("POST", "identity/entity/name/agent-claude", {"policies": ["a-plain"]})
        for args in (("--check",), ()):
            result = self.configure(*args)
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("refused", result.stderr)
            self.assertFalse(self.policy_written("openbao-reader"))

    def pam_oidc(self):
        self.lab.ok("POST", "sys/auth/pam-oidc", {"type": "oidc"})
        self.lab.ok("POST", "auth/pam-oidc/role/pam-approver", json.loads(lab.ROLE_FILE.read_text()))
        group = self.lab.ok("GET", f"identity/group/name/{lab.GROUP}")["data"]
        self.lab.ok("DELETE", f"identity/group-alias/id/{group['alias']['id']}")
        accessor = self.lab.ok("GET", "sys/auth")["data"]["pam-oidc/"]["accessor"]
        self.lab.ok("POST", "identity/group-alias", {"name": lab.CLAIM, "mount_accessor": accessor, "canonical_id": group["id"]})
        return accessor

    def assertRefused(self, phrase):
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(phrase, result.stderr)

    def test_the_approver_mount_as_designed_passes(self):
        self.pam_oidc()
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_another_group_on_the_approver_mount(self):
        accessor = self.pam_oidc()
        gid = self.lab.ok("POST", "identity/group", {"name": "extra", "type": "external"})["data"]["id"]
        self.lab.ok("POST", "identity/group-alias", {"name": "extra", "mount_accessor": accessor, "canonical_id": gid})
        self.assertRefused("extra is aliased on pam-oidc")

    def test_refuses_an_approver_group_with_more_policies(self):
        self.pam_oidc()
        self.lab.ok("POST", f"identity/group/name/{lab.GROUP}", {"policies": ["pam-approver", "pam-sync"]})
        self.assertRefused("hold only")

    def test_refuses_an_internal_approver_group(self):
        self.pam_oidc()
        self.lab.ok("DELETE", f"identity/group/name/{lab.GROUP}")
        self.lab.ok("POST", "identity/group", {"name": lab.GROUP, "type": "internal", "policies": ["pam-approver"]})
        self.assertRefused("must be external")

    def test_refuses_an_approver_role_with_no_alias(self):
        self.pam_oidc()
        group = self.lab.ok("GET", f"identity/group/name/{lab.GROUP}")["data"]
        self.lab.ok("DELETE", f"identity/group-alias/id/{group['alias']['id']}")
        self.assertRefused("isn't aliased on pam-oidc")

    def test_refuses_an_approver_role_with_no_group(self):
        self.pam_oidc()
        self.lab.ok("DELETE", f"identity/group/name/{lab.GROUP}")
        self.assertRefused(f"group {lab.GROUP} doesn't exist")

    def test_an_approver_mount_with_no_role_passes(self):
        self.pam_oidc()
        self.lab.ok("DELETE", "auth/pam-oidc/role/pam-approver")
        group = self.lab.ok("GET", f"identity/group/name/{lab.GROUP}")["data"]
        self.lab.ok("DELETE", f"identity/group-alias/id/{group['alias']['id']}")
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_an_approver_role_with_other_policies(self):
        self.pam_oidc()
        role = json.loads(lab.ROLE_FILE.read_text())
        self.lab.ok("POST", "auth/pam-oidc/role/pam-approver", {**role, "token_policies": ["pam-approver", "pam-sync"]})
        self.assertRefused("must use only")

    def test_refuses_an_approver_role_with_the_default_policy(self):
        self.pam_oidc()
        role = json.loads(lab.ROLE_FILE.read_text())
        self.lab.ok("POST", "auth/pam-oidc/role/second", {**role, "token_no_default_policy": False})
        self.assertRefused("must set token_no_default_policy")

    def approver_entity(self, accessor):
        eid = self.lab.ok("POST", "identity/entity", {"name": "owner"})["data"]["id"]
        self.lab.ok("POST", "identity/entity-alias", {"name": "owner-sub", "canonical_id": eid, "mount_accessor": accessor})
        return eid

    def test_refuses_an_approver_entity_in_an_internal_group(self):
        eid = self.approver_entity(self.pam_oidc())
        self.lab.ok("POST", "identity/group", {"name": "more", "type": "internal", "policies": ["pam-sync"], "member_entity_ids": [eid]})
        self.assertRefused("is in another group")

    def test_refuses_an_approver_entity_merged_with_another_mount(self):
        eid = self.approver_entity(self.pam_oidc())
        people = self.lab.ok("GET", "sys/auth")["data"]["people/"]["accessor"]
        self.lab.ok("POST", "identity/entity-alias", {"name": "owner-sub", "canonical_id": eid, "mount_accessor": people})
        self.assertRefused("merged with another mount")

    def test_refuses_a_policy_on_an_approver_entity(self):
        accessor = self.pam_oidc()
        eid = self.lab.ok("POST", "identity/entity", {"name": "owner", "policies": ["pam-sync"]})["data"]["id"]
        self.lab.ok("POST", "identity/entity-alias", {"name": "owner-sub", "canonical_id": eid, "mount_accessor": accessor})
        self.assertRefused("holds policies directly")

    def test_an_entity_called_root_cant_hide(self):
        eid = self.lab.ok("POST", "identity/entity", {"name": "root", "policies": ["pam-sync"]})["data"]["id"]
        self.lab.ok("POST", "identity/entity-alias", {"name": "root", "canonical_id": eid, "mount_accessor": self.lab.jwt_accessor})
        self.assertRefused("identity root holds pam-sync")

    def test_a_group_called_root_cant_hide(self):
        gid = self.lab.ok("POST", "identity/group", {"name": "root", "type": "external"})["data"]["id"]
        self.lab.ok("POST", "identity/group-alias", {"name": "root", "mount_accessor": self.lab.jwt_accessor, "canonical_id": gid})
        self.assertRefused("group root is aliased on the jwt mount")

    def test_a_role_called_root_cant_hide(self):
        self.lab.ok("POST", "auth/jwt/role/root", {
            "role_type": "jwt", "user_claim": "preferred_username", "bound_audiences": [lab.AUDIENCE],
            "token_policies": ["pam-agent"], "token_no_default_policy": True, "groups_claim": "groups"})
        self.assertRefused("role root sets groups_claim")

    def test_a_policy_called_root_stays_out_of_the_list(self):
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_a_wider_live_policy(self):
        self.lab.policy("pam-agent", lab.AGENT_POLICY + 'path "kv/data/pam/live/x" {\n  capabilities = ["read"]\n}\n')
        self.assertRefused("live pam-agent: plain grant")

    def test_refuses_a_legacy_grant_in_a_repo_policy(self):
        (self.config / "pam-test.hcl").write_text('path "kv/data/pam/live/x" {\n  policy = "read"\n}\n')
        self.assertRefused("plain grant")

    def test_refuses_a_direct_reader_policy_on_an_agent(self):
        self.lab.ok("POST", "identity/entity/name/agent-claude", {"policies": ["pam-sync"]})
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("agent-claude holds pam-sync", result.stderr)

    def test_refuses_an_external_group_on_the_jwt_mount(self):
        gid = self.lab.ok("POST", "identity/group", {"name": "sneaky", "type": "external", "policies": ["pam-sync"]})["data"]["id"]
        self.lab.ok("POST", "identity/group-alias", {"name": "sneaky", "mount_accessor": self.lab.jwt_accessor, "canonical_id": gid})
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("aliased on the jwt mount", result.stderr)

    def test_refuses_a_groups_claim_on_an_agent_role(self):
        self.lab.ok("POST", "auth/jwt/role/agent-claude", {
            "role_type": "jwt", "user_claim": "preferred_username", "bound_audiences": [lab.AUDIENCE],
            "token_policies": ["pam-agent"], "token_no_default_policy": True, "groups_claim": "groups"})
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("groups_claim", result.stderr)

    def test_the_guard_ignores_modules_planted_beside_it(self):
        pathlib.Path(self.tree, "scripts/pam/argparse.py").write_text("import sys\nsys.exit(0)\n")
        (self.config / "pam-test.hcl").write_text('path "kv/data/pam/live/x" {\n  capabilities = ["read"]\n}\n')
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("plain grant", result.stderr)

    def test_refuses_a_plain_grant_in_a_repo_policy(self):
        (self.config / "pam-test.hcl").write_text('path "kv/data/pam/live/poc-canary" {\n  capabilities = ["read"]\n}\n')
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("plain grant", result.stderr)

    def test_refuses_a_control_group_with_no_ttl(self):
        path = self.config / "pam-agent.hcl"
        path.write_text(path.read_text().replace('ttl = "15m"', ""))
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no ttl", result.stderr)

    def test_shows_the_diff_against_live(self):
        self.lab.policy("openbao-reader", 'path "sys/health" {\n  capabilities = ["read"]\n}\n')
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-path "sys/health"', result.stdout)

    def test_policy_diff_ignores_only_trailing_newlines(self):
        name = "openbao-reader"
        policy = (self.config / f"{name}.hcl").read_text()
        self.assertTrue(policy.endswith("\n"))
        # OpenBao returns the file's final newline; jq -r adds another one.
        # The preview must not show that serialization difference as a change.
        self.lab.policy(name, policy)
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(f"--- live/{name}", result.stdout)

        self.lab.policy(name, policy + "# changed policy content\n")
        result = self.configure("--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"--- live/{name}", result.stdout)
        self.assertIn("-# changed policy content", result.stdout)

    def test_a_listing_failure_stops_the_run(self):
        token = self.lab.ok("POST", "auth/token/create", {"policies": ["limited"], "ttl": "10m"})["auth"]["client_token"]
        self.lab.policy("limited", 'path "sys/policies/acl*" {\n  capabilities = ["read", "list"]\n}\n')
        self.env["LAB_ROOT_TOKEN"] = token
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("could not list", result.stderr)


if __name__ == "__main__":
    unittest.main()
