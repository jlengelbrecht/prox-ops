#!/usr/bin/env bash
# Verify that every committed example agrees with the catalog it claims, and
# with itself.
#
# Two families of check live here. The first is about FINGERPRINTS - which
# catalog document a fixture claims. The second is about DERIVED STATE - fields
# a fixture computes from other fields in the same document, which JSON Schema
# deliberately does not police (see "why not JSON Schema" below).
#
# `catalog_version` exists to settle one question: which catalog document was
# this answer computed against. That makes every fixture carrying a digest a
# claim about a specific document, and claims rot. The first catalog change
# that forgets to re-stamp them leaves fingerprints that are confidently wrong,
# which is worse than carrying none - a reader has no way to tell.
#
# WHAT IS CHECKED
#
#   1. Every example file is listed in EXPECTED below, with the EXACT set of
#      digests it may carry and the exact catalog_document_version it may name.
#      Not a global whitelist: the placeholders each stand for a DIFFERENT
#      hypothetical catalog, so a fixture assigned to one of them must fail if
#      it is switched to another, even though both are documented. An
#      unlisted or missing example is also a failure - a new fixture has to be
#      given an owner here, deliberately.
#   2. openapi.yaml embeds the real digest inline in its request and response
#      examples. Those rot exactly like the JSON fixtures do, so every digest
#      in the spec must be the real one; the spec never illustrates a
#      hypothetical catalog.
#   3. The abbreviated digest in README.md still matches the real one, since
#      the digest table is what a reader actually consults.
#   4. DERIVED POLICY STATE in the /v1/status fixtures. `resolves_now` is not
#      an independent fact: it is "can this policy select anything right now",
#      which is answerable from the same document by resolving the policy's
#      prefer_order against each placement's `eligible`. `resolves_today` is
#      the same question against `status`/`selectable`. A fixture that asserts
#      either against its own placements teaches the wrong meaning for the
#      field, which is the one thing a worked example must not do.
#
# WHY NOT JSON SCHEMA. This is a RELATIONAL JOIN - policies[].prefer_order
# against placements[].name - and JSON Schema has no way to express one. It
# could only be faked by enumerating placement names into the schema, which
# would hard-code catalog data the contract deliberately does not carry. The
# structural state-machine invariants (unseen implies OFFLINE and null
# heartbeat fields and ineligible, silence requires a prior report, an enrolled
# edge placement may not claim static, and so on) stay in status.schema.json
# where they belong: those are single-object facts and the schema checks them
# well. Cross-object facts are checked here instead, against the fixtures.
#
# Run from anywhere. Requires python3 with PyYAML, which the contract's other
# gate commands already use.
#
# NOT WIRED INTO CI. Doing that touches workflow files, which is a separate
# decision for the repository owner; see README "Validating this contract".

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

python3 - "$repo_root" <<'PY'
import hashlib
import json
import pathlib
import re
import sys

import yaml

root = pathlib.Path(sys.argv[1])
catalog_path = root / "kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml"
contract = root / "contracts/agent-router"
examples = contract / "examples"

catalog = yaml.safe_load(catalog_path.read_text())["data"]["catalog.yaml"]
REAL = "sha256:" + hashlib.sha256(catalog.encode()).hexdigest()
DOC = str(yaml.safe_load(catalog)["version"])

# The documented placeholders. Each stands for a DIFFERENT hypothetical
# catalog; see the digest table in README.md. Obviously fabricated on purpose.
EDGE = "sha256:" + "decafbad" * 8      # cachyos-7900xtx brought up
ALL_EDGE = "sha256:" + "f00dface" * 8  # all three edge placements brought up
RETIRED = "sha256:" + "deadbeef" * 8   # local-unrestricted and any-24gb retired
METERED = "sha256:" + "cafebabe" * 8   # a metered funding source declared

# path -> (digests the file may carry, catalog_document_version it may name)
# An empty digest set means the file must carry no digest at all.
EXPECTED = {
    "errors/caller-unauthenticated.json": (set(), None),
    "errors/catalog-schema-unsupported.json": (set(), None),
    "errors/catalog-unavailable.json": (set(), None),
    "errors/catalog-version-stale.json": ({REAL, RETIRED}, None),
    "errors/heartbeat-node-identity-mismatch.json": (set(), None),
    "errors/heartbeat-unauthenticated.json": (set(), None),
    "errors/internal-error.json": ({REAL}, None),
    "errors/invalid-request.json": (set(), None),
    "errors/metered-authorization-required.json": ({METERED}, None),
    "errors/metered-denied-no-alternative.json": ({METERED}, None),
    "errors/no-eligible-profile.json": ({REAL}, None),
    "errors/rate-limited.json": (set(), None),
    "errors/unknown-placement-policy.json": ({RETIRED}, None),
    "errors/unknown-profile.json": ({RETIRED}, None),
    "execution-profile/docs-low-risk-cluster-only.json": ({REAL}, DOC),
    "execution-profile/local-code-standard.json": ({REAL}, DOC),
    "execution-profile/metered-denied-substituted.json": ({METERED}, "1.3.0"),
    "execution-profile/security-tagged-excludes-unrestricted.json": ({REAL}, DOC),
    "heartbeat/state-available.json": (set(), None),
    "heartbeat/state-draining.json": (set(), None),
    "heartbeat/state-interactive.json": (set(), None),
    "heartbeat/state-offline.json": (set(), None),
    "heartbeat/state-serving.json": (set(), None),
    "heartbeat/unmeasured-laptop-on-battery.json": (set(), None),
    "place/placed-kserve-only-candidate.json": ({REAL}, None),
    "place/placed-warm-edge.json": ({EDGE}, None),
    "place/unavailable-all-withdrawn.json": ({ALL_EDGE}, None),
    "place/unavailable-policy-edge-only.json": ({REAL}, None),
    "status/status-today.json": ({REAL}, DOC),
    "status/status-restarted-unseen.json": ({EDGE}, "1.2.0"),
    "status/status-with-edge.json": ({EDGE}, "1.2.0"),
}

