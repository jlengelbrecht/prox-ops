#!/usr/bin/env bash
# Break glass: a new OpenBao root token from the recovery key alone, for when
# no admin can log in (authentik or Entra down, every admin token expired).
#
# The bao CLI's generate-root needs a token since 2.5.3, so this calls the
# unauthenticated sys/generate-root endpoints directly. They are enabled only
# on the server's loopback listener (127.0.0.1:8210, see the HelmRelease), so
# the script runs inside a server pod through kubectl exec. The recovery key
# is read there with a hidden prompt; it never reaches this machine's command
# line or shell history.
#
# Usage:
#   scripts/openbao-break-glass.sh          print a root token; revoke it when done
#   scripts/openbao-break-glass.sh --test   mint one, check it is root, revoke it
#                                           at once and print nothing secret (drills)
#
# A printed root token never expires on its own. Revoke it when you are done:
# bao token revoke -self with that token, or Revoke token in the UI's user menu.
set -euo pipefail

NS=openbao
POD=${POD:-openbao-0}
CONTAINER=openbao

mode=print
case "${1:-}" in
  "") ;;
  --test) mode=test ;;
  -h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

command -v kubectl >/dev/null || { echo "error: kubectl not found" >&2; exit 1; }

# Runs in the pod (busybox sh). The OTP comes from otp_length, and the result
# is decoded with a shell XOR, because "bao operator generate-root
# -generate-otp" and "-decode" both call the authenticated endpoint.
kubectl -n "$NS" exec -it "$POD" -c "$CONTAINER" -- sh -c '
set -eu
mode=$1
B=${BG_ADDR:-https://127.0.0.1:8210} # BG_ADDR: only for testing against a local server
gr() { c=$1; shift; bao "$c" -address="$B" "$@"; }

if [ "$(gr read -field=started sys/generate-root/attempt)" = true ]; then
  echo "A root generation attempt is already in progress; cancelling it."
  gr delete sys/generate-root/attempt >/dev/null
fi

len=$(gr read -field=otp_length sys/generate-root/attempt)
otp=$(tr -dc A-Za-z0-9 </dev/urandom | head -c "$len")
nonce=$(gr write -field=nonce sys/generate-root/attempt otp="$otp")
trap "gr delete sys/generate-root/attempt >/dev/null 2>&1 || true" EXIT
trap "exit 130" INT TERM HUP

printf "Recovery key (hidden): "
read -rs key
echo
enc=$(printf "%s" "$key" | gr write -field=encoded_token sys/generate-root/update nonce="$nonce" key=-)
unset key
trap - EXIT
[ -n "$enc" ] || { echo "error: no token returned; is the recovery key right?" >&2; exit 1; }

set -- $(printf "%s" "$otp" | od -An -v -tu1)
tok=""
for x in $(printf "%s" "$enc" | base64 -d | od -An -v -tu1); do
  tok="$tok$(printf "\\$(printf %03o $((x ^ $1)))")"
  shift
done

policies=$(BAO_TOKEN=$tok bao token lookup -field=policies)
if [ "$mode" = test ]; then
  BAO_TOKEN=$tok bao token revoke -self >/dev/null
  if BAO_TOKEN=$tok bao token lookup >/dev/null 2>&1; then
    echo "error: the test token is still valid; revoke it by hand" >&2
    exit 1
  fi
  echo "OK: the recovery key produced a token with policies $policies; it is revoked."
else
  echo "Root token (policies $policies). Revoke it as soon as you are done:"
  echo "$tok"
fi
' break-glass "$mode"
