#!/usr/bin/env bash
#
# PID 1 of the edge-llama-swap container: runs llama-swap, and takes it off the
# network the moment the host claims the GPU for interactive work.
#
# Why a supervisor instead of `docker stop`
# -----------------------------------------
# EDGE-WORKER-CONTRACT §1.2 requires that entering INTERACTIVE or DRAINING does
# two things at once: publish the state on the next heartbeat, AND stop
# accepting new work at the inference endpoint, so the control-plane and
# data-plane views agree. The data-plane half has to be a 503 or a wire
# failure — those are the only signals agentgateway's passive health treats as
# a failure. llama-swap's other withdrawal shapes (removing the model from the
# config, or a null profile pin) answer 404, which the gateway reads as a
# working endpoint returning a client error, so it would keep sending traffic.
# Closing the listener gives connection-refused, which 35.5 measured as an
# instant client-visible failure and failover (0.002s) rather than the 10s
# connect deadline a dropped packet costs.
#
# The obvious way to close the listener is `docker stop`. That would mean the
# interactive guard — a thing that runs whenever the desktop is in use — needs
# the docker socket, and anything that can drive docker can start a privileged
# container. Watching a file instead keeps the guard unprivileged: it writes
# and removes one path, and this process, already inside the blast radius, does
# the rest.
#
# Environment:
#   EDGE_STATE_DIR      state directory shared with the host guard (default /edge/state)
#   EDGE_CONFIG         llama-swap config path (default /etc/llama-swap/config.yaml)
#   EDGE_TLS_CERT       server certificate (default /pki/edge-host.crt)
#   EDGE_TLS_KEY        server key (default /pki/edge-host.key)
#   EDGE_LISTEN         in-container listen address (default :8443)
#   EDGE_POLL_INTERVAL  seconds between claim-file checks (default 1)
#   EDGE_DRAIN_TIMEOUT  seconds to let in-flight requests finish (default 40)
#   LLAMA_SWAP_BIN      llama-swap binary (default /usr/local/bin/llama-swap)
#   EDGE_API_KEY_FILE   file holding the edge bearer credential. Preferred over
#                       EDGE_API_KEY: an env var set through compose is readable
#                       with `docker inspect`, a mounted file is not, and the
#                       file can be rotated without recreating the container.
#   EDGE_API_KEY        the credential itself, if no file is used. llama-swap's
#                       config reads it as ${env.EDGE_API_KEY} either way.
#
# Files it reads and writes under EDGE_STATE_DIR:
#   interactive-claim   (read)  present = the host wants the GPU back
#   phase               (write) serving | draining | withdrawn — what this
#                               process is actually doing right now, which is
#                               what scripts/edge-heartbeat.sh turns into
#                               SERVING/AVAILABLE, DRAINING and INTERACTIVE.

set -uo pipefail

STATE_DIR="${EDGE_STATE_DIR:-/edge/state}"
CONFIG="${EDGE_CONFIG:-/etc/llama-swap/config.yaml}"
TLS_CERT="${EDGE_TLS_CERT:-/pki/edge-host.crt}"
TLS_KEY="${EDGE_TLS_KEY:-/pki/edge-host.key}"
LISTEN="${EDGE_LISTEN:-:8443}"
POLL_INTERVAL="${EDGE_POLL_INTERVAL:-1}"
DRAIN_TIMEOUT="${EDGE_DRAIN_TIMEOUT:-40}"
SWAP_BIN="${LLAMA_SWAP_BIN:-/usr/local/bin/llama-swap}"

CLAIM_FILE="$STATE_DIR/interactive-claim"
PHASE_FILE="$STATE_DIR/phase"

SWAP_PID=""
SHUTTING_DOWN=0

