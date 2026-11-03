#!/usr/bin/env bash
# Creates OpenBao's objects on the YubiHSM 2 and stores its secrets and the
# seal-key backup in 1Password. Run once, before OpenBao is deployed.
#
# Drives openbao-hsm-enrol (yubihsm-tools image) in a short-lived, hardened
# openbao-hsm-admin pod in the openbao namespace, which reaches the HSM through
# yubihsm-connector. It:
#   1. starts the pod and runs a read-only preflight with the HSM admin key
#      (Private/yubihsm-xp-admin): ids, labels and domains 9/10 must be free,
#   2. generates OpenBao's HSM password and the backup wrap key, and stores
#      them with your own 1Password session BEFORE the HSM uses them:
#        OpenBao-Automation/openbao-hsm  auth-password, auth-key-id, pkcs11-pin-prefix
#        Private/openbao-backup-wrap     wrap-key (64 hex), wrap-key-id
#   3. creates the seal key, imports the wrap key, creates the auth key,
#   4. stores the wrapped seal key as the document Private/openbao-seal-backup
#      (and the seal public key on the wrap-key item), as soon as it exists,
#   5. reports the restore test (a throwaway key restored under a second
#      import of your wrap-key copy), and deletes the pod.
#
# Run it yourself, signed in to 1Password as yourself (`op signin`) and with
# kubectl access to the cluster. Secrets are generated here and move only
# through pipes (to `op` on stdin, to the pod over `kubectl exec -i`): never on
# an argv, never printed, never in a file. The RAM-backed temp dir (shredded on
# exit) holds only the tool's output, i.e. the wrapped blob and public key.
#
# The wrap key and the wrapped seal key both live in your Private vault, so the
# backup is exactly as strong as access to that vault. That is the design (D1
# option b); a second YubiHSM kept offline (option c) would remove it.
#
# The admin pod is applied directly with kubectl rather than through Flux: it
# is ephemeral, owner-run, and deleted by the exit trap (or its one-hour
# deadline).
#
# Usage:
#   scripts/openbao-hsm-enrol.sh --dry-run   print the plan; touches nothing
#   scripts/openbao-hsm-enrol.sh --check     preflight only (sign-in, items, HSM), then delete the pod
#   scripts/openbao-hsm-enrol.sh             the whole enrolment
set -euo pipefail
umask 077

NS=openbao
POD=openbao-hsm-admin
IMAGE=ghcr.io/jlengelbrecht/yubihsm-tools:sha-beac1e9bb3ed517788714e33a633661b24295ae3@sha256:a46c023b4d260c54eabacfbb57ea969efe51f7d7f02445f795e7ab63aaa982dc
TOOL=openbao-hsm-enrol
ADMIN_REF=op://Private/yubihsm-xp-admin
BAO_VAULT=OpenBao-Automation
BAO_ITEM=openbao-hsm
WRAP_VAULT=Private
WRAP_ITEM=openbao-backup-wrap
BLOB_TITLE=openbao-seal-backup

mode=run
case "${1:-}" in
  "") ;;
  --dry-run) mode=dry-run ;;
  --check) mode=check ;;
  -h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac
[[ $# -le 1 ]] || { echo "one argument at most" >&2; exit 2; }

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Only the owner's own sessions: drop any agent or Connect credentials.
unset OP_SERVICE_ACCOUNT_TOKEN OP_CONNECT_HOST OP_CONNECT_TOKEN
# Use the real kubectl, never a stand-in left on PATH.
PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -v '/scratchpad/' | paste -sd: -)

for tool in op kubectl jq openssl sha256sum base64 shred; do
  command -v "$tool" >/dev/null || die "$tool not found"
done
case $IMAGE in *REPLACE*) die "IMAGE is not pinned yet" ;; esac

if [[ $mode == dry-run ]]; then
  cat <<EOF
Plan:
  1. check the 1Password sign-in; $BAO_VAULT/$BAO_ITEM, $WRAP_VAULT/$WRAP_ITEM and
     $WRAP_VAULT/$BLOB_TITLE must not exist yet
  2. start pod $NS/$POD ($IMAGE), hardened, deleted on exit
  3. $TOOL --check with $ADMIN_REF
  4. generate a 48-character OpenBao password and a 256-bit wrap key; store and read back
  5. $TOOL --apply; store the wrapped seal key as the document $WRAP_VAULT/$BLOB_TITLE
     and the seal public key on $WRAP_VAULT/$WRAP_ITEM, as soon as they arrive
  6. report the restore test; delete the pod
EOF
  exit 0
fi

work=$(mktemp -d -p "${XDG_RUNTIME_DIR:-/dev/shm}")
case $(stat -f -c %T "$work") in
  tmpfs | ramfs) ;;
  *) die "$work is not RAM-backed; set XDG_RUNTIME_DIR to a tmpfs directory" ;;
