#!/usr/bin/env bash
#
# Interactive-priority guard for the Bazzite RTX 5090 edge worker.
#
# EDGE-WORKER-CONTRACT §4 for this host: "Gaming/interactive use has ABSOLUTE
# priority. Abrupt mid-request withdrawal is expected and normal — no graceful
# drain is guaranteed." §1.2 adds that the host decides its own state and
# never asks the cluster for permission — it announces, and it stops accepting
# work.
#
# This process is the detector. It does exactly one thing when it decides the
# desktop wants the GPU: it creates $EDGE_STATE_DIR/interactive-claim. The
# supervisor inside the container (scripts/edge-supervisor.sh) watches that
# path and drains llama-swap; the heartbeat producer watches it and publishes
# DRAINING then INTERACTIVE. It also renews $EDGE_STATE_DIR/guard-lease once
# per loop pass that completes a VALID GPU sample — the fail-closed liveness
# interlock inherited unchanged from the CachyOS node (STORY-035-6a, cycles
# 5/10/11; see edge/cachyos/scripts/edge-interactive-guard.sh for the full
# defect history, which is not repeated here).
#
# How it decides — and why this is NOT the CachyOS arithmetic
# -----------------------------------------------------------
# CachyOS/ROCm could split VRAM into compute-vs-graphics and treat "compute"
# as "ours", because llama-server was the only compute client there. On this
# Bazzite/NVIDIA-Wayland host that assumption is measurably false: the
# compositor, browsers and Electron apps all hold CUDA compute contexts
# (~643 MiB of compute VRAM at an idle desktop, measured 2026-08-24). So the
# split here is OURS-vs-FOREIGN, with "ours" defined by the container cgroup
# (see edge_pid_is_ours in edge-common.sh), and three trip rules:
#
#   1. foreign VRAM (used - ours) above the measured idle desktop baseline by
#      more than EDGE_INTERACTIVE_VRAM_MB. A game, a video editor, LM Studio —
#      anything VRAM-hungry that is not our inference. Needs no loaded-model
#      reading. This is the primary gaming detector: games allocate GBs.
#   2. any single foreign compute client holding more than
#      EDGE_FOREIGN_COMPUTE_MB (default 1024 MiB) — a CUDA job or another
#      model runtime that is large in absolute terms even if rule 1's
#      aggregate threshold has not tripped. Idle desktop clients measure
#      77–250 MiB each on this host, so a gigabyte is unambiguous.
#   3. the card busy (> EDGE_INTERACTIVE_GPU_PCT) while llama-swap verifiably
#      has nothing loaded: sustained utilization that is not ours, from a
#      client too small for rules 1–2. Requires a REAL loaded-model count of
#      zero — an `unknown` reading skips this rule (see over_the_line()).
#
# The unchanged limits, both real:
#   * Detection is fast but not instantaneous: for the first few seconds of a
#     game launch the desktop competes with a model that has not finished
#     yielding. On this host the model leaves ~12 GB of the 32 GB card free,
#     so a launching game has far more headroom than on the 24 GB CachyOS
#     card — but the contract's answer is the same: abrupt withdrawal is
#     acceptable here, and the `claim` subcommand (from a launcher hook or by
#     hand, BEFORE the GPU is wanted) remains the primary mechanism, with the
#     detector as backstop.
#   * Interactive work that is neither VRAM- nor compute-hungry (scrolling a
#     browser) is invisible to all three rules.
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
#   NVIDIA_SMI                     absolute path to nvidia-smi (default:
#                                  /usr/bin/nvidia-smi) — MUST be absolute
#                                  (STORY-035-6a cycle 5, inherited)
#   EDGE_OUR_CGROUP_TAG            cgroup substring that marks our container's
#                                  processes (default edge-llama-swap.service)
#   EDGE_GUARD_INTERVAL            seconds between samples and lease renewals
#   EDGE_GUARD_TRIP_SAMPLES        consecutive samples over the line (default 3)
#   EDGE_GUARD_RELEASE_SAMPLES     consecutive samples under it (default 30)
#   EDGE_DESKTOP_BASELINE_MB       idle desktop VRAM, measured (default 1500)
#   EDGE_INTERACTIVE_VRAM_MB       foreign VRAM above baseline that trips
#                                  (default 2048)
#   EDGE_FOREIGN_COMPUTE_MB        per-process foreign compute floor (default 1024)
#   EDGE_INTERACTIVE_GPU_PCT       trip level for busy-with-nothing-loaded (default 25)
#   EDGE_GUARD_RUNNING_TIMEOUT     dedicated --max-time bound for the guard's
#                                  own /running query (default 1); cmd_watch
#                                  refuses to start unless it sits comfortably
#                                  inside EDGE_GUARD_LEASE_TTL

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=edge-common.sh
. "$SCRIPT_DIR/edge-common.sh"

