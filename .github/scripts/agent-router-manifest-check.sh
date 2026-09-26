#!/usr/bin/env bash
# Check that clients/agent-router/manifest.json agrees with the catalog it
# names, and with the ConfigMap the router actually loads. Runs offline with
# no credentials; used by .github/workflows/spektr-upgrade-check.yaml.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
MANIFEST="$ROOT/clients/agent-router/manifest.json"
FAILED=0
ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILED=1; }

echo "agent-router client manifest check"

schema="$(jq -r '.schema' "$MANIFEST")"
if [ "$schema" = "prox-ops/agent-router-client/v1" ]; then ok "schema $schema"; else fail "unknown schema: $schema"; fi

path="$(jq -r '.catalog.path' "$MANIFEST")"
want="$(jq -r '.catalog.digest' "$MANIFEST")"
if [ ! -f "$ROOT/$path" ]; then
  fail "catalog.path does not exist: $path"
else
  got="sha256:$(sha256sum "$ROOT/$path" | cut -d' ' -f1)"
  if [ "$got" = "$want" ]; then ok "catalog digest $got"; else fail "catalog.digest is $want but $path hashes to $got"; fi

  ver="$(yq -r '.version' "$ROOT/$path")"
  doc="$(jq -r '.catalog.document_version' "$MANIFEST")"
  if [ "$ver" = "$doc" ]; then ok "document_version $ver"; else fail "catalog.document_version is $doc but the file says $ver"; fi

  # The router loads the rendered ConfigMap, not the file, so the generator
  # must hand it exactly these bytes.
  rendered="sha256:$(kustomize build "$ROOT/$(dirname "$path")" \
    | yq -r 'select(.kind == "ConfigMap" and .metadata.name == "agent-router-catalog") | .data["catalog.yaml"]' \
    | head -c -1 | sha256sum | cut -d' ' -f1)"
  if [ "$rendered" = "$want" ]; then
    ok "rendered ConfigMap carries the same bytes"
  else
    fail "rendered ConfigMap value hashes to $rendered, not $want"
  fi
fi

min="$(jq -r '.validator.min_version' "$MANIFEST")"
rec="$(jq -r '.validator.recommended_version' "$MANIFEST")"
max="$(jq -r '.validator.max_version_exclusive' "$MANIFEST")"
lowest="$(printf '%s\n' "$min" "$rec" | sort -V | head -1)"
highest="$(printf '%s\n' "$rec" "$max" | sort -V | tail -1)"
if [ "$lowest" = "$min" ] && [ "$highest" = "$max" ] && [ "$rec" != "$max" ]; then
  ok "validator window $min <= $rec < $max"
else
  fail "validator window is inconsistent: min $min, recommended $rec, max (exclusive) $max"
fi

art_ok=1
while IFS=$'\t' read -r platform file sum; do
  case "$file" in
    *"_${rec}_"*) ;;
    *) fail "$platform artifact $file is not the recommended $rec"; art_ok=0 ;;
  esac
  [[ "$sum" =~ ^[0-9a-f]{64}$ ]] || { fail "$platform sha256 is not a sha256: $sum"; art_ok=0; }
done < <(jq -r '.validator.artifacts | to_entries[] | [.key, .value.file, .value.sha256] | @tsv' "$MANIFEST")
[ "$art_ok" -eq 1 ] && ok "validator artifacts name $rec"

if [ "$FAILED" -ne 0 ]; then
  echo "RESULT: manifest and catalog disagree"
  exit 1
fi
echo "RESULT: manifest is consistent"
