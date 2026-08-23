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
# The guard-ready lease (STORY-035-6a Part 2)
# ---------------------------------------------
# interactive-claim alone made the interlock fail OPEN in one specific way:
# it is a positive assertion the guard makes when it wants the GPU back, and
# a guard that has crashed or hung makes no assertions at all -- absence of a
# claim looks identical whether the desktop is quiet or the detector is dead.
# That is exactly what happened in production: the container came back after
# a reboot on its own restart policy, the guard did not, and the endpoint
# served with no interlock watching it, because nothing was checking that the
# guard was actually alive to watch.
#
# guard-lease flips that: it is a POSITIVE, continuously-renewed assertion of
# guard liveness (edge-interactive-guard.sh's `watch` loop renews it every
# EDGE_GUARD_INTERVAL), and this process treats it exactly like
# interactive-claim's opposite -- no fresh lease closes the listener, with no
# distinction made between "guard never started", "guard crashed" and "guard
# is hung". All three mean the same thing here: nothing can currently prove a
# human wanting the GPU back would be noticed, so the safe default is closed.
# This is enforced independently of interactive-claim and requires no
# Kubernetes, no agentgateway, no heartbeat and no network round-trip --
# EDGE_STATE_DIR is a local bind mount and every check below is a local file
# read, which is what keeps this working on a node that is entirely offline.
#
# Environment:
#   EDGE_STATE_DIR      state directory shared with the host guard (default /edge/state)
#   EDGE_CONFIG         llama-swap config path (default /etc/llama-swap/config.yaml)
#   EDGE_TLS_CERT       server certificate (default /pki/edge-host.crt)
#   EDGE_TLS_KEY        server key (default /pki/edge-host.key)
#   EDGE_LISTEN         in-container listen address (default :8443)
#   EDGE_POLL_INTERVAL  seconds between claim-file/lease checks (default 1)
#   EDGE_GUARD_LEASE_TTL
#                       seconds a guard lease stays fresh (default 6) --
#                       three times the guard's own default 2s sampling
#                       interval, chosen so one slow rocm-smi call does not
#                       trip a false withdrawal, while a genuinely dead or
#                       hung guard is still caught inside two of its own
#                       sampling periods. MUST match the value
#                       edge-interactive-guard.sh's host units run with, or
#                       one side's idea of "fresh" disagrees with the other's.
#                       The worst-case exposure this design accepts -- guard
#                       dies the instant after renewing, so its lease reads
#                       as fresh for the full TTL -- is
#                       EDGE_GUARD_LEASE_TTL + EDGE_POLL_INTERVAL (default
#                       6s + 1s = 7s) between guard death and listener
#                       withdrawal. That 7s window is the residual exposure;
#                       see README.md "Guard-ready lease" for the measured
#                       drill numbers and for what shrinks it (a shorter
#                       interval/TTL trades false-positive risk for it).
#   EDGE_DRAIN_TIMEOUT  seconds to let in-flight requests finish (default 40)
#   EDGE_MODELS_DIR     read-only mount of the model store to scan for the
#                       cache-observation manifest (default /models)
#   EDGE_CACHE_SCAN_INTERVAL
#                       seconds between manifest refreshes (default 60) --
#                       independent of EDGE_POLL_INTERVAL because a filesystem
#                       walk of the model store is far more expensive than a
#                       claim/lease file read and does not need that cadence.
#                       Must stay comfortably below the freshness bound
#                       scripts/edge-heartbeat.sh's EDGE_CACHE_MANIFEST_MAX_AGE
#                       enforces on the host side (see env.example), the same
#                       relationship EDGE_GUARD_LEASE_TTL has to
#                       EDGE_GUARD_INTERVAL above.
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
#   guard-lease          (read)  epoch seconds, renewed by the host guard's
#                                `watch` loop every EDGE_GUARD_INTERVAL;
#                                missing or older than EDGE_GUARD_LEASE_TTL
#                                means "the guard cannot currently prove it is
#                                alive" and closes the listener regardless of
#                                interactive-claim.
#   phase               (write) serving | draining | withdrawn — what this
#                               process is actually doing right now, which is
#                               what scripts/edge-heartbeat.sh turns into
#                               SERVING/AVAILABLE, DRAINING and INTERACTIVE.
#   cache-manifest.json (write) STORY-035-9c Part A. Which model artifacts are
#                               present under EDGE_MODELS_DIR right now, plus
#                               freshness/completeness metadata, so the host
#                               can derive cached_models without ever reading
#                               the docker-volume-backed model store itself —
#                               see "Cache-observation manifest" below.
#
# Cache-observation manifest (STORY-035-9c Part A)
# --------------------------------------------------
# The model store (`edge-model-cache`) is a Docker-managed volume; its
# mountpoint requires root and the host user cannot read it, so
# scripts/edge-heartbeat.sh cannot derive cached_models from the filesystem
# the way it derives everything else. `docker exec` from the host was
# considered and REJECTED (owner security ruling): a heartbeat unit that can
# exec into a container has Docker control-plane authority, and a process that
# can drive Docker can start a privileged container — the exact threat this
# supervisor exists to keep away from the host daemons. A read-only /models
# mount does not make `docker exec` a read-only capability.
#
# The approved mechanism observes over the EXISTING shared-state boundary
# instead of opening a new one: this process already has the real store at
# EDGE_MODELS_DIR (:ro) and already writes EDGE_STATE_DIR, which the host
# heartbeat already reads. write_cache_manifest() below does nothing but a
# filenames/existence scan of EDGE_MODELS_DIR — it never opens, reads or hints
# at the CONTENTS of a model artifact, and it never loads anything — and
# writes the result to cache-manifest.json, ATOMICALLY (temp file in the same
# directory, then `mv`) so the host can never observe a partially-written
# scan. It runs on the same cadence as the claim/lease poll, gated by
# EDGE_CACHE_SCAN_INTERVAL so a large model tree is not restatted every
# EDGE_POLL_INTERVAL.
#
# The manifest publishes relative artifact PATHS / presence only — never a
# catalog model_id. Translating a path to a catalog id through
# model-id-map.json is scripts/edge-heartbeat.sh's job on the host side,
# exactly as it already translates llama-swap's runtime model ids; this
# process has no need of, and is never given, that map. An artifact this scan
# finds that the host-side map does not recognise is the host's problem to
# warn about and ignore, not this process's to guess at.
#
# Failure is always published, never hidden: if EDGE_MODELS_DIR is not a
# directory (mount missing, volume not attached), the manifest is written with
# `complete: false` and an empty artifact list rather than left stale or
# skipped — a host reading a fresh-but-incomplete manifest must report no
# cached models, the same as it must for a stale one. on_signal() below also
# removes the manifest on a graceful stop, so a deliberately stopped container
# is reflected at the host immediately rather than only after the manifest
# ages past the host's freshness bound.