export EDGE_LOG_TAG=guard

INTERVAL="${EDGE_GUARD_INTERVAL:-2}"
TRIP_SAMPLES="${EDGE_GUARD_TRIP_SAMPLES:-3}"
RELEASE_SAMPLES="${EDGE_GUARD_RELEASE_SAMPLES:-30}"
BASELINE_MB="${EDGE_DESKTOP_BASELINE_MB:-1500}"
TRIP_VRAM_MB="${EDGE_INTERACTIVE_VRAM_MB:-2048}"
TRIP_GPU_PCT="${EDGE_INTERACTIVE_GPU_PCT:-25}"
RUNNING_TIMEOUT="${EDGE_GUARD_RUNNING_TIMEOUT:-1}"

# Marks a claim this process made, so an operator's manual claim is never
# released by the detector cooling off.
AUTO_MARKER="$EDGE_STATE_DIR/interactive-claim.auto"

# The guard-ready lease (STORY-035-6a Part 2, inherited unchanged).
LEASE_FILE="$EDGE_STATE_DIR/guard-lease"

edge_require_tools jq curl date awk grep

mkdir -p "$EDGE_STATE_DIR"

# One GPU-only reading, as:
#   "<foreign_over_baseline_mb> <gpu_pct> <used_mb> <total_mb> <ours_mb> <foreign_heavy>"
# where foreign_over_baseline_mb is VRAM held by anything that is not our
# container, above the measured idle baseline. Deliberately does not touch
# /running at all — this is exactly the call cmd_watch renews the lease on,
# and it must succeed in local-nvidia-smi time, never blocked on a network
# round-trip (STORY-035-6a cycle 11, inherited).
gpu_reading() {
    local gpu total used pct ours heavy total_mb used_mb ours_mb foreign
    gpu=$(edge_gpu_sample) || return 1
    read -r total used pct ours heavy <<<"$gpu"
    total_mb=$((total / 1024 / 1024))
    used_mb=$((used / 1024 / 1024))
    ours_mb=$((ours / 1024 / 1024))
    foreign=$((used_mb - ours_mb - BASELINE_MB))
    [ "$foreign" -lt 0 ] && foreign=0
    printf '%s %s %s %s %s %s\n' "$foreign" "$pct" "$used_mb" "$total_mb" "$ours_mb" "$heavy"
}

# models_loaded for one guard pass: the literal string `unknown` when
# llama-swap did not answer within $1 seconds, otherwise a count of models
# that still hold their llama-server process (models on their way out
# included). `unknown` rather than 0, because those are opposite facts and
# only one of them is a reason to act. See over_the_line().
models_loaded_reading() {
    local timeout="${1:-}" running loaded
    if running=$(edge_gpu_holding_models "$timeout" 2>/dev/null); then
        loaded=$(printf '%s\n' "$running" | grep -c '[^[:space:]]' || true)
    else
        loaded=unknown
    fi
    printf '%s' "$loaded"
}

