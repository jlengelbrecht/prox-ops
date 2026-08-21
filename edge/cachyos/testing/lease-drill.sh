#!/usr/bin/env bash
#
# lease-drill.sh — Part 3 proof for STORY-035-6a (guard-ready lease).
#
# CYCLE 3 REWRITE. The previous version treated `ss -ltn` on the host as the
# fail-closed/serving observable. That is unsatisfiable by construction:
# docker-compose publishes the port with `-p ADDR:PORT:PORT`, so the host
# socket is owned by docker's DNAT/proxy plumbing for the container's entire
# lifetime and never closes when llama-swap stops inside it. Every scenario
# "failed" against a pass condition that could not physically be met, even
# though PM measurement at the client boundary proved the lease behaves
# correctly (see the story's cycle-2 instrument-defect section).
#
# The authoritative observable is now a real client request across the
# published LAN boundary: TCP connect + TLS handshake + an authenticated
# HTTP request, exactly what agentgateway or any other consumer would send.
# `ss -ltn` is still recorded, but purely as context, labelled so no future
# reader mistakes docker's always-open publish socket for a defect.
#
# Exercises the four scenarios AC8 requires and prints, for each, the
# evidence the story's cycle-3 acceptance table demands: `curl` exit code,
# HTTP code, a UTC timestamp, the lease age against its TTL, the phase file,
# an in-container check that llama-swap itself is not running, and the host
# `ss` line as informational-only context. A fail-closed scenario passes only
# when ALL of: lease missing/stale/invalid, phase withdrawn, llama-swap
# absent, and the client request fails at the transport/service boundary
# (non-zero curl exit, HTTP 000) — never merely an application-level 503.
# Recovery passes only on a fresh lease plus an authenticated 200.
#
# CYCLE 5 addition: scenario e proves the guard-lease fix for the "guard
# blind since boot" production defect (see README.md "A blind guard is not a
# live guard"). It breaks the GPU sampler itself — points a running guard's
# ROCM_SMI at a path that does not exist — while the guard process keeps
# running, and proves the same fail-closed/recovery instrument applies:
# sampling failure alone, with no crash and no stopped unit, must still
# withdraw service within the lease TTL, because a lease renewal now means
# "the guard can see the GPU", not merely "the process is looping".
#
# CANNOT run inside the dev-agent sandbox that authored it — no docker, no
# systemctl, no reads outside the worktree (see the story's EXECUTION
# SPLIT). Run this on the real host, as the user the systemd --user units
# run as, with the edge-interactive-guard unit and the edge-llama-swap
# container already up and healthy.
#
# Idempotent: every scenario, and the script's own exit trap, leave the
# guard unit and the container running — a failed or interrupted run does
# not leave the node withdrawn. Scenario e's env-file edit is likewise
# restored unconditionally, including from the exit trap on an interrupted
# run, so a killed drill never leaves the host's ROCM_SMI pointed at a path
# that does not exist. A lock file refuses a second concurrent run rather
# than interleaving two drills' kills and restarts.
#
# Requires: docker, systemctl, curl, ss (informational only), date, awk,
# sed, grep, and
# passwordless `systemctl --user` / `docker` for the invoking user — both
# already assumed by normal operation of this node.
#
# Usage:
#   lease-drill.sh --endpoint URL --ca-cert PATH [options]
#
# Required (or via the environment names README.md already documents):
#   --endpoint URL          Base URL of the published endpoint, e.g.
#                            https://cachyos-7900xtx.homelab0.org:8443
#                            (EDGE_ENDPOINT)
#   --ca-cert PATH           Dedicated edge CA bundle (EDGE_CA_CERT). Verification
#                            stays on throughout, exactly like conformance.sh.
#   A bearer credential, via EDGE_API_KEY_FILE (preferred; matches
#   scripts/edge-common.sh) or EDGE_API_KEY. Never pass one on the command
#   line — it would be readable from /proc on this shared host.
#
# Optional:
#   --resolve HOST:PORT:IP  Passed straight to curl --resolve, for the
#                            interim where the edge hostname has no LAN DNS
#                            record yet (EDGE_CURL_RESOLVE).
#   --probe-path PATH       Authenticated route to probe (default /v1/models,
#                            the same route conformance.sh's tls_validation
#                            check uses).
#   --port PORT              Port to show in the informational `ss -ltn`
#                            context only (EDGE_PORT, default 8443).
#   --guard-unit NAME        systemd --user unit name (default edge-interactive-guard.service)
#   --container NAME         docker container name (default edge-llama-swap)
#   --ttl N                  Guard lease TTL in seconds; must match the
#                            container's EDGE_GUARD_LEASE_TTL (default 6)
#   --poll N                 Supervisor poll interval in seconds; must match
#                            EDGE_POLL_INTERVAL (default 1)
#   --probe-timeout N        Per-request curl timeout in seconds (default 5)
#   --timeout N               Overall per-scenario wait budget in seconds (default 120)
#   --phase-settle-timeout N  Bound, in seconds, on how long a fail-closed
#                              scenario polls the phase file for `withdrawn`
#                              after the client already observed a failure,
#                              since the supervisor writes draining before
#                              withdrawn and the two are not simultaneous
#                              (default: LEASE_TTL + 2*POLL_INTERVAL + 5,
#                              i.e. "a few seconds beyond TTL")
#                              (EDGE_PHASE_SETTLE_TIMEOUT)
#   --env-file PATH           Host env file the guard's systemd unit loads
#                              via EnvironmentFile= (default
#                              ~/.config/edge-cachyos/edge.env). Scenario e
#                              edits this file's ROCM_SMI line to break the
#                              sampler and restores it afterward — see
#                              scenario e below. (EDGE_HOST_ENV_FILE)
#
# Environment (same names as env.example; flags win over these):
#   EDGE_ENDPOINT, EDGE_CA_CERT, EDGE_API_KEY_FILE, EDGE_API_KEY,
#   EDGE_CURL_RESOLVE, EDGE_PORT, EDGE_GUARD_LEASE_TTL, EDGE_POLL_INTERVAL,
#   EDGE_STATE_DIR (defaults to ${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state,
#   the same default scripts/edge-common.sh resolves %S to, so this script
#   reads exactly the phase/lease files the running units and container see),
#   EDGE_HOST_ENV_FILE (see --env-file above)