esac
pod_started=""
cleanup() {
  if [[ -n $pod_started ]]; then
    kubectl -n "$NS" delete pod "$POD" --wait=false >/dev/null 2>&1 || true
  fi
  find "$work" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

# ---- 1Password helpers (as in homelab-identity/scripts/hsm-reset.sh) -----------

item_count() { # <vault> <title>
  local out
  out=$(op item list --vault "$1" --format json) || die "could not list items in $1"
  jq -s --arg t "$2" '(.[0] // []) | map(select(.title == $t)) | length' <<<"$out"
}
create_item() { # <vault> <title>; fields JSON {"label": {"type","value"}} on stdin
  jq --arg t "$2" '{title: $t, category: "SECURE_NOTE",
      fields: [to_entries[] | {id: (.key | gsub("[^A-Za-z0-9]"; "_")), label: .key,
                               type: .value.type, value: .value.value}]}' |
    op item create --vault "$1" - >/dev/null
}
same_value() { # <op-reference> <value>
  [[ $(op read -n "$1" | sha256sum) == $(printf '%s' "$2" | sha256sum) ]]
}
admin_pw() { op read -n "$ADMIN_REF/password"; }

# ---- preflight -------------------------------------------------------------------

log "Checking the 1Password sign-in"
op whoami --format json >"$work/whoami" 2>/dev/null || die "sign in to 1Password first: eval \"\$(op signin)\""
[[ $(jq -r '.user_type // empty' "$work/whoami") != SERVICE_ACCOUNT ]] ||
  die "1Password is signed in as a service account; sign in as yourself"
[[ $(op read -n "$ADMIN_REF/auth-key-id") == 2 ]] || die "$ADMIN_REF/auth-key-id is not 2"

log "Checking that the 1Password items don't exist yet"
for vt in "$BAO_VAULT/$BAO_ITEM" "$WRAP_VAULT/$WRAP_ITEM" "$WRAP_VAULT/$BLOB_TITLE"; do
  n=$(item_count "${vt%%/*}" "${vt#*/}") || exit 1
  [[ $n == 0 ]] || die "$vt already exists; OpenBao may already be enrolled. Stop and ask before removing it"
done

log "Starting the key-management pod"
kubectl -n "$NS" get pod "$POD" >/dev/null 2>&1 && die "pod $NS/$POD already exists; delete it first"
pod_started=1
kubectl apply -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD
  namespace: $NS
  labels:
    app.kubernetes.io/name: openbao-hsm-admin
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 3600
  automountServiceAccountToken: false
  enableServiceLinks: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 65532
    runAsGroup: 65532
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: tools
      image: $IMAGE
      command: ["sleep", "3600"]
      resources:
        requests: {cpu: 10m, memory: 32Mi}
        limits: {memory: 128Mi}
      securityContext:
        allowPrivilegeEscalation: false
        privileged: false
        readOnlyRootFilesystem: true
        capabilities: {drop: ["ALL"]}
        seccompProfile: {type: RuntimeDefault}
      volumeMounts:
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: tmp
      emptyDir: {medium: Memory, sizeLimit: 16Mi}
EOF
kubectl -n "$NS" wait --for=condition=Ready "pod/$POD" --timeout=180s >/dev/null ||
  die "pod $NS/$POD did not become ready"

pod() { kubectl -n "$NS" exec -i "$POD" -c tools -- "$TOOL" "$@"; }

log "Preflight on the HSM (read-only)"
admin_pw | { cat; echo; } | pod --check

if [[ $mode == check ]]; then
  log "Preflight OK; nothing was changed"
  exit 0
fi

# ---- secrets, stored before the HSM uses them -------------------------------------

