#!/usr/bin/env bash
# Verify that the /v1/route contract has not grown session mechanics.
#
# WHY THIS EXISTS
#
# EPIC-035 §10ao froze an ownership boundary: Orca is the Agent Development
# Environment and owns worktrees, terminal/process lifecycle, execution
# environment and the agent-process host; agent-router recommends and nothing
# else. ADE-BOUNDARY.md writes that down. This file makes the half of it that
# lives in a payload shape mechanical, because a boundary that only exists in
# prose is a boundary that erodes one plausible field at a time - and every one
# of those fields arrives with a good reason attached.
#
# The failure this prevents is not a typo. It is a later story adding
# `session_id` "just for correlation", or `exec_host` "just as a hint", and the
# router quietly becoming a scheduler with a session registry. Nothing else in
# this directory would notice: such a field is valid JSON Schema, passes
# openapi-spec-validator, and reads as helpful in review.
#
# WHAT IT CHECKS, in priority order
#
#   1. EXACT PROPERTY-SET PIN. RouteRequest and ExecutionProfile carry exactly
#      the top-level properties ADE-BOUNDARY.md §4 allowlists - no additions,
#      no silent removals. The allowlists are READ FROM THAT DOCUMENT, so an
#      addition cannot pass without the boundary declaration moving in the same
#      change. That is the point: growth is allowed, silence is not.
#   2. CLOSURE GUARD. `additionalProperties: false` stays on both shapes and on
#      every nested route-scoped object. Without closure the pin is decoration -
#      an open object accepts `session_id` without any schema naming it.
#   3. SEMANTIC-TOKEN DENYLIST. Property names and fixture keys are normalized
#      to tokens (split on _, -, and camelCase, then lowercased) and matched
#      against ADE-BOUNDARY.md §5. Tokens rather than word boundaries, so
#      `sessionId`, `session_id` and `SESSION-ID` are one finding. Applied at
#      any nesting depth - to NAMES only, never to description prose, which may
#      legitimately say "no session field exists".
#   4. FIXTURE SWEEP. Every route-scoped fixture obeys the pin and the denylist,
#      so an example cannot demonstrate a field the schema forbids.
#
# SCOPE: the /v1/route input/output contract ONLY - RouteRequest,
# ExecutionProfile, the nested route definitions they reference, the /v1/route
# inline examples in openapi.yaml, and examples/execution-profile/*.json.
# The heartbeat, status and place contracts are deliberately exempt:
# operational telemetry legitimately speaks of processes and hosts, and a check
# that cried wolf there would teach people to ignore it.
#
# Usage: ./verify-ade-boundary.sh   (exit 0 = boundary intact)
set -euo pipefail
cd "$(dirname "$0")"

python3 - <<'PY'
import glob, json, re, sys

try:
    import yaml
except ImportError:
    sys.exit("FAIL  PyYAML is required (this check parses openapi.yaml). "
             "Install it - 'pip install pyyaml' - rather than skipping: a "
             "check that cannot run must fail, not silently pass.")

BOUNDARY = "ADE-BOUNDARY.md"
OPENAPI  = "openapi.yaml"
PROFILE  = "schemas/execution-profile.schema.json"
FIXTURES = "examples/execution-profile/*.json"

failed = []
def check(ok, msg):
    print(("ok    " if ok else "FAIL  ") + msg)
    if not ok:
        failed.append(msg)


# --- the boundary document is the source of truth for both lists ------------

doc = open(BOUNDARY, encoding="utf-8").read()

def block(marker):
    """Contents of the fenced code block following <!-- marker -->."""
    m = re.search(re.escape(f"<!-- {marker} -->") + r"\s*```[a-z]*\n(.*?)```",
                  doc, re.S)
    if not m:
        sys.exit(f"FAIL  {BOUNDARY} has no '{marker}' block - the check cannot "
                 f"source its pin, so it would pass vacuously. Refusing.")
    return [ln.strip() for ln in m.group(1).splitlines() if ln.strip()]

PIN = {
    "RouteRequest":     set(block("allowlist:RouteRequest")),
    "ExecutionProfile": set(block("allowlist:ExecutionProfile")),
}
DENY_TOKENS = set(block("denylist:tokens"))
DENY_PAIRS  = {tuple(ln.split()) for ln in block("denylist:pairs")}

