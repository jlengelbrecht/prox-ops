#!/usr/bin/env python3
"""Refuses a policy set that would weaken the agent PAM gate. scripts/openbao-configure.sh runs
it before writing anything.

  policy_guard.py <policy-dir> [--live <state.json>]

Most control group failures are closed. These checks cover the ones that aren't, or that
fail silently:
- a grant on a gated path with no control group, in the same policy or another one on the
  token. OpenBao keeps only the first policy's control group for a path, and an exact path
  beats a gated glob, so name order can remove the gate;
- a capability on a gated path that controlled_capabilities leaves out, which runs unapproved;
- a control_group with no ttl, whose requests vanish;
- an agent identity holding anything but pam-agent, including the default policy;
- a way to join groups at login: an external group aliased on the jwt mount, or a groups_claim
  on an agent role. Neither shows on the entity until an agent has logged in;
- anything on the pam-oidc mount that makes an approver login more than an approver: another group
  aliased there, pam-approvers holding more than pam-approver, a role with other policies or the
  default policy, or an approver's entity that holds a policy directly, sits in another group, or
  is merged with a login from another mount.

--live adds what's on the server, as read by openbao-configure.sh:

  {"jwt_accessor": "...", "pam_oidc_accessor": "...", "policies": {name: text}, "entities": [...],
   "groups": [...], "roles": {name: role}, "pam_oidc_roles": {name: role}}

An accessor key is the empty string when that mount doesn't exist. If a key is missing while
aliases exist, the guard can't tell which mount they belong to, so it refuses. It also refuses a
group id the state doesn't describe, and checks live policy text as well as the repo's: a wider
policy on the server is what's in force, whatever the repo says.
"""
import argparse
import json
import pathlib
import re
import sys

AGENT_POLICY = "pam-agent"
APPROVER_POLICY = "pam-approver"
APPROVER_GROUP = "pam-approvers"
GATED = "kv/data/pam/*"
# Not agent policies, so they read kv/pam directly. On an agent identity, they're refused below.
UNGATED_BY_DESIGN = {"openbao-admin", "pam-sync"}
# The older form of capabilities: path "x" { policy = "read" }.
LEGACY_POLICY = {
    "deny": ["deny"],
    "read": ["read", "list"],
    "write": ["create", "read", "update", "patch", "delete", "list"],
    "sudo": ["create", "read", "update", "patch", "delete", "list", "sudo"],
}

TOKEN = re.compile(r'''
    \s+ | \#[^\n]* | //[^\n]* | /\*.*?\*/
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<word>[A-Za-z0-9_.-]+)
  | (?P<punct>[{}\[\]=,])
''', re.S | re.X)


def tokenize(text):
    tokens, at = [], 0
    while at < len(text):
        match = TOKEN.match(text, at)
        if not match:
            raise ValueError(f"can't read the policy near {text[at:at + 30]!r}")
        at = match.end()
        if match.group("string"):
            tokens.append(("string", json.loads(match.group("string"))))
        elif match.group("word"):
            tokens.append(("word", match.group("word")))
        elif match.group("punct"):
            tokens.append((match.group("punct"), match.group("punct")))
    return tokens


class Parser:
    """The subset of HCL policies use: attributes, labelled blocks, lists and objects. A block
    becomes a key in its parent, with its labels joined onto the name."""

    def __init__(self, text):
        self.tokens = tokenize(text)
        self.at = 0

    def peek(self):
        return self.tokens[self.at][0] if self.at < len(self.tokens) else None

    def next(self):
        if self.at >= len(self.tokens):
            raise ValueError("the policy ends too early")
        self.at += 1
        return self.tokens[self.at - 1]

    def expect(self, kind):
        got, value = self.next()
        if got != kind:
            raise ValueError(f"expected {kind}, found {value!r}")
        return value

    def body(self, closing=None):
        entries = []
        while self.peek() != closing:
            if self.peek() == ",":
                self.next()
                continue
            kind, key = self.next()
            if kind not in ("word", "string"):
                raise ValueError(f"unexpected {key!r}")
            labels = []
            while self.peek() == "string":
                labels.append(self.next()[1])
            if self.peek() == "=":
                self.next()
                entries.append((key, self.value()))
            else:
                self.expect("{")
                entries.append((" ".join([key, *labels]), self.block()))
        return entries

    def block(self):
        entries = self.body("}")
        self.expect("}")
        return entries

    def value(self):
        kind, value = self.next()
        if kind == "{":
            return self.block()
        if kind == "[":
            items = []
            while self.peek() != "]":
                items.append(self.value())
                if self.peek() == ",":
                    self.next()
            self.next()
            return items
        if kind in ("string", "word"):
            return value
        raise ValueError(f"unexpected {value!r}")