set -uo pipefail

ENDPOINT="${EDGE_ENDPOINT:-}"
CA_CERT="${EDGE_CA_CERT:-}"
API_KEY="${EDGE_API_KEY:-}"
API_KEY_FILE="${EDGE_API_KEY_FILE:-}"
RESOLVE="${EDGE_CURL_RESOLVE:-}"
PROBE_PATH="/v1/models"
PORT="${EDGE_PORT:-8443}"
GUARD_UNIT="edge-interactive-guard.service"
CONTAINER="edge-llama-swap"
LEASE_TTL="${EDGE_GUARD_LEASE_TTL:-6}"
POLL_INTERVAL="${EDGE_POLL_INTERVAL:-1}"
PROBE_TIMEOUT=5
DRILL_TIMEOUT=120
PHASE_SETTLE_TIMEOUT="${EDGE_PHASE_SETTLE_TIMEOUT:-0}"
STATE_DIR="${EDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state}"
ENV_FILE="${EDGE_HOST_ENV_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/edge-cachyos/edge.env}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --endpoint) ENDPOINT="$2"; shift 2 ;;
        --ca-cert) CA_CERT="$2"; shift 2 ;;
        --api-key-file) API_KEY_FILE="$2"; shift 2 ;;
        --resolve) RESOLVE="$2"; shift 2 ;;
        --probe-path) PROBE_PATH="$2"; shift 2 ;;
        --port) PORT="$2"; shift 2 ;;
        --guard-unit) GUARD_UNIT="$2"; shift 2 ;;
        --container) CONTAINER="$2"; shift 2 ;;
        --ttl) LEASE_TTL="$2"; shift 2 ;;
        --poll) POLL_INTERVAL="$2"; shift 2 ;;
        --probe-timeout) PROBE_TIMEOUT="$2"; shift 2 ;;
        --timeout) DRILL_TIMEOUT="$2"; shift 2 ;;
        --phase-settle-timeout) PHASE_SETTLE_TIMEOUT="$2"; shift 2 ;;
        --env-file) ENV_FILE="$2"; shift 2 ;;
        -h|--help)
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$API_KEY" ] && [ -n "$API_KEY_FILE" ] && [ -r "$API_KEY_FILE" ]; then
    API_KEY=$(tr -d '\r\n' <"$API_KEY_FILE")