set -uo pipefail

STATE_DIR="${EDGE_STATE_DIR:-/edge/state}"
CONFIG="${EDGE_CONFIG:-/etc/llama-swap/config.yaml}"
TLS_CERT="${EDGE_TLS_CERT:-/pki/edge-host.crt}"
TLS_KEY="${EDGE_TLS_KEY:-/pki/edge-host.key}"
LISTEN="${EDGE_LISTEN:-:8443}"
POLL_INTERVAL="${EDGE_POLL_INTERVAL:-1}"
LEASE_TTL="${EDGE_GUARD_LEASE_TTL:-6}"
DRAIN_TIMEOUT="${EDGE_DRAIN_TIMEOUT:-40}"
SWAP_BIN="${LLAMA_SWAP_BIN:-/usr/local/bin/llama-swap}"
MODELS_DIR="${EDGE_MODELS_DIR:-/models}"
CACHE_SCAN_INTERVAL="${EDGE_CACHE_SCAN_INTERVAL:-60}"

CLAIM_FILE="$STATE_DIR/interactive-claim"
LEASE_FILE="$STATE_DIR/guard-lease"
PHASE_FILE="$STATE_DIR/phase"
CACHE_MANIFEST_FILE="$STATE_DIR/cache-manifest.json"

SWAP_PID=""
SHUTTING_DOWN=0
LAST_CACHE_SCAN=0

