"""A throwaway OpenBao dev server for the agent PAM tests: in memory, on a free
loopback port, removed when the process exits. BAO_BIN (set by run.sh) names the
binary.
"""
import atexit
import base64
import json
import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONFIG = ROOT / "kubernetes/apps/openbao/openbao/config"
AGENT_POLICY = (CONFIG / "pam-agent.hcl").read_text()
SYNC_POLICY = (CONFIG / "pam-sync.hcl").read_text()
APPROVER_POLICY = (CONFIG / "pam-approver.hcl").read_text()
ROLE_FILE = ROOT / "scripts/pam/pam-approver-role.json"
GROUP = "pam-approvers"
CLAIM = "openbao-admins"  # What Authentik sends. The group's alias maps it to GROUP.
AUDIENCE = "warpgate-agents"
ISSUER = "lab"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Keys:
    """Signs the ID tokens Authentik would issue."""

    def __init__(self):
        self.key = rsa.generate_private_key(65537, 2048)
        self.public_pem = self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def sign(self, claims):
        head = b64url(json.dumps({"alg": "RS256", "typ": "JWT", "kid": "lab"}).encode())
        body = b64url(json.dumps(claims).encode())
        sig = self.key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{b64url(sig)}"

    def jwt(self, sub, groups=(), aud=AUDIENCE, issuer=ISSUER, **extra):
        now = int(time.time())
        return self.sign({
            "iss": issuer, "sub": sub, "aud": aud, "iat": now, "nbf": now - 5, "exp": now + 300,
            "preferred_username": sub, "groups": list(groups), **extra,
        })