fi

[ -n "$ENDPOINT" ] || { echo "FATAL --endpoint or EDGE_ENDPOINT is required" >&2; exit 2; }
[ -n "$CA_CERT" ] && [ -r "$CA_CERT" ] || { echo "FATAL --ca-cert '$CA_CERT' (or EDGE_CA_CERT) must be a readable file" >&2; exit 2; }
[ -n "$API_KEY" ] || { echo "FATAL no bearer credential: set EDGE_API_KEY_FILE or EDGE_API_KEY (never pass one as a flag)" >&2; exit 2; }

for bin in ss docker systemctl curl date awk sed grep; do
    command -v "$bin" >/dev/null 2>&1 || { echo "FATAL required tool '$bin' not found on PATH" >&2; exit 2; }
done

ENDPOINT="${ENDPOINT%/}"
# 0 means "not set by flag or env" — fall back to a bound derived from the
# TTL actually in effect. PM measurement settled at 9s against TTL=6s/poll=1s;
# this formula gives 13s for those defaults, "a few seconds beyond TTL".
[ "$PHASE_SETTLE_TIMEOUT" -gt 0 ] 2>/dev/null || PHASE_SETTLE_TIMEOUT="$((LEASE_TTL + POLL_INTERVAL * 2 + 5))"
FAILURES=0
LOCK_FILE="${TMPDIR:-/tmp}/edge-lease-drill.lock"
# Credential written to a 0600 file and handed to curl as --header @file, so it
# never appears in any process's argument list. See client_probe().
AUTH_HEADER_FILE=$(mktemp "${TMPDIR:-/tmp}/edge-lease-drill-auth.XXXXXX")
chmod 600 "$AUTH_HEADER_FILE"
printf 'Authorization: Bearer %s\n' "$API_KEY" >"$AUTH_HEADER_FILE"
# Reused across every client_probe() call to capture curl's --show-error
# diagnostics, which are otherwise generated and silently thrown away.
CURL_ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/edge-lease-drill-curl-err.XXXXXX")
LEASE_FILE="$STATE_DIR/guard-lease"
PHASE_FILE="$STATE_DIR/phase"

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# Context only — never the pass/fail signal. Docker owns this socket for the
# container's whole lifetime; see the header comment.
ss_snapshot() {
    ss -ltn 2>/dev/null | { grep -F ":$PORT " || true; }
}

# One real client request across the LAN boundary: TCP + TLS + an
# authenticated GET. Prints "<curl-exit-code> <http-code>"; http-code is 000
# when no response was ever received (connection refused/reset, or a TLS
# failure), which is the fail-closed proof the story requires.
client_probe() {
    local -a opts=(--silent --show-error --max-time "$PROBE_TIMEOUT" \
        --output /dev/null --write-out '%{http_code}')
    [ -n "$CA_CERT" ] && opts+=(--cacert "$CA_CERT")
    [ -n "$RESOLVE" ] && opts+=(--resolve "$RESOLVE")
    local code rc
    # The header goes in a private file rather than argv: this script's header
    # promises the credential is never passed on the command line, and
    # -H "Authorization: Bearer $API_KEY" would break that promise --
    # /proc/<pid>/cmdline is world-readable for the life of the request.
    code=$(curl "${opts[@]}" --header "@$AUTH_HEADER_FILE" "$ENDPOINT$PROBE_PATH" 2>"$CURL_ERR_FILE")
    rc=$?
    # --show-error only earns its keep if something reads what it produces:
    # surface it on this script's own stderr (never mixed into the "<rc>
    # <code>" stdout contract callers parse with `read`), and only when the
    # request actually failed -- a healthy probe has nothing worth printing.
    if [ "$rc" -ne 0 ] && [ -s "$CURL_ERR_FILE" ]; then
        printf 'client_probe: %s\n' "$(cat "$CURL_ERR_FILE")" >&2
    fi
    printf '%s %s\n' "$rc" "${code:-000}"
}

