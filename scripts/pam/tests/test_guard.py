"""The rules of the policy guard, as unit tests."""
import pathlib
import sys
import tempfile
import unittest

import lab

sys.path.insert(0, str(lab.ROOT / "scripts/pam"))
import policy_guard as guard  # noqa: E402

CONTROL = '''control_group = {
    ttl = "15m"
    factor "owner" {
      controlled_capabilities = [%s]
      identity {
        group_names = ["openbao-admins"]
        approvals   = 1
      }
    }
  }'''


def gated(path, capabilities='"read"', controlled='"read"'):
    return f'path "{path}" {{\n  capabilities = [{capabilities}]\n  {CONTROL % controlled}\n}}\n'


class Overlap(unittest.TestCase):
    def test_patterns(self):
        for pattern, expected in [
            ("kv/data/pam/live/x", True), ("kv/data/pam/*", True), ("kv/data/*", True), ("kv/*", True),
            ("*", True), ("kv/+/pam/live/*", True), ("+/data/pam/x", True), ("kv/data/pa*", True),
            ("kv/data/{{identity.entity.name}}/x", True), ("kv/data/pam/inbox/{{identity.entity.name}}/*", True),
            ("kv/metadata/pam/live/*", False), ("kv/data/warpgate/*", False), ("kv/data/pam", False),
            ("kv/data/pamela", False), ("auth/token/revoke-self", False), ("kv/+/x", False),
        ]:
            self.assertEqual(guard.overlaps(pattern, guard.GATED), expected, pattern)


class Policy(unittest.TestCase):
    def assertRefused(self, name, text, phrase):
        findings = guard.check_policy(name, text)
        self.assertTrue([f for f in findings if phrase in f], findings)

    def test_the_repo_policies_pass(self):
        self.assertEqual(guard.check(lab.CONFIG, {}), [])

    def test_plain_grant_on_a_gated_path(self):
        self.assertRefused("x", 'path "kv/data/pam/live/canary" {\n  capabilities = ["read"]\n}\n', "plain grant")

    def test_wide_plain_grant(self):
        self.assertRefused("x", 'path "kv/*" {\n  capabilities = ["read"]\n}\n', "plain grant")

    def test_plain_grant_inside_the_agent_policy(self):
        text = (lab.CONFIG / "pam-agent.hcl").read_text() + 'path "kv/data/pam/live/x" {\n  capabilities = ["read"]\n}\n'
        self.assertRefused("pam-agent", text, "plain grant")

    def test_the_legacy_policy_attribute_is_a_grant(self):
        for value in ("read", "write", "sudo"):
            self.assertRefused("x", f'path "kv/data/pam/live/x" {{\n  policy = "{value}"\n}}\n', "plain grant")

    def test_the_legacy_deny_isnt_a_grant(self):
        self.assertEqual(guard.check_policy("x", 'path "kv/data/pam/*" {\n  policy = "deny"\n}\n'), [])

    def test_an_unknown_legacy_value_is_refused(self):
        self.assertRefused("x", 'path "kv/data/other" {\n  policy = "everything"\n}\n', "unknown policy value")

    def test_a_legacy_grant_on_an_agent_identity_can_remove_the_gate(self):
        text = 'path "kv/data/pam/live/*" {\n  policy = "read"\n}\n'
        state = {"entities": [{"name": "agent-claude", "policies": ["pam-agent", "old"]}], "groups": [], "roles": {}}
        self.assertIn("can remove the gate", " ".join(guard.check_identities(state, {"old": text})))

    def test_a_wider_live_policy_is_caught_though_the_repo_copy_is_clean(self):
        repo = (lab.CONFIG / "pam-agent.hcl").read_text()
        wider = repo + 'path "kv/data/pam/live/x" {\n  capabilities = ["read"]\n}\n'
        findings = guard.check(lab.CONFIG, {"policies": {"pam-agent": wider}})
        self.assertIn("live pam-agent: plain grant", " ".join(findings))

    def test_a_live_policy_matching_the_repo_isnt_checked_twice(self):
        text = (lab.CONFIG / "pam-agent.hcl").read_text()
        self.assertEqual(guard.check(lab.CONFIG, {"policies": {"pam-agent": text}}), [])

    def test_an_identity_is_judged_on_the_live_text(self):
        with tempfile.TemporaryDirectory() as policy_dir:
            pathlib.Path(policy_dir, "other.hcl").write_text('path "sys/health" {\n  capabilities = ["read"]\n}\n')
            state = {"policies": {"other": 'path "kv/*" {\n  capabilities = ["read"]\n}\n'}, "groups": [], "roles": {},
                     "entities": [{"name": "agent-claude", "policies": ["pam-agent", "other"]}]}
            self.assertIn("identity agent-claude holds other: can remove the gate", " ".join(guard.check(policy_dir, state)))

    def test_a_live_policy_only_on_the_server_is_checked(self):
        findings = guard.check(lab.CONFIG, {"policies": {"extra": 'path "kv/*" {\n  capabilities = ["read"]\n}\n'}})
        self.assertIn("live extra: plain grant", " ".join(findings))

    def test_deny_isnt_a_grant(self):
        self.assertEqual(guard.check_policy("x", 'path "kv/data/pam/*" {\n  capabilities = ["deny"]\n}\n'), [])

    def test_a_capability_left_uncontrolled(self):
        self.assertRefused("x", gated("kv/data/pam/live/*", '"read", "update"', '"update"'), "outside controlled_capabilities")

    def test_create_is_only_uncontrolled_in_an_inbox(self):
        self.assertEqual(guard.check_policy("x", gated("kv/data/pam/inbox/y/*", '"create", "read"')), [])
        self.assertRefused("x", gated("kv/data/pam/live/*", '"create", "read"'), "outside controlled_capabilities")

    def test_the_inbox_exemption_is_anchored(self):
        self.assertRefused("x", gated("kv/data/pam/live/inbox/*", '"create", "read"'), "outside controlled_capabilities")

    def test_no_ttl(self):
        self.assertRefused("x", gated("kv/data/other").replace('ttl = "15m"', ""), "no ttl")

    def test_ttl_inside_identity_doesnt_count(self):
        text = gated("kv/data/other").replace('ttl = "15m"', "").replace("approvals   = 1", 'approvals = 1\n ttl = "15m"')
        self.assertRefused("x", text, "no ttl")

    def test_an_unreadable_policy_is_refused(self):
        self.assertTrue(guard.check_policy("x", 'path "kv" {'))

    def test_admin_and_sync_may_read_directly(self):
        self.assertEqual(guard.check_policy("openbao-admin", (lab.CONFIG / "openbao-admin.hcl").read_text()), [])
        self.assertEqual(guard.check_policy("pam-sync", (lab.CONFIG / "pam-sync.hcl").read_text()), [])