# Combined snapshot for cmd_status only — cmd_watch calls gpu_reading() and
# models_loaded_reading() separately so it can renew the lease between them.
sample() {
    local g foreign pct used_mb total_mb ours_mb heavy loaded
    g=$(gpu_reading) || return 1
    read -r foreign pct used_mb total_mb ours_mb heavy <<<"$g"
    loaded=$(models_loaded_reading)
    printf '%s %s %s %s %s %s %s\n' "$foreign" "$pct" "$loaded" "$used_mb" "$total_mb" "$ours_mb" "$heavy"
}

# True when this sample says something other than our inference wants the GPU.
# Three rules — see the header for what each catches on THIS host.
#
# `loaded` may be the string `unknown`, meaning llama-swap did not answer. The
# rule that needs it is skipped for that sample rather than evaluated against
# a stand-in zero: missing telemetry must not manufacture a claim — and must
# not manufacture a RELEASE either, which it cannot, because a withdrawn node
# has no llama-swap to answer /running and `unknown` is the steady state while
# a claim is held; rules 1 and 2 keep evaluating on their own terms and a
# quiet card still cools off into the release hold-down.
over_the_line() {
    local foreign="$1" pct="$2" loaded="$3" heavy="$4"
    # 1. Foreign VRAM above the idle desktop baseline: a game, LM Studio, a
    #    video editor. Needs no loaded-model reading at all.
    [ "$foreign" -gt "$TRIP_VRAM_MB" ] && return 0
    # 2. A single foreign compute client above the per-process floor: real
    #    GPU work by absolute size, whatever the aggregate says.
    [ "$heavy" -gt 0 ] && return 0
    # 3. The card busy while llama-swap verifiably has nothing loaded. Unlike
    #    the CachyOS variant this cannot fall back to "zero compute clients"
    #    (the desktop always holds compute contexts here), so it requires a
    #    real zero from /running and is skipped on `unknown`.
    if [ "$loaded" != unknown ] && [ "$loaded" -eq 0 ] && [ "$pct" -gt "$TRIP_GPU_PCT" ]; then
        return 0
    fi
    return 1
}