class Bao:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="pam-lab-")
        self.root = secrets.token_hex(16)
        self.port = free_port()
        self.addr = f"http://127.0.0.1:{self.port}"
        # Audit devices can only be declared in server config. "lab" has the
        # defaults, as on the cluster; "plain" shows what hmac_accessor=false does.
        self.audit_path = os.path.join(self.dir, "audit.log")
        self.plain_audit_path = os.path.join(self.dir, "audit-plain.log")
        config = os.path.join(self.dir, "audit.hcl")
        with open(config, "w") as f:
            f.write(f"""
audit "file" "lab" {{
  options {{
    file_path = "{self.audit_path}"
  }}
}}
audit "file" "plain" {{
  options {{
    file_path     = "{self.plain_audit_path}"
    hmac_accessor = "false"
  }}
}}
""")
        self.proc = subprocess.Popen(
            [os.environ["BAO_BIN"], "server", "-dev", f"-config={config}",
             f"-dev-root-token-id={self.root}", f"-dev-listen-address=127.0.0.1:{self.port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={"HOME": self.dir, "PATH": os.environ["PATH"]},
        )
        atexit.register(self.close)
        for _ in range(100):
            try:
                if self.api("GET", "sys/health", token=None)[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.1)
        raise RuntimeError("dev server did not start")

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        shutil.rmtree(self.dir, ignore_errors=True)

    def api(self, method, path, token="", body=None, headers=None):
        """Returns (status, parsed body). An empty token string means root."""
        token = self.root if token == "" else token
        req = urllib.request.Request(
            f"{self.addr}/v1/{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     **({"X-Vault-Token": token} if token else {}), **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as f:
                raw, status = f.read(), f.status
        except urllib.error.HTTPError as e:
            with e:
                raw, status = e.read(), e.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode()

    def ok(self, method, path, body=None, headers=None):
        status, data = self.api(method, path, body=body, headers=headers)
        if status not in (200, 204):
            raise RuntimeError(f"{method} {path}: {status} {data}")
        return data

    def policy(self, name, text):
        return self.api("PUT", f"sys/policies/acl/{name}", body={"policy": text})

    def token(self, *policies):
        return self.ok("POST", "auth/token/create", {
            "policies": list(policies), "no_default_policy": True, "ttl": "10m",
        })["auth"]["client_token"]


class Lab(Bao):
    """A dev server set up the way the design has it: KV v2 at kv/, a JWT mount with one role
    per harness, pre-created agent entities, and the pam policies. The approver logs in through
    a second mount, standing in for pam-oidc, which carries the pam-approvers external group."""

    def __init__(self):
        super().__init__()
        self.keys = Keys()
        self.ok("POST", "sys/mounts/kv", {"type": "kv", "options": {"version": "2"}})
        self.ok("POST", "sys/mounts/kv/tune", {"audit_non_hmac_request_keys": ["reason", "delivery"]})
        self.ok("POST", "sys/auth/jwt", {"type": "jwt"})
        self.ok("POST", "auth/jwt/config", {
            "jwt_validation_pubkeys": [self.keys.public_pem], "bound_issuer": ISSUER,
        })
        self.jwt_accessor = self.ok("GET", "sys/auth")["data"]["jwt/"]["accessor"]
        self.ok("PUT", "sys/policies/acl/pam-agent", {"policy": AGENT_POLICY})
        self.ok("PUT", "sys/policies/acl/pam-sync", {"policy": SYNC_POLICY})
        self.ok("PUT", "sys/policies/acl/pam-approver", {"policy": APPROVER_POLICY})
        self.ok("POST", "sys/auth/people", {"type": "jwt"})
        self.ok("POST", "auth/people/config", {
            "jwt_validation_pubkeys": [self.keys.public_pem], "bound_issuer": ISSUER,
        })
        self.ok("POST", "auth/people/role/human", {
            "role_type": "jwt", "user_claim": "sub", "bound_audiences": [AUDIENCE],
            "groups_claim": "groups", "token_policies": ["pam-approver"], "token_no_default_policy": True,
            "token_ttl": "1h",
        })
        people = self.ok("GET", "sys/auth")["data"]["people/"]["accessor"]
        for harness in ("claude", "codex", "opencode"):
            self.agent_identity(harness)
        gid = self.ok("POST", "identity/group", {
            "name": GROUP, "type": "external", "policies": ["pam-approver"],
        })["data"]["id"]
        self.ok("POST", "identity/group-alias", {
            "name": CLAIM, "mount_accessor": people, "canonical_id": gid,
        })

    def agent_identity(self, harness, policy="pam-agent"):
        name = f"agent-{harness}"
        self.ok("POST", f"auth/jwt/role/{name}", {
            "role_type": "jwt", "user_claim": "preferred_username", "bound_audiences": [AUDIENCE],
            "bound_claims": {"preferred_username": name}, "token_policies": [policy],
            "token_no_default_policy": True, "token_ttl": "20m", "token_max_ttl": "20m",
        })
        eid = self.ok("POST", "identity/entity", {"name": name})["data"]["id"]
        self.ok("POST", "identity/entity-alias", {
            "name": name, "canonical_id": eid, "mount_accessor": self.jwt_accessor,
        })
        return eid

    def login(self, name, role=None, groups=(), mount="jwt"):
        status, data = self.api("POST", f"auth/{mount}/login", token=None, body={
            "role": role or name, "jwt": self.keys.jwt(name, groups),
        })
        if status != 200:
            raise RuntimeError(f"login {name}: {status} {data}")
        return data["auth"]["client_token"]

    def agent(self, harness="claude"):
        return self.login(f"agent-{harness}")

    def writer(self):
        """A token with a gated update. pam-agent has none, but the page must stay safe if a
        write is ever held, and OpenBao's behaviour with one stays pinned."""
        if self.api("GET", "auth/jwt/role/agent-writer")[0] != 200:
            self.policy("pam-writer", '''
path "kv/data/pam/live/*" {
  capabilities = ["read", "update"]
  control_group = {
    ttl = "15m"
    factor "owner" {
      controlled_capabilities = ["read", "update"]
      identity {
        group_names = ["pam-approvers"]
        approvals   = 1
      }
    }
  }
}
''')
            self.agent_identity("writer", policy="pam-writer")
        return self.login("agent-writer")

    def approver(self):
        return self.login("owner", role="human", groups=[CLAIM], mount="people")

    def seed(self, path, **values):
        self.ok("POST", f"kv/data/{path}", {"data": values})

    def approve(self, accessor, approver=None):
        return self.api("POST", "sys/control-group/authorize", token=approver or self.approver(),
                        body={"accessor": accessor})

    def request(self, accessor, approver=None):
        return self.api("POST", "sys/control-group/request", token=approver or self.approver(),
                        body={"accessor": accessor})

    def unwrap(self, wrapping_token):
        return self.api("POST", "sys/wrapping/unwrap", token=wrapping_token, body={})

    def audit_lines(self, plain=False):
        time.sleep(0.1)
        with open(self.plain_audit_path if plain else self.audit_path) as f:
            return f.read().splitlines()


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lab = Lab()
        cls.addClassCleanup(cls.lab.close)
        cls.approver = cls.lab.approver()

    def hold(self, path="kv/data/pam/live/demo", token=None, value="ONE"):
        """A read that's waiting for approval. Returns (agent token, wrap_info)."""
        self.lab.seed(path.removeprefix("kv/data/"), v=value)
        token = token or self.lab.agent()
        status, reply = self.lab.api("GET", path, token=token)
        self.assertEqual(status, 200)
        self.assertIsNone(reply["data"])
        return token, reply["wrap_info"]

    def read_as(self, token, path):
        return self.lab.api("GET", path, token=token)