log() {
    printf '%s supervisor: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

set_phase() {
    printf '%s\n' "$1" >"$PHASE_FILE" 2>/dev/null || log "WARN cannot write $PHASE_FILE"
}

# Minimal JSON string escaping for the two characters that can appear in a
# filesystem path and would otherwise break the hand-built JSON below. No jq
# dependency in this image on purpose -- the manifest's shape is fixed and
# small enough that adding one would be a container-image change to buy
# nothing scripts/edge-heartbeat.sh's own jq parsing does not already give the
# host side.
json_escape_path() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# Filenames/existence scan of EDGE_MODELS_DIR ONLY -- see "Cache-observation
# manifest" above for why this exists and what it must never do. `find
# -type f` and `-printf` stat directory entries; neither opens a file's
# contents.
write_cache_manifest() {
    local scanned_at tmp complete=true rel first=1

    scanned_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    tmp=$(mktemp "$STATE_DIR/.cache-manifest.XXXXXX" 2>/dev/null) || {
        log "WARN cannot create temp file for cache manifest"
        return
    }

    {
        printf '{\n  "scanned_at": "%s",\n' "$scanned_at"
        if [ -d "$MODELS_DIR" ]; then
            printf '  "complete": true,\n  "artifacts": [\n'
            while IFS= read -r rel; do
                [ -n "$rel" ] || continue
                if [ "$first" -eq 1 ]; then first=0; else printf ',\n'; fi
                printf '    "%s"' "$(json_escape_path "$rel")"
            done < <(cd "$MODELS_DIR" 2>/dev/null && find . -type f -printf '%P\n' 2>/dev/null | LC_ALL=C sort)
            printf '\n  ]\n}\n'
        else
            complete=false
            printf '  "complete": false,\n  "artifacts": []\n}\n'
        fi
    } >"$tmp"

    if [ "$complete" = false ]; then
        log "WARN models directory '$MODELS_DIR' is not present; publishing an incomplete cache manifest"
    fi

    # mktemp creates the file 0600, and this process's uid is not the host
    # user's - published that way, the host heartbeat cannot read it and would
    # (correctly, fail-closed) report empty cached_models forever. The manifest
    # is a list of filenames, not a secret: make it world-readable BEFORE the
    # atomic rename so no reader ever sees a permission flap either.
    chmod 644 "$tmp" 2>/dev/null
    mv -f "$tmp" "$CACHE_MANIFEST_FILE" 2>/dev/null || {
        log "WARN failed to publish cache manifest"
        rm -f "$tmp"
    }
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

# Freshness of the host-side guard's lease -- see edge-interactive-guard.sh's
# renew_lease(). A missing file, an unparseable one, or one older than
# LEASE_TTL are all "not fresh": the guard might never have started, might
# have crashed, or might be hung inside a syscall that stopped it renewing.
# This process cannot tell those apart from here, and it does not need to --
# every one of them means "nothing can currently prove a human's interactive
# use would be caught," which is reason enough to close the listener
# regardless of what interactive-claim says.
lease_fresh() {
    local lease now age
    [ -r "$LEASE_FILE" ] || return 1
    lease=$(cat "$LEASE_FILE" 2>/dev/null) || return 1
    case "$lease" in
        ''|*[!0-9]*) return 1 ;;
    esac
    now=$(date -u +%s)
    age=$((now - lease))
    # A lease timestamped in the future (host clock stepped backward after the
    # guard wrote it) must never read as fresh: a negative age would otherwise
    # satisfy `<= LEASE_TTL` and keep serving indefinitely -- the fail-open
    # this check exists to prevent, just reached through the clock instead of
    # through the guard.
    [ "$age" -ge 0 ] && [ "$age" -le "$LEASE_TTL" ]
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
    # A deliberate, graceful stop is removed immediately rather than left to
    # age out: the host must never treat a stopped container's last-known-good
    # scan as still current. A crash (SIGKILL, no trap) skips this and falls
    # back to the freshness bound in scripts/edge-heartbeat.sh, which is the
    # same fail-to-empty guarantee on a longer, unavoidable timer.
    rm -f "$CACHE_MANIFEST_FILE"
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
# like a healthy edge that never serves. Likewise a missing or stale lease --
# on a fresh boot the guard has not run even once yet, and starting the
# listener open on that gap is precisely the production defect this story
# fixes. Start withdrawn, say so, and let the guard prove itself before
# serving.
if [ -e "$CLAIM_FILE" ]; then
    log "interactive claim present at startup; staying withdrawn"
    set_phase withdrawn
elif ! lease_fresh; then
    log "no fresh guard lease at startup (TTL=${LEASE_TTL}s); staying withdrawn until the guard proves it is alive"
    set_phase withdrawn
else
    start_swap
fi

# First scan happens immediately (LAST_CACHE_SCAN=0 makes the loop's own
# freshness check fire on its first pass), so a freshly started container does
# not report an empty cache for a whole EDGE_CACHE_SCAN_INTERVAL before its
# first heartbeat can see anything.
while [ "$SHUTTING_DOWN" -eq 0 ]; do
    now_epoch=$(date -u +%s)
    if [ $((now_epoch - LAST_CACHE_SCAN)) -ge "$CACHE_SCAN_INTERVAL" ]; then
        write_cache_manifest
        LAST_CACHE_SCAN=$now_epoch
    fi

    if [ -e "$CLAIM_FILE" ] || ! lease_fresh; then
        if swap_running; then
            if [ -e "$CLAIM_FILE" ]; then
                stop_swap "interactive claim"
            else
                stop_swap "guard lease stale or absent (TTL=${LEASE_TTL}s)"
            fi
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