# Seconds since the guard's lease was last renewed, or "missing"/"invalid".
read_lease_age() {
    if [ ! -r "$LEASE_FILE" ]; then
        echo missing
        return
    fi
    local lease now
    lease=$(cat "$LEASE_FILE" 2>/dev/null)
    case "$lease" in
        ''|*[!0-9]*) echo invalid; return ;;
    esac
    now=$(date -u +%s)
    echo "$((now - lease))"
}

read_phase() {
    if [ -r "$PHASE_FILE" ]; then
        tr -d '[:space:]' <"$PHASE_FILE"
    else
        echo unknown
    fi
}

# Evidence about the service itself, independent of the phase file the
# supervisor writes and independent of the client probe — a third,
# corroborating signal that llama-swap's own process is gone.
swap_process_present() {
    docker exec "$CONTAINER" pgrep -x llama-swap >/dev/null 2>&1
}

guard_pid() {
    systemctl --user show -p MainPID --value "$GUARD_UNIT" 2>/dev/null
}

guard_active() {
    [ "$(systemctl --user is-active "$GUARD_UNIT" 2>/dev/null)" = active ]
}

container_running() {
    [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" = true ]
}

# Polls client_probe until it fails (non-zero exit) or the overall budget is
# exhausted. Prints "<rc> <code> <latency>", where latency is measured from
# $1 (an epoch second the caller supplies — e.g. the moment the guard was
# killed, not the moment this function was entered), per the story's
# withdrawal-latency definition.
wait_for_fail_closed() {
    local since_epoch="$1" start_wall rc code
    start_wall=$(date +%s)
    while :; do
        read -r rc code <<<"$(client_probe)"
        [ "$rc" -ne 0 ] && break
        [ "$(($(date +%s) - start_wall))" -ge "$DRILL_TIMEOUT" ] && break
        sleep "$POLL_INTERVAL"
    done
    printf '%s %s %s\n' "$rc" "$code" "$(($(date +%s) - since_epoch))"
}

# Polls client_probe until it succeeds with 200, or the budget is exhausted.
# Latency measured from $1 per the story's recovery-latency definition.
wait_for_serving() {
    local since_epoch="$1" start_wall rc code
    start_wall=$(date +%s)
    while :; do
        read -r rc code <<<"$(client_probe)"
        [ "$rc" -eq 0 ] && [ "$code" = 200 ] && break
        [ "$(($(date +%s) - start_wall))" -ge "$DRILL_TIMEOUT" ] && break
        sleep "$POLL_INTERVAL"
    done
    printf '%s %s %s\n' "$rc" "$code" "$(($(date +%s) - since_epoch))"
}

# Polls the phase file until it reads exactly $2, or PHASE_SETTLE_TIMEOUT
# elapses. The supervisor writes draining before withdrawn (stop_swap sets
# draining, then withdrawn only once the drain completes), so a fail-closed
# scenario's client can already be refused while the phase is still
# transitional -- CYCLE 4 finding. Sampling the phase exactly once right
# after the client fails is a race with that write; this settles it first.
# Prints "<final-phase> <elapsed-seconds-since $1>". If the target is never
# reached within budget, the last-observed phase is returned as-is so the
# caller's own withdrawn-check reports it as the real failure it is.
wait_for_phase() {
    local since_epoch="$1" target="$2" start_wall phase
    start_wall=$(date +%s)
    while :; do
        phase=$(read_phase)
        [ "$phase" = "$target" ] && break
        [ "$(($(date +%s) - start_wall))" -ge "$PHASE_SETTLE_TIMEOUT" ] && break
        sleep "$POLL_INTERVAL"
    done
    printf '%s %s\n' "$phase" "$(($(date +%s) - since_epoch))"
}

# One fail-closed evidence block. ALL FOUR of lease/phase/process/client-probe
# must independently show withdrawal for the scenario to PASS.
assert_fail_closed() {
    local scenario="$1" action="$2" since_epoch="$3" latency_label="$4"
    local ss_before ss_after rc code elapsed lease_age phase phase_elapsed swap_state ok=1 reasons=""

    ss_before=$(ss_snapshot)
    echo
    echo "=== scenario $scenario: $action ==="
    printf 'timestamp (UTC)      : %s\n' "$(now_utc)"
    printf 'action                : %s\n' "$action"

    read -r rc code elapsed <<<"$(wait_for_fail_closed "$since_epoch")"

    # The client can already be refused while the supervisor is still in
    # `draining` (it writes `withdrawn` only once the stop completes) --
    # poll until the phase settles, bounded by PHASE_SETTLE_TIMEOUT, before
    # judging it. This is a distinct moment from the client-observed
    # failure above and is reported as its own figure, never substituted
    # for the withdrawal latency.
    read -r phase phase_elapsed <<<"$(wait_for_phase "$since_epoch" withdrawn)"

    lease_age=$(read_lease_age)
    if swap_process_present; then swap_state=present; else swap_state=absent; fi
    ss_after=$(ss_snapshot)

    printf 'curl exit code        : %s (must be non-zero)\n' "$rc"
    printf 'HTTP code             : %s (expected 000 / no application response)\n' "$code"
    printf 'lease age             : %s (TTL=%ss)\n' "$lease_age" "$LEASE_TTL"
    printf 'phase                 : %s (expected withdrawn, polled up to %ss)\n' "$phase" "$PHASE_SETTLE_TIMEOUT"
    printf 'llama-swap process    : %s (expected absent)\n' "$swap_state"
    printf 'host publish socket — informational only: %s -> %s\n' \
        "${ss_before:-<no listener>}" "${ss_after:-<no listener>}"
    printf '%s: %ss\n' "$latency_label" "$elapsed"
    printf 'phase-settle latency (same reference point -> phase file reached withdrawn): %ss\n' "$phase_elapsed"

    [ "$rc" -ne 0 ] || { ok=0; reasons+="client request succeeded (curl exit 0); "; }
    [ "$code" = 000 ] || { ok=0; reasons+="HTTP code was '$code', not a transport failure; "; }
    [ "$phase" = withdrawn ] || { ok=0; reasons+="phase was '$phase', not withdrawn; "; }
    [ "$swap_state" = absent ] || { ok=0; reasons+="llama-swap process is still present; "; }
    case "$lease_age" in
        missing|invalid) ;;
        *)
            if [ "$lease_age" -le "$LEASE_TTL" ]; then
                ok=0
                reasons+="lease age ${lease_age}s is still within TTL ${LEASE_TTL}s; "
            fi
            ;;
    esac

    if [ "$ok" -eq 1 ]; then
        printf 'RESULT                : PASS\n'
    else
        printf 'RESULT                : FAIL — %s\n' "$reasons"
        FAILURES=$((FAILURES + 1))
    fi
}

