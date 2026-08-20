#!/usr/bin/env bash
#
# Interactive-priority drill (STORY-035-6 AC4).
#
# Starts a competing GPU workload, then watches the three things that have to
# happen and times each of them:
#
#   1. the guard notices and claims the GPU              (detection latency)
#   2. the LAN endpoint stops accepting connections       (withdrawal latency)
#   3. after the workload ends and the hold-down expires,
#      the endpoint comes back                            (recovery latency)
#
# Step 2 is the one that matters for the contract. EDGE-WORKER-CONTRACT §1.2
# requires that entering INTERACTIVE stops new work at the inference endpoint,
# not just that the heartbeat says so — so this drill measures the socket, not
# the state file. A refused connection is the wire failure agentgateway's
# passive health acts on; 35.5 measured that as an instant client-visible
# failure rather than the 10 s connect deadline a dropped packet costs.
#
# Prerequisites: scripts/edge-interactive-guard.sh is already watching (systemd
# unit, or a container), and the edge container is up and serving.
#
# Usage:
#   interactive-drill.sh [--vram-mb N] [--seconds N] [--timeout N]
#
# Environment:
#   EDGE_BIND_ADDR    address the endpoint is published on (required)
#   EDGE_PORT         port it is published on (default 8443)
#   EDGE_STATE_DIR    shared claim/phase directory (default: the host path the
#                     systemd units use, ${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state)
#   GPU_LOAD_BIN      compiled testing/gpu-load.cpp (default: ./gpu-load)

set -uo pipefail

VRAM_MB=512
SECONDS_RUN=60
DRILL_TIMEOUT=420

while [ "$#" -gt 0 ]; do
    case "$1" in
        --vram-mb) VRAM_MB="$2"; shift 2 ;;
        --seconds) SECONDS_RUN="$2"; shift 2 ;;
        --timeout) DRILL_TIMEOUT="$2"; shift 2 ;;
        -h|--help)
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

ADDR="${EDGE_BIND_ADDR:?EDGE_BIND_ADDR is required}"
PORT="${EDGE_PORT:-8443}"
STATE_DIR="${EDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state}"
LOAD_BIN="${GPU_LOAD_BIN:-./gpu-load}"

CLAIM_FILE="$STATE_DIR/interactive-claim"
PHASE_FILE="$STATE_DIR/phase"

[ -x "$LOAD_BIN" ] || { echo "FATAL '$LOAD_BIN' is not executable; build testing/gpu-load.cpp first" >&2; exit 2; }

START=$(date +%s)

elapsed() { echo $(($(date +%s) - START)); }

endpoint_open() {
    timeout 2 bash -c "exec 3<>/dev/tcp/$ADDR/$PORT" 2>/dev/null
}

phase() {
    [ -r "$PHASE_FILE" ] && tr -d '[:space:]' <"$PHASE_FILE" || echo unknown
}

# `kill -0` still succeeds for a child that has exited but not been reaped, so
# on its own it would report the workload as running until the drill reaps it —
# and the drill reaps it only after the loop, so "workload end" would never be
# observed and the recovery timing would be measured against nothing.
# edge-supervisor.sh reads the same thing out of /proc for the same reason.
load_running() {
    kill -0 "$LOAD_PID" 2>/dev/null || return 1
    local state
    state=$(awk '/^State:/ {print $2}' "/proc/$LOAD_PID/status" 2>/dev/null)
    [ "$state" != "Z" ]
}

mark() {
    printf 't+%-4ss  %s\n' "$(elapsed)" "$*"
}

if ! endpoint_open; then
    echo "FATAL endpoint $ADDR:$PORT is not accepting connections; nothing to withdraw" >&2
    exit 2
fi
if [ -e "$CLAIM_FILE" ]; then
    echo "FATAL an interactive claim is already present; release it before drilling" >&2
    exit 2
fi

mark "baseline: endpoint accepting, phase=$(phase), no claim"

"$LOAD_BIN" "$VRAM_MB" "$SECONDS_RUN" >/dev/null 2>&1 &
LOAD_PID=$!
mark "started competing GPU workload (${VRAM_MB}MB, ${SECONDS_RUN}s, pid $LOAD_PID)"

T_CLAIM=""
T_DOWN=""
T_LOADEND=""
T_RELEASE=""
T_UP=""

while [ "$(elapsed)" -lt "$DRILL_TIMEOUT" ]; do
    if [ -z "$T_CLAIM" ] && [ -e "$CLAIM_FILE" ]; then
        T_CLAIM=$(elapsed)
        mark "GUARD CLAIMED the GPU (detection latency ${T_CLAIM}s)"
    fi
    if [ -n "$T_CLAIM" ] && [ -z "$T_DOWN" ] && ! endpoint_open; then
        T_DOWN=$(elapsed)
        mark "ENDPOINT REFUSING connections, phase=$(phase) (withdrawal latency ${T_DOWN}s)"
    fi
    if [ -z "$T_LOADEND" ] && ! load_running; then
        T_LOADEND=$(elapsed)
        mark "competing workload finished"
    fi
    if [ -n "$T_CLAIM" ] && [ -z "$T_RELEASE" ] && [ ! -e "$CLAIM_FILE" ]; then
        T_RELEASE=$(elapsed)
        mark "guard released the claim"
    fi
    if [ -n "$T_RELEASE" ] && [ -z "$T_UP" ] && endpoint_open; then
        T_UP=$(elapsed)
        mark "ENDPOINT ACCEPTING again, phase=$(phase) (recovery ${T_UP}s)"
        break
    fi
    sleep 1
done

wait "$LOAD_PID" 2>/dev/null

echo
echo "--- drill summary ---"
printf 'detection  (workload start -> claim)          : %s\n' "${T_CLAIM:-NOT OBSERVED}"
printf 'withdrawal (workload start -> refused)        : %s\n' "${T_DOWN:-NOT OBSERVED}"
printf 'workload end                                  : %s\n' "${T_LOADEND:-NOT OBSERVED}"
printf 'release    (workload start -> claim cleared)  : %s\n' "${T_RELEASE:-NOT OBSERVED}"
printf 'recovery   (workload start -> accepting)      : %s\n' "${T_UP:-NOT OBSERVED}"

if [ -z "$T_CLAIM" ] || [ -z "$T_DOWN" ] || [ -z "$T_UP" ]; then
    echo "DRILL FAILED: the node did not withdraw and return"
    exit 1
fi
echo "DRILL PASSED"
