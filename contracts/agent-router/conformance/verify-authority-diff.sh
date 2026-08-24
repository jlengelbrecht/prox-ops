#!/usr/bin/env bash
# Verify that authority-diff.sh still classifies each rule family the way
# AUTHORITY-DIFF.md says it does.
#
# WHY THIS EXISTS
#
# The taxonomy in AUTHORITY-DIFF.md is a set of judgement calls - which fields
# carry authority, which way `null` points for each family, whether declaring a
# non-admitting entry grants anything - and judgement calls rot silently. A
# rule table is one dictionary edit away from classifying `forbidden_for` as a
# grant, and the classifier would still run, still print a verdict, and still
# exit 0. Nothing else in this repository would notice.
#
# So every case below asserts a verdict in BOTH directions: forward is the
# fixture's edit applied, reverse is the same edit undone. Direction matters -
# most families invert (a grant added is a grant removed the other way round)
# and two deliberately do not: a mixed change is expanding whichever way it is
# read, and so is an unrecognized one. Asserting only the forward direction
# would let a rule that ignores direction entirely pass every case, and would
# leave the removal half of every add/remove rule untested.
#
# The suite is deliberately fixture-only: it never reads the live catalog, so
# it cannot start passing or failing because somebody edited the cluster's
# catalog, and it runs on a laptop with no cluster access at all.
#
# Cost: two classifier processes per case, each a python3 start plus a PyYAML
# import - a few seconds in total, inside the 10 s budget. If this suite ever
# grows past that, batch the runs rather than dropping the reverse direction;
# the reverse direction is where the rules are actually pinned.
#
# Usage: bash verify-authority-diff.sh   (exit 0 = taxonomy intact)
set -euo pipefail
cd "$(dirname "$0")"

CLS=./authority-diff.sh
FIX=fixtures/authority-diff

[[ -r $CLS ]] || { echo "FAIL  no classifier at $CLS"; exit 1; }
[[ -d $FIX ]] || { echo "FAIL  no fixture directory at $FIX"; exit 1; }

# name | old | new | forward | reverse | what the case pins
CASES=(
  "identical|base.yaml|base.yaml|neutral|neutral|an unchanged catalog moves no authority"
  "envelope|envelope-configmap.yaml|envelope-plain.yaml|neutral|neutral|the ConfigMap around a catalog is not catalog data"
  "documentary|base.yaml|documentary-new.yaml|neutral|neutral|version/updated/prose/estimates carry no authority"
  "prefer-order-reorder|base.yaml|prefer-order-reorder-new.yaml|neutral|neutral|order is preference, not permission"
  "declaration-only|base.yaml|declaration-only-new.yaml|neutral|neutral|an entry added (fwd) or removed (rev) behind status: planned + selectable: false grants nothing"
  "planned-entry|base.yaml|planned-entry-new.yaml|neutral|neutral|status gates the entry rule on its own: entry added (fwd) / removed (rev) at status: planned, selectable: TRUE"
  "selectable-enable|base.yaml|selectable-enable-new.yaml|expanding|narrowing|the selectable flip, both ways"
  "forbidden-add|base.yaml|forbidden-add-new.yaml|narrowing|expanding|a forbidden_for tag subtracts, both ways"
  "placement-add|base.yaml|placement-add-new.yaml|expanding|narrowing|a selectable placement joins the approved set"
  "physical-add|base.yaml|physical-add-new.yaml|expanding|narrowing|a physical candidate is a grant"
  "capability-raise|base.yaml|capability-raise-new.yaml|expanding|narrowing|a bigger guarantee satisfies more requests"
  "constraint-raise|base.yaml|constraint-raise-new.yaml|narrowing|expanding|a higher hard floor admits fewer candidates"
  "entitlement-add|base.yaml|entitlement-add-new.yaml|expanding|narrowing|a funding candidate is a way to run"
  "spillover-open|base.yaml|spillover-open-new.yaml|expanding|narrowing|invariant 4: 'none' is the closed value"
  "status-withdraw|base.yaml|status-withdraw-new.yaml|narrowing|expanding|'available' is the admitting status"
  "mixed|base.yaml|mixed-new.yaml|expanding|expanding|any expansion dominates, in either direction"
  "unknown-key|base.yaml|unknown-key-new.yaml|expanding|expanding|an unrecognized field is assumed to grant"
  "unknown-empty|base.yaml|unknown-empty-new.yaml|expanding|expanding|an unrecognized KEY added (fwd) / removed (rev) is surface even when its value is an empty container"
)

