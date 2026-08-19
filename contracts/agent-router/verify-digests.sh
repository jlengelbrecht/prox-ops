#!/usr/bin/env bash
# Verify that every committed example agrees with the catalog it claims.
#
# `catalog_version` exists to settle one question: which catalog document was
# this answer computed against. That makes every example carrying the REAL
# digest a fixture, and fixtures rot. The first catalog change that forgets to
# re-stamp them leaves behind fingerprints that are confidently wrong, which is
# worse than carrying none at all - a reader has no way to tell.
#
# So this script checks three things and exits non-zero on any of them:
#
#   1. Every digest in examples/ is either the real digest of the committed
#      catalog, or one of the documented placeholders that mark an example as
#      depicting a catalog which does not exist yet.
#   2. No example carries the real digest while naming a different
#      catalog_document_version.
#   3. The abbreviated digest in README.md still matches the real one.
#
# Rule 1 is deliberately a whitelist rather than a "looks like a digest" check:
# a hand-edited fixture with a plausible but invented fingerprint is exactly
# the failure this is meant to catch.
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
examples = root / "contracts/agent-router/examples"
readme = root / "contracts/agent-router/README.md"

catalog = yaml.safe_load(catalog_path.read_text())["data"]["catalog.yaml"]
real = "sha256:" + hashlib.sha256(catalog.encode()).hexdigest()
document_version = str(yaml.safe_load(catalog)["version"])

# Documented placeholders. Each marks a DIFFERENT hypothetical catalog; see the
# digest table in README.md. They are obviously fabricated on purpose.
placeholders = {"sha256:" + word * 8 for word in ("decafbad", "f00dface", "deadbeef")}

failures = []

for path in sorted(examples.rglob("*.json")):
    text = path.read_text()
    rel = path.relative_to(root)
    for digest in sorted(set(re.findall(r"sha256:[0-9a-f]{64}", text))):
        if digest == real or digest in placeholders:
            continue
        failures.append(
            f"{rel}\n"
            f"    carries   {digest}\n"
            f"    committed {real}\n"
            f"    ...and it is not one of the documented placeholders either."
        )
    claimed = json.loads(text).get("catalog_document_version")
    if claimed is not None and real in text and claimed != document_version:
        failures.append(
            f"{rel}\n"
            f"    carries the real digest but claims catalog {claimed},\n"
            f"    while the committed catalog is {document_version}."
        )

short = real[7:15] + "…" + real[-6:]
if short not in readme.read_text():
    failures.append(
        f"contracts/agent-router/README.md\n"
        f"    the digest table does not mention sha256:{short},\n"
        f"    which is the committed catalog's abbreviated digest."
    )

print(f"catalog {document_version}, digest {real}")
if failures:
    print("")
    print("Examples disagree with the committed catalog:")
    print("")
    for failure in failures:
        print(f"  {failure}")
        print("")
    print("Re-stamp the affected examples, or give an illustrative one a")
    print("documented placeholder digest. A fingerprint that names the wrong")
    print("catalog is worse than none.")
    sys.exit(1)

print("every example agrees with the catalog it claims")
PY
