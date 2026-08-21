#!/usr/bin/env bash
#
# Interactive-priority guard for the CachyOS RX 7900 XTX edge worker.
#
# EDGE-WORKER-CONTRACT §4 for this host: "the machine's interactive desktop
# work outranks AI; AI work is expected to yield, not fight for the GPU."
# §1.2 adds that the host decides its own state and never asks the cluster for
# permission — it announces, and it stops accepting work.
#
# This process is the detector. It does exactly one thing when it decides the
# desktop wants the GPU: it creates $EDGE_STATE_DIR/interactive-claim. The
# supervisor inside the container (scripts/edge-supervisor.sh) watches that
# path and drains llama-swap; the heartbeat producer watches it and publishes
# DRAINING then INTERACTIVE. Keeping the guard to one file write is what lets
# it run with no docker privileges and no cluster credentials beyond the
# read-only llama-swap query below.
#
# It also renews a second file, $EDGE_STATE_DIR/guard-lease, once per loop
# pass that completes a VALID GPU sample (STORY-035-6a Part 2, semantics
# tightened in cycle 5). interactive-claim answers "does the desktop want the
# GPU"; guard-lease answers a different question the claim file cannot: "can
# this detector currently see the GPU at all". A crashed or hung guard leaves
# no claim behind, so a design that infers safety from claim-presence alone
# fails OPEN exactly when it matters most -- which is the defect this lease
# exists to close. Cycle 5 closed a second, subtler version of the same
# defect: a guard whose rocm-smi has stopped resolving keeps looping and
# would keep renewing the lease under the original design, proving only that
# the process was alive while blind to the GPU it exists to watch. See "How
# the lease enforces fail-closed" below and edge-supervisor.sh, which is the
# enforcement point.
#
# How it decides
# --------------
# The question is not "is the GPU busy" — our own inference makes it busy. It
# is "is anything OTHER than our inference using the GPU". rocm-smi answers
# that directly: its KFD process table reports VRAM held by compute clients,
# and llama-server is the only compute client on this box, so
#
#   graphics_vram = vram_used - compute_vram
#
# is VRAM held by the desktop — the compositor at idle, plus whatever a game,
# a video editor or a browser adds. Subtract the measured idle compositor
# footprint and anything left over is a human wanting the GPU. No estimate of
# the model's own footprint enters the arithmetic, which matters because that
# figure moves with context size, quant and llama.cpp version.
#
# A GPU-heavy client that allocates almost no VRAM would not show up, so there
# is a second, narrower trip: sustained GPU busy time while llama-swap reports
# nothing loaded is, by definition, someone else's work.
#
# Two limits, both real, both measured on this host (see README.md):
#
#   * A loaded Qwen3.6-27B leaves ~2 GB of the card free. A desktop client can
#     therefore allocate only ~2 GB before it fails outright, which is less
#     headroom than the trip threshold plus the drain needs. Detection is fast
#     but it is not instant, and for the first few seconds of a game launch the
#     desktop is competing with a model that has not finished yielding.
#   * Interactive work that is latency-sensitive but neither VRAM- nor
#     compute-hungry (scrolling a browser) is invisible to both signals.
#
# Both are why the `claim` subcommand — called from a game launcher, a
# compositor rule, or by hand *before* the GPU is wanted — is the primary
# mechanism on this host and the detector is the backstop, not the reverse.
#
# How the lease enforces fail-closed
# -----------------------------------
# Only `watch` renews the lease -- `claim`/`release`/`status` are one-shot
# invocations, not a running loop, so they have no ongoing liveness to
# assert. That means the lease is absent from the very first moment after
# install, and stays absent until `watch` has completed at least one pass;
# edge-supervisor.sh treats absence exactly like staleness, so a node never
# serves before its guard has proven itself at least once. This is the fix
# for the second production defect (STORY-035-6a): after a reboot, docker's
# restart policy can bring the container back before graphical-session.target
# has started this unit, and the previous design inferred safety from guard
# liveness with nothing actually checking it -- a container that came up
# alone served with no interlock at all. The lease makes that state
# observable and, because the supervisor enforces it at its own listener
# rather than trusting this process to say so, unable to fail open.
#
# Cycle 5 (production defect: a guard blind since boot). A real cold boot
# exposed that "the watch loop is running" and "the watch loop can see the
# GPU" are different facts: the systemd user manager's boot-time PATH does
# not include rocm-smi's install location, so every sample failed from boot
# onward while the guard kept renewing its lease on schedule anyway --
# fail-closed on process liveness, fail-OPEN on detection. Two changes close
# it: ROCM_SMI must now be an absolute path (env.example, validated by
# install.sh) so it no longer depends on which PATH started this process,
# and renew_lease() below is now called only after a valid sample, so a
# guard that cannot see the GPU stops proving liveness and the existing TTL
# withdraws service exactly as it would for a crashed guard -- one safety
# clock, not two.
#
# Cycle 10 (production defect: /running latency could starve the lease). The
# loop used to call edge_gpu_holding_models() -- which queries llama-swap's
# /running over HTTP, bounded by EDGE_HTTP_TIMEOUT (default 5s) -- from
# inside sample(), before renew_lease() ran. A reachable-but-slow /running
# could therefore push a single loop pass past EDGE_GUARD_LEASE_TTL (default
# 6s) on its own: worst case EDGE_HTTP_TIMEOUT (5s) + the previous pass's
# INTERVAL (2s) sleep = ~7s between renewals, starving a healthy guard's
# lease and withdrawing a healthy node -- flapping caused by network latency
# in a query the lease was never supposed to depend on.
#
# Cycle 10's first fix moved /running onto a background poller writing a
# cache file, decoupling it from the sampling loop entirely. That solved the
# starvation but introduced a worse seam: the loop then compared a
# *live* compute-client count against a *stale, differently-timed*
# models_loaded reading, which could manufacture a false INTERACTIVE claim
# (over_the_line() rule 2) when the two numbers stopped describing the same
# observation window. Cycle 11 replaces the cache with a bounded synchronous
# query instead, so GPU sight and model telemetry are read in the same guard
# pass again -- see below.
#
# Cycle 11 (replaces the cycle-10 cache with a bounded synchronous query).
# renew_lease() is called immediately after a successful GPU sample, before
# /running is ever queried, so /running cannot delay renewal at all -- only
# the *next* sample's start. /running is then queried synchronously, but
# against EDGE_GUARD_RUNNING_TIMEOUT (default 1s) rather than the general
# EDGE_HTTP_TIMEOUT (default 5s): a dedicated, short, guard-only bound, never
# used for anything else that talks to llama-swap. A timeout or failure reads
# as models_loaded=unknown for that pass, exactly as a failed live query
# always has -- see over_the_line()'s comment on why `unknown` skips rule 2
# rather than standing in for zero.
#
# Worst case between two successful lease renewals is therefore
# EDGE_GUARD_RUNNING_TIMEOUT + INTERVAL + T_sample, where T_sample is
# rocm-smi's own execution time (typically well under a second). At the
# documented defaults (1s + 2s + <1s against a 6s TTL) that is comfortably
# inside the TTL whatever /running does: slow, hung, or simply wrong. cmd_watch
# validates this arithmetic at startup rather than trusting an operator not to
# misconfigure it -- see the check right before the case statement below.
#
# There is deliberately no cache, no cache-age timer, and no second clock:
# one guard pass, one bounded synchronous /running call, one TTL.
#
# Usage:
#   edge-interactive-guard.sh watch      # default: sample, decide, renew the
#                                         # lease, forever
#   edge-interactive-guard.sh claim      # withdraw now, and stay withdrawn
#   edge-interactive-guard.sh release    # hand the GPU back to AI work
#   edge-interactive-guard.sh status     # one sample, no action
#
# `claim` is sticky: `watch` will not release a claim it did not make, so a
# human or a launcher hook always outranks the detector.
#
# Environment (see env.example):
#   EDGE_STATE_DIR                 state directory shared with the container
#   EDGE_ENDPOINT/API_KEY/CA_CERT  read-only llama-swap query for loaded models
#   ROCM_SMI                       absolute path to rocm-smi (default:
#                                  /opt/rocm/bin/rocm-smi) -- MUST be
#                                  absolute; a bare command name resolves
#                                  through PATH, which at boot does not
#                                  include rocm-smi's install location
#                                  (STORY-035-6a cycle 5)
#   EDGE_GUARD_INTERVAL            seconds between samples, and between lease
#                                  renewals (default 2) -- see
#                                  EDGE_GUARD_LEASE_TTL in edge-supervisor.sh
#                                  for how this bounds the fail-closed window
#   EDGE_GUARD_TRIP_SAMPLES        consecutive samples over the line (default 3)
#   EDGE_GUARD_RELEASE_SAMPLES     consecutive samples under it (default 30)
#   EDGE_DESKTOP_BASELINE_MB       idle compositor VRAM, measured (default 1200)
#   EDGE_INTERACTIVE_VRAM_MB       graphics VRAM above baseline that trips
#                                  (default 512 — it has to fit inside the ~2 GB
#                                  a loaded model leaves free)
#   EDGE_INTERACTIVE_GPU_PCT       trip level for busy-with-nothing-loaded (default 25)
#   EDGE_GUARD_RUNNING_TIMEOUT     dedicated --max-time bound, in seconds, for
#                                  the guard's own synchronous /running query
#                                  (default 1) -- see "Cycle 11" above. Kept
#                                  far below EDGE_GUARD_LEASE_TTL on purpose;
#                                  cmd_watch refuses to start if it is not.

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=edge-common.sh
. "$SCRIPT_DIR/edge-common.sh"