do_claim() {
    local why="$1" auto="$2"
    if edge_claim_present; then
        # A manual claim on top of an automatic one still has to upgrade it to
        # sticky, or the detector would later release a claim a human made.
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
# VALID GPU sample — not merely that the process is alive. Inherited unchanged
# from the CachyOS node (STORY-035-6a cycle 5): a failed sample must not
# refresh it, and EDGE_GUARD_LEASE_TTL is the only grace period. Blind must
# mean closed. Epoch seconds via temp file + rename, so a reader never
# observes a partial write.
renew_lease() {
    local tmp
    tmp="$LEASE_FILE.tmp.$$"
    printf '%s\n' "$(date -u +%s)" >"$tmp" 2>/dev/null && mv -f "$tmp" "$LEASE_FILE" 2>/dev/null
}

cmd_status() {
    local s foreign pct loaded used_mb total_mb ours_mb heavy verdict
    if ! s=$(sample); then
        edge_log "no GPU sample available (NVIDIA_SMI=$NVIDIA_SMI)"
        return 1
    fi
    read -r foreign pct loaded used_mb total_mb ours_mb heavy <<<"$s"
    verdict=no
    over_the_line "$foreign" "$pct" "$loaded" "$heavy" && verdict=yes
    printf 'vram_used_mb=%s vram_total_mb=%s ours_vram_mb=%s foreign_heavy_clients=%s foreign_vram_over_baseline_mb=%s gpu_pct=%s models_loaded=%s\n' \
        "$used_mb" "$total_mb" "$ours_mb" "$heavy" "$foreign" "$pct" "$loaded"
    printf 'thresholds: foreign_vram>%sMB, any foreign compute client>%sMB, or (gpu>%s%% with models_loaded=0); baseline=%sMB, trip=%s samples, release=%s samples, interval=%ss\n' \
        "$TRIP_VRAM_MB" "$EDGE_FOREIGN_COMPUTE_MB" "$TRIP_GPU_PCT" "$BASELINE_MB" "$TRIP_SAMPLES" "$RELEASE_SAMPLES" "$INTERVAL"
    printf 'over_the_line=%s claim=%s phase=%s\n' \
        "$verdict" "$(edge_claim_present && echo present || echo absent)" "$(edge_phase)"
}

# Refuses to start `watch` if RUNNING_TIMEOUT + INTERVAL is not comfortably
# inside EDGE_GUARD_LEASE_TTL (STORY-035-6a cycle 11, inherited: validate the
# timing relationship at startup rather than trusting an operator not to
# configure an unsafe combination).
check_running_timeout_safety() {
    local lease_ttl="${EDGE_GUARD_LEASE_TTL:-6}"
    awk -v rt="$RUNNING_TIMEOUT" -v iv="$INTERVAL" -v ttl="$lease_ttl" \
        'BEGIN { exit !(rt + iv + iv < ttl) }' \
        || edge_die "EDGE_GUARD_RUNNING_TIMEOUT ($RUNNING_TIMEOUT) + EDGE_GUARD_INTERVAL ($INTERVAL), with a margin of one more INTERVAL, must be less than EDGE_GUARD_LEASE_TTL ($lease_ttl) -- that is not the case, which lets a slow-but-answering /running call starve lease renewal exactly like STORY-035-6a cycle 10's regression"
}

cmd_watch() {
    local over=0 under=0 g foreign pct heavy loaded
    local last_loaded_known=yes loaded_known
    check_running_timeout_safety
    edge_log "watching: interval=${INTERVAL}s trip=${TRIP_SAMPLES} release=${RELEASE_SAMPLES}" \
        "baseline=${BASELINE_MB}MB vram_trip=${TRIP_VRAM_MB}MB foreign_compute_floor=${EDGE_FOREIGN_COMPUTE_MB}MB gpu_trip=${TRIP_GPU_PCT}% running_timeout=${RUNNING_TIMEOUT}s"

    while true; do
        if g=$(gpu_reading); then
            read -r foreign pct _ _ _ heavy <<<"$g"
            # Renewed immediately on a valid sample, BEFORE /running is ever
            # queried (STORY-035-6a cycle 11, inherited).
            renew_lease

            loaded=$(models_loaded_reading "$RUNNING_TIMEOUT")
            loaded_known=yes
            [ "$loaded" = unknown ] && loaded_known=no
            if [ "$loaded_known" != "$last_loaded_known" ]; then
                if [ "$loaded_known" = no ]; then
                    edge_log "WARN llama-swap /running unreadable; the busy-with-nothing-loaded rule is suspended until it answers"
                else
                    edge_log "llama-swap /running readable again (models_loaded=$loaded)"
                fi
                last_loaded_known="$loaded_known"
            fi
            if over_the_line "$foreign" "$pct" "$loaded" "$heavy"; then
                under=0
                over=$((over + 1))
                if [ "$over" -ge "$TRIP_SAMPLES" ]; then
                    do_claim "foreign_vram_mb=$foreign gpu_pct=$pct models_loaded=$loaded foreign_heavy=$heavy" 1
                fi
            else
                over=0
                under=$((under + 1))
                # Only ever release what this detector claimed. A manual or
                # launcher-hook claim outranks a cooling-off GPU.
                if [ "$under" -ge "$RELEASE_SAMPLES" ] && [ -e "$AUTO_MARKER" ]; then
                    do_release "quiet for $under samples"
                fi
            fi
        else
            # No reading is not the same as a quiet GPU. Refusing to release on
            # a blind sample keeps the failure one-way: a broken nvidia-smi can
            # leave the node withdrawn, never wrongly in service. Nor is the
            # lease renewed here (STORY-035-6a cycle 5, inherited).
            over=0
            edge_log "WARN GPU sample failed (NVIDIA_SMI=$NVIDIA_SMI); not renewing the lease -- if this persists, the previous lease will age past its TTL and the supervisor will withdraw service"
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