class Identities(unittest.TestCase):
    TEXTS = {"pam-agent": 'path "x" {\n capabilities = ["read"]\n}\n', "a-plain": 'path "kv/data/pam/live/x" {\n capabilities = ["read"]\n}\n',
             "other": 'path "sys/health" {\n capabilities = ["read"]\n}\n', "default": 'path "sys/health" {\n capabilities = ["read"]\n}\n'}

    def findings(self, entity=None, role=None, groups=()):
        state = {"entities": [{"name": "agent-claude", "aliases": [], **(entity or {})}], "groups": list(groups),
                 "roles": {"agent-claude": {"token_policies": ["pam-agent"], "token_no_default_policy": True, **(role or {})}}}
        return guard.check_identities(state, self.TEXTS)

    def test_clean(self):
        self.assertEqual(self.findings(), [])

    def test_policy_on_the_entity_counts_too(self):
        self.assertEqual(self.findings({"policies": ["pam-agent"]}, {"token_policies": []}), [])

    def test_plain_grant_on_the_entity(self):
        self.assertIn("can remove the gate", " ".join(self.findings({"policies": ["a-plain"]})))

    def test_plain_grant_through_a_group(self):
        groups = [{"id": "g", "policies": ["a-plain"]}]
        self.assertIn("can remove the gate", " ".join(self.findings({"group_ids": ["g"]}, groups=groups)))

    def test_plain_grant_through_an_inherited_group(self):
        groups = [{"id": "parent", "policies": ["a-plain"]}]
        self.assertIn("can remove the gate", " ".join(self.findings({"inherited_group_ids": ["parent"]}, groups=groups)))

    def test_plain_grant_on_the_role(self):
        self.assertIn("can remove the gate", " ".join(self.findings(role={"token_policies": ["pam-agent", "a-plain"]})))

    def test_an_unrelated_extra_policy(self):
        self.assertIn("nothing else", " ".join(self.findings({"policies": ["other"]})))

    def test_a_policy_with_no_text_is_assumed_to_bypass(self):
        self.assertIn("can remove the gate", " ".join(self.findings({"policies": ["gone"]})))

    def test_default_policy(self):
        self.assertIn("default", " ".join(self.findings(role={"token_no_default_policy": False})))

    def test_external_group_on_the_jwt_mount(self):
        state = {"jwt_accessor": "auth_jwt_1", "entities": [], "roles": {},
                 "groups": [{"id": "g", "name": "x", "alias": {"mount_accessor": "auth_jwt_1", "name": "y"}}]}
        self.assertIn("aliased on the jwt mount", " ".join(guard.check_identities(state, self.TEXTS)))

    def test_external_group_on_another_mount_is_fine(self):
        state = {"jwt_accessor": "auth_jwt_1", "entities": [], "roles": {},
                 "groups": [{"id": "g", "name": "x", "alias": {"mount_accessor": "auth_oidc_1", "name": "y"}}]}
        self.assertEqual(guard.check_identities(state, self.TEXTS), [])

    def test_groups_claim_on_an_agent_role(self):
        self.assertIn("groups_claim", " ".join(self.findings(role={"groups_claim": "groups"})))

    def test_a_group_the_state_doesnt_describe(self):
        for field in ("group_ids", "inherited_group_ids"):
            self.assertIn("doesn't describe", " ".join(self.findings({field: ["missing"]})))

    def test_entity_found_by_alias_not_name(self):
        state = {"jwt_accessor": "auth_jwt_1", "entities": [{"name": "sneaky", "policies": ["a-plain"],
                 "aliases": [{"mount_accessor": "auth_jwt_1"}]}], "groups": [], "roles": {}}
        self.assertTrue(guard.check_identities(state, self.TEXTS))