# Read by edge_log() in edge-common.sh, and inherited by anything this script
# spawns, so a stray child's output is attributable too.
export EDGE_LOG_TAG=guard

INTERVAL="${EDGE_GUARD_INTERVAL:-2}"
TRIP_SAMPLES="${EDGE_GUARD_TRIP_SAMPLES:-3}"
RELEASE_SAMPLES="${EDGE_GUARD_RELEASE_SAMPLES:-30}"
BASELINE_MB="${EDGE_DESKTOP_BASELINE_MB:-1200}"
TRIP_VRAM_MB="${EDGE_INTERACTIVE_VRAM_MB:-512}"
TRIP_GPU_PCT="${EDGE_INTERACTIVE_GPU_PCT:-25}"

# Dedicated, short --max-time bound for the guard's own /running query
# (STORY-035-6a cycle 11) -- never EDGE_HTTP_TIMEOUT, which is the general
# 5s default other callers use. Validated against INTERVAL and
# EDGE_GUARD_LEASE_TTL at the top of cmd_watch.
RUNNING_TIMEOUT="${EDGE_GUARD_RUNNING_TIMEOUT:-1}"

# Marks a claim this process made, so an operator's manual claim is never
# released by the detector cooling off.
AUTO_MARKER="$EDGE_STATE_DIR/interactive-claim.auto"

