"""Pins how OpenBao's control groups behave, defects included. A failure here after an upgrade
means behaviour changed, so reread the design's section 2 before trusting the gate.
"""
import json
import unittest

import lab

GROUP = lab.GROUP
SAMPLE = {
    "ttl": 'ttl = "5m"',
    "controlled": 'controlled_capabilities = ["read"]',
    "approvals": "approvals = 1",
    "identity_extra": "",
}


def gated(path, capabilities='"read"', **overrides):
    parts = {**SAMPLE, **overrides}
    return f'''
path "{path}" {{
  capabilities = [{capabilities}]
  control_group = {{
    {parts["ttl"]}
    factor "owner" {{
      {parts["controlled"]}
      identity {{
        group_names = ["{GROUP}"]
        {parts["approvals"]}
        {parts["identity_extra"]}
      }}
    }}
  }}
}}
'''


def plain(path):
    return f'path "{path}" {{\n  capabilities = ["read"]\n}}\n'


class Defect1ApprovalIsAWindow(lab.Case):
    def test_one_approval_allows_repeated_reads(self):
        _, wrap = self.hold()
        self.lab.approve(wrap["accessor"], self.approver)
        for _ in range(3):
            status, reply = self.lab.unwrap(wrap["token"])
            self.assertEqual(status, 200)
            self.assertEqual(reply["data"]["data"], {"v": "ONE"})

    def test_one_approval_replays_a_write(self):
        self.lab.seed("pam/live/replay", v="start")
        status, reply = self.lab.api("POST", "kv/data/pam/live/replay", token=self.lab.writer(),
                                     body={"data": {"v": "new"}})
        self.assertEqual(status, 200)
        wrap = reply["wrap_info"]
        self.lab.approve(wrap["accessor"], self.approver)
        versions = [self.lab.unwrap(wrap["token"])[1]["data"]["version"] for _ in range(3)]
        self.assertEqual(versions, [2, 3, 4])

    def test_the_window_closes_when_the_wrapping_token_is_revoked(self):
        _, wrap = self.hold()
        self.lab.approve(wrap["accessor"], self.approver)
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 200)
        self.assertEqual(self.lab.api("POST", "auth/token/revoke-self", token=wrap["token"], body={})[0], 204)
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 400)


class Defect2PlainGrantRemovesTheGate(lab.Case):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        path = "kv/data/pam/live/overlap"
        cls.path = path
        cls.lab.seed("pam/live/overlap", v="LEAK")
        cls.lab.policy("a-plain", plain(path))
        cls.lab.policy("z-gated", gated(path))
        cls.lab.policy("a-gated", gated(path))
        cls.lab.policy("z-plain", plain(path))

    def assertUngated(self, *policies):
        status, reply = self.read_as(self.lab.token(*policies), self.path)
        self.assertEqual(status, 200)
        self.assertEqual(reply["data"]["data"], {"v": "LEAK"})
        self.assertIsNone(reply["wrap_info"])

    def test_plain_policy_named_first_removes_the_gate(self):
        self.assertUngated("a-plain", "z-gated")

    def test_input_order_doesnt_matter(self):
        self.assertUngated("z-gated", "a-plain")

    def test_gated_policy_named_first_stays_gated(self):
        _, reply = self.read_as(self.lab.token("a-gated", "z-plain"), self.path)
        self.assertIsNone(reply["data"])
        self.assertTrue(reply["wrap_info"]["token"])

    def test_exact_path_without_a_gate_beats_a_gated_glob(self):
        self.lab.policy("glob", gated("kv/data/pam/live/*") + plain(self.path))
        self.assertUngated("glob")

    def test_the_legacy_policy_attribute_is_a_plain_grant(self):
        self.lab.policy("a-legacy", f'path "{self.path}" {{\n  policy = "read"\n}}\n')
        self.assertUngated("a-legacy", "z-gated")

    def test_a_capability_left_out_of_controlled_capabilities_runs_unapproved(self):
        self.lab.policy("subset", gated(self.path, '"read", "update"', controlled='controlled_capabilities = ["update"]'))
        self.assertUngated("subset")