# One recovery evidence block. Nothing weaker than a fresh lease plus an
# authenticated 200 counts.
assert_serving() {
    local scenario="$1" action="$2" since_epoch="$3" latency_label="$4"
    local ss_before rc code elapsed lease_age phase swap_state ok=1 reasons=""

    ss_before=$(ss_snapshot)
    echo
    echo "=== scenario $scenario: $action ==="
    printf 'timestamp (UTC)      : %s\n' "$(now_utc)"
    printf 'action                : %s\n' "$action"

    read -r rc code elapsed <<<"$(wait_for_serving "$since_epoch")"

    lease_age=$(read_lease_age)
    phase=$(read_phase)
    if swap_process_present; then swap_state=present; else swap_state=absent; fi

    printf 'curl exit code        : %s (must be 0)\n' "$rc"
    printf 'HTTP code             : %s (expected 200, authenticated)\n' "$code"
    printf 'lease age             : %s (TTL=%ss)\n' "$lease_age" "$LEASE_TTL"
    printf 'phase                 : %s (expected serving)\n' "$phase"
    printf 'llama-swap process    : %s (expected present)\n' "$swap_state"
    printf 'host publish socket — informational only: %s -> %s\n' \
        "${ss_before:-<no listener>}" "$(ss_snapshot)"
    printf '%s: %ss\n' "$latency_label" "$elapsed"

    [ "$rc" -eq 0 ] || { ok=0; reasons+="client request did not succeed (curl exit $rc); "; }
    [ "$code" = 200 ] || { ok=0; reasons+="HTTP code was '$code', not 200; "; }
    [ "$phase" = serving ] || { ok=0; reasons+="phase was '$phase', not serving; "; }
    [ "$swap_state" = present ] || { ok=0; reasons+="llama-swap process is not present; "; }
    case "$lease_age" in
        missing|invalid) ok=0; reasons+="lease age is $lease_age, not fresh; " ;;
        *)
            if [ "$lease_age" -gt "$LEASE_TTL" ]; then
                ok=0
                reasons+="lease age ${lease_age}s exceeds TTL ${LEASE_TTL}s; "
            fi
            ;;
    esac

    if [ "$ok" -eq 1 ]; then
        printf 'RESULT                : PASS\n'
    else
        printf 'RESULT                : FAIL — %s\n' "$reasons"
        FAILURES=$((FAILURES + 1))
    fi
}