NAMES = {REAL: "the real digest", EDGE: "the cachyos-brought-up placeholder",
         ALL_EDGE: "the all-edge-brought-up placeholder",
         RETIRED: "the retirements placeholder",
         METERED: "the metered-funding placeholder"}


def name(digest):
    return NAMES.get(digest, "an undocumented digest")


def digests(text):
    return set(re.findall(r"sha256:[0-9a-f]{64}", text))


failures = []
found = {str(p.relative_to(examples)) for p in examples.rglob("*.json")}

for missing in sorted(found - set(EXPECTED)):
    failures.append(
        f"examples/{missing}\n"
        f"    is not listed in EXPECTED in this script. Every fixture is bound\n"
        f"    to the catalog it depicts on purpose; add it there."
    )
for gone in sorted(set(EXPECTED) - found):
    failures.append(f"examples/{gone}\n    is listed in EXPECTED but does not exist.")

for rel in sorted(found & set(EXPECTED)):
    want_digests, want_doc = EXPECTED[rel]
    text = (examples / rel).read_text()
    have = digests(text)
    if have != want_digests:
        for extra in sorted(have - want_digests):
            failures.append(
                f"examples/{rel}\n"
                f"    carries {extra}\n"
                f"      which is {name(extra)},\n"
                f"    but this fixture is bound to "
                f"{', '.join(sorted(name(d) for d in want_digests)) or 'no digest at all'}."
            )
        for absent in sorted(want_digests - have):
            failures.append(
                f"examples/{rel}\n"
                f"    no longer carries {absent}\n"
                f"      which is {name(absent)}, the digest this fixture is bound to."
            )
    have_doc = json.loads(text).get("catalog_document_version")
    if have_doc != want_doc:
        failures.append(
            f"examples/{rel}\n"
            f"    names catalog document version {have_doc!r}, expected {want_doc!r}."
        )

spec = contract / "openapi.yaml"
for digest in sorted(digests(spec.read_text()) - {REAL}):
    failures.append(
        f"contracts/agent-router/openapi.yaml\n"
        f"    embeds {digest}\n"
        f"      which is {name(digest)}.\n"
        f"    The spec illustrates the CURRENT catalog only: {REAL}."
    )

# ---------------------------------------------------------------------------
# 4. Derived policy state in the status fixtures.
# ---------------------------------------------------------------------------
for rel in sorted(r for r in EXPECTED if r.startswith("status/")):
    doc = json.loads((examples / rel).read_text())
    placements = {pl["name"]: pl for pl in doc.get("placements", [])}
    for policy in doc.get("policies", []):
        order = policy.get("prefer_order", [])
        unknown = [n for n in order if n not in placements]
        if unknown:
            failures.append(
                f"examples/{rel}\n"
                f"    policy {policy['name']} prefers {', '.join(unknown)},\n"
                f"    which this document does not list as a placement."
            )
            continue
        live = [n for n in order if placements[n]["eligible"]]
        selectable = [n for n in order
                      if placements[n]["status"] == "available" and placements[n]["selectable"]]
        for field, names, question in (
            ("resolves_now", live, "eligible right now"),
            ("resolves_today", selectable, "selectable in this catalog"),
        ):
            expected = bool(names)
            if policy.get(field) is not expected:
                detail = ", ".join(names) if names else "nothing it names is"
                failures.append(
                    f"examples/{rel}\n"
                    f"    policy {policy['name']} says {field}: {str(policy.get(field)).lower()},\n"
                    f"    but it prefers {', '.join(order)}\n"
                    f"    and {detail} {question}. Expected {str(expected).lower()}."
                )

short = REAL[7:15] + "…" + REAL[-6:]
if short not in (contract / "README.md").read_text():
    failures.append(
        f"contracts/agent-router/README.md\n"
        f"    the digest table does not mention sha256:{short},\n"
        f"    which is the committed catalog's abbreviated digest."
    )

print(f"catalog {DOC}, digest {REAL}")
if failures:
    print("")
    print("Fixtures disagree with the catalog they are bound to, or with themselves:")
    print("")
    for failure in failures:
        print(f"  {failure}")
        print("")
    print("For a fingerprint: re-stamp the fixture, or move it to a different")
    print("documented placeholder AND update EXPECTED to say so. A fingerprint")
    print("that names the wrong catalog is worse than none.")
    print("")
    print("For derived policy state: the placements are the fact and the")
    print("resolves_* field is the summary, so fix the summary. If the summary")
    print("looks right, the placements are what actually changed.")
    sys.exit(1)

print(f"{len(EXPECTED)} fixtures + openapi.yaml agree with the catalog each is bound to,")
print("and every status policy's resolves_now/resolves_today matches its own placements")
PY
