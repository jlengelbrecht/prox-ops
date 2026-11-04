#!/usr/bin/env bash
# Sets up OpenBao after init: the admin and reader policies, OIDC login
# through authentik (groups openbao-admins / openbao-readers), the
# Kubernetes auth method, the snapshot job's role, the database engine with the
# Zabbix login, and the Kubernetes secrets engine for the merge gate. Safe to run again: every step is an upsert. Only the first
# run writes the Zabbix static role, and that rotates Zabbix's password at once.
#
# Needs the 1Password item Automation/openbao-oidc (field oidc-client-secret,
# the same value authentik's OpenBao provider uses) and the authentik
# blueprint "OpenBao" applied, and the field OPENBAO_PASSWORD on
# Automation/postgres-cluster (the same password CNPG gives the openbao role).
#
# Rotating the client secret: authentik picks up a new 1Password value by
# itself, but OpenBao only when this script runs again. Until then new OIDC
# logins fail (existing tokens keep working), so run it with --token-stdin and
# a current admin token right after changing the field. Without one, a new
# root token takes the recovery key (bao operator generate-root).
#
# Token: the initial root token from Private/openbao-init, or, once that is
# revoked, an admin token pasted on stdin with --token-stdin (copy it from the
# web UI's user menu after an OIDC login).
#
# Run it yourself, signed in to 1Password as yourself, with kubectl access.
#
# Usage:
#   scripts/openbao-configure.sh                 configure (root token from 1Password)
#   scripts/openbao-configure.sh --token-stdin   configure with a token read from stdin
#   scripts/openbao-configure.sh --revoke-root   revoke the initial root token, once an
#                                                admin has logged in through OIDC
set -euo pipefail
umask 077

NS=openbao
POD=openbao-0
CONTAINER=openbao
ROOT_REF=op://Private/openbao-init/root-token
OIDC_REF=op://Automation/openbao-oidc/oidc-client-secret
DB_REF=op://Automation/postgres-cluster/OPENBAO_PASSWORD
# OpenBao's templates, filled in with the username and password it stores.
DB_LOGIN='{{username}}:{{password}}'
DB_URL="postgresql://$DB_LOGIN@postgres-rw.database.svc:5432/postgres?sslmode=require"
POLICY_DIR=$(cd "$(dirname "$0")/.." && pwd)/kubernetes/apps/openbao/openbao/config
DISCOVERY_URL=https://authentik.homelab0.org/application/o/openbao/

mode=configure
token_source=root
case "${1:-}" in
  "") ;;
  --token-stdin) token_source=stdin ;;
  --revoke-root) mode=revoke ;;
  -h | --help) sed -n '2,/^set -euo/{/^set -euo/d;s/^# \{0,1\}//;p}' "$0"; exit 0 ;;
  *) echo "unknown argument: $1" >&2; exit 2 ;;
esac

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

unset OP_SERVICE_ACCOUNT_TOKEN OP_CONNECT_HOST OP_CONNECT_TOKEN
for tool in op kubectl jq shred; do
  command -v "$tool" >/dev/null || die "$tool not found"
done

work=$(mktemp -d -p "${XDG_RUNTIME_DIR:-/dev/shm}")
cleanup() {
  find "$work" -type f -exec shred -u {} + 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP
case $(stat -f -c %T "$work") in
  tmpfs | ramfs) ;;
  *) die "$work is not RAM-backed; set XDG_RUNTIME_DIR to a tmpfs directory" ;;
esac

# bao inside the pod. The token goes in as the first line of stdin, never on a
# command line; after it comes the file named by $payload, if set (a JSON body
# or policy for a trailing "-").
payload=""
bao() { # <args...>
  {
    cat "$work/token"
    printf '\n'
    if [[ -n $payload ]]; then cat "$payload"; fi
  } | kubectl -n "$NS" exec -i "$POD" -c "$CONTAINER" -- \
    sh -c 'read -r t; BAO_TOKEN=$t exec bao "$@"' bao "$@"
}
bao_json() { bao "$@" -format=json; }
bao_write_json() { # <path> <json-file>
  payload=$2 bao write "$1" - >/dev/null
}

