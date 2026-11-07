"""What the pam policies allow, checked on the dev server with real logins."""
import json
import re
import unittest

import lab


class AgentPolicy(lab.Case):
    def test_inbox(self):
        agent = self.lab.agent()
        path = "kv/data/pam/inbox/agent-claude/inbox-test"
        self.assertEqual(self.lab.api("POST", path, token=agent, body={"data": {"value": "1"}})[0], 200)
        self.assertEqual(self.lab.api("POST", path, token=agent, body={"data": {"value": "2"}})[0], 403)
        status, reply = self.lab.api("GET", path, token=agent)
        self.assertEqual(status, 200)
        self.assertIsNone(reply["data"])
        self.assertTrue(reply["wrap_info"]["token"])

    def test_another_agents_inbox_is_closed(self):
        agent = self.lab.agent()
        self.lab.seed("pam/inbox/agent-codex/x", value="y")
        self.assertEqual(self.lab.api("POST", "kv/data/pam/inbox/agent-codex/z", token=agent,
                                      body={"data": {"value": "1"}})[0], 403)
        self.assertEqual(self.lab.api("GET", "kv/data/pam/inbox/agent-codex/x", token=agent)[0], 403)

    def test_agents_cant_change_or_create_live_secrets(self):
        self.lab.seed("pam/live/fixed", v="old")
        agent = self.lab.agent()
        for path in ("kv/data/pam/live/fixed", "kv/data/pam/live/brand-new"):
            self.assertEqual(self.lab.api("POST", path, token=agent, body={"data": {"value": "1"}})[0], 403)
        self.assertEqual(self.lab.ok("GET", "kv/data/pam/live/fixed")["data"]["metadata"]["version"], 1)

    def test_nothing_else_is_open(self):
        agent = self.lab.agent()
        self.lab.seed("warpgate/k8s-read", token="t")
        for method, path in [("GET", "kv/data/warpgate/k8s-read"), ("GET", "kv/subkeys/pam/live/demo"),
                             ("GET", "auth/token/lookup-self"), ("POST", "sys/control-group/request"),
                             ("LIST", "kv/metadata/pam/inbox/")]:
            self.assertEqual(self.lab.api(method, path, token=agent, body={} if method == "POST" else None)[0], 403, path)

    def test_token_can_only_be_held_for_twenty_minutes(self):
        auth = self.lab.api("POST", "auth/jwt/login", token=None, body={
            "role": "agent-claude", "jwt": self.lab.keys.jwt("agent-claude")})[1]["auth"]
        self.assertEqual(auth["lease_duration"], 1200)
        self.assertEqual(auth["policies"], ["pam-agent"])
        self.assertEqual(self.lab.api("POST", "auth/token/renew-self", token=auth["client_token"], body={})[0], 403)

    def test_sync_policy_reads_kv_pam_and_nothing_else(self):
        sync = self.lab.token("pam-sync")
        self.lab.seed("pam/live/synced", v="x")
        self.lab.seed("warpgate/k8s-read", token="t")
        self.assertEqual(self.lab.api("GET", "kv/data/pam/live/synced", token=sync)[1]["data"]["data"], {"v": "x"})
        self.assertEqual(self.lab.api("GET", "kv/data/warpgate/k8s-read", token=sync)[0], 403)
        self.assertEqual(self.lab.api("GET", "kv/metadata/warpgate/k8s-read", token=sync)[0], 403)
        status, _ = self.lab.api("PATCH", "kv/metadata/pam/live/synced", token=sync,
                                 body={"custom_metadata": {"pam_synced_version": "1"}},
                                 headers={"Content-Type": "application/merge-patch+json"})
        self.assertEqual(status, 204)
        self.assertEqual(self.lab.api("POST", "kv/data/pam/live/synced", token=sync, body={"data": {"v": "y"}})[0], 403)

    def test_sync_policy_can_patch_custom_metadata_only(self):
        sync = self.lab.token("pam-sync")
        self.lab.seed("pam/live/patched", v="x")
        patch = {"Content-Type": "application/merge-patch+json"}
        for body in ({"max_versions": 1}, {"delete_version_after": "1s"},
                     {"custom_metadata": {"a": "1"}, "max_versions": 1}):
            self.assertEqual(self.lab.api("PATCH", "kv/metadata/pam/live/patched", token=sync, body=body, headers=patch)[0], 403)
        self.assertEqual(self.lab.ok("GET", "kv/metadata/pam/live/patched")["data"]["max_versions"], 0)