# The guard-ready lease (STORY-035-6a Part 2). edge-supervisor.sh withdraws
# the listener whenever this file is missing or older than its
# EDGE_GUARD_LEASE_TTL, independent of interactive-claim entirely -- that is
# what makes a hung or killed guard fail closed instead of leaving a stale
# claim file as the only signal a desktop-sharing safety control has.
LEASE_FILE="$EDGE_STATE_DIR/guard-lease"

edge_require_tools jq curl date awk

mkdir -p "$EDGE_STATE_DIR"

# One GPU-only reading, as:
#   "<desktop_vram_mb> <gpu_pct> <used_mb> <total_mb> <compute_mb> <compute_clients>"
# where desktop_vram_mb is graphics VRAM above the measured idle baseline.
# Deliberately does not touch /running or models_loaded at all (STORY-035-6a
# cycle 11) -- this is exactly the call cmd_watch renews the lease on, and it
# must be able to succeed and return in local-rocm-smi time, never blocked on
# a network round-trip.
gpu_reading() {
    local gpu total used pct compute procs total_mb used_mb compute_mb desktop
    gpu=$(edge_gpu_sample) || return 1
    read -r total used pct compute procs <<<"$gpu"
    total_mb=$((total / 1024 / 1024))
    used_mb=$((used / 1024 / 1024))
    compute_mb=$((compute / 1024 / 1024))
    desktop=$((used_mb - compute_mb - BASELINE_MB))
    [ "$desktop" -lt 0 ] && desktop=0
    printf '%s %s %s %s %s %s\n' "$desktop" "$pct" "$used_mb" "$total_mb" "$compute_mb" "$procs"
}

