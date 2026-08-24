#!/usr/bin/env bash
#
# Shared helpers for the Bazzite edge worker's host-side daemons
# (edge-heartbeat.sh and edge-interactive-guard.sh). Not executable on its own.
#
# Ported from edge/cachyos/scripts/edge-common.sh. The contract is identical;
# the GPU sampling is not, and the difference is a measured fact about this
# host, not a preference:
#
#   On CachyOS/ROCm, rocm-smi's KFD process table lists *compute* clients
#   only, and llama-server is the only compute client on that box — so
#   "used - compute = graphics/desktop VRAM" falls out for free.
#
#   On Bazzite/NVIDIA-Wayland, ordinary desktop processes hold CUDA/compute
#   contexts: kwin_wayland, browsers and Electron apps all appear in
#   `nvidia-smi --query-compute-apps` (measured on this host: ~643 MiB of
#   "compute" VRAM at an idle desktop). "Compute client" therefore means
#   nothing about who the client is, and the CachyOS arithmetic would read
#   the compositor as foreign compute work.
#
#   What this host CAN observe deterministically is which processes are OURS:
#   the inference server runs inside the edge-llama-swap container, whose
#   processes sit in a systemd cgroup whose path contains the container unit
#   name. So the split here is ours-vs-foreign rather than
#   compute-vs-graphics:
#
#     foreign_vram = vram_used - vram_held_by_our_container
#
#   and "the desktop wants the GPU" is foreign VRAM above the measured idle
#   baseline. Identifying our own process by cgroup rather than by process
#   name is deliberate: a foreign llama-server someone runs by hand must
#   count as foreign, and a renamed binary inside our container must still
#   count as ours.

# shellcheck shell=bash

# The claim/phase directory, as this side sees it. The systemd units set it
# explicitly (Environment=EDGE_STATE_DIR=%S/edge-bazzite-state) and the quadlet
# sets it to /edge/state inside the container; this default is what a manual
# invocation on the host gets, and it is the same rule systemd resolves %S by,
# so `scripts/edge-interactive-guard.sh status` from a shell sees exactly what
# the running unit sees.
EDGE_STATE_DIR="${EDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/edge-bazzite-state}"
EDGE_CLAIM_FILE="$EDGE_STATE_DIR/interactive-claim"
EDGE_PHASE_FILE="$EDGE_STATE_DIR/phase"
# Cache-observation manifest (STORY-035-9c Part A shape, kept on Bazzite even
# though rootless podman would let the host read the store directly): the
# supervisor writes it from its own /models:ro mount, the heartbeat reads it.
# Keeping the manifest keeps the two node implementations behaviourally
# identical and keeps the heartbeat's fail-to-empty semantics in one place.
EDGE_CACHE_MANIFEST_FILE="${EDGE_CACHE_MANIFEST_FILE:-$EDGE_STATE_DIR/cache-manifest.json}"

# Absolute path, not a bare command name (STORY-035-6a cycle 5, inherited):
# the systemd user manager's boot-time PATH is not a login shell's, and a
# sensor the guard cannot find must be a deployment failure, not a silent
# blind guard. scripts/install.sh refuses to install if this is not an
# existing executable file.
NVIDIA_SMI="${NVIDIA_SMI:-/usr/bin/nvidia-smi}"

# Substring a compute process's /proc/<pid>/cgroup must contain to count as
# OURS. The quadlet-generated container unit is edge-llama-swap.service, and
# every process inside the container lives in a cgroup path containing it.
EDGE_OUR_CGROUP_TAG="${EDGE_OUR_CGROUP_TAG:-edge-llama-swap.service}"

# Per-process VRAM, in MiB, above which a foreign compute client counts as a
# serious GPU consumer on its own (guard rule 2). Idle desktop compute
# clients on this host measure 77-250 MiB each; anything holding a gigabyte
# is real work — a CUDA job, LM Studio, a game's compute queue.
EDGE_FOREIGN_COMPUTE_MB="${EDGE_FOREIGN_COMPUTE_MB:-1024}"

edge_log() {
    printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${EDGE_LOG_TAG:-edge}" "$*" >&2
}