class Defect3NoTtl(lab.Case):
    def test_policy_saves_and_reads_vanish(self):
        path = "kv/data/pam/live/nottl"
        self.lab.seed("pam/live/nottl", v="HIDDEN")
        self.assertEqual(self.lab.policy("nottl", gated(path, ttl=""))[0], 204)
        status, reply = self.read_as(self.lab.token("nottl"), path)
        self.assertEqual(status, 200)
        self.assertIsNone(reply["data"])
        self.assertIsNone(reply["wrap_info"])


class Defect4DocumentedSamples(lab.Case):
    def test_controlled_capabilities_is_mandatory(self):
        status, reply = self.lab.policy("nocap", gated("kv/data/x", controlled=""))
        self.assertEqual(status, 400)
        self.assertIn("missing controlled capabilities", json.dumps(reply))

    def test_ttl_inside_identity_doesnt_count(self):
        path = "kv/data/pam/live/idttl"
        self.lab.seed("pam/live/idttl", v="HIDDEN")
        status, _ = self.lab.policy("idttl", gated(path, ttl="", identity_extra='ttl = "5m"'))
        if status == 204:  # It parses, and behaves as if there were no ttl.
            _, reply = self.read_as(self.lab.token("idttl"), path)
            self.assertIsNone(reply["data"])
            self.assertIsNone(reply["wrap_info"])
        else:
            self.assertEqual(status, 400)

    def test_issue_3666_stays_fixed(self):
        for approvals in ("approvals = 0", ""):
            status, reply = self.lab.policy("zero", gated("kv/data/x", approvals=approvals))
            self.assertEqual(status, 400)
            self.assertIn("missing required approvals", json.dumps(reply))


class Defect5And6Audit(lab.Case):
    def test_only_marker_is_wrap_info_once_per_request(self):
        before = len(self.lab.audit_lines())
        agent, wrap = self.hold("kv/data/pam/live/audit-marker")
        self.lab.api("GET", "kv/data/pam/live/audit-marker", token=agent)
        for _ in range(3):  # Client polling, an approval, and replays.
            self.lab.unwrap(wrap["token"])
        self.lab.approve(wrap["accessor"], self.approver)
        self.lab.unwrap(wrap["token"])
        self.lab.unwrap(wrap["token"])
        lines = self.lab.audit_lines()[before:]
        self.assertFalse([line for line in lines if "control group" in line.lower() or "control_group" in line.lower()])
        # The compact form is what the Alloy filter matches (assumption A3).
        marked = [line for line in lines if '"creation_path":"kv/data/pam/' in line]
        self.assertEqual(len(marked), 2)
        for line in marked:
            entry = json.loads(line)
            self.assertEqual(entry["type"], "response")
            self.assertEqual(entry["response"]["wrap_info"]["creation_path"], "kv/data/pam/live/audit-marker")

    def test_accessors_are_hmacd_unless_hmac_accessor_is_off(self):
        _, wrap = self.hold("kv/data/pam/live/audit-accessor")
        self.assertFalse([line for line in self.lab.audit_lines() if wrap["accessor"] in line])
        self.assertTrue([line for line in self.lab.audit_lines(plain=True) if wrap["accessor"] in line])

    def test_reason_is_in_clear_and_so_is_any_kv_field_called_reason(self):
        agent = self.lab.agent()
        self.lab.seed("pam/live/audit-reason", v="x")
        self.lab.api("GET", "kv/data/pam/live/audit-reason?reason=REASONCANARY", token=agent)
        self.lab.ok("POST", "kv/data/pam/live/audit-field", {"data": {"reason": "FIELDCANARY", "password": "PWCANARY"}})
        log = "\n".join(self.lab.audit_lines())
        self.assertIn("REASONCANARY", log)
        self.assertIn("FIELDCANARY", log)
        self.assertNotIn("PWCANARY", log)