# Every path below reads from 1Password, so fail here, not halfway through.
op whoami --format json >"$work/whoami" 2>/dev/null || die "sign in to 1Password first: eval \"\$(op signin)\""
[[ $(jq -r '.user_type // empty' "$work/whoami") != SERVICE_ACCOUNT ]] ||
  die "1Password is signed in as a service account; sign in as yourself"

if [[ $token_source == stdin ]]; then
  log "Reading the token from stdin"
  # -s: at a terminal the token would otherwise echo, and end up in scrollback.
  if [[ -t 0 ]]; then
    IFS= read -rs -p "OpenBao token: " t || true
    echo
  else
    IFS= read -r t || true
  fi
  [[ -n ${t:-} ]] || die "no token on stdin"
  printf '%s' "$t" >"$work/token"
  unset t
else
  op read -n "$ROOT_REF" >"$work/token" 2>/dev/null || die "could not read $ROOT_REF"
fi

bao_json token lookup >"$work/self" 2>/dev/null || die "the token was rejected by OpenBao"
policies=$(jq -r '.data.policies | join(",")' "$work/self")
log "Token accepted (policies: $policies)"

oidc_accessor() { bao_json auth list | jq -r '."oidc/".accessor // empty'; }

if [[ $mode == revoke ]]; then
  [[ $policies == root ]] || die "this is not the root token; nothing was revoked"
  acc=$(oidc_accessor)
  [[ -n $acc ]] || die "OIDC login is not set up yet; run this script without --revoke-root first"
  members=$(bao_json read identity/group/name/openbao-admins 2>/dev/null |
    jq -r '.data.member_entity_ids // [] | length')
  [[ ${members:-0} -gt 0 ]] ||
    die "no one has logged in as an OpenBao admin yet. Log in at https://bao.homelab0.org (method OIDC) as a member of openbao-admins, then run this again"
  bao token revoke -self >/dev/null
  log "Root token revoked. A new one can only be made with the recovery key (bao operator generate-root)."
  log "Delete the now-useless root-token field from Private/openbao-init."
  exit 0
fi

# --- policies -------------------------------------------------------------
for p in openbao-admin openbao-reader openbao-snapshot openbao-zabbix-db openbao-gate; do
  log "Writing policy $p"
  payload=$POLICY_DIR/$p.hcl bao policy write "$p" - >/dev/null
done

# --- OIDC login through authentik ------------------------------------------
if [[ -z $(oidc_accessor) ]]; then
  log "Enabling the OIDC auth method at oidc/"
  bao auth enable -path=oidc -description="authentik (openbao-admins, openbao-readers)" oidc >/dev/null
fi
acc=$(oidc_accessor)

log "Configuring OIDC against authentik"
op read -n "$OIDC_REF" >"$work/oidc-secret" 2>/dev/null || die "could not read $OIDC_REF; does the item exist?"
jq -n --arg url "$DISCOVERY_URL" --rawfile secret "$work/oidc-secret" '{
    oidc_discovery_url: $url,
    oidc_client_id: "openbao",
    oidc_client_secret: $secret,
    default_role: "default"}' >"$work/oidc-config.json"
bao_write_json auth/oidc/config "$work/oidc-config.json" ||
  die "OpenBao refused the OIDC config. Check that authentik's OpenBao provider exists ($DISCOVERY_URL)"

# Only the two role groups may log in at all (bound_claims matches any listed
# value); the groups claim then decides the policies. Entities are keyed on
# authentik's sub (a stable hashed user id), not the username, which can be
# renamed or reused; the username is kept as alias metadata for display.
log "Writing the OIDC role default"
jq -n '{
    role_type: "oidc",
    user_claim: "sub",
    claim_mappings: {preferred_username: "username"},
    groups_claim: "groups",
    oidc_scopes: ["profile", "groups"],
    bound_claims: {groups: ["openbao-admins", "openbao-readers"]},
    allowed_redirect_uris: [
      "https://bao.homelab0.org/ui/vault/auth/oidc/oidc/callback",
      "http://localhost:8250/oidc/callback"],
    token_policies: ["default"],
    token_ttl: "1h",
    token_max_ttl: "8h"}' >"$work/role.json"
