"""The real scripts/openbao-configure.sh against the dev server, with kubectl and op replaced by
shims."""
import json
import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import unittest

import lab

KUBECTL = '''#!/bin/sh
# Stands in for: kubectl -n openbao exec -i openbao-0 -c openbao -- sh -c ... bao <args>
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
'''
OP = '''#!/bin/sh
case "$1" in
  whoami) echo '{"user_type":"MEMBER"}' ;;
  read) printf %s "$LAB_ROOT_TOKEN" ;;
  *) exit 1 ;;
esac
'''


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
        for name, text in (("kubectl", KUBECTL), ("op", OP)):
            (shims / name).write_text(text)
            (shims / name).chmod(stat.S_IRWXU)
        os.symlink(os.environ["BAO_BIN"], shims / "bao")
        self.config = pathlib.Path(self.tree, "kubernetes/apps/openbao/openbao/config")
        shutil.copytree(lab.CONFIG, self.config)
        shutil.copytree(lab.ROOT / "scripts/pam", pathlib.Path(self.tree, "scripts/pam"), ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copy(lab.ROOT / "scripts/openbao-configure.sh", pathlib.Path(self.tree, "scripts"))
        self.env = {"PATH": f"{shims}:{os.environ['PATH']}", "BAO_ADDR": self.lab.addr, "LAB_ROOT_TOKEN": self.lab.root,
                    "HOME": self.tree, "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", "/dev/shm")}

    def configure(self, *args):
        return subprocess.run([os.path.join(self.tree, "scripts/openbao-configure.sh"), *args], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def policy_written(self, name):
        return self.lab.api("GET", f"sys/policies/acl/{name}")[0] == 200

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

    def test_a_listing_failure_stops_the_run(self):
        token = self.lab.ok("POST", "auth/token/create", {"policies": ["limited"], "ttl": "10m"})["auth"]["client_token"]
        self.lab.policy("limited", 'path "sys/policies/acl*" {\n  capabilities = ["read", "list"]\n}\n')
        self.env["LAB_ROOT_TOKEN"] = token
        result = self.configure("--check")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("could not list", result.stderr)


if __name__ == "__main__":
    unittest.main()