class PolicyDir(unittest.TestCase):
    def test_an_empty_or_missing_directory_fails(self):
        with tempfile.TemporaryDirectory() as empty:
            for path in (empty, empty + "/missing"):
                findings = guard.check(path, {})
                self.assertTrue(any("no policies found" in f for f in findings), findings)


class Accessors(unittest.TestCase):
    ENTITY = {"name": "x", "aliases": [{"mount_accessor": "auth_jwt_1"}]}
    GROUP = {"id": "g", "alias": {"mount_accessor": "auth_jwt_1"}}

    def findings(self, **state):
        return " ".join(guard.check_accessors(state))

    def test_missing_keys_with_entity_aliases(self):
        found = self.findings(entities=[self.ENTITY])
        self.assertIn("no jwt_accessor", found)
        self.assertIn("no pam_oidc_accessor", found)

    def test_missing_keys_with_group_aliases(self):
        self.assertIn("no jwt_accessor", self.findings(groups=[self.GROUP], pam_oidc_accessor=""))
        self.assertIn("no pam_oidc_accessor", self.findings(groups=[self.GROUP], jwt_accessor=""))

    def test_a_null_key_counts_as_missing(self):
        self.assertIn("no jwt_accessor", self.findings(entities=[self.ENTITY], jwt_accessor=None, pam_oidc_accessor=""))

    def test_empty_means_the_mount_doesnt_exist(self):
        self.assertEqual(self.findings(entities=[self.ENTITY], jwt_accessor="", pam_oidc_accessor=""), "")

    def test_no_aliases_needs_no_keys(self):
        self.assertEqual(self.findings(entities=[{"name": "x"}], groups=[{"id": "g", "alias": {}}]), "")

    def test_check_includes_this(self):
        self.assertIn("no jwt_accessor", " ".join(guard.check(lab.CONFIG, {"entities": [self.ENTITY], "groups": [], "roles": {}})))


