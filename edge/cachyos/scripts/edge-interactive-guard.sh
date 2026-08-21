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
# The fix: /running is answered by running_poller_loop(), a single
# background job cmd_watch starts once and never waits on. It writes its
# answer (a model count, or `unknown` if the query failed) to
# $EDGE_STATE_DIR/running-cache; sample() reads that file through
# cached_loaded() instead of querying live -- a local file read, no network
# round-trip, no blocking. The main loop's only remaining blocking call is
# edge_gpu_sample() (a local rocm-smi invocation -- no HTTP, no --max-time),
# so the worst-case interval between two successful lease renewals is now
# INTERVAL + T_sample, where T_sample is rocm-smi's own execution time --
# typically well under a second, and never influenced by how /running is
# behaving. At the documented defaults (INTERVAL=2s, EDGE_GUARD_LEASE_TTL=6s)
# that leaves at least 4s of margin before rocm-smi's own cost even enters
# the arithmetic, whatever /running does: slow, hung, or simply wrong.
#
# What this costs: models_loaded can lag the poller's own cadence (about
# INTERVAL, when /running answers promptly) by up to RUNNING_CACHE_MAX_AGE
# (default 15s) before a stuck /running reads as `unknown` instead of an
# increasingly stale number. That is the correct trade -- over_the_line()'s
# rule 2 (compute clients vs. models loaded) already treats `unknown` as
# "skip this rule", so a slow /running suspends the compute-client rule
# rather than manufacturing a false claim or a false release. It can never
# touch the lease or the other two rules, which is the property cycle 10
# exists to guarantee.
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
#   EDGE_RUNNING_CACHE_MAX_AGE     seconds before a stuck background /running
#                                  poll reads as `unknown` instead of an
#                                  increasingly stale model count (default 15
#                                  -- see "Cycle 10" above)

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

# Marks a claim this process made, so an operator's manual claim is never
# released by the detector cooling off.
AUTO_MARKER="$EDGE_STATE_DIR/interactive-claim.auto"

# The guard-ready lease (STORY-035-6a Part 2). edge-supervisor.sh withdraws
# the listener whenever this file is missing or older than its
# EDGE_GUARD_LEASE_TTL, independent of interactive-claim entirely -- that is
# what makes a hung or killed guard fail closed instead of leaving a stale
# claim file as the only signal a desktop-sharing safety control has.
LEASE_FILE="$EDGE_STATE_DIR/guard-lease"

# The background /running poller's answer (STORY-035-6a cycle 10) -- see
# poll_running_once(), running_poller_loop() and cached_loaded() below, and
# the "Cycle 10" comment above for why this exists.
RUNNING_CACHE_FILE="$EDGE_STATE_DIR/running-cache"
RUNNING_CACHE_MAX_AGE="${EDGE_RUNNING_CACHE_MAX_AGE:-15}"
RUNNING_POLLER_PID=""

edge_require_tools jq curl date

mkdir -p "$EDGE_STATE_DIR"