class ApproverToken(lab.Case):
    PATHS = {"sys/control-group/authorize", "sys/control-group/request", "auth/token/accessors/", "auth/token/revoke-accessor"}

    def test_the_policy_names_exactly_the_four_paths(self):
        self.assertEqual(set(re.findall(r'path "([^"]+)"', lab.APPROVER_POLICY)), self.PATHS)

    def test_listing_accessors_needs_sudo(self):
        self.lab.policy("no-sudo", 'path "auth/token/accessors/" {\n  capabilities = ["list"]\n}\n')
        self.assertEqual(self.lab.api("LIST", "auth/token/accessors/", token=self.lab.token("no-sudo"))[0], 403)
        self.assertEqual(self.lab.api("LIST", "auth/token/accessors/", token=self.approver)[0], 200)

    def test_it_can_approve_and_nothing_else(self):
        _, wrap = self.hold()
        self.assertEqual(self.lab.approve(wrap["accessor"], self.approver)[0], 200)
        for method, path in [("GET", "kv/data/pam/live/demo"), ("GET", "sys/mounts"), ("LIST", "sys/policies/acl"),
                             ("GET", "identity/group/name/pam-approvers"), ("GET", "auth/token/lookup-self")]:
            self.assertEqual(self.lab.api(method, path, token=self.approver)[0], 403, path)

    def test_a_narrow_role_on_the_admin_mount_is_still_admin(self):
        """Identity policies follow the entity, which is why the page has its own mount."""
        self.lab.ok("PUT", "sys/policies/acl/openbao-admin", {"policy": (lab.CONFIG / "openbao-admin.hcl").read_text()})
        self.lab.ok("POST", "sys/auth/admin-mount", {"type": "jwt"})
        self.lab.ok("POST", "auth/admin-mount/config", {"jwt_validation_pubkeys": [self.lab.keys.public_pem], "bound_issuer": lab.ISSUER})
        accessor = self.lab.ok("GET", "sys/auth")["data"]["admin-mount/"]["accessor"]
        gid = self.lab.ok("POST", "identity/group", {"name": "admins", "type": "external", "policies": ["openbao-admin"]})["data"]["id"]
        self.lab.ok("POST", "identity/group-alias", {"name": lab.CLAIM, "mount_accessor": accessor, "canonical_id": gid})
        common = {"role_type": "jwt", "user_claim": "sub", "bound_audiences": [lab.AUDIENCE],
                  "token_policies": ["pam-approver"], "token_no_default_policy": True}
        self.lab.ok("POST", "auth/admin-mount/role/admin", common)
        self.lab.ok("POST", "auth/admin-mount/role/narrow", {**common, "groups_claim": "groups"})
        self.lab.ok("POST", "auth/admin-mount/role/admin-groups", {**common, "groups_claim": "groups"})
        login = lambda role, groups: self.lab.login("someone", role=role, groups=groups, mount="admin-mount")
        admin = login("admin-groups", [lab.CLAIM])
        self.assertEqual(self.lab.api("GET", "sys/mounts", token=login("narrow", [lab.CLAIM]))[0], 200)
        # Dropping the groups claim is no fix: it takes the entity out of the group, for every session.
        self.assertEqual(self.lab.api("GET", "sys/mounts", token=login("admin", []))[0], 403)
        self.assertEqual(self.lab.api("GET", "sys/mounts", token=admin)[0], 403)


class LookupAccessor(lab.Case):
    """Pins exactly what the grant exposes, so a change in what agents can see shows up."""

    def test_what_it_shows_for_someone_elses_token(self):
        agent = self.lab.agent()
        accessor = self.lab.ok("POST", "auth/token/create", {"policies": ["pam-sync"], "ttl": "10m"})["auth"]["accessor"]
        status, reply = self.lab.api("POST", "auth/token/lookup-accessor", token=agent, body={"accessor": accessor})
        self.assertEqual(status, 200)
        self.assertEqual(reply["data"]["id"], "")
        self.assertLessEqual(set(reply["data"]), {
            "accessor", "creation_time", "creation_ttl", "display_name", "entity_id", "expire_time", "explicit_max_ttl",
            "external_namespace_policies", "id", "identity_policies", "issue_time", "meta", "num_uses", "orphan", "path",
            "policies", "renewable", "ttl", "type"})
        self.assertIn("pam-sync", reply["data"]["policies"])

    def test_it_cant_list_or_revoke(self):
        agent = self.lab.agent()
        self.assertEqual(self.lab.api("LIST", "auth/token/accessors/", token=agent)[0], 403)
        self.assertEqual(self.lab.api("POST", "auth/token/revoke-accessor", token=agent, body={"accessor": "x"})[0], 403)


class Quota(unittest.TestCase):
    """Each test has its own server: a quota survives deletion only as a used-up bucket."""

    def setUp(self):
        self.lab = lab.Lab()
        self.addCleanup(self.lab.close)
        self.lab.seed("pam/live/demo", v="x")
        self.lab.seed("other/x", v="x")

    def burst(self, path, count=8):
        return [self.lab.api("GET", path)[0] for _ in range(count)]

    def test_a_path_with_a_trailing_slash_saves_and_limits_nothing(self):
        for path in ("kv/data/pam/", "kv/data/pam"):
            status, _ = self.lab.api("POST", "sys/quotas/rate-limit/flat", body={"path": path, "rate": 3, "interval": "1m", "inheritable": True})
            self.assertEqual(status, 204)
            self.assertNotIn(429, self.burst("kv/data/pam/live/demo"))

    def test_the_quota_we_ship_is_enforced_and_scoped(self):
        quota = json.loads((lab.ROOT / "scripts/pam/pam-quota.json").read_text())
        self.assertEqual(self.lab.api("POST", "sys/quotas/rate-limit/pam-requests", body=quota)[0], 204)
        codes = self.burst("kv/data/pam/live/demo", quota["rate"] + 5)
        self.assertEqual(codes.count(200), quota["rate"])
        self.assertEqual(codes.count(429), 5)
        self.assertEqual(set(self.burst("kv/data/other/x")), {200})
        self.assertEqual(set(self.burst("kv/metadata/pam/live/demo")), {200})
        self.assertEqual(self.lab.unwrap("s.nothing")[0], 400)


if __name__ == "__main__":
    unittest.main()
