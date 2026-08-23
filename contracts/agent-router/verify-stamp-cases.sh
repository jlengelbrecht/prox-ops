#!/usr/bin/env bash
# STORY-035-12. Builds agent-stamp-validate from the current source tree and
# runs it against the committed fixtures in examples/stamp/, asserting the
# exact exit code and the exact reason_code of every named check the story
# freezes. This is the NORMATIVE gate 2/6 verifier: it is wired into
# .github/workflows/contract-checks.yaml exactly like verify-digests.sh and
# verify-placement-cases.sh.
#
# WHY A BUILT BINARY, NOT A UNIT TEST. Gate 4 requires a smoke-run against
# the BUILT ARTIFACT of the same commit - proving the CLI seam actually
# works end to end (flags, JSON decode, exit codes), not only that the pure
# Go function behind it does. `go test ./internal/stampvalidate/...` already
# proves the pure-function semantics in depth (16 named acceptance cases,
# under -race); this script proves the CLI wrapper faithfully exposes them.
#
# TIME-SENSITIVE FIXTURES. agent-stamp-validate reads the real wall clock
# (it is the only place in this whole feature that does - ValidateFinal
# itself never does). A "placement evidence is fresh" fixture therefore
# cannot be a static file with a fixed resolved_at: by the time this script
# runs, any fixed timestamp is either always-expired or, worse,
# unpredictably fresh depending on when CI happens to run. So:
#   - "expired" fixtures use a fixed resolved_at far in the past
#     (2000-01-01) with ttl_seconds 30 - deterministically expired no matter
#     when this script runs. These ARE static committed files.
#   - "fresh" fixtures are generated HERE, at run time, with resolved_at a
#     few seconds before the moment this script invokes the binary - the
#     same reason verify-placement-cases.sh computes some of its inputs in
#     Python rather than committing a value that would drift.
#   - every stamp fixture's expires_at is pinned to 2099-01-01, so passage
#     of real time between authoring this script and running it never flips
#     the not_expired check by accident.
#
# SYNTHETIC CATALOG. The real, committed catalog 1.3.0 has no
# approved-pair profile with a non-empty forbidden_for (local-unrestricted
# has one but is not an approved pair; every approved-pair profile's
# forbidden_for is empty). forbidden-override.stamp.json needs one to exist,
# so this script derives a SYNTHETIC catalog from the real committed
# ConfigMap - unmodified except that local-code-standard.forbidden_for gains
# ["security"] - rather than hand-maintaining a second full catalog
# document that could silently drift from the real one.
#
# Usage: ./verify-stamp-cases.sh   (exit 0 = every case matched its expected verdict)
set -euo pipefail
cd "$(dirname "$0")"

ROOT="$(cd ../.. && pwd)"
MODULE="$ROOT/services/agent-router"
FIXTURES="$(pwd)/examples/stamp"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== building agent-stamp-validate from $MODULE =="
BIN="$WORK/agent-stamp-validate"
(cd "$MODULE" && go build -o "$BIN" ./cmd/agent-stamp-validate)

echo "== extracting the real committed catalog =="
REAL_CATALOG="$WORK/real-catalog.yaml"
python3 - "$ROOT/kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml" "$REAL_CATALOG" <<'PY'
import sys, yaml
cm = yaml.safe_load(open(sys.argv[1]))
open(sys.argv[2], "w").write(cm["data"]["catalog.yaml"])
PY

echo "== deriving the synthetic forbidden-pair catalog (real catalog + local-code-standard.forbidden_for) =="
SYNTHETIC_CATALOG="$WORK/synthetic-forbidden-pair-catalog.yaml"
python3 - "$REAL_CATALOG" "$SYNTHETIC_CATALOG" <<'PY'
import sys, yaml
cat = yaml.safe_load(open(sys.argv[1]))
cat["profiles"]["local-code-standard"]["forbidden_for"] = ["security"]
yaml.safe_dump(cat, open(sys.argv[2], "w"), sort_keys=False)
PY

echo "== generating the time-sensitive fresh-evidence fixtures =="
FRESH_EVIDENCE="$WORK/fresh.evidence.json"
python3 - "$FRESH_EVIDENCE" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

resolved_at = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
doc = {
    "resolved_at": resolved_at,
    "result": {
        "status": "placed",
        "model": "qwen36-27b",
        "placement": "cachyos-7900xtx",
        "readiness": "warm",
        "estimated_cold_start_s": 0,
        "headers": {"x-placement": "cachyos-7900xtx"},
        "ttl_seconds": 30,
        "reason": {"code": "placed_warm", "message": "generated fresh by verify-stamp-cases.sh"},
        "alternatives": [],
        "catalog_version": "sha256:2fd681e0e988bf6be94b8923b1485a0aa174a4e66aec580a2d383c764b60e229",
    },
}
json.dump(doc, open(sys.argv[1], "w"))
PY