# One sample, as:
#   "<desktop_vram_mb> <gpu_pct> <models_loaded> <used_mb> <total_mb> <compute_mb> <compute_clients>"
# where desktop_vram_mb is graphics VRAM above the measured idle baseline, and
# models_loaded is the literal string `unknown` when llama-swap did not answer.
#
# models_loaded counts models that still hold their llama-server process, models
# on their way out included, because it is compared against KFD compute clients
# and an unloading model is still one of those.
#
# `unknown` rather than 0, because those are opposite facts and only one of them
# is a reason to withdraw the node. See over_the_line().
#
# $1 (optional): "cached" reads the last background-polled models-loaded
# reading via cached_loaded() -- a local file read, never blocking on
# /running (STORY-035-6a cycle 10; cmd_watch uses this). Anything else (the
# default) queries /running live, i.e. this command's pre-cycle-10 one-shot
# behaviour, which is what cmd_status still wants for a diagnostic snapshot.
sample() {
    local mode="${1:-live}"
    local gpu total used pct compute procs total_mb used_mb compute_mb loaded desktop running
    gpu=$(edge_gpu_sample) || return 1
    read -r total used pct compute procs <<<"$gpu"
    total_mb=$((total / 1024 / 1024))
    used_mb=$((used / 1024 / 1024))
    compute_mb=$((compute / 1024 / 1024))

    if [ "$mode" = cached ]; then
        loaded=$(cached_loaded)
    elif running=$(edge_gpu_holding_models 2>/dev/null); then
        loaded=$(printf '%s\n' "$running" | grep -c '[^[:space:]]' || true)
    else
        loaded=unknown
    fi

    desktop=$((used_mb - compute_mb - BASELINE_MB))
    [ "$desktop" -lt 0 ] && desktop=0

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

# One /running query, on the poller's own clock, never the GPU-sampling
# loop's (STORY-035-6a cycle 10). Writes "<epoch> <count-or-unknown>" via
# temp-file + rename, the same pattern renew_lease() uses, so a reader never
# observes a partial write. $BASHPID, not $$: this runs inside a backgrounded
# subshell, where $$ still names the parent shell, not this one.
poll_running_once() {
    local running loaded tmp
    if running=$(edge_gpu_holding_models 2>/dev/null); then
        loaded=$(printf '%s\n' "$running" | grep -c '[^[:space:]]' || true)
    else
        loaded=unknown
    fi
    tmp="$RUNNING_CACHE_FILE.tmp.$BASHPID"
    printf '%s %s\n' "$(date -u +%s)" "$loaded" >"$tmp" 2>/dev/null && mv -f "$tmp" "$RUNNING_CACHE_FILE" 2>/dev/null
}

# Runs as a single persistent background job for the lifetime of `watch`,
# started once by cmd_watch. Entirely decoupled from the GPU-sampling loop
# below: however long a slow or hung /running takes (up to
# EDGE_HTTP_TIMEOUT per call), it only ever delays this loop's own next
# iteration, never the main loop's next GPU sample or lease renewal.
running_poller_loop() {
    while true; do
        poll_running_once
        sleep "$INTERVAL"
    done
}

# Non-blocking read of the poller's last answer. Missing, unparseable, or
# older than RUNNING_CACHE_MAX_AGE all read as unknown -- the same "missing
# telemetry must not manufacture a claim" rule sample() has always applied to
# a failed live query, just fed from a cache instead of a call made on this
# loop's own clock. The max-age bound keeps a permanently stuck /running from
# serving one stale number forever; see the "Cycle 10" comment above for why
# it defaults to 15s.
cached_loaded() {
    local ts loaded
    if [ -r "$RUNNING_CACHE_FILE" ] && read -r ts loaded <"$RUNNING_CACHE_FILE" 2>/dev/null; then
        case "$ts" in
            ''|*[!0-9]*) printf unknown; return ;;
        esac
        if [ $(( $(date -u +%s) - ts )) -le "$RUNNING_CACHE_MAX_AGE" ]; then
            printf '%s' "$loaded"
            return
        fi
    fi
    printf unknown
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

cmd_watch() {
    local over=0 under=0 s desktop pct loaded procs last_loaded_known=yes loaded_known
    edge_log "watching: interval=${INTERVAL}s trip=${TRIP_SAMPLES} release=${RELEASE_SAMPLES}" \
        "baseline=${BASELINE_MB}MB vram_trip=${TRIP_VRAM_MB}MB gpu_trip=${TRIP_GPU_PCT}%"

    # STORY-035-6a cycle 10: /running runs on its own background clock so it
    # can never delay a GPU sample or a lease renewal -- see
    # running_poller_loop() above. systemd's default KillMode=control-group
    # reaps this alongside the main process on a normal stop/restart; the
    # trap below additionally covers a bare `kill` of just this process.
    running_poller_loop &
    RUNNING_POLLER_PID=$!
    trap '[ -n "$RUNNING_POLLER_PID" ] && kill "$RUNNING_POLLER_PID" 2>/dev/null' EXIT

    while true; do
        if s=$(sample cached); then
            read -r desktop pct loaded _ _ _ procs <<<"$s"
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
            # Renewed only on a valid sample (STORY-035-6a cycle 5) -- see
            # renew_lease()'s comment for why "the loop executed" is not
            # good enough and why this is not a second failure-count timer.
            renew_lease
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