# The bearer credential these daemons need for their read-only llama-swap
# queries. llama-swap requires it on /running and /api/version, not only on the
# inference routes, so without it the loaded-model count reads as unknown and
# the guard loses its compute-client rule.
#
# A file rather than a value in the environment, for the reason the supervisor
# prefers one: an environment variable is inherited by every child these scripts
# spawn and is readable from /proc, a file is read once here. The systemd units
# point EDGE_API_KEY_FILE at the host copy; EDGE_API_KEY still works if
# something sets it directly.
if [ -z "${EDGE_API_KEY:-}" ] && [ -n "${EDGE_API_KEY_FILE:-}" ] && [ -r "$EDGE_API_KEY_FILE" ]; then
    EDGE_API_KEY=$(tr -d '\r\n' <"$EDGE_API_KEY_FILE")
fi
if [ -z "${EDGE_API_KEY:-}" ]; then
    edge_log "WARN no EDGE_API_KEY and no readable EDGE_API_KEY_FILE; llama-swap queries will be unauthenticated, so the loaded-model count reads as unknown"
fi

edge_die() {
    edge_log "FATAL $1"
    exit "${2:-1}"
}

edge_require_tools() {
    local bin
    for bin in "$@"; do
        command -v "$bin" >/dev/null 2>&1 || edge_die "required tool '$bin' not found on PATH" 2
    done
}

# Assemble the curl options used for every call to llama-swap. Verification is
# always on and always against the dedicated edge CA — EDGE-WORKER-CONTRACT §2
# is explicit that the production shape verifies a real CA and a hostname SAN,
# and a daemon that quietly skipped it would be testing something the cluster
# will never do. EDGE_CURL_RESOLVE covers the interim where the edge hostname
# has no LAN DNS record yet, exactly as conformance.sh's --resolve does.
#
# $1 (optional): override --max-time in seconds; unset falls back to
# EDGE_HTTP_TIMEOUT (default 5).
edge_curl_opts() {
    local timeout="${1:-${EDGE_HTTP_TIMEOUT:-5}}"
    EDGE_CURL_OPTS=(--silent --show-error --max-time "$timeout")
    [ -n "${EDGE_CA_CERT:-}" ] && EDGE_CURL_OPTS+=(--cacert "$EDGE_CA_CERT")
    [ -n "${EDGE_CURL_RESOLVE:-}" ] && EDGE_CURL_OPTS+=(--resolve "$EDGE_CURL_RESOLVE")
    return 0
}

# True when /proc/<pid>/cgroup places the process inside our container unit.
# A vanished PID (raced an exit) reads as not-ours, which under-counts ours
# and over-counts foreign — the safe direction for a guard whose job is to
# yield to humans.
edge_pid_is_ours() {
    local pid="$1"
    [ -r "/proc/$pid/cgroup" ] || return 1
    grep -q "$EDGE_OUR_CGROUP_TAG" "/proc/$pid/cgroup" 2>/dev/null
}

# One GPU sample, as five space-separated integers:
#   <vram_total_bytes> <vram_used_bytes> <gpu_busy_pct> <ours_vram_bytes> <foreign_heavy_clients>
#
# ours_vram_bytes: VRAM held by compute processes inside our container
# (cgroup-matched — see header). foreign_heavy_clients: count of compute
# processes NOT ours each holding more than EDGE_FOREIGN_COMPUTE_MB.
#
# Note the semantic difference from the CachyOS sample (fields 4/5 there are
# compute VRAM and compute-client count): on this host compute-client
# arithmetic is meaningless (desktop apps are compute clients), so the split
# is ours-vs-foreign. Field positions 1-3 are identical, which is what the
# heartbeat consumes.
#
# Prints nothing and returns non-zero if the sample cannot be taken, so callers
# can tell "GPU says 0%" from "no GPU reading", which are very different facts
# to put in a heartbeat.
edge_gpu_sample() {
    local gpu_row total_mib used_mib pct apps line pid mem ours_mib=0 foreign_heavy=0
    gpu_row=$("$NVIDIA_SMI" --query-gpu=memory.total,memory.used,utilization.gpu \
        --format=csv,noheader,nounits -i "${EDGE_GPU_INDEX:-0}" 2>/dev/null) || return 1
    [ -n "$gpu_row" ] || return 1
    IFS=', ' read -r total_mib used_mib pct <<<"$(printf '%s' "$gpu_row" | tr -d ' ')"
    case "$total_mib$used_mib$pct" in *[!0-9]*|'') return 1 ;; esac

    # Per-process compute allocations. An empty table is a valid reading
    # (nothing has a compute context); a failed invocation is not.
    apps=$("$NVIDIA_SMI" --query-compute-apps=pid,used_gpu_memory \
        --format=csv,noheader,nounits 2>/dev/null) || return 1
    while IFS=',' read -r pid mem; do
        pid=$(printf '%s' "$pid" | tr -d ' ')
        mem=$(printf '%s' "$mem" | tr -d ' ')
        [ -n "$pid" ] || continue
        case "$pid$mem" in *[!0-9]*|'') continue ;; esac
        if edge_pid_is_ours "$pid"; then
            ours_mib=$((ours_mib + mem))
        elif [ "$mem" -gt "$EDGE_FOREIGN_COMPUTE_MB" ]; then
            foreign_heavy=$((foreign_heavy + 1))
        fi
    done <<<"$apps"

    printf '%s %s %s %s %s\n' \
        "$((total_mib * 1024 * 1024))" "$((used_mib * 1024 * 1024))" "$pct" \
        "$((ours_mib * 1024 * 1024))" "$foreign_heavy"
}