failed=0
run_case() {
  # run_case <name> <catalog> <stamp> <evidence-or-empty> <want-exit>
  local name="$1" catalog="$2" stamp="$3" evidence="$4" want_exit="$5"
  local args=(--catalog "$catalog" --stamp "$stamp")
  if [[ -n "$evidence" ]]; then
    args+=(--evidence "$evidence")
  fi
  local out rc
  set +e
  out="$("$BIN" "${args[@]}" 2>"$WORK/stderr")"
  rc=$?
  set -e
  if [[ "$rc" != "$want_exit" ]]; then
    echo "FAIL  $name: exit code = $rc, want $want_exit"
    echo "      stderr: $(cat "$WORK/stderr")"
    failed=$((failed + 1))
    return
  fi
  echo "$out" > "$WORK/last-verdict.json"
  echo "ok    $name (exit $rc)"
}

check_reason() {
  # check_reason <name> <check-name> <want-passed:true|false> <want-reason-code>
  # Guarded with if/! rather than a bare statement: under `set -e` a bare
  # command's non-zero exit would abort the whole script on the FIRST
  # mismatch instead of being tallied into $failed like every other case.
  local name="$1" check="$2" want_passed="$3" want_reason="$4"
  if ! python3 - "$WORK/last-verdict.json" "$check" "$want_passed" "$want_reason" <<'PY'
import json, sys
verdict = json.load(open(sys.argv[1]))
check_name, want_passed, want_reason = sys.argv[2], sys.argv[3] == "true", sys.argv[4]
matches = [c for c in verdict["checks"] if c["name"] == check_name]
if not matches:
    print(f"FAIL  no check named {check_name!r} in verdict")
    sys.exit(1)
c = matches[0]
if c["passed"] != want_passed or c["reason_code"] != want_reason:
    print(f"FAIL  check {check_name!r} = {c}, want passed={want_passed} reason_code={want_reason!r}")
    sys.exit(1)
PY
  then
    failed=$((failed + 1))
  else
    echo "ok    $name: $check == $want_reason (passed=$want_passed)"
  fi
}

# --- case: valid_router_stamp_passes ---
run_case valid_router_stamp_passes "$REAL_CATALOG" "$FIXTURES/valid-router-stamp.stamp.json" "" 0

# --- case: valid_override_passes ---
run_case valid_override_passes "$REAL_CATALOG" "$FIXTURES/valid-override.stamp.json" "" 0

# --- case: invalid_pairing_fails ---
run_case invalid_pairing_fails "$REAL_CATALOG" "$FIXTURES/invalid-pairing.stamp.json" "" 1
check_reason invalid_pairing_fails approved_pair false pair_not_approved

# --- case: stale_catalog_fails_closed ---
run_case stale_catalog_fails_closed "$REAL_CATALOG" "$FIXTURES/stale-catalog.stamp.json" "" 1
check_reason stale_catalog_fails_closed catalog_drift_fails_closed false catalog_version_stale
check_reason stale_catalog_fails_closed approved_pair true ok

# --- case: local_placement_missing_fails ---
run_case local_placement_missing_fails "$REAL_CATALOG" "$FIXTURES/local-placement-missing.stamp.json" "" 1
check_reason local_placement_missing_fails placement false placement_evidence_missing

# --- case: local_placement_fresh_passes ---
run_case local_placement_fresh_passes "$REAL_CATALOG" "$FIXTURES/local-placement.stamp.json" "$FRESH_EVIDENCE" 0
check_reason local_placement_fresh_passes placement true placed_and_fresh

# --- case: placement_evidence_expired_fails ---
run_case placement_evidence_expired_fails "$REAL_CATALOG" "$FIXTURES/local-placement.stamp.json" "$FIXTURES/local-placement-expired.evidence.json" 1
check_reason placement_evidence_expired_fails placement false placement_evidence_expired

# --- case: reuse_stamp_fresh_evidence_passes (SAME stamp as the expired case above, only the evidence changes) ---
run_case reuse_stamp_fresh_evidence_passes "$REAL_CATALOG" "$FIXTURES/local-placement.stamp.json" "$FRESH_EVIDENCE" 0
check_reason reuse_stamp_fresh_evidence_passes placement true placed_and_fresh

# --- case: vendor_fabricated_placement_fails (a vendor stamp with ANY placement evidence attached) ---
run_case vendor_fabricated_placement_fails "$REAL_CATALOG" "$FIXTURES/valid-router-stamp.stamp.json" "$FIXTURES/local-placement-expired.evidence.json" 1
check_reason vendor_fabricated_placement_fails placement false vendor_profile_with_placement

# --- case: expires_at_not_reset (expires_at in the past; created_at looks recent and must not rescue it) ---
run_case expires_at_not_reset "$REAL_CATALOG" "$FIXTURES/expires-at-not-reset.stamp.json" "" 1
check_reason expires_at_not_reset not_expired false stamp_expired

# --- case: unauthorized_metered_fails / metered_cannot_self_authorize (the MVP CLI supplies no authority, ever) ---
run_case metered_cannot_self_authorize "$REAL_CATALOG" "$FIXTURES/metered-intent-only.stamp.json" "" 1
check_reason metered_cannot_self_authorize metered_dual_key false metered_authority_missing