def parse_policy(text):
    """Returns [(path, capabilities, control_group or None)]."""
    paths = []
    for key, block in Parser(text).body():
        if not key.startswith("path "):
            raise ValueError(f"unexpected {key!r}")
        attributes = dict(block)
        capabilities = list(attributes.get("capabilities", []))
        if "policy" in attributes:
            if attributes["policy"] not in LEGACY_POLICY:
                raise ValueError(f"unknown policy value {attributes['policy']!r}")
            capabilities += LEGACY_POLICY[attributes["policy"]]
        paths.append((key.removeprefix("path "), capabilities, attributes.get("control_group")))
    return paths


def overlaps(a, b):
    """Could one request path match both ACL patterns? '+' is one segment, a trailing '*' is any
    rest, and a template is unknown until login, so it counts as '+'."""
    a, b = (re.sub(r"\{\{[^}]*\}\}", "+", p) for p in (a, b))

    def segment_end(text, at):
        end = text.find("/", at)
        return len(text) if end < 0 else end

    i = j = 0
    while True:
        if a[i:] == "*" or b[j:] == "*":
            return True
        if i == len(a) or j == len(b):
            return i == len(a) and j == len(b)
        if a[i] == "+" or b[j] == "+":
            if a[i] == "+" and "*" in b[j:segment_end(b, j)]:
                return True
            if b[j] == "+" and "*" in a[i:segment_end(a, i)]:
                return True
            i, j = segment_end(a, i), segment_end(b, j)
        elif a[i] == b[j]:
            i, j = i + 1, j + 1
        else:
            return False


def controlled(group):
    capabilities = set()
    for key, block in group:
        if key.startswith("factor "):
            capabilities.update(dict(block).get("controlled_capabilities", []))
    return capabilities


def check_policy(name, text):
    try:
        paths = parse_policy(text)
    except ValueError as e:
        return [f"{name}: {e}"]
    findings = []
    for path, capabilities, group in paths:
        if group is not None and not dict(group).get("ttl"):
            findings.append(f"{name}: {path} has a control_group with no ttl, so its requests vanish")
        grants = set(capabilities) - {"deny"}
        if name in UNGATED_BY_DESIGN or not grants or not overlaps(path, GATED):
            continue
        if group is None:
            findings.append(f"{name}: plain grant on {path} overlaps the gated {GATED} and can remove the gate")
            continue
        unapproved = grants - controlled(group)
        if path.startswith("kv/data/pam/inbox/"):
            unapproved -= {"create"}  # A new inbox value can't be read back, so it needs no approval.
        if unapproved:
            findings.append(f"{name}: {path} leaves {sorted(unapproved)} outside controlled_capabilities")
    return findings


def agent_entities(state):
    accessors = state.get("jwt_accessor")
    return [
        e for e in state.get("entities", [])
        if e["name"].startswith("agent-") or any(a.get("mount_accessor") == accessors for a in e.get("aliases") or [])
    ]


def check_identities(state, texts):
    entities = {e["name"]: e for e in agent_entities(state)}
    roles = state.get("roles", {})
    groups = {g["id"]: g for g in state.get("groups", [])}
    findings = []
    accessor = state.get("jwt_accessor")
    for group in groups.values():
        if accessor and (group.get("alias") or {}).get("mount_accessor") == accessor:
            findings.append(f"group {group.get('name', group['id'])} is aliased on the jwt mount, so a login claim can add its policies")
    for name, role in sorted(roles.items()):
        if role.get("groups_claim"):
            findings.append(f"role {name} sets groups_claim, so a login claim can add policies")
    for name in sorted({*entities, *roles}):
        entity, role = entities.get(name, {}), roles.get(name)
        held = set(entity.get("policies") or [])
        for group_id in (entity.get("group_ids") or []) + (entity.get("inherited_group_ids") or []):
            if group_id not in groups:
                findings.append(f"identity {name} is in group {group_id}, which the live state doesn't describe")
            held.update(groups.get(group_id, {}).get("policies") or [])
        if role is not None:
            held.update(role.get("token_policies") or [])
            if not role.get("token_no_default_policy"):
                held.add("default")
        for policy in sorted(held - {AGENT_POLICY}):
            paths = parse_policy(texts[policy]) if policy in texts else None
            bypass = paths is None or any(overlaps(p, GATED) and set(c) - {"deny"} for p, c, _ in paths)
            why = "can remove the gate" if bypass else "agents hold pam-agent and nothing else"
            findings.append(f"identity {name} holds {policy}: {why}")
        if AGENT_POLICY not in held:
            findings.append(f"identity {name} doesn't hold {AGENT_POLICY}")
    return findings