# shellcheck disable=SC2329  # invoked indirectly via `trap cleanup ...` below
cleanup() {
    rm -f "${AUTH_HEADER_FILE:-}" "${CURL_ERR_FILE:-}"
    # The trap is armed before the lock is acquired (so a failed acquisition
    # still cleans up AUTH_HEADER_FILE above), which means a losing run's own
    # exit reaches this function too. Everything below this point -- removing
    # the lock file, restoring ENV_BACKUP, and restarting the guard/container
    # -- must run only for the process that actually acquired the lock.
    # Otherwise a losing run deletes the winning run's lock and restarts the
    # guard/container the in-flight drill deliberately stopped, corrupting it
    # and defeating the lock entirely.
    [ "${LOCK_ACQUIRED:-0}" -eq 1 ] || return 0
    rm -f "$LOCK_FILE"
    # Scenario e leaves ENV_BACKUP set for as long as $ENV_FILE holds the
    # broken ROCM_SMI value. If the script dies or is interrupted before the
    # scenario restores it itself, restore it here too, before touching the
    # guard unit, so a killed drill never leaves the deployed host config
    # pointed at a sensor binary that does not exist.
    if [ -n "${ENV_BACKUP:-}" ] && [ -r "$ENV_BACKUP" ]; then
        mv -f "$ENV_BACKUP" "$ENV_FILE"
        systemctl --user restart "$GUARD_UNIT" >/dev/null 2>&1
    fi
    guard_active || systemctl --user start "$GUARD_UNIT" >/dev/null 2>&1
    container_running || docker start "$CONTAINER" >/dev/null 2>&1
}
LOCK_ACQUIRED=0
trap cleanup EXIT INT TERM

if ! ( set -o noclobber; : >"$LOCK_FILE" ) 2>/dev/null; then
    echo "FATAL another lease-drill.sh run appears to be in progress ($LOCK_FILE exists)" >&2
    exit 2
