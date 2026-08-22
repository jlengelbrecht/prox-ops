#!/usr/bin/env bash
# Verify that the committed catalog still yields the frozen placement decision
# table for qwen36-27b, and that the gateway can actually receive the header the
# contract tells callers to send.
#
# WHY THIS EXISTS
#
# The six cases below were argued in STORY-035-8 to hold "by construction":
# they fall out of `warm_preference: strict` plus the order of `prefer_order`,
# with no code in between. That argument is correct, and it is exactly why it
# needs a test. Nothing in the catalog says "edge must lead prefer_order" - a
# future story reordering that list, or flipping cachyos-7900xtx back to
# selectable: false, would silently invert rows this table froze, and no
# schema, no kubeconform run and no digest check would notice. verify-digests.sh
# polices whether fixtures agree with the catalog; this polices whether the
# catalog still means what R25 decided.
#
# There is no agent-router service to test against - Phase D (35.9-35.11) is
# undispatched - so this deliberately does NOT test an implementation. It
# re-derives the decision from the catalog's own declared semantics and asserts
# the frozen outcome. When the real router lands it must be tested against its
# own behaviour; this file is the specification it has to satisfy, not a
# substitute for that.
#
# SEMANTICS BEING APPLIED (as documented in the catalog itself)
#
#   1. Candidates = placements that are selectable AND listed for the model,
#      taken in prefer_order order.
#   2. warm_preference: strict => any known-warm candidate beats any candidate
#      that is cold or unknown, regardless of prefer_order.
#   3. Otherwise the first candidate in prefer_order wins.
#
# Usage: ./verify-placement-cases.sh   (exit 0 = table intact)
set -euo pipefail
cd "$(dirname "$0")"

CATALOG=../../kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml

python3 - "$CATALOG" <<'PY'
import sys, yaml

cm = yaml.safe_load(open(sys.argv[1]))
cat = yaml.safe_load(cm["data"]["catalog.yaml"])

MODEL  = "qwen36-27b"
POLICY = "prefer-warm-local"
EDGE, KSERVE = "cachyos-7900xtx", "kserve-a5000"

pol        = cat["policies"]["placement_policy"][POLICY]
order      = pol["prefer_order"]
strict     = pol["warm_preference"] == "strict"
declared   = cat["models"][MODEL]["placements"]
selectable = {n for n, p in cat["placements"].items() if p.get("selectable")}


def decide(warm, ineligible=frozenset()):
    """warm maps placement -> True (warm) / False (cold) / None (unknown)."""
    cands = [p for p in order
             if p in declared and p in selectable and p not in ineligible]
    if not cands:
        return None
    if strict:
        hot = [p for p in cands if warm.get(p) is True]
        if hot:
            return hot[0]
    return cands[0]


CASES = [
    ("edge warm  / kserve cold",    {EDGE: True,  KSERVE: False}, frozenset(),   EDGE),
    ("edge cold  / kserve warm",    {EDGE: False, KSERVE: True},  frozenset(),   KSERVE),
    ("both warm",                   {EDGE: True,  KSERVE: True},  frozenset(),   EDGE),
    ("both cold",                   {EDGE: False, KSERVE: False}, frozenset(),   EDGE),
    ("both unknown",                {},                           frozenset(),   EDGE),
    ("edge ineligible",             {EDGE: True,  KSERVE: False}, frozenset({EDGE}), KSERVE),
]

# Guard the premises too: if these drift, the cases above stop meaning what
# R25 froze even when they still pass.
premises = [
    (strict,                      f"{POLICY}.warm_preference must be 'strict'"),
    (order[0] == EDGE,            f"{POLICY}.prefer_order must lead with {EDGE}"),
    (EDGE in declared,            f"models.{MODEL}.placements must include {EDGE}"),
    (KSERVE in declared,          f"models.{MODEL}.placements must include {KSERVE}"),
    (EDGE in selectable,          f"{EDGE} must be selectable"),
    (KSERVE in selectable,        f"{KSERVE} must be selectable"),
]

failed = 0
for ok, why in premises:
    if not ok:
        print(f"FAIL  premise: {why}")
        failed += 1

for name, warm, inelig, want in CASES:
    got = decide(warm, inelig)
    flag = "ok  " if got == want else "FAIL"
    if got != want:
        failed += 1
    print(f"{flag}  {name:26} -> {got}" + ("" if got == want else f"  (expected {want})"))

if failed:
    print(f"\n{failed} check(s) failed: the catalog no longer yields the R25 frozen table.")
    sys.exit(1)

print(f"\nR25 frozen decision table intact for {MODEL} under policy '{POLICY}' "
      f"(catalog {cat['version']}).")
PY

# --- second check: the contract's header value must reach a route rule ---
#
# A placement name is a public identifier: it appears verbatim as the value of
# the x-placement request header. If the HTTPRoute matches some abbreviated
# spelling instead, a conformant caller copying PlaceResult.headers matches no
# rule, falls through to the default backend, and the placement it asked for is
# silently never used. Nothing else catches this: both files are individually
# valid, and the mismatch only shows up as "the edge is somehow never selected".
python3 - <<'PY2'
import glob, json, sys, yaml

ROUTE = "../../kubernetes/apps/ai/agentgateway-pilot/app/httproute.yaml"

rt = [d for d in yaml.safe_load_all(open(ROUTE))
      if d and d["metadata"]["name"] == "agentgateway-pilot"][0]
accepted = {h["value"]
            for r in rt["spec"]["rules"]
            for m in r.get("matches", [])
            for h in m.get("headers", [])
            if h["name"] == "x-placement"}

failed = 0
for f in sorted(glob.glob("examples/place/*.json")):
    hdr = (json.load(open(f)).get("headers") or {}).get("x-placement")
    if not hdr:
        continue
    ok = hdr in accepted
    print(f"{'ok  ' if ok else 'FAIL'}  {f.split('/')[-1]:38} x-placement: {hdr}")
    failed += not ok

if failed:
    print(f"\n{failed} example(s) emit an x-placement no route rule matches - "
          f"those requests would silently fall through to the default backend.")
    print(f"route rules accept: {sorted(accepted)}")
    sys.exit(1)

print(f"\nEvery committed x-placement value is matched by a route rule "
      f"({sorted(accepted)}).")
PY2