# models_loaded for one guard pass: the literal string `unknown` when
# llama-swap did not answer within $1 seconds, otherwise a count of models
# that still hold their llama-server process (models on their way out
# included, because it is compared against KFD compute clients and an
# unloading model is still one of those).
#
# `unknown` rather than 0, because those are opposite facts and only one of
# them is a reason to withdraw the node. See over_the_line().
#
# $1: --max-time bound in seconds, passed straight to edge_gpu_holding_models.
# cmd_watch always passes RUNNING_TIMEOUT (the short, dedicated bound);
# cmd_status passes nothing, which falls back to EDGE_HTTP_TIMEOUT -- a
# one-shot diagnostic snapshot has no lease-renewal timing to protect.
models_loaded_reading() {
    local timeout="${1:-}" running loaded
    if running=$(edge_gpu_holding_models "$timeout" 2>/dev/null); then
        loaded=$(printf '%s\n' "$running" | grep -c '[^[:space:]]' || true)
    else
        loaded=unknown
    fi
    printf '%s' "$loaded"
}

# Combined GPU + models_loaded sample, as:
#   "<desktop_vram_mb> <gpu_pct> <models_loaded> <used_mb> <total_mb> <compute_mb> <compute_clients>"
# Used by cmd_status only -- a one-shot diagnostic snapshot, not the loop
# cmd_watch runs. cmd_watch calls gpu_reading() and models_loaded_reading()
# separately so it can renew the lease between them; see "Cycle 11" above.
sample() {
    local g desktop pct used_mb total_mb compute_mb procs loaded
    g=$(gpu_reading) || return 1
    read -r desktop pct used_mb total_mb compute_mb procs <<<"$g"
    loaded=$(models_loaded_reading)
    printf '%s %s %s %s %s %s %s\n' "$desktop" "$pct" "$loaded" "$used_mb" "$total_mb" "$compute_mb" "$procs"
}

# True when this sample says something other than our inference wants the GPU.
# Three independent reasons, because the three ways to use this card look
# nothing like each other in telemetry.
#
# `loaded` may be the string `unknown`, meaning llama-swap did not answer. The
# rules that need it are skipped for that sample rather than evaluated against a
# stand-in zero: a zero would make rule 2 read our own llama-server as a foreign
# compute client and withdraw a perfectly healthy node for the whole release
# hold-down, on nothing but a telemetry blip — inverting the rule exactly when
# its input is least reliable. Missing telemetry must not manufacture a claim.
#
# What it must not do either is manufacture a RELEASE, and it does not: a
# withdrawn node has no llama-swap to answer /running, so `unknown` is the
# normal reading while a claim is held. The other two rules keep evaluating on
# their own terms, and a quiet card still cools off into the release hold-down.
# That is why an unknown reading skips a rule rather than freezing the state
# machine.
over_the_line() {
    local desktop="$1" pct="$2" loaded="$3" procs="$4"
    # 1. A graphics client holds VRAM beyond the idle compositor: a game, a
    #    video editor, a browser compositing hardware-accelerated video. Needs
    #    no loaded-model reading at all.
    [ "$desktop" -gt "$TRIP_VRAM_MB" ] && return 0
    # 2. More compute clients than llama-swap has models loaded: somebody
    #    else's HIP/OpenCL process. Needs no size estimate, so it holds
    #    whatever the model or its context size is — but it does need a real
    #    count to compare against, so an unknown reading skips it.
    if [ "$loaded" != unknown ] && [ "$procs" -gt "$loaded" ]; then
        return 0
    fi
    # 3. The card is busy while nothing of ours is on it at all. Zero compute
    #    clients is itself proof that no llama-server is computing, so this rule
    #    stands on its own when the loaded-model count is unknown.
    if [ "$procs" -eq 0 ] && [ "$pct" -gt "$TRIP_GPU_PCT" ]; then
        [ "$loaded" = unknown ] && return 0
        [ "$loaded" -eq 0 ] && return 0
    fi
    return 1
}

do_claim() {
    local why="$1" auto="$2"
    if edge_claim_present; then
        # A manual claim on top of an automatic one still has to upgrade it to
        # sticky, or the detector would later release a claim a human made.
        # The reverse — an automatic claim landing on a manual one — must not
        # downgrade it, so only this direction touches the marker.
        if [ "$auto" -eq 0 ] && [ -e "$AUTO_MARKER" ]; then
            rm -f "$AUTO_MARKER"
            edge_log "CLAIM  ($why) — existing automatic claim upgraded to sticky"
        fi
        return 0
    fi
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$why" >"$EDGE_CLAIM_FILE"
    if [ "$auto" -eq 1 ]; then
        : >"$AUTO_MARKER"
    else
        rm -f "$AUTO_MARKER"
    fi
    edge_log "CLAIM  ($why) — endpoint withdrawing, heartbeat will report INTERACTIVE"
}

