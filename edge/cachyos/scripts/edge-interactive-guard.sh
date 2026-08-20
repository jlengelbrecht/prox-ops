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
# Usage:
#   edge-interactive-guard.sh watch      # default: sample and decide, forever
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
#   ROCM_SMI                       rocm-smi command (default: rocm-smi)
#   EDGE_GUARD_INTERVAL            seconds between samples (default 2)
#   EDGE_GUARD_TRIP_SAMPLES        consecutive samples over the line (default 3)
#   EDGE_GUARD_RELEASE_SAMPLES     consecutive samples under it (default 30)
#   EDGE_DESKTOP_BASELINE_MB       idle compositor VRAM, measured (default 1200)
#   EDGE_INTERACTIVE_VRAM_MB       graphics VRAM above baseline that trips
#                                  (default 512 — it has to fit inside the ~2 GB
#                                  a loaded model leaves free)
#   EDGE_INTERACTIVE_GPU_PCT       trip level for busy-with-nothing-loaded (default 25)

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

edge_require_tools jq curl date

mkdir -p "$EDGE_STATE_DIR"

# One sample, as:
#   "<desktop_vram_mb> <gpu_pct> <models_loaded> <used_mb> <total_mb> <compute_mb> <compute_clients>"
# where desktop_vram_mb is graphics VRAM above the measured idle baseline, and
# models_loaded is the literal string `unknown` when llama-swap did not answer.
#
# `unknown` rather than 0, because those are opposite facts and only one of them
# is a reason to withdraw the node. See over_the_line().
sample() {
    local gpu total used pct compute procs total_mb used_mb compute_mb loaded desktop running
    gpu=$(edge_gpu_sample) || return 1
    read -r total used pct compute procs <<<"$gpu"
    total_mb=$((total / 1024 / 1024))
    used_mb=$((used / 1024 / 1024))
    compute_mb=$((compute / 1024 / 1024))

    if running=$(edge_running_models 2>/dev/null); then
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
    while true; do
        if s=$(sample); then
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
        else
            # No reading is not the same as a quiet GPU. Refusing to release on
            # a blind sample keeps the failure one-way: a broken rocm-smi can
            # leave the node withdrawn, never wrongly in service.
            over=0
            edge_log "WARN GPU sample failed; holding current state"
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