class HeldRequests(lab.Case):
    def test_old_value_stays_until_the_write_runs(self):
        self.lab.seed("pam/live/held", v="old")
        _, reply = self.lab.api("POST", "kv/data/pam/live/held", token=self.lab.writer(), body={"data": {"v": "new"}})
        self.assertIsNone(reply["data"])
        self.assertEqual(self.lab.ok("GET", "kv/data/pam/live/held")["data"]["data"], {"v": "old"})

    def test_unwrap_before_approval_is_a_400_and_keeps_the_request(self):
        _, wrap = self.hold()
        for _ in range(3):
            self.assertEqual(self.lab.unwrap(wrap["token"])[0], 400)
        self.assertEqual(self.lab.request(wrap["accessor"], self.approver)[0], 200)

    def test_pending_and_denied_look_the_same_to_unwrap(self):
        _, pending = self.hold()
        _, denied = self.hold()
        self.lab.api("POST", "auth/token/revoke-accessor", token=self.approver, body={"accessor": denied["accessor"]})
        self.assertEqual(self.lab.unwrap(pending["token"]), self.lab.unwrap(denied["token"]))

    def test_lookup_accessor_tells_pending_from_denied_without_using_up_the_token(self):
        agent, wrap = self.hold()
        lookup = lambda: self.lab.api("POST", "auth/token/lookup-accessor", token=agent, body={"accessor": wrap["accessor"]})
        status, reply = lookup()
        self.assertEqual(status, 200)
        self.assertEqual(reply["data"]["id"], "")
        self.lab.approve(wrap["accessor"], self.approver)
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 200)
        self.lab.api("POST", "auth/token/revoke-accessor", token=self.approver, body={"accessor": wrap["accessor"]})
        self.assertEqual(lookup()[0], 400)

    def test_wrapping_token_used_anywhere_else_is_burned(self):
        _, wrap = self.hold()
        self.assertEqual(self.lab.api("GET", "auth/token/lookup-self", token=wrap["token"])[0], 403)
        self.assertEqual(self.lab.request(wrap["accessor"], self.approver)[0], 400)

    def test_the_window_cant_be_lengthened(self):
        agent, wrap = self.hold()
        _, reply = self.lab.api("GET", "kv/data/pam/live/demo", token=agent, headers={"X-Vault-Wrap-TTL": "20h"})
        self.assertEqual(reply["wrap_info"]["ttl"], 900)
        _, reply = self.lab.api("GET", "kv/data/pam/live/demo", token=agent, headers={"X-Vault-Wrap-TTL": "30s"})
        self.assertEqual(reply["wrap_info"]["ttl"], 900)
        self.assertEqual(self.lab.api("POST", "sys/wrapping/rewrap", token=wrap["token"], body={})[0], 400)
        self.lab.approve(wrap["accessor"], self.approver)
        self.assertEqual(self.lab.api("POST", "sys/wrapping/rewrap", token=wrap["token"], body={})[0], 403)

    def test_requester_cancels_with_revoke_self(self):
        _, wrap = self.hold()
        self.assertEqual(self.lab.api("POST", "auth/token/revoke-self", token=wrap["token"], body={})[0], 204)
        self.assertEqual(self.lab.request(wrap["accessor"], self.approver)[0], 400)

    def test_deny_kills_pending_and_approved_requests(self):
        for approve_first in (False, True):
            _, wrap = self.hold()
            if approve_first:
                self.lab.approve(wrap["accessor"], self.approver)
            self.lab.api("POST", "auth/token/revoke-accessor", token=self.approver, body={"accessor": wrap["accessor"]})
            self.assertEqual(self.lab.unwrap(wrap["token"])[0], 400)

    def test_request_runs_as_the_requester_so_revocation_stops_it(self):
        agent, wrap = self.hold()
        self.lab.approve(wrap["accessor"], self.approver)
        self.lab.ok("POST", "auth/token/revoke", {"token": agent})
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 403)

    def test_disabled_entity_stops_an_approved_request(self):
        self.lab.agent_identity("disabled")
        agent = self.lab.login("agent-disabled")
        _, wrap = self.hold(token=agent)
        self.lab.approve(wrap["accessor"], self.approver)
        self.lab.ok("POST", "identity/entity/name/agent-disabled", {"disabled": True})
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 403)

    def test_removed_policy_stops_an_approved_request(self):
        self.lab.policy("removed", gated("kv/data/pam/live/demo"))
        self.lab.agent_identity("removed")
        self.lab.ok("POST", "auth/jwt/role/agent-removed", {
            "role_type": "jwt", "user_claim": "preferred_username", "bound_audiences": [lab.AUDIENCE],
            "token_policies": ["removed"], "token_no_default_policy": True,
        })
        token = self.lab.login("agent-removed")
        _, wrap = self.hold(token=token)
        self.lab.approve(wrap["accessor"], self.approver)
        self.lab.policy("removed", plain("kv/data/nothing"))
        self.assertEqual(self.lab.unwrap(wrap["token"])[0], 403)

    def test_gates_work_on_pki(self):
        self.lab.ok("POST", "sys/mounts/pki", {"type": "pki"})
        self.lab.ok("POST", "pki/root/generate/internal", {"common_name": "lab"})
        self.lab.ok("POST", "pki/roles/r", {"allow_any_name": True, "max_ttl": "1h"})
        self.lab.policy("pki", gated("pki/issue/r", '"update"', controlled='controlled_capabilities = ["update"]'))
        status, reply = self.lab.api("POST", "pki/issue/r", token=self.lab.token("pki"),
                                     body={"common_name": "a.lab", "ttl": "5m"})
        self.assertEqual(status, 200)
        self.assertIsNone(reply["data"])
        self.assertTrue(reply["wrap_info"]["token"])


