#!/usr/bin/env bash
# Run in an operator terminal with a personal 1Password CLI session and Homelab0 access.
set +x
set -euo pipefail

if [[ $# != 1 || ! -t 0 ]]; then
  printf 'Usage in an interactive terminal: %s <Homelab0 kube context>\n' "$0" >&2
  exit 2
fi

context=$1
namespace=mcp
server=https://10.20.67.20:6443
secret=onepassword-connect-ai-access-credentials
kubectl_cmd=(kubectl --context="$context")

if ! "${kubectl_cmd[@]}" config view --minify -o json 2>/dev/null |
  jq -e --arg context "$context" --arg server "$server" '
    .contexts[0].name == $context and
    .clusters[0].cluster.server == $server and
    .clusters[0].cluster["insecure-skip-tls-verify"] != true
  ' >/dev/null 2>&1; then
  printf 'The selected context must point to Homelab0 and verify its TLS certificate.\n' >&2
  exit 1
fi
if ! "${kubectl_cmd[@]}" get namespace "$namespace" -o name >/dev/null 2>&1; then
  printf 'Cannot verify authenticated access to the mcp namespace.\n' >&2
  exit 1
fi

printf 'Load open-connector Credential File from Private into %s/%s.\n' "$namespace" "$secret"
if ! read -r -p 'Type the kube context to continue: ' confirmation || [[ $confirmation != "$context" ]]; then
  exit 1
fi

# Chart 2.1.1 expects OP_SESSION to be the base64 of the JSON.
# The Kubernetes Secret data encoding is a separate layer.
if ! credentials=$(
  env -u OP_SERVICE_ACCOUNT_TOKEN -u OP_CONNECT_HOST -u OP_CONNECT_TOKEN \
    op document get 'open-connector Credential File' --vault Private 2>/dev/null |
    jq -ce 'select(type == "object" and has("verifier") and has("encCredentials")
      and has("version") and has("deviceUuid") and has("uniqueKey"))' 2>/dev/null |
    base64 | tr -d '\n'
); then
  printf 'Could not read a valid Connect credentials document from the signed-in 1Password CLI.\n' >&2
  exit 1
fi

if ! printf '%s' "$credentials" |
  "${kubectl_cmd[@]}" -n "$namespace" create secret generic "$secret" \
    --from-file=1password-credentials.json=/dev/stdin --dry-run=client -o json 2>/dev/null |
  "${kubectl_cmd[@]}" -n "$namespace" apply --server-side \
    --field-manager=homelab-ai-access-connect-bootstrap -f - >/dev/null 2>&1; then
  printf 'Kubernetes did not accept the credentials Secret. Check privileges and field ownership.\n' >&2
  exit 1
fi
unset credentials
printf 'Applied %s/%s.\n' "$namespace" "$secret"