do_release() {
    local why="$1"
    edge_claim_present || return 0
    rm -f "$EDGE_CLAIM_FILE" "$AUTO_MARKER"
    edge_log "RELEASE ($why) — endpoint returning to service"
}

# Positive, continuously-renewed proof that THIS loop iteration completed a
# VALID GPU sample -- not merely that the process is alive (STORY-035-6a
# cycle 5). Called only from the success branch of cmd_watch's loop, on
# purpose: the first production defect made "the process exists" the
# liveness signal and that was wrong, but the same mistake one level down --
# "the loop is iterating" -- is just as wrong, because a guard whose
# rocm-smi has stopped resolving (a broken PATH, a removed binary) keeps
# looping forever while seeing nothing. A lease renewal must mean "the guard
# can currently see the GPU", so a failed sample must not refresh it.
#
# This is deliberately not a second failure-count timer. EDGE_GUARD_LEASE_TTL
# is the only grace period: one transient failed sample does not expire the
# lease immediately (the previous lease is still fresh for whatever TTL it
# has left), but sampling broken for longer than the TTL lets it expire
# exactly like a hung or crashed guard would, and edge-supervisor.sh
# withdraws the listener the same way either way. Blind must mean closed.
#
# Epoch seconds, not RFC3339: the supervisor's freshness check is pure
# integer arithmetic on the two ends of one host clock, and epoch avoids
# parsing a timestamp inside the container just to subtract two numbers.
# Written via a temp file + rename so a reader never observes a partial
# write.
renew_lease() {
    local tmp
    tmp="$LEASE_FILE.tmp.$$"
    printf '%s\n' "$(date -u +%s)" >"$tmp" 2>/dev/null && mv -f "$tmp" "$LEASE_FILE" 2>/dev/null
}

cmd_status() {
    local s desktop pct loaded used_mb total_mb compute_mb procs verdict
    if ! s=$(sample); then
        edge_log "no GPU sample available (ROCM_SMI=$ROCM_SMI)"
        return 1
    fi
    read -r desktop pct loaded used_mb total_mb compute_mb procs <<<"$s"
    verdict=no
    over_the_line "$desktop" "$pct" "$loaded" "$procs" && verdict=yes
    printf 'vram_used_mb=%s vram_total_mb=%s compute_vram_mb=%s compute_clients=%s desktop_vram_over_baseline_mb=%s gpu_pct=%s models_loaded=%s\n' \
        "$used_mb" "$total_mb" "$compute_mb" "$procs" "$desktop" "$pct" "$loaded"
    printf 'thresholds: desktop_vram>%sMB, compute_clients>models_loaded, or (gpu>%s%% with nothing of ours running); baseline=%sMB, trip=%s samples, release=%s samples, interval=%ss\n' \
        "$TRIP_VRAM_MB" "$TRIP_GPU_PCT" "$BASELINE_MB" "$TRIP_SAMPLES" "$RELEASE_SAMPLES" "$INTERVAL"
    printf 'over_the_line=%s claim=%s phase=%s\n' \
        "$verdict" "$(edge_claim_present && echo present || echo absent)" "$(edge_phase)"
}

# Refuses to start `watch` if RUNNING_TIMEOUT + INTERVAL is not comfortably
# inside EDGE_GUARD_LEASE_TTL (STORY-035-6a cycle 11: "validate the timing
# relationship at startup rather than letting an operator configure an
# obviously unsafe combination"). The host guard does not otherwise need
# EDGE_GUARD_LEASE_TTL -- renewal cadence, not the TTL, is what it controls --
# but reads it here purely for this sanity check, with the same default
# (6) the container and README document. A margin of one more INTERVAL beyond
# RUNNING_TIMEOUT + INTERVAL is required so one slow-but-under-timeout
# /running call still leaves real headroom before the TTL, not just enough
# to avoid failing by a hair. Arithmetic goes through awk, not bash `$(())`,
# because EDGE_GUARD_INTERVAL may legitimately be fractional (see do_release's
# call site below) and bash integer arithmetic rejects that outright.
check_running_timeout_safety() {
    local lease_ttl="${EDGE_GUARD_LEASE_TTL:-6}"
    awk -v rt="$RUNNING_TIMEOUT" -v iv="$INTERVAL" -v ttl="$lease_ttl" \
        'BEGIN { exit !(rt + iv + iv < ttl) }' \
        || edge_die "EDGE_GUARD_RUNNING_TIMEOUT ($RUNNING_TIMEOUT) + EDGE_GUARD_INTERVAL ($INTERVAL), with a margin of one more INTERVAL, must be less than EDGE_GUARD_LEASE_TTL ($lease_ttl) -- that is not the case, which lets a slow-but-answering /running call starve lease renewal exactly like STORY-035-6a cycle 10's regression"
}

