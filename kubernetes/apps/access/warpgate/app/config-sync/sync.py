"""Apply state.json to Warpgate through its admin API.

Additive: creates and updates what the state lists, deletes nothing. Weakening
the agent write path by any route, including the admin UI, fails the job and
raises an alert, even when this run repairs it.
"""
import http.client
import json
import os
import socket
import ssl
import sys

HOST, PORT, SNI = "warpgate", 443, "access.homelab0.org"
BASE = "/@warpgate/admin/api"
ADMIN_TARGET, ADMIN_ROLE, MAX_GRACE = "k8s-admin", "agent-admin", 3600
TOKEN = os.environ["WARPGATE_ADMIN_TOKEN"]
CTX = ssl.create_default_context()


class Conn(http.client.HTTPSConnection):
    # The certificate names the public host; the Service is reached directly.
    def connect(self):
        raw = socket.create_connection((HOST, PORT), timeout=20)
        self.sock = CTX.wrap_socket(raw, server_hostname=SNI)


def api(method, path, body=None):
    conn = Conn(HOST, PORT, context=CTX, timeout=20)
    data = None if body is None else json.dumps(body)
    headers = {"X-Warpgate-Token": TOKEN, "Content-Type": "application/json"}
    conn.request(method, BASE + path, data, headers)
    res = conn.getresponse()
    raw = res.read()
    if res.status >= 300:
        sys.exit(f"{method} {path}: HTTP {res.status}")
    return json.loads(raw) if raw else None


def by_name(path, key="name"):
    return {item[key]: item for item in api("GET", path)}


def require(ok, message):
    if not ok:
        sys.exit(f"refused: {message}")


def token_ref(source_name, source, target):
    """The source target's secret reference, pointed at this target's path."""
    token = source["auth"]["token"]
    require(isinstance(token, str) and source_name in token, "source token is not a secret reference")
    return token.replace(source_name, target)


def sync_target(name, want, roles, targets):
    require(name != ADMIN_TARGET or want["require_approval"] is True, f"{name} must require approval")
    source_name = want["copy_options_from"]
    source = targets[source_name]["options"]
    auth = {**source["auth"], "token": token_ref(source_name, source, name)}
    options = {**source, "auth": auth}
    body = {
        "name": name,
        "description": want["description"],
        "options": options,
        "ticket_requests_disabled": True,
        "ticket_require_approval": False,
        "require_approval": want["require_approval"],
    }
    if name not in targets:
        targets[name] = api("POST", "/targets", body)
        print(f"created target {name}")
    elif any(targets[name].get(k) != v for k, v in body.items() if k != "name"):
        api("PUT", f"/targets/{targets[name]['id']}", body)
        print(f"updated target {name}")
    tid = targets[name]["id"]
    have = {r["name"] for r in api("GET", f"/targets/{tid}/roles")}
    for role in want["roles"]:
        if role not in have:
            api("POST", f"/targets/{tid}/roles/{roles[role]['id']}")
            print(f"added role {role} to target {name}")


def tampered():
    """Weakened settings, found before sync repairs them."""
    found = []
    target = by_name("/targets").get(ADMIN_TARGET)
    if target and target["require_approval"] is not True:
        found.append(f"{ADMIN_TARGET} had approval turned off")
    grace = api("GET", "/parameters")["admin_approval_grace_period_seconds"]
    if grace is not None and grace > MAX_GRACE:
        found.append("approval memory was over 1 hour")
    return found


def sync(state):
    params = state["parameters"]
    require(0 < params["admin_approval_grace_period_seconds"] <= MAX_GRACE, "approval memory is over 1 hour")
    current = api("GET", "/parameters")
    changed = {k: v for k, v in params.items() if current.get(k) != v}
    if changed:
        api("PUT", "/parameters", changed)
        print(f"updated parameters {sorted(changed)}")

    roles = by_name("/roles")
    for name, description in state["roles"].items():
        if name not in roles:
            roles[name] = api("POST", "/roles", {"name": name, "description": description})
            print(f"created role {name}")

    targets = by_name("/targets")
    for name, want in state["targets"].items():
        sync_target(name, want, roles, targets)

    users = by_name("/users", "username")
    for name, want in state["users"].items():
        if name not in users:
            print(f"skipped user {name}: not in Warpgate yet")
            continue
        uid = users[name]["id"]
        # Revoked assignments stay listed, inactive. Granting one reactivates it.
        have = {r["name"] for r in api("GET", f"/users/{uid}/roles") if r["is_active"]}
        for role in want["roles"]:
            if role not in have:
                api("POST", f"/users/{uid}/roles/{roles[role]['id']}", {})
                print(f"added role {role} to user {name}")


def verify():
    """Hard limits. Editing state.json can't relax these."""
    target = by_name("/targets")[ADMIN_TARGET]
    require(target["require_approval"] is True, f"{ADMIN_TARGET} no longer requires approval")
    roles = by_name("/roles")
    held = {t["name"] for t in api("GET", f"/role/{roles[ADMIN_ROLE]['id']}/targets")}
    require(held == {ADMIN_TARGET}, f"{ADMIN_ROLE} must hold only {ADMIN_TARGET}")
    grace = api("GET", "/parameters")["admin_approval_grace_period_seconds"]
    require(grace is not None and grace <= MAX_GRACE, "approval memory is over 1 hour")
    users = by_name("/users", "username")
    for name, user in users.items():
        if name.startswith("agent-"):
            require(not api("GET", f"/users/{user['id']}/admin-roles"), f"{name} holds an admin role")


if __name__ == "__main__":
    with open("/config/state.json") as f:
        desired = json.load(f)
    weakened = tampered()
    sync(desired)
    verify()
    if weakened:
        sys.exit("refused: repaired, but " + "; ".join(weakened))
    print("in sync")