bao_write_json auth/oidc/role/default "$work/role.json"

# External identity groups: authentik group name -> OpenBao policy.
for pair in openbao-admins:openbao-admin openbao-readers:openbao-reader; do
  group=${pair%%:*} policy=${pair#*:}
  log "Mapping authentik group $group to policy $policy"
  jq -n --arg p "$policy" '{type: "external", policies: [$p]}' >"$work/group.json"
  bao_write_json "identity/group/name/$group" "$work/group.json"
  bao_json read "identity/group/name/$group" >"$work/group-read.json"
  id=$(jq -r '.data.id' "$work/group-read.json")
  if [[ $(jq -r '.data.alias.name // empty' "$work/group-read.json") != "$group" ]]; then
    jq -n --arg n "$group" --arg a "$acc" --arg id "$id" \
      '{name: $n, mount_accessor: $a, canonical_id: $id}' >"$work/alias.json"
    bao_write_json identity/group-alias "$work/alias.json"
  fi
done

# --- Kubernetes auth --------------------------------------------------------
# Roles are added with the workloads that use them. OpenBao reviews tokens
# with its own service account (system:auth-delegator, from the chart).
if [[ -z $(bao_json auth list | jq -r '."kubernetes/".accessor // empty') ]]; then
  log "Enabling the Kubernetes auth method at kubernetes/"
  bao auth enable -path=kubernetes -description="cluster service accounts" kubernetes >/dev/null
fi
log "Configuring Kubernetes auth"
jq -n '{kubernetes_host: "https://kubernetes.default.svc"}' >"$work/k8s.json"
bao_write_json auth/kubernetes/config "$work/k8s.json"

# The daily snapshot job (kubernetes/apps/openbao/snapshots): its service
# account only, a token that lives just long enough for one snapshot. The
# audience "openbao" must match the job's projected token: a token issued for
# the Kubernetes API is refused, and the job's token is useless anywhere else.
# Every future role sets the same audience.
log "Writing the Kubernetes auth role openbao-snapshot"
jq -n '{
    bound_service_account_names: ["openbao-snapshots"],
    bound_service_account_namespaces: ["openbao"],
    audience: "openbao",
    token_policies: ["openbao-snapshot"],
    token_ttl: "10m",
    token_max_ttl: "10m",
    token_no_default_policy: true}' >"$work/snapshot-role.json"
bao_write_json auth/kubernetes/role/openbao-snapshot "$work/snapshot-role.json"

# External Secrets, for Zabbix's namespace only. Same audience rule as above.
log "Writing the Kubernetes auth role zabbix-db"
jq -n '{
    bound_service_account_names: ["zabbix-openbao"],
    bound_service_account_namespaces: ["observability"],
    audience: "openbao",
    token_policies: ["openbao-zabbix-db"],
    token_ttl: "10m",
    token_max_ttl: "10m",
    token_no_default_policy: true}' >"$work/zabbix-role.json"
bao_write_json auth/kubernetes/role/zabbix-db "$work/zabbix-role.json"

# The merge gate's evidence worker (EPIC-051): its service account only.
log "Writing the Kubernetes auth role merge-evidence"
jq -n '{
    bound_service_account_names: ["merge-evidence"],
    bound_service_account_namespaces: ["steward"],
    audience: "openbao",
    token_policies: ["openbao-gate"],
    token_ttl: "10m",
    token_max_ttl: "10m",
    token_no_default_policy: true}' >"$work/gate-auth-role.json"
bao_write_json auth/kubernetes/role/merge-evidence "$work/gate-auth-role.json"

# --- Kubernetes secrets engine ----------------------------------------------
# Short gate-reader tokens, one namespace per request. service_account_name mode
# creates nothing in the cluster, and "*" is safe here: a token can only exist
# where kubernetes/apps/openbao/gate-reader put an account, which is the
# allowlist. 10m is the shortest token Kubernetes issues. Revoking the lease
# doesn't revoke the token; to do that, delete the account and let Flux
# recreate it.
if [[ -z $(bao_json secrets list | jq -r '."kubernetes/".accessor // empty') ]]; then
  log "Enabling the Kubernetes secrets engine at kubernetes/"
  bao secrets enable -path=kubernetes -description="short tokens for gate-reader accounts" kubernetes >/dev/null