class Approvers(unittest.TestCase):
    ACCESSOR = "auth_oidc_pam"

    def group(self, **changes):
        return {"id": "a", "name": "pam-approvers", "type": "external", "policies": ["pam-approver"],
                "alias": {"mount_accessor": self.ACCESSOR}, **changes}

    def role(self, **changes):
        return {"token_policies": ["pam-approver"], "token_no_default_policy": True, **changes}

    def findings(self, groups=None, roles=None, entities=(), accessor=ACCESSOR):
        return " ".join(guard.check_approvers({
            "pam_oidc_accessor": accessor, "groups": [self.group()] if groups is None else groups,
            "pam_oidc_roles": {"pam-approver": self.role()} if roles is None else roles, "entities": list(entities)}))

    def test_a_state_without_the_role_listing_is_refused(self):
        found = " ".join(guard.check_approvers({"pam_oidc_accessor": self.ACCESSOR, "groups": [], "entities": []}))
        self.assertIn("no pam_oidc_roles", found)

    def test_clean(self):
        self.assertEqual(self.findings(), "")

    def test_nothing_set_up_yet(self):
        self.assertEqual(guard.check_approvers({}), [])

    def test_another_group_on_the_mount(self):
        other = {"id": "b", "name": "admins", "alias": {"mount_accessor": self.ACCESSOR}}
        self.assertIn("admins is aliased on pam-oidc", self.findings([self.group(), other]))

    def test_a_group_on_another_mount_is_fine(self):
        other = {"id": "b", "name": "admins", "alias": {"mount_accessor": "auth_oidc_1"}}
        self.assertEqual(self.findings([self.group(), other]), "")

    def test_group_missing(self):
        self.assertIn("doesn't", self.findings([]))

    def test_group_must_be_external(self):
        self.assertIn("must be external", self.findings([self.group(type="internal")]))

    def test_group_must_hold_only_the_approver_policy(self):
        self.assertIn("hold only", self.findings([self.group(policies=["pam-approver", "openbao-admin"])]))
        self.assertIn("hold only", self.findings([self.group(policies=[])]))

    def test_group_cant_have_a_parent(self):
        self.assertIn("parent group", self.findings([self.group(parent_group_ids=["p"])]))

    def test_group_must_be_aliased_on_the_mount(self):
        self.assertIn("isn't aliased", self.findings([self.group(alias={"mount_accessor": "auth_oidc_1"})]))

    def test_role_must_use_only_the_approver_policy(self):
        self.assertIn("must use only", self.findings(roles={"x": self.role(token_policies=["pam-approver", "openbao-admin"])}))

    def test_role_must_drop_the_default_policy(self):
        self.assertIn("token_no_default_policy", self.findings(roles={"x": self.role(token_no_default_policy=False)}))

    def test_policy_directly_on_an_approver_entity(self):
        entity = {"name": "owner", "policies": ["openbao-admin"], "aliases": [{"mount_accessor": self.ACCESSOR}]}
        self.assertIn("holds policies directly", self.findings(entities=[entity]))

    def test_approver_entity_in_another_group(self):
        entity = {"name": "owner", "group_ids": ["a", "internal"], "aliases": [{"mount_accessor": self.ACCESSOR}]}
        self.assertIn("in another group", self.findings(entities=[entity]))

    def test_approver_entity_with_an_inherited_group(self):
        entity = {"name": "owner", "group_ids": ["a"], "inherited_group_ids": ["parent"], "aliases": [{"mount_accessor": self.ACCESSOR}]}
        self.assertIn("in another group", self.findings(entities=[entity]))

    def test_approver_entity_in_only_the_approver_group_is_fine(self):
        entity = {"name": "owner", "group_ids": ["a"], "aliases": [{"mount_accessor": self.ACCESSOR}]}
        self.assertEqual(self.findings(entities=[entity]), "")

    def test_approver_entity_merged_with_another_mount(self):
        entity = {"name": "owner", "aliases": [{"mount_accessor": self.ACCESSOR}, {"mount_accessor": "auth_oidc_1"}]}
        self.assertIn("merged with another mount", self.findings(entities=[entity]))

    def test_policy_on_another_entity_is_fine(self):
        entity = {"name": "other", "policies": ["x"], "aliases": [{"mount_accessor": "auth_oidc_1"}]}
        self.assertEqual(self.findings(entities=[entity]), "")


if __name__ == "__main__":
    unittest.main()
