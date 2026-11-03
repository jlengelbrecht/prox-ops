#!/usr/bin/env bash
# Initialises OpenBao once, after the first deploy, and stores its recovery key
# and initial root token in your private 1Password vault.
#
# With the HSM seal, `bao operator init` encrypts OpenBao's root key with the
# HSM's seal key and unseals at once, so no unseal keys exist. It returns:
#   - one recovery key (1 share, threshold 1): authorises recovery operations,
#     such as generating a new root token. It does not decrypt anything; only
#     the HSM seal key does.
#   - the initial root token, used to set up login and policies, then revoked.
# Both go straight into Private/openbao-init and are never printed. Print the
# recovery key from 1Password for the offline copy.
#
# Run it yourself, signed in to 1Password as yourself, with kubectl access.
#
# Usage:
#   scripts/openbao-init.sh --check   show the cluster's state; changes nothing
#   scripts/openbao-init.sh           initialise (refuses if already initialised)
set -euo pipefail
umask 077

NS=openbao
POD=openbao-0
CONTAINER=openbao
VAULT=Private
ITEM=openbao-init

mode=run
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  -h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

unset OP_SERVICE_ACCOUNT_TOKEN OP_CONNECT_HOST OP_CONNECT_TOKEN
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '/scratchpad/' | paste -sd: -)
for tool in op kubectl jq sha256sum shred; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

work=$(mktemp -d -p "${XDG_RUNTIME_DIR:-/dev/shm}")
case $(stat -f -c %T "$work") in
  tmpfs | ramfs) ;;
  *) die "$work is not RAM-backed; set XDG_RUNTIME_DIR to a tmpfs directory" ;;
esac
init_done=""
cleanup() {
  # If init ran but its output didn't reach 1Password, keep it rather than
  # lose the only copy of the recovery key and root token.
  if [[ -n $init_done && -s $work/init.json ]]; then
    install -m 600 "$work/init.json" "$HOME/openbao-init.json" 2>/dev/null &&
      printf 'error: OpenBao IS initialised but its recovery key and root token were NOT stored in 1Password. They are in %s (0600): store them as %s/%s, then shred that file. Tell the PM.\n' \
        "$HOME/openbao-init.json" "$VAULT" "$ITEM" >&2
  fi
  find "$work" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

bao_in() { # <pod> <args...>
  local pod=$1
  shift
  kubectl -n "$NS" exec "$pod" -c "$CONTAINER" -- bao "$@"
}
status_of() { # <pod> -> "initialized sealed" or "unreachable"
  local out
  out=$(bao_in "$1" status -format=json 2>/dev/null) || true
  [[ -n $out ]] || { echo unreachable; return; }
  jq -r '"\(.initialized) \(.sealed)"' <<<"$out" 2>/dev/null || echo unreachable
}

log "Cluster state"
for p in openbao-0 openbao-1 openbao-2; do
  phase=$(kubectl -n "$NS" get pod "$p" -o jsonpath='{.status.phase}' 2>/dev/null || echo missing)
  printf '    %s: %s, initialized/sealed: %s\n' "$p" "$phase" "$(status_of "$p")"
done
[[ $(status_of "$POD") == "false true" ]] ||
  die "$POD is not 'uninitialized and sealed'; OpenBao may already be initialised. Nothing was changed"

if [[ $mode == check ]]; then
  log "Ready to initialise; nothing was changed"
  exit 0
fi

log "Checking the 1Password sign-in"
op whoami --format json >"$work/whoami" 2>/dev/null || die "sign in to 1Password first: eval \"\$(op signin)\""
[[ $(jq -r '.user_type // empty' "$work/whoami") != SERVICE_ACCOUNT ]] ||
  die "1Password is signed in as a service account; sign in as yourself"
n=$(op item list --vault "$VAULT" --format json | jq --arg t "$ITEM" '[.[] | select(.title == $t)] | length') ||
  die "could not list items in $VAULT"
[[ $n == 0 ]] || die "$VAULT/$ITEM already exists. Nothing was changed"

log "Initialising OpenBao on $POD (the HSM unseals it)"
init_done=1
bao_in "$POD" operator init -recovery-shares=1 -recovery-threshold=1 -format=json >"$work/init.json"
root=$(jq -r '.root_token // empty' "$work/init.json")
recovery=$(jq -r '.recovery_keys_b64[0] // empty' "$work/init.json")
[[ -n $root && -n $recovery ]] || die "init returned no root token or recovery key"

log "Storing the recovery key and root token in $VAULT/$ITEM"
jq -n --rawfile r <(printf '%s' "$recovery") --rawfile t <(printf '%s' "$root") '{
    title: "openbao-init", category: "SECURE_NOTE",
    fields: [
      {id: "recovery_key", label: "recovery-key", type: "CONCEALED", value: $r},
      {id: "root_token", label: "root-token", type: "CONCEALED", value: $t},
      {id: "notes", label: "notes", type: "STRING",
       value: "OpenBao recovery key (1 of 1; authorises recovery operations, decrypts nothing) and the initial root token (revoke after setup). Keep a printed copy of the recovery key offline."}]}' |
  op item create --vault "$VAULT" - >/dev/null
if [[ $(op read -n "op://$VAULT/$ITEM/recovery-key" | sha256sum) != $(printf '%s' "$recovery" | sha256sum) ]] ||
  [[ $(op read -n "op://$VAULT/$ITEM/root-token" | sha256sum) != $(printf '%s' "$root" | sha256sum) ]]; then
  die "the init output did not read back from 1Password. $VAULT/$ITEM may exist but be wrong; a correct copy is kept in $HOME/openbao-init.json"
fi
init_done=""
unset root recovery

# Up to 10 minutes: the replicas start one after another (OrderedReady), each
# possibly pulling the image on a new node first.
log "Waiting for all three replicas to join and unseal"
for _ in $(seq 1 120); do
  ok=0
  for p in openbao-0 openbao-1 openbao-2; do
    [[ $(status_of "$p") == "true false" ]] && ok=$((ok + 1))
  done
  [[ $ok == 3 ]] && break
  sleep 5
done
for p in openbao-0 openbao-1 openbao-2; do
  printf '    %s: initialized/sealed: %s\n' "$p" "$(status_of "$p")"
done
[[ $ok == 3 ]] || die "not all replicas are unsealed yet (the recovery key and root token ARE stored). Tell the PM"

log "Done: OpenBao is initialised and unsealed by the HSM on all three replicas."
log "Print the recovery key from $VAULT/$ITEM for the offline copy, then tell the PM \"OpenBao initialised\"."