def check_approvers(state):
    accessor = state.get("pam_oidc_accessor")
    groups = state.get("groups", [])
    findings = []
    for group in groups:
        if accessor and group.get("name") != APPROVER_GROUP and (group.get("alias") or {}).get("mount_accessor") == accessor:
            findings.append(f"group {group.get('name')} is aliased on pam-oidc; only {APPROVER_GROUP} may be")
    approvers = next((g for g in groups if g.get("name") == APPROVER_GROUP), None)
    if approvers is None:
        if accessor:
            findings.append(f"pam-oidc exists but group {APPROVER_GROUP} doesn't")
    else:
        if approvers.get("type") != "external":
            findings.append(f"group {APPROVER_GROUP} must be external")
        if set(approvers.get("policies") or []) != {APPROVER_POLICY}:
            findings.append(f"group {APPROVER_GROUP} must hold only {APPROVER_POLICY}")
        if approvers.get("parent_group_ids"):
            findings.append(f"group {APPROVER_GROUP} has a parent group, which adds its policies")
        if accessor and (approvers.get("alias") or {}).get("mount_accessor") != accessor:
            findings.append(f"group {APPROVER_GROUP} isn't aliased on pam-oidc")
    for name, role in sorted(state.get("pam_oidc_roles", {}).items()):
        if set(role.get("token_policies") or []) != {APPROVER_POLICY}:
            findings.append(f"pam-oidc role {name} must use only {APPROVER_POLICY}")
        if not role.get("token_no_default_policy"):
            findings.append(f"pam-oidc role {name} must set token_no_default_policy")
    for entity in state.get("entities", []):
        mounts = {a.get("mount_accessor") for a in entity.get("aliases") or []}
        if not accessor or accessor not in mounts:
            continue
        if entity.get("policies"):
            findings.append(f"entity {entity.get('name')} logs in through pam-oidc and holds policies directly")
        if mounts - {accessor}:
            findings.append(f"entity {entity.get('name')} logs in through pam-oidc and is merged with another mount")
        member_of = set(entity.get("group_ids") or []) | set(entity.get("inherited_group_ids") or [])
        if member_of - {(approvers or {}).get("id")}:
            findings.append(f"entity {entity.get('name')} logs in through pam-oidc and is in another group")
    return findings


def check_accessors(state):
    aliases = [a for e in state.get("entities", []) for a in e.get("aliases") or []]
    aliases += [g["alias"] for g in state.get("groups", []) if g.get("alias")]
    if not aliases:
        return []
    return [f"the live state has aliases but no {key}, so their mounts can't be told apart"
            for key in ("jwt_accessor", "pam_oidc_accessor") if state.get(key) is None]


def check(policy_dir, state):
    live = state.get("policies", {})
    repo = {path.stem: path.read_text() for path in sorted(pathlib.Path(policy_dir).glob("*.hcl"))}
    findings = check_accessors(state)
    for name, text in sorted(repo.items()):
        findings += check_policy(name, text)
    for name, text in sorted(live.items()):
        if repo.get(name) != text:
            findings += [f"live {finding}" for finding in check_policy(name, text)]
    try:
        findings += check_identities(state, {**repo, **live})
    except ValueError as e:
        findings.append(str(e))
    return findings + check_approvers(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("policy_dir")
    parser.add_argument("--live")
    args = parser.parse_args()
    state = json.loads(pathlib.Path(args.live).read_text()) if args.live else {}
    findings = check(args.policy_dir, state)
    for finding in findings:
        print(f"refused: {finding}", file=sys.stderr)
    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