fi
LOCK_ACQUIRED=1

# --- preconditions ------------------------------------------------------------
guard_active || { echo "FATAL $GUARD_UNIT is not active; bring the node to a healthy baseline before drilling" >&2; exit 2; }
container_running || { echo "FATAL container $CONTAINER is not running; bring the node to a healthy baseline before drilling" >&2; exit 2; }
baseline_rc_code=$(client_probe)
read -r baseline_rc baseline_code <<<"$baseline_rc_code"
if [ "$baseline_rc" -ne 0 ] || [ "$baseline_code" != 200 ]; then
    echo "FATAL baseline authenticated request to $ENDPOINT$PROBE_PATH did not return 200 (exit=$baseline_rc code=$baseline_code); bring the node to a healthy baseline before drilling" >&2
    exit 2
fi

echo "lease-drill.sh starting at $(now_utc)"
echo "endpoint=$ENDPOINT probe_path=$PROBE_PATH guard_unit=$GUARD_UNIT container=$CONTAINER lease_ttl=${LEASE_TTL}s poll_interval=${POLL_INTERVAL}s state_dir=$STATE_DIR"
echo "note: host ss output below is context only — docker holds the publish socket open for the container's whole lifetime and it does not close when llama-swap stops"

# --- scenario a: reboot ordering race -----------------------------------------
# EDGE-WORKER-CONTRACT Issue 2: on a real reboot, docker's restart policy can
# bring the container back before graphical-session.target has started the
# guard. Reproduced here without an actual reboot: stop the guard, wait for
# its existing lease to genuinely age past TTL (the way a real boot's much
# longer wall-clock time would on its own), then restart only the container
# and prove a real client request cannot complete until a fresh lease exists.
systemctl --user stop "$GUARD_UNIT"
guard_death_epoch=$(date -u +%s)
echo
echo "guard stopped at $(now_utc); waiting ${LEASE_TTL}s + margin for its existing lease to age past TTL, the way a real reboot's boot time would"
sleep "$((LEASE_TTL + POLL_INTERVAL + 2))"
docker restart "$CONTAINER" >/dev/null
assert_fail_closed a \
    "container restarted with the guard stopped and its lease stale (reboot ordering race)" \
    "$guard_death_epoch" \
    "observed latency (guard death -> first client-observed unavailable request)"

lease_fresh_wait_epoch=$(date -u +%s)
systemctl --user start "$GUARD_UNIT"
assert_serving a-recovery \
    "guard started after the container came back" \
    "$lease_fresh_wait_epoch" \
    "recovery latency (guard restart -> first successful authenticated request)"

# --- scenario b: container/docker restart -------------------------------------
# The guard stays up and its lease stays fresh throughout; only the
# container restarts. Proves the supervisor's startup gate checks the lease
# (not only interactive-claim) even on the fast path where a fresh lease
# already exists.
guard_active || { echo "FATAL guard did not come back for scenario b" >&2; exit 1; }
restart_epoch=$(date -u +%s)
docker restart "$CONTAINER" >/dev/null
assert_serving b \
    "container restarted (docker restart), guard already healthy" \
    "$restart_epoch" \
    "observed latency (container restart -> first successful authenticated request)"

# --- scenario c: guard crash ---------------------------------------------------
# The crash itself is a direct `kill -9` to the guard's own PID, never
# `systemctl stop` — a clean stop is a materially weaker claim than an actual
# crash. Immediately afterward this script also runs `systemctl --user stop`
# on the now-dead unit, purely so Restart=always does not respawn it mid
# observation and blur the measured latency; that stop is drill bookkeeping,
# not the crash mechanism, and it only runs after the kill already happened.
pid=$(guard_pid)
if [ -z "$pid" ] || [ "$pid" = 0 ]; then
    echo "FATAL could not read $GUARD_UNIT's MainPID" >&2
    exit 1