log() {
    printf '%s supervisor: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

set_phase() {
    printf '%s\n' "$1" >"$PHASE_FILE" 2>/dev/null || log "WARN cannot write $PHASE_FILE"
}

# A dead-but-unreaped child still answers `kill -0`, so a plain signal probe
# would report a crashed llama-swap as healthy forever. Read the state out of
# /proc and treat Z (zombie) as gone.
swap_running() {
    [ -n "$SWAP_PID" ] || return 1
    kill -0 "$SWAP_PID" 2>/dev/null || return 1
    local state
    state=$(awk '/^State:/ {print $2}' "/proc/$SWAP_PID/status" 2>/dev/null)
    [ "$state" != "Z" ]
}

start_swap() {
    log "starting llama-swap on $LISTEN"
    "$SWAP_BIN" \
        --config "$CONFIG" \
        --listen "$LISTEN" \
        --tls-cert-file "$TLS_CERT" \
        --tls-key-file "$TLS_KEY" &
    SWAP_PID=$!
    set_phase serving
}

# Graceful stop. llama-swap closes the listener first and then drains, so new
# connections are refused from the moment SIGTERM lands while in-flight
# requests keep their answer. That is the DRAINING half of the contract's
# withdrawal; the refusal is what the gateway sees.
stop_swap() {
    local reason="$1" waited=0
    swap_running || return 0
    log "draining llama-swap ($reason)"
    set_phase draining
    kill -TERM "$SWAP_PID" 2>/dev/null
    while swap_running && [ "$waited" -lt "$DRAIN_TIMEOUT" ]; do
        sleep 1
        waited=$((waited + 1))
    done
    if swap_running; then
        log "drain exceeded ${DRAIN_TIMEOUT}s, killing"
        kill -KILL "$SWAP_PID" 2>/dev/null
    fi
    wait "$SWAP_PID" 2>/dev/null
    SWAP_PID=""
    log "llama-swap stopped after ${waited}s"
}

on_signal() {
    SHUTTING_DOWN=1
    stop_swap "container shutdown"
    set_phase withdrawn
    exit 0
}

trap on_signal TERM INT

mkdir -p "$STATE_DIR"

# The credential reaches llama-swap through its own environment, because that
# is the only thing its config's ${env.EDGE_API_KEY} macro can read. Sourcing
# it from a file here keeps it out of the container's declared environment,
# where `docker inspect` would show it to anyone who can reach the daemon.
if [ -n "${EDGE_API_KEY_FILE:-}" ]; then
    if [ ! -r "$EDGE_API_KEY_FILE" ]; then
        log "FATAL EDGE_API_KEY_FILE '$EDGE_API_KEY_FILE' is not readable"
        exit 2
    fi
    EDGE_API_KEY=$(tr -d '\r\n' <"$EDGE_API_KEY_FILE")
    export EDGE_API_KEY
fi

if [ -z "${EDGE_API_KEY:-}" ]; then
    # llama-swap treats an empty apiKeys list as default-allow. An unset
    # credential here would therefore not fail loudly, it would quietly publish
    # an unauthenticated LLM on the LAN — the one outcome EDGE-WORKER-CONTRACT
    # §2 exists to prevent.
    log "FATAL no EDGE_API_KEY or EDGE_API_KEY_FILE; refusing to start an unauthenticated endpoint"
    exit 2
fi

for required in "$CONFIG" "$TLS_CERT" "$TLS_KEY"; do
    if [ ! -r "$required" ]; then
        log "FATAL required file not readable: $required"
        exit 2
    fi
done

# A claim left behind by a crash must not be inherited silently: it would look
# like a healthy edge that never serves. Start withdrawn, say so, and let the
# guard clear it.
if [ -e "$CLAIM_FILE" ]; then
    log "interactive claim present at startup; staying withdrawn"
    set_phase withdrawn
else
    start_swap
fi

while [ "$SHUTTING_DOWN" -eq 0 ]; do
    if [ -e "$CLAIM_FILE" ]; then
        if swap_running; then
            stop_swap "interactive claim"
            set_phase withdrawn
        fi
    else
        if ! swap_running; then
            if [ -n "$SWAP_PID" ]; then
                # Distinguishes "we stopped it" from "it died". Only the latter
                # reaches here with a non-empty PID, because stop_swap clears it.
                wait "$SWAP_PID" 2>/dev/null
                log "llama-swap exited unexpectedly (status $?); letting the container restart"
                set_phase withdrawn
                exit 1
            fi
            start_swap
        fi
    fi
    sleep "$POLL_INTERVAL"
done
