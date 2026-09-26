#!/usr/bin/env bash
# Spektr upgrade check: can this cluster take a given agent-router release?
#
# Spektr (ai-control-plane) is an upstream product we consume; it no longer
# tells us before a release needs changes on our side. So before a release is
# merged, this script checks it against OUR deployment:
#
#   1. the release's tarballs match its SHA256SUMS;
#   2. the release body carries no "CONSUMER ACTION REQUIRED" section;
#   3. that release's catalog-validate accepts our catalog with no warnings.
#      Warnings count as failures: v0.3.0 only WARNS that a catalog without
#      routing_pairs "will refuse every request", and exits 0;
#   4. that release's agent-stamp-validate accepts a claude/strong stamp
#      against our catalog (the approved_pair check is where v0.3.0 bites);
#   5. the release's deploy base renders with the components and patches from
#      our agent-router ks.yaml, and the result passes kubeconform.
#
# Usage: spektr-upgrade-check.sh --tag <vX.Y.Z[-rc.N]> [--catalog-configmap <path>]
# Needs gh (authenticated with read access to the release), yq, jq, kustomize,
# kubeconform. Runs the same locally and in .github/workflows/spektr-upgrade-check.yaml.
set -euo pipefail

REPO="jlengelbrecht/ai-control-plane"
ROOT="$(git rev-parse --show-toplevel)"
CATALOG_CM="$ROOT/kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml"
KS="$ROOT/kubernetes/apps/ai/agent-router/app/ks.yaml"
STAMP_TEMPLATE="$ROOT/.github/scripts/testdata/spektr-stamp.template.json"
TAG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --catalog-configmap) CATALOG_CM="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$TAG" ] || { echo "usage: $0 --tag <release tag> [--catalog-configmap <path>]" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
FAILED=0
ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILED=1; }

echo "Spektr upgrade check: $TAG"

# 1. Download and verify. Only the linux/amd64 verifiers and the deploy base.
gh release download "$TAG" -R "$REPO" -D "$WORK" \
  -p "catalog-validate_${TAG}_linux_amd64.tar.gz" \
  -p "agent-stamp-validate_${TAG}_linux_amd64.tar.gz" \
  -p "deploy-base_${TAG}.tar.gz" \
  -p SHA256SUMS
if (cd "$WORK" && sha256sum -c --ignore-missing --quiet SHA256SUMS); then
  ok "release tarballs match SHA256SUMS"
else
  fail "release tarballs do not match SHA256SUMS"
  exit 1
fi
mkdir -p "$WORK/bin" "$WORK/render"
tar xzf "$WORK/catalog-validate_${TAG}_linux_amd64.tar.gz" -C "$WORK/bin"
tar xzf "$WORK/agent-stamp-validate_${TAG}_linux_amd64.tar.gz" -C "$WORK/bin"
tar xzf "$WORK/deploy-base_${TAG}.tar.gz" -C "$WORK/render"

# 2. Consumer action flagged by the release itself.
gh release view "$TAG" -R "$REPO" --json body --jq .body > "$WORK/release-body.md"
if grep -qi "consumer action required" "$WORK/release-body.md"; then
  fail "release notes flag CONSUMER ACTION REQUIRED - read them before merging:"
  grep -i -A15 "consumer action required" "$WORK/release-body.md" | sed 's/^/        /'
else
  ok "release notes flag no consumer action"
fi

# 3. Our catalog, extracted byte-exactly (yq adds a trailing newline).
yq -r '.data["catalog.yaml"]' "$CATALOG_CM" | head -c -1 > "$WORK/catalog.yaml"
DIGEST="sha256:$(sha256sum "$WORK/catalog.yaml" | cut -d' ' -f1)"
echo "  catalog $(yq -r '.version' "$WORK/catalog.yaml") $DIGEST"
if "$WORK/bin/catalog-validate" -catalog "$WORK/catalog.yaml" > "$WORK/cv.json" 2> "$WORK/cv.err" \
   && [ "$(jq -r .valid "$WORK/cv.json")" = "true" ]; then
  if grep -qi "warning" "$WORK/cv.err"; then
    fail "catalog-validate $TAG warns about our catalog:"
    sed 's/^/        /' "$WORK/cv.err"
  else
    ok "catalog-validate $TAG accepts our catalog"
  fi
else
  fail "catalog-validate $TAG rejects our catalog:"
  jq -r '.checks[]? | select(.passed == false) | "        \(.name): \(.reason_code) - \(.message)"' "$WORK/cv.json" 2>/dev/null || true
  sed 's/^/        /' "$WORK/cv.err"
fi

# 4. A stamp the router would issue today, checked by that release's validator.
EXPIRES="$(date -u -d '+1 hour' +%Y-%m-%dT%H:%M:%SZ)"
CREATED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
jq --arg d "$DIGEST" --arg v "$(yq -r '.version' "$WORK/catalog.yaml")" --arg e "$EXPIRES" --arg c "$CREATED" \
  '.catalog_version = $d | .catalog_document_version = $v | .expires_at = $e | .created_at = $c' \
  "$STAMP_TEMPLATE" > "$WORK/stamp.json"
if "$WORK/bin/agent-stamp-validate" -catalog "$WORK/catalog.yaml" -stamp "$WORK/stamp.json" > "$WORK/sv.json" 2> "$WORK/sv.err"; then
  ok "agent-stamp-validate $TAG accepts a claude/strong stamp"
else
  fail "agent-stamp-validate $TAG rejects a claude/strong stamp:"
  jq -r '.checks[]? | select(.passed == false) | "        \(.name): \(.reason_code)"' "$WORK/sv.json" 2>/dev/null || true
  sed 's/^/        /' "$WORK/sv.err"
fi

# 5. Render the base the way Flux does: our ks.yaml's components and patches,
#    read from the file so they can't drift from what the cluster applies.
{
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "namespace: ai"
  echo "resources: [./base]"
  yq -o yaml '{"components": [.spec.components[] | "./base/" + sub("^\./"; "")]}' "$KS"
  yq -o yaml '{"patches": .spec.patches}' "$KS"
} > "$WORK/render/kustomization.yaml"
if kustomize build "$WORK/render" > "$WORK/rendered.yaml" 2> "$WORK/render.err"; then
  if kubeconform -strict -ignore-missing-schemas -summary "$WORK/rendered.yaml" > "$WORK/kc.out" 2>&1; then
    ok "base renders with our patches and passes kubeconform ($(tail -1 "$WORK/kc.out"))"
  else
    fail "rendered base fails kubeconform:"
    sed 's/^/        /' "$WORK/kc.out"
  fi
else
  fail "base does not render with our ks.yaml patches:"
  sed 's/^/        /' "$WORK/render.err"
fi

if [ "$FAILED" -ne 0 ]; then
  echo "RESULT: $TAG is NOT safe to take as-is"
  exit 1
fi
echo "RESULT: $TAG passes every check"