# Static GPU identity for the heartbeat's gpu.model / gpu.arch, TAB-separated —
# the product name contains spaces ("NVIDIA GeForce RTX 5090"), so callers
# must split on tab. arch is the CUDA compute capability rendered as sm_NNN
# (12.0 -> sm_120), the NVIDIA analogue of the catalog's gfx1100.
edge_gpu_identity() {
    local row name cap
    row=$("$NVIDIA_SMI" --query-gpu=name,compute_cap \
        --format=csv,noheader -i "${EDGE_GPU_INDEX:-0}" 2>/dev/null) || return 1
    [ -n "$row" ] || return 1
    name="${row%,*}"
    cap="${row##*, }"
    name=$(printf '%s' "$name" | sed 's/[[:space:]]*$//')
    cap=$(printf '%s' "$cap" | tr -d ' .')
    [ -n "$name" ] || return 1
    printf '%s\t%s\n' "$name" "sm_${cap:-unknown}"
}

# One validated /running body, or non-zero if there was no reading at all.
#
# The exit status separates "llama-swap says nothing is loaded" from "llama-swap
# did not answer", and both callers depend on that: a failed query folded into
# zero would invert the guard's rules exactly when its input is least reliable.
#
# $1 (optional): passed straight through to edge_curl_opts as a --max-time
# override; see its comment.
edge_running_body() {
    local timeout="${1:-}" body
    edge_curl_opts "$timeout"
    body=$(curl "${EDGE_CURL_OPTS[@]}" \
        -H "Authorization: Bearer ${EDGE_API_KEY:-}" \
        "${EDGE_ENDPOINT%/}/running" 2>/dev/null) || return 1
    printf '%s' "$body" | jq -e 'type == "object" and has("running")' >/dev/null 2>&1 || return 1
    printf '%s' "$body"
}

# Model ids llama-swap currently SERVES, one per line — a model on its way out
# is not one of them. These are RUNTIME ids; translating them to catalog
# model_ids is the caller's job and is the whole point of model-id-map.json.
edge_running_models() {
    local body
    body=$(edge_running_body) || return 1
    printf '%s' "$body" | jq -r '.running[]? | select(.state != "stopping") | .model' 2>/dev/null
}

# Model ids whose llama-server process is still on the card, one per line,
# INCLUDING models in state `stopping` — a model being unloaded still holds
# its process and its VRAM for as long as llama-swap says it is stopping.
#
# $1 (optional): --max-time override, see edge_curl_opts.
edge_gpu_holding_models() {
    local timeout="${1:-}" body
    body=$(edge_running_body "$timeout") || return 1
    printf '%s' "$body" | jq -r '.running[]?.model' 2>/dev/null
}

# Is the LAN-facing endpoint answering at all? /health is llama-swap's only
# unauthenticated route, which makes this a pure liveness question rather than
# a credential question.
edge_endpoint_alive() {
    edge_curl_opts
    curl "${EDGE_CURL_OPTS[@]}" --output /dev/null --fail "${EDGE_ENDPOINT%/}/health" 2>/dev/null
}

edge_phase() {
    if [ -r "$EDGE_PHASE_FILE" ]; then
        tr -d '[:space:]' <"$EDGE_PHASE_FILE"
    else
        printf 'unknown'
    fi
}

edge_claim_present() {
    [ -e "$EDGE_CLAIM_FILE" ]
}