# Classes that must stay covered. Deleting a case is allowed; deleting one of
# these is a taxonomy change and has to be argued in AUTHORITY-DIFF.md first.
REQUIRED=(
  identical selectable-enable forbidden-add            # the PM-frozen semantics
  placement-add physical-add prefer-order-reorder      # the catalog's own shapes
  planned-entry                                        # status gates the entry rule
  mixed unknown-key unknown-empty                      # dominance and the default
)

VERDICT_LINE='^verdict: (expanding|narrowing|neutral)$'

fail=0
used=""

verdict_of() {  # <old> <new> -> the verdict on stdout; non-zero on tool trouble
  local out count
  if ! out="$(bash "$CLS" "$1" "$2" 2>&1)"; then
    printf '        classifier exited non-zero:\n%s\n' "$out" >&2
    return 1
  fi
  count="$(grep -cE "$VERDICT_LINE" <<<"$out" || true)"
  if [[ "$count" != "1" ]]; then
    printf '        expected exactly one verdict line, found %s:\n%s\n' \
           "$count" "$out" >&2
    return 1
  fi
  grep -oE "$VERDICT_LINE" <<<"$out" | cut -d' ' -f2
}

printf -- '-- %s case(s), each asserted in both directions --\n' "${#CASES[@]}"
for spec in "${CASES[@]}"; do
  IFS='|' read -r name old new want_fwd want_rev why <<<"$spec"
  used="$used $old $new"
  oldf="$FIX/$old"
  newf="$FIX/$new"
  if [[ ! -r $oldf || ! -r $newf ]]; then
    printf 'FAIL  %-22s missing fixture: %s or %s\n' "$name" "$oldf" "$newf"
    fail=$((fail + 1))
    continue
  fi
  got_fwd="$(verdict_of "$oldf" "$newf")" || got_fwd="<tool-error>"
  got_rev="$(verdict_of "$newf" "$oldf")" || got_rev="<tool-error>"
  if [[ $got_fwd == "$want_fwd" && $got_rev == "$want_rev" ]]; then
    printf 'ok    %-22s %-10s %-10s %s\n' "$name" "$got_fwd" "$got_rev" "$why"
  else
    printf 'FAIL  %-22s %-10s %-10s want %s / %s\n' \
           "$name" "$got_fwd" "$got_rev" "$want_fwd" "$want_rev"
    fail=$((fail + 1))
  fi
done

expect_error() {  # <label> [args...] - the classifier must refuse, silently
  local label="$1" out
  shift
  if out="$(bash "$CLS" "$@" 2>&1)"; then
    printf 'FAIL  %-22s expected a non-zero exit, got:\n%s\n' "$label" "$out"
    return 1
  fi
  if grep -q '^verdict: ' <<<"$out"; then
    printf 'FAIL  %-22s a tool error printed a verdict:\n%s\n' "$label" "$out"
    return 1
  fi
  printf 'ok    %-22s refused, no verdict\n' "$label"
}

printf -- '\n-- a tool error is an exit, never a verdict --\n'
expect_error "no arguments" || fail=$((fail + 1))
expect_error "one argument" "$FIX/base.yaml" || fail=$((fail + 1))
expect_error "missing file" "$FIX/base.yaml" "$FIX/nope.yaml" || fail=$((fail + 1))
expect_error "unparsable input" "$FIX/base.yaml" "$FIX/broken.yaml" || fail=$((fail + 1))
used="$used broken.yaml"

printf -- '\n-- every committed fixture is exercised, every class is covered --\n'
for path in "$FIX"/*.yaml; do
  name="${path##*/}"
  case " $used " in
    *" $name "*) ;;
    *)
      printf 'FAIL  %s is committed but no case reads it - a fixture nothing asserts is decoration\n' "$name"
      fail=$((fail + 1))
      ;;
  esac
done
for want in "${REQUIRED[@]}"; do
  found=0
  for spec in "${CASES[@]}"; do
    if [[ ${spec%%|*} == "$want" ]]; then found=1; fi
  done
  if [[ $found -eq 0 ]]; then
    printf 'FAIL  required class %s has no case - see AUTHORITY-DIFF.md\n' "$want"
    fail=$((fail + 1))
  fi
done
printf 'ok    %s fixture(s) present, %s required class(es) covered\n' \
       "$(ls -1 "$FIX"/*.yaml | wc -l)" "${#REQUIRED[@]}"

echo
if [[ $fail -ne 0 ]]; then
  printf '%s check(s) failed: authority-diff.sh no longer implements the taxonomy in AUTHORITY-DIFF.md.\n' "$fail"
  printf 'Changing a verdict is allowed - argue it in AUTHORITY-DIFF.md and move the case in the same change.\n'
  exit 1
fi
printf 'authority-diff taxonomy intact: %s case(s) in both directions, 4 tool-error paths.\n' "${#CASES[@]}"