fi
log "Configuring the Kubernetes secrets engine"
jq -n '{kubernetes_host: "https://kubernetes.default.svc"}' >"$work/k8s-engine.json"
bao_write_json kubernetes/config "$work/k8s-engine.json"
log "Writing the Kubernetes secrets engine role gate-reader"
jq -n '{
    service_account_name: "gate-reader",
    allowed_kubernetes_namespaces: ["*"],
    token_default_ttl: "10m",
    token_max_ttl: "10m"}' >"$work/gate-role.json"
bao_write_json kubernetes/roles/gate-reader "$work/gate-role.json"

# --- database engine --------------------------------------------------------
# Writing the static role the first time rotates the password at once, so
# refuse unless both sides are ready: CNPG must have stopped managing the
# password, and External Secrets must be set up to fetch the new one.
log "Checking that Zabbix is ready to move to OpenBao"
kubectl -n database get cluster postgres -o json >"$work/cluster.json"
[[ $(jq -r '.spec.managed.roles[] | select(.name == "zabbix") | .passwordSecret // empty' "$work/cluster.json") == "" ]] ||
  die "CNPG still manages the zabbix password; merge the Zabbix change and let Flux apply it first"
kubectl -n observability get vaultdynamicsecret zabbix-db >/dev/null 2>&1 ||
  die "the External Secrets generator observability/zabbix-db is missing; merge the Zabbix change and let Flux apply it first"

# CNPG grants the membership, with Postgres' defaults. Changing the password
# also needs ADMIN OPTION, which CNPG can't set, and turning off INHERIT and SET
# means openbao can reset the password but not act as zabbix or read its data.
log "Giving the openbao Postgres role ADMIN OPTION on zabbix, without its privileges"
primary=$(jq -r '.status.currentPrimary // empty' "$work/cluster.json")
[[ -n $primary ]] || die "CNPG reports no primary for the postgres cluster"
kubectl -n database exec "$primary" -c postgres -- \
  psql -q -d postgres -v ON_ERROR_STOP=1 \
  -c 'GRANT zabbix TO openbao WITH ADMIN OPTION, INHERIT FALSE, SET FALSE' >/dev/null ||
  die "could not grant ADMIN OPTION; check that CNPG created the openbao role"

if [[ -z $(bao_json secrets list | jq -r '."database/".accessor // empty') ]]; then
  log "Enabling the database secrets engine at database/"
  bao secrets enable -path=database -description="CNPG roles rotated by OpenBao" database >/dev/null
fi

log "Configuring the connection to CNPG"
op read -n "$DB_REF" >"$work/db-password" 2>/dev/null || die "could not read $DB_REF; does the field exist?"
# sslmode=require, not verify-full: CNPG's CA renews every 90 days, and a copy
# here would expire and stop rotation.
jq -n --arg url "$DB_URL" --rawfile password "$work/db-password" '{
    plugin_name: "postgresql-database-plugin",
    allowed_roles: ["zabbix"],
    connection_url: $url,
    username: "openbao",
    password: $password}' >"$work/db-config.json"
bao_write_json database/config/cnpg "$work/db-config.json" ||
  die "OpenBao refused the database connection. Check that CNPG has reconciled the openbao role and the network policies are in place"

log "Writing the static role zabbix (rotates the password on first run)"
jq -n '{db_name: "cnpg", username: "zabbix", rotation_period: "720h"}' >"$work/zabbix-static.json"
bao_write_json database/static-roles/zabbix "$work/zabbix-static.json"

log "Done. Log in at https://bao.homelab0.org: method OIDC, role default (leave blank)."
if [[ $policies == root ]]; then
  log "Once that works as an admin, revoke the root token: scripts/openbao-configure.sh --revoke-root"
fi