# --- case: forbidden_override_fails (SYNTHETIC catalog: local-code-standard given a forbidden_for) ---
run_case forbidden_override_fails "$SYNTHETIC_CATALOG" "$FIXTURES/forbidden-override.stamp.json" "" 1
check_reason forbidden_override_fails forbidden_for false forbidden_for_tag

# --- case: determinism (shell-level smoke check; internal/stampvalidate's
# Test_determinism proves this exhaustively under -race with concurrent
# goroutines - this only proves the CLI wrapper does not introduce its own
# nondeterminism, e.g. map iteration leaking into JSON key order) ---
out1="$("$BIN" --catalog "$REAL_CATALOG" --stamp "$FIXTURES/valid-router-stamp.stamp.json")"
out2="$("$BIN" --catalog "$REAL_CATALOG" --stamp "$FIXTURES/valid-router-stamp.stamp.json")"
if [[ "$out1" != "$out2" ]]; then
  echo "FAIL  determinism: two runs against the identical fixture produced different stdout"
  failed=$((failed + 1))
else
  echo "ok    determinism: two runs against the identical fixture produced byte-identical stdout"
fi

# --- case: failure_before_launch (malformed input exits 2 with empty stdout, no verdict printed) ---
BAD_STAMP="$WORK/malformed.stamp.json"
echo '{not valid json' > "$BAD_STAMP"
set +e
bad_out="$("$BIN" --catalog "$REAL_CATALOG" --stamp "$BAD_STAMP" 2>"$WORK/stderr")"
bad_rc=$?
set -e
if [[ "$bad_rc" != 2 || -n "$bad_out" ]]; then
  echo "FAIL  failure_before_launch: malformed input gave exit=$bad_rc stdout=$bad_out, want exit=2 and empty stdout"
  failed=$((failed + 1))
else
  echo "ok    failure_before_launch: malformed input exits 2 with no verdict printed"
fi

# --- gate 5: boundary greps. The validator must be provably unable to
# launch anything - no os/exec, no process/session/worktree vocabulary, no
# Kubernetes client, no HTTP client/server, no runtime.endpoint - across
# BOTH internal/stampvalidate and cmd/agent-stamp-validate, source and
# tests alike.
#
# Scoped to CODE, not prose (the same scoping ADE-BOUNDARY.md's own denylist
# uses: "this applies to names, never to prose"): full-line comments are
# stripped before matching, so a doc comment that EXPLAINS the boundary (as
# this very package's doc comments do, extensively) does not trip the gate
# that documents it. ---
echo "== boundary greps: internal/stampvalidate + cmd/agent-stamp-validate =="
BOUNDARY_DIRS=("$MODULE/internal/stampvalidate" "$MODULE/cmd/agent-stamp-validate")
BOUNDARY_PATTERNS=(
  '"os/exec"'
  '"net/http"'
  'net\.Dial'
  'k8s\.io/client-go'
  'runtime\.endpoint'
  '\bsession\b'
  '\bworktree\b'
  '\bwebhook\b'
)
boundary_hit=0
while IFS= read -r -d '' f; do
  code_only="$(grep -vE '^[[:space:]]*//' "$f")"
  for pattern in "${BOUNDARY_PATTERNS[@]}"; do
    if grep -qE "$pattern" <<<"$code_only"; then
      echo "FAIL  boundary grep: pattern '$pattern' matched in $f (outside comments)"
      boundary_hit=1
    fi
  done
done < <(find "${BOUNDARY_DIRS[@]}" -name '*.go' -print0)
if [[ "$boundary_hit" -ne 0 ]]; then
  failed=$((failed + 1))
else
  echo "ok    boundary greps: no forbidden vocabulary found outside comments"
fi

# --- gate 7: pair authority reuse. stampvalidate's PRODUCTION code (not its
# tests, which deliberately construct one routing.Pair literal to PROVE the
# live-table mutation case) must import routing and read
# routing.ApprovedPairs live - never restate a second pair matrix. ---
echo "== gate 7: pair-authority reuse, no duplicated matrix =="
if ! grep -q 'routing\.ApprovedPairs' "$MODULE/internal/stampvalidate/validate.go"; then
  echo "FAIL  internal/stampvalidate/validate.go does not reference routing.ApprovedPairs"
  failed=$((failed + 1))
else
  matrix_hit=0
  while IFS= read -r -d '' f; do
    if grep -nE '\{[[:space:]]*Harness:[[:space:]]*"[a-z]+",[[:space:]]*ModelProfile:[[:space:]]*"[a-zA-Z/_-]+"' "$f"; then
      echo "FAIL  a literal {Harness: ..., ModelProfile: ...} pair matrix appears in $f"
      matrix_hit=1
    fi
  done < <(find "$MODULE/internal/stampvalidate" -name '*.go' ! -name '*_test.go' -print0)
  if [[ "$matrix_hit" -ne 0 ]]; then
    failed=$((failed + 1))
  else
    echo "ok    stampvalidate reuses routing.ApprovedPairs directly; no literal pair matrix found in production code"
  fi
fi

if [[ "$failed" -gt 0 ]]; then
  echo
  echo "$failed check(s) failed."
  exit 1
fi

echo
echo "All stamp-validation cases and boundary gates passed."