class Approval(lab.Case):
    def test_root_cant_approve(self):
        _, wrap = self.hold()
        status, reply = self.lab.approve(wrap["accessor"], self.lab.root)
        self.assertEqual(status, 500)
        self.assertIn("empty entity ID", json.dumps(reply))

    def test_non_member_is_recorded_nowhere(self):
        self.lab.policy("authorizer", 'path "sys/control-group/authorize" {\n  capabilities = ["update"]\n}\n')
        self.lab.ok("POST", "auth/people/role/outsider", {
            "role_type": "jwt", "user_claim": "sub", "bound_audiences": [lab.AUDIENCE], "token_policies": ["authorizer"],
        })
        _, wrap = self.hold()
        status, reply = self.lab.approve(wrap["accessor"], self.lab.login("mallory", role="outsider", mount="people"))
        self.assertEqual(status, 200)
        self.assertFalse(reply["data"]["approved"])
        self.assertEqual(self.lab.request(wrap["accessor"], self.approver)[1]["data"]["authorizations"], [])

    def test_second_approval_by_the_same_person_is_a_500(self):
        _, wrap = self.hold()
        self.assertEqual(self.lab.approve(wrap["accessor"], self.approver)[0], 200)
        self.assertEqual(self.lab.approve(wrap["accessor"], self.approver)[0], 500)

    def test_agent_cant_see_or_approve_its_own_request(self):
        agent, wrap = self.hold()
        self.assertEqual(self.lab.request(wrap["accessor"], agent)[0], 403)
        self.assertEqual(self.lab.approve(wrap["accessor"], agent)[0], 403)

    def test_approver_sees_a_write_body_in_plain_text(self):
        self.lab.seed("pam/live/body", v="x")
        _, reply = self.lab.api("POST", "kv/data/pam/live/body", token=self.lab.writer(),
                                body={"data": {"password": "PLAINCANARY"}})
        data = self.lab.request(reply["wrap_info"]["accessor"], self.approver)[1]["data"]
        self.assertEqual(data["request_data"]["data"], {"password": "PLAINCANARY"})

    def test_reason_reaches_the_approver_and_isnt_stored(self):
        agent = self.lab.agent()
        self.lab.seed("pam/live/reason", v="x")
        _, reply = self.lab.api("GET", "kv/data/pam/live/reason?reason=rotate%20dns", token=agent)
        wrap = reply["wrap_info"]
        data = self.lab.request(wrap["accessor"], self.approver)[1]["data"]
        self.assertEqual(data["request_data"], {"reason": "rotate dns"})
        self.assertEqual(data["request_entity"]["name"], "agent-claude")
        self.assertEqual((data["request_operation"], data["request_path"]), ("read", "kv/data/pam/live/reason"))
        self.lab.approve(wrap["accessor"], self.approver)
        self.assertEqual(self.lab.unwrap(wrap["token"])[1]["data"]["data"], {"v": "x"})

    def test_a_scan_of_all_accessors_finds_only_held_requests(self):
        _, wrap = self.hold()
        keys = self.lab.api("LIST", "auth/token/accessors/", token=self.approver)[1]["data"]["keys"]
        held = [key for key in keys if self.lab.request(key, self.approver)[0] == 200]
        self.assertIn(wrap["accessor"], held)
        self.assertTrue(set(keys) - set(held))


if __name__ == "__main__":
    unittest.main()