for name, want in PIN.items():
    if not want:
        sys.exit(f"FAIL  {BOUNDARY} allowlist for {name} is empty.")
if not DENY_TOKENS or not DENY_PAIRS:
    sys.exit(f"FAIL  {BOUNDARY} denylist is empty - nothing would be rejected.")


# --- tokenization -----------------------------------------------------------

WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

def tokens(name):
    """session_id, sessionId, SESSION-ID -> ['session', 'id']."""
    out = []
    for part in re.split(r"[_\-]+", str(name)):
        out += [w.lower() for w in WORD.findall(part)]
    return out

def offence(name):
    """Why this name is forbidden, or None."""
    t = tokens(name)
    for tok in t:
        if tok in DENY_TOKENS:
            return f"token '{tok}'"
    for a, b in DENY_PAIRS:
        for i in range(len(t) - 1):
            if t[i] == a and t[i + 1] == b:
                return f"adjacent pair '{a} {b}'"
    return None


# --- walkers ----------------------------------------------------------------

def schema_names(node, path="", acc=None):
    """Every declared property name in a JSON Schema, at any depth, with the
    path it was found at. Only keys under properties/patternProperties and
    entries in `required` - never description prose, never schema keywords."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        for kw in ("properties", "patternProperties"):
            for name, sub in (node.get(kw) or {}).items():
                acc.append((name, f"{path}.{kw}.{name}"))
                schema_names(sub, f"{path}.{kw}.{name}", acc)
        for name in node.get("required") or []:
            acc.append((name, f"{path}.required"))
        for key, sub in node.items():
            if key in ("properties", "patternProperties", "required"):
                continue
            if isinstance(sub, (dict, list)):
                schema_names(sub, f"{path}.{key}", acc)
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            schema_names(sub, f"{path}[{i}]", acc)
    return acc

def open_objects(node, path="", acc=None):
    """Route-scoped object DEFINITIONS that declare properties but no closure.

    `type: "object"` is what separates a definition from an applicator fragment.
    An if/then/else or allOf branch also carries `properties`, but it constrains
    an object some definition already closed; putting additionalProperties:false
    on one would forbid every field it does not itself mention, which is a
    schema bug rather than a tighter contract. Every real object in this
    contract declares its type, so requiring it costs nothing - and the pin and
    the denylist below still see every name at every depth either way."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        ty = node.get("type")
        # A union like ["object","null"] is still object-capable, and an
        # object-typed node with NO properties at all is a fully open bag -
        # the worst case, since any key would validate. Both need closure.
        object_capable = ty == "object" or (isinstance(ty, list) and "object" in ty)
        if object_capable and node.get("additionalProperties") is not False:
            acc.append(path or "<root>")
        for key, sub in node.items():
            if isinstance(sub, (dict, list)):
                open_objects(sub, f"{path}.{key}", acc)
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            open_objects(sub, f"{path}[{i}]", acc)
    return acc

def data_keys(node, path="", acc=None):
    """Every key in a fixture/example payload, at any depth."""
    acc = [] if acc is None else acc
    if isinstance(node, dict):
        for key, sub in node.items():
            acc.append((key, f"{path}.{key}"))
            data_keys(sub, f"{path}.{key}", acc)
    elif isinstance(node, list):
        for i, sub in enumerate(node):
            data_keys(sub, f"{path}[{i}]", acc)
    return acc


# --- load the route-scoped contract ----------------------------------------

spec    = yaml.safe_load(open(OPENAPI, encoding="utf-8"))
profile = json.load(open(PROFILE, encoding="utf-8"))
request = spec["components"]["schemas"]["RouteRequest"]

SHAPES = {
    "RouteRequest":     (request, f"{OPENAPI} components.schemas.RouteRequest"),
    "ExecutionProfile": (profile, PROFILE),
}

route_examples = []   # (label, payload, shape it must obey)
op = spec["paths"]["/v1/route"]["post"]
for label, ex in (op["requestBody"]["content"]["application/json"]
                  .get("examples") or {}).items():
    if "value" in ex:
        route_examples.append((f"openapi /v1/route request '{label}'",
                               ex["value"], "RouteRequest"))