fi
kill_epoch=$(date -u +%s)
kill -KILL "$pid" 2>/dev/null
systemctl --user stop "$GUARD_UNIT" >/dev/null 2>&1
echo
echo "guard PID $pid killed with SIGKILL at $(now_utc) (unit then stopped only to hold the crash steady for observation)"
assert_fail_closed c \
    "guard process killed (SIGKILL); lease left to expire (TTL=${LEASE_TTL}s)" \
    "$kill_epoch" \
    "withdrawal latency (guard death -> first client-observed unavailable request)"

# --- scenario d: guard restart -------------------------------------------------
guard_restart_epoch=$(date -u +%s)
systemctl --user start "$GUARD_UNIT"
assert_serving d \
    "guard restarted after the crash" \
    "$guard_restart_epoch" \
    "recovery latency (guard restart -> first successful authenticated request)"

# --- scenario e: sensor blindness (STORY-035-6a cycle 5) -----------------------
# The production defect this reproduces was never a crash: the guard process
# ran the whole time, looping on schedule, while its GPU sampler silently
# failed from boot onward (rocm-smi not on the boot-time PATH). The original
# lease renewed on every loop pass regardless, so "the process is alive" kept
# reading as "safe" while the guard was blind. Fix: a renewal now requires a
# valid sample, so breaking only the sampler -- never touching the guard unit's
# running state via kill or stop -- must still walk the lease to staleness and
# withdraw service within one TTL, with no crash anywhere in the chain.
#
# Editing the deployed env file is the mechanism, not a stand-in for one:
# EnvironmentFile= is what actually feeds ROCM_SMI to the unit, so this is the
# same lever a real misconfiguration would pull. Backed up and restored
# unconditionally -- including from cleanup() on an interrupted run -- so a
# failed drill never leaves a host pointed at a sensor binary that does not
# exist.
guard_active || { echo "FATAL guard did not come back before scenario e" >&2; exit 1; }
[ -r "$ENV_FILE" ] || { echo "FATAL host env file '$ENV_FILE' (--env-file / EDGE_HOST_ENV_FILE) is not readable; cannot drill sensor blindness" >&2; exit 1; }
ENV_BACKUP="${ENV_FILE}.lease-drill.bak.$$"
cp -p "$ENV_FILE" "$ENV_BACKUP"
BROKEN_ROCM_SMI="/nonexistent-rocm-smi-lease-drill"
if grep -q '^ROCM_SMI=' "$ENV_FILE"; then
    sed -i "s|^ROCM_SMI=.*|ROCM_SMI=$BROKEN_ROCM_SMI|" "$ENV_FILE"
else
    printf 'ROCM_SMI=%s\n' "$BROKEN_ROCM_SMI" >>"$ENV_FILE"
fi
blind_epoch=$(date -u +%s)
systemctl --user restart "$GUARD_UNIT"
echo
echo "guard restarted at $(now_utc) with ROCM_SMI=$BROKEN_ROCM_SMI; the process keeps running (guard_active=$(guard_active && echo yes || echo no)) but every sample now fails"
assert_fail_closed e \
    "GPU sensor unreachable (ROCM_SMI points at a nonexistent path) while the guard process keeps running; lease left to expire (TTL=${LEASE_TTL}s)" \
    "$blind_epoch" \
    "withdrawal latency (sampler broken -> first client-observed unavailable request)"

mv -f "$ENV_BACKUP" "$ENV_FILE"
unset ENV_BACKUP
sensor_restored_epoch=$(date -u +%s)
systemctl --user restart "$GUARD_UNIT"
assert_serving e-recovery \
    "ROCM_SMI restored and guard restarted" \
    "$sensor_restored_epoch" \
    "recovery latency (sensor restored -> first successful authenticated request)"

echo
if [ "$FAILURES" -eq 0 ]; then
    echo "lease-drill.sh: ALL SCENARIOS PASSED"
    exit 0
fi
echo "lease-drill.sh: $FAILURES SCENARIO(S) FAILED"
exit 1