cmd_watch() {
    local over=0 under=0 g desktop pct procs loaded
    local last_loaded_known=yes loaded_known
    check_running_timeout_safety
    edge_log "watching: interval=${INTERVAL}s trip=${TRIP_SAMPLES} release=${RELEASE_SAMPLES}" \
        "baseline=${BASELINE_MB}MB vram_trip=${TRIP_VRAM_MB}MB gpu_trip=${TRIP_GPU_PCT}% running_timeout=${RUNNING_TIMEOUT}s"

    while true; do
        if g=$(gpu_reading); then
            read -r desktop pct _ _ _ procs <<<"$g"
            # Renewed immediately on a valid sample, BEFORE /running is ever
            # queried (STORY-035-6a cycle 11) -- see renew_lease()'s comment
            # for why "the loop executed" is not good enough, and the "Cycle
            # 11" header comment for why /running must never sit between a
            # successful sample and its renewal.
            renew_lease

            loaded=$(models_loaded_reading "$RUNNING_TIMEOUT")
            # Logged on the edge only. While a claim is held there is no
            # llama-swap to ask, so an unknown reading is the steady state and
            # logging every sample would bury everything else.
            loaded_known=yes
            [ "$loaded" = unknown ] && loaded_known=no
            if [ "$loaded_known" != "$last_loaded_known" ]; then
                if [ "$loaded_known" = no ]; then
                    edge_log "WARN llama-swap /running unreadable; the compute-client rule is suspended until it answers"
                else
                    edge_log "llama-swap /running readable again (models_loaded=$loaded)"
                fi
                last_loaded_known="$loaded_known"
            fi
            if over_the_line "$desktop" "$pct" "$loaded" "$procs"; then
                under=0
                over=$((over + 1))
                if [ "$over" -ge "$TRIP_SAMPLES" ]; then
                    do_claim "desktop_vram_mb=$desktop gpu_pct=$pct models_loaded=$loaded compute_clients=$procs" 1
                fi
            else
                over=0
                under=$((under + 1))
                # Only ever release what this detector claimed. A manual or
                # launcher-hook claim outranks a cooling-off GPU.
                if [ "$under" -ge "$RELEASE_SAMPLES" ] && [ -e "$AUTO_MARKER" ]; then
                    # Sample count rather than seconds: EDGE_GUARD_INTERVAL may
                    # legitimately be fractional, and integer arithmetic on it
                    # would fail in the one log line that explains the release.
                    do_release "quiet for $under samples"
                fi
            fi
        else
            # No reading is not the same as a quiet GPU. Refusing to release on
            # a blind sample keeps the failure one-way: a broken rocm-smi can
            # leave the node withdrawn, never wrongly in service. Nor is the
            # lease renewed here: a guard that cannot sample cannot prove it
            # would notice a human wanting the GPU, which is the same fact a
            # crashed guard establishes, so it gets the same TTL-bounded
            # consequence -- the supervisor withdraws once the previous lease
            # ages out, not before and not via any separate counter.
            over=0
            edge_log "WARN GPU sample failed (ROCM_SMI=$ROCM_SMI); not renewing the lease -- if this persists, the previous lease will age past its TTL and the supervisor will withdraw service"
        fi
        sleep "$INTERVAL"
    done
}

case "${1:-watch}" in
    watch) cmd_watch ;;
    claim) mkdir -p "$EDGE_STATE_DIR"; do_claim "manual" 0 ;;
    release) do_release "manual" ;;
    status) cmd_status ;;
    -h|--help)
        awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
        ;;
    *) echo "unknown subcommand: $1" >&2; exit 2 ;;
esac