bao_pw=$(openssl rand -base64 96 | tr -dc 'A-Za-z0-9')
bao_pw=${bao_pw:0:48}
[[ ${#bao_pw} == 48 ]] || die "could not generate a password"
wrap_key=$(openssl rand -hex 32)
[[ $wrap_key =~ ^[0-9a-f]{64}$ ]] || die "could not generate the wrap key"

created_items=""
log "Storing OpenBao's HSM password in $BAO_VAULT/$BAO_ITEM"
jq -n --rawfile p <(printf '%s' "$bao_pw") '{
    "auth-password": {type: "CONCEALED", value: $p},
    "auth-key-id": {type: "STRING", value: "0x0011"},
    "pkcs11-pin-prefix": {type: "STRING", value: "0011"}}' | create_item "$BAO_VAULT" "$BAO_ITEM"
same_value "op://$BAO_VAULT/$BAO_ITEM/auth-password" "$bao_pw" ||
  die "the OpenBao password did not read back. Nothing is on the HSM yet; tell the PM (it removes $BAO_VAULT/$BAO_ITEM before a retry)"
created_items="$BAO_VAULT/$BAO_ITEM"

log "Storing the backup wrap key in $WRAP_VAULT/$WRAP_ITEM"
jq -n --rawfile k <(printf '%s' "$wrap_key") '{
    "wrap-key": {type: "CONCEALED", value: $k},
    "wrap-key-id": {type: "STRING", value: "0x0020"},
    "algorithm": {type: "STRING", value: "aes256-ccm-wrap"},
    "wraps": {type: "STRING", value: "0x0211 openbao-seal (rsa3072); restore: import this key with import-wrapped into a YubiHSM 2, then put-wrapped the openbao-seal-backup document"}}' |
  create_item "$WRAP_VAULT" "$WRAP_ITEM" ||
  die "could not store $WRAP_VAULT/$WRAP_ITEM. Nothing is on the HSM yet, but $created_items was created; tell the PM (it removes it before a retry)"
same_value "op://$WRAP_VAULT/$WRAP_ITEM/wrap-key" "$wrap_key" ||
  die "the wrap key did not read back. Nothing is on the HSM yet; tell the PM (it removes $created_items and $WRAP_VAULT/$WRAP_ITEM before a retry)"

# ---- enrolment ---------------------------------------------------------------------

log "Enrolling OpenBao on the HSM"
rc=0
{ admin_pw; echo; printf '%s\n' "$bao_pw"; printf '%s\n' "$wrap_key"; } |
  pod --apply >"$work/out" 2> >(tee "$work/err" >&2) || rc=$?
unset bao_pw wrap_key

blob=$(sed -n 's/^BLOB //p' "$work/out")
pub=$(sed -n 's/^SEAL-PUBKEY //p' "$work/out")
if [[ -n $blob ]]; then
  log "Storing the wrapped seal key as the document $WRAP_VAULT/$BLOB_TITLE"
  printf '%s' "$blob" >"$work/openbao-seal-0x0211.wrapped"
  if ! op document create "$work/openbao-seal-0x0211.wrapped" --vault "$WRAP_VAULT" \
      --title "$BLOB_TITLE" --file-name openbao-seal-0x0211.wrapped >/dev/null ||
    [[ $(op document get "$BLOB_TITLE" --vault "$WRAP_VAULT" | sha256sum) != $(printf '%s' "$blob" | sha256sum) ]]; then
    # Keep the only off-HSM copy rather than shredding it with the temp dir.
    # It is useless without the wrap key (in $WRAP_VAULT/$WRAP_ITEM).
    keep="$HOME/openbao-seal-0x0211.wrapped"
    install -m 600 "$work/openbao-seal-0x0211.wrapped" "$keep"
    die "the seal key 0x0211 EXISTS on the HSM, but its backup is NOT in 1Password. A copy is at $keep; make sure the document $WRAP_VAULT/$BLOB_TITLE holds exactly that file (replace it if one was created). Don't re-run; tell the PM (it can also re-export the backup with get-wrapped)"
  fi
  if [[ -n $pub ]]; then
    op item edit "$WRAP_ITEM" --vault "$WRAP_VAULT" "seal-public-key[text]=$(printf '%s' "$pub" | base64 -d)" \
      </dev/null >/dev/null || log "warning: the seal public key was not stored on $WRAP_VAULT/$WRAP_ITEM (it can be read from the HSM later); carrying on"
  fi
fi

if [[ $rc != 0 ]]; then
  if [[ -n $blob ]]; then
    die "openbao-hsm-enrol failed (exit $rc) after the seal key was created; its backup was stored. Tell the PM; don't re-run"
  fi
  # Only a STOP from the tool itself, before it reached the seal key, proves no
  # seal key was made: if the exec stream dropped, the tool may have carried on.
  if grep -q "generating rsa3072 key 0x0211" "$work/err" 2>/dev/null ||
    ! grep -q "^openbao-hsm-enrol: STOP:" "$work/err" 2>/dev/null; then
    die "openbao-hsm-enrol failed (exit $rc) and no backup came back. The seal key 0x0211 MAY exist on the HSM (the tool got to it, or the connection dropped), as may the wrap key 0x0020 and auth key 0x0011; the two 1Password items were created. Don't re-run; tell the PM (it checks the HSM and re-exports or cleans up)"
  fi
  die "openbao-hsm-enrol failed (exit $rc) before the seal key was created. The wrap key 0x0020 and auth key 0x0011 may exist on the HSM, and the two 1Password items were created. Don't re-run; tell the PM (it cleans up before a retry)"
fi
grep -qx "RESTORE-TEST PASSED" "$work/out" || die "the restore test didn't report a pass. Tell the PM"
[[ -n $blob ]] || die "no backup came back from the HSM. Tell the PM"

log "Done: seal key, wrap key and auth key on the HSM; backup stored; restore test passed."
log "Tell the PM \"OpenBao enrolled\"."