for code, resp in (op.get("responses") or {}).items():
    for label, ex in ((resp.get("content") or {})
                      .get("application/json", {}).get("examples") or {}).items():
        if "value" in ex:
            # Only a 2xx body is an ExecutionProfile. An error example is a
            # different shape and must not be pin-validated as a profile -
            # but its keys are still route-scoped surface, so it IS swept
            # for denylisted semantics.
            shape = "ExecutionProfile" if str(code).startswith("2") else None
            route_examples.append((f"openapi /v1/route {code} '{label}'",
                                   ex["value"], shape))
for path in sorted(glob.glob(FIXTURES)):
    route_examples.append((path, json.load(open(path, encoding="utf-8")),
                           "ExecutionProfile"))

if not route_examples:
    sys.exit("FAIL  no route-scoped examples found - the fixture sweep would "
             "pass vacuously. Refusing.")


# --- 1. exact property-set pin ---------------------------------------------

print("-- exact property-set pin (sourced from ADE-BOUNDARY.md) --")
for name, (schema, where) in SHAPES.items():
    got, want = set((schema.get("properties") or {})), PIN[name]
    extra, missing = sorted(got - want), sorted(want - got)
    check(not extra and not missing,
          f"{name} carries exactly its {len(want)} allowlisted properties ({where})")
    for p in extra:
        print(f"        + '{p}' is not in ADE-BOUNDARY.md's allowlist. Amend the "
              f"boundary declaration and this schema in the same change, or drop it.")
    for p in missing:
        print(f"        - '{p}' is allowlisted but no longer declared. Removing a "
              f"property is a contract change too; update the allowlist.")


# --- 2. closure guard -------------------------------------------------------

print("\n-- closure guard --")
for name, (schema, where) in SHAPES.items():
    check(schema.get("additionalProperties") is False,
          f"{name} keeps additionalProperties: false ({where})")
for name, (schema, where) in SHAPES.items():
    leaks = open_objects(schema)
    check(not leaks, f"{name}: every nested route-scoped object stays closed")
    for p in leaks:
        print(f"        open object at {p} - without closure the pin above is "
              f"decoration; the object accepts anything.")


# --- 3. semantic-token denylist, at any depth ------------------------------

print("\n-- semantic-token denylist (names only, never prose) --")
for name, (schema, where) in SHAPES.items():
    hits = [(n, p, offence(n)) for n, p in schema_names(schema) if offence(n)]
    check(not hits, f"{name}: no session/process semantics in any property name")
    for n, p, why in hits:
        print(f"        '{n}' at {p} -> {why}. That is ADE-owned; the router "
              f"holds no session state (ADE-BOUNDARY.md §2).")


# --- 4. fixture sweep -------------------------------------------------------

print("\n-- fixture sweep --")
for label, payload, shape in route_examples:
    problems = []
    if shape is not None and isinstance(payload, dict):
        extra = sorted(set(payload) - PIN[shape])
        problems += [f"top-level '{p}' is not in the {shape} allowlist" for p in extra]
    problems += [f"'{k}' at {p} -> {offence(k)}"
                 for k, p in data_keys(payload) if offence(k)]
    what = f"{shape} pin and denylist" if shape else "denylist (error-shaped example)"
    check(not problems, f"{label:58} obeys the {what}")
    for p in problems:
        print(f"        {p}")


# --- verdict ----------------------------------------------------------------

if failed:
    print(f"\n{len(failed)} check(s) failed. The /v1/route contract has drifted "
          f"across the ownership boundary frozen in ADE-BOUNDARY.md.")
    print("Adding a property is allowed - amend ADE-BOUNDARY.md's allowlist in "
          "the same change. Adding session mechanics is not.")
    sys.exit(1)

print(f"\nADE boundary intact: {len(PIN['RouteRequest'])} RouteRequest and "
      f"{len(PIN['ExecutionProfile'])} ExecutionProfile properties pinned, both "
      f"shapes closed, {len(route_examples)} route-scoped examples clean.")
PY
