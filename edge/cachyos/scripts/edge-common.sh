#!/usr/bin/env bash
#
# Shared helpers for the CachyOS edge worker's host-side daemons
# (edge-heartbeat.sh and edge-interactive-guard.sh). Not executable on its own.
#
# Both daemons need the same two things — a GPU sample and an authenticated,
# CA-verified call to llama-swap — and getting either subtly different between
# them would make the heartbeat disagree with the thing that withdrew the node.

# shellcheck shell=bash

# The claim/phase directory, as this side sees it. The systemd units set it
# explicitly (Environment=EDGE_STATE_DIR=%S/edge-cachyos-state) and docker-compose
# sets it to /edge/state inside the container; this default is what a manual
# invocation on the host gets, and it is the same rule systemd resolves %S by,
# so `scripts/edge-interactive-guard.sh status` from a shell sees exactly what
# the running unit sees.
EDGE_STATE_DIR="${EDGE_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/edge-cachyos-state}"
EDGE_CLAIM_FILE="$EDGE_STATE_DIR/interactive-claim"
EDGE_PHASE_FILE="$EDGE_STATE_DIR/phase"
# Cache-observation manifest (STORY-035-9c Part A): scripts/edge-supervisor.sh
# writes it from its already-mounted /models:ro, atomically, and
# scripts/edge-heartbeat.sh reads it read-only to derive cached_models. Same
# shared-state boundary as CLAIM_FILE/PHASE_FILE above, one more file on it —
# no new mount, no new daemon.
# Overridable like every other default here: the heartbeat's own self-tests
# and the live evidence harness point this at crafted manifests, and an
# unconditional assignment silently defeated exactly that during 35.9c's
# acceptance.
EDGE_CACHE_MANIFEST_FILE="${EDGE_CACHE_MANIFEST_FILE:-$EDGE_STATE_DIR/cache-manifest.json}"

# Absolute path, not a bare command name (STORY-035-6a cycle 5): a bare name
# resolves through PATH, and the systemd user manager's boot-time PATH
# (/usr/local/bin:/usr/bin) does not include rocm-smi's install location,
# unlike an interactive login shell's -- the root cause of a guard that ran
# from boot while never once successfully sampling the GPU. env.example
# documents this default and scripts/install.sh refuses to install if the
# configured path is not an existing, executable file.
ROCM_SMI="${ROCM_SMI:-/opt/rocm/bin/rocm-smi}"

edge_log() {
    printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${EDGE_LOG_TAG:-edge}" "$*" >&2
}

# The bearer credential these daemons need for their read-only llama-swap
# queries. llama-swap requires it on /running and /api/version, not only on the
# inference routes, so without it the loaded-model count reads as unknown and
# the guard loses its compute-client rule entirely.
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
# has no LAN DNS record yet (34.19b), exactly as conformance.sh's --resolve does.
#
# $1 (optional): override --max-time in seconds, for a caller that needs a
# tighter bound than EDGE_HTTP_TIMEOUT -- STORY-035-6a cycle 11's guard-side
# /running query is the reason this exists, see edge-interactive-guard.sh.
# Unset or empty falls back to EDGE_HTTP_TIMEOUT (default 5), i.e. every
# pre-cycle-11 caller is unaffected.
edge_curl_opts() {
    local timeout="${1:-${EDGE_HTTP_TIMEOUT:-5}}"
    EDGE_CURL_OPTS=(--silent --show-error --max-time "$timeout")
    [ -n "${EDGE_CA_CERT:-}" ] && EDGE_CURL_OPTS+=(--cacert "$EDGE_CA_CERT")
    [ -n "${EDGE_CURL_RESOLVE:-}" ] && EDGE_CURL_OPTS+=(--resolve "$EDGE_CURL_RESOLVE")
    return 0
}

# One GPU sample, as five space-separated integers:
#   <vram_total_bytes> <vram_used_bytes> <gpu_busy_pct> <compute_vram_bytes> <compute_clients>
#
# The last two are what make the interactive guard work without calibration.
# rocm-smi's KFD process table reports VRAM held by *compute* clients — which
# on this host is llama-server and nothing else — so used - compute is VRAM
# held by graphics clients: the compositor, a game, a video editor. That
# subtraction answers "is the desktop using the GPU" directly, instead of
# inferring it from a total that our own 22 GB model dominates. The client
# count answers the other half: more compute clients than llama-swap has models
# loaded means somebody else is computing, with no size estimate involved.
#
# Prints nothing and returns non-zero if the sample cannot be taken, so callers
# can tell "GPU says 0%" from "no GPU reading", which are very different facts
# to put in a heartbeat.
edge_gpu_sample() {
    local raw parsed
    raw=$("$ROCM_SMI" --showmeminfo vram --showuse --showpids --json 2>/dev/null) || return 1
    [ -n "$raw" ] || return 1
    parsed=$(printf '%s' "$raw" | jq -er --arg card "${EDGE_GPU_CARD:-}" '
        def pick($re):
            to_entries
            | map(select(.key | test($re; "i")))
            | (.[0].value // empty);

        # "PID1234": "<name>, <gpus>, <vram_bytes>, <sdma>, <cu>"
        ((.system // {})
          | to_entries
          | map(select(.key | test("^pid[0-9]+$"; "i")))
          | map(.value | split(", ") | .[2] | tonumber)) as $procs
        | (if $card != "" and has($card) then .[$card]
           else (to_entries | map(select(.key | test("^card"; "i"))) | .[0].value)
           end) as $g
        | if $g == null then empty else
            [ ($g | pick("vram +total +memory")   | tonumber),
              ($g | pick("vram +total +used +memory") | tonumber),
              ($g | pick("gpu +use")              | tonumber),
              ($procs | add // 0),
              ($procs | length) ]
            | @tsv
          end
    ' 2>/dev/null | tr '\t' ' ')
    # A filter that selected no card exits 0 with no output, which would hand
    # callers a row of empty fields that arithmetic silently reads as zeroes —
    # a fabricated sample, and the one thing the comment above promises this
    # function will not do. An empty result is no reading.
    [ -n "$parsed" ] || return 1
    printf '%s\n' "$parsed"
}

# Static GPU identity for the heartbeat's gpu.model / gpu.arch, TAB-separated —
# the card series contains spaces ("AMD Radeon RX 7900 XTX"), so callers must
# split on tab. Separate from the sample because it never changes and rocm-smi
# is not free.
edge_gpu_identity() {
    local raw
    raw=$("$ROCM_SMI" --showproductname --json 2>/dev/null) || return 1
    printf '%s' "$raw" | jq -er --arg card "${EDGE_GPU_CARD:-}" '
        def pick($re):
            to_entries
            | map(select(.key | test($re; "i")))
            | (.[0].value // empty);

        (if $card != "" and has($card) then .[$card]
         else (to_entries | map(select(.key | test("^card"; "i"))) | .[0].value)
         end) as $g
        | if $g == null then empty else
            [ ($g | pick("card +series") // "unmeasured"),
              ($g | pick("gfx +version") // "unmeasured") ]
            | @tsv
          end
    ' 2>/dev/null
}

# One validated /running body, or non-zero if there was no reading at all.
#
# The exit status separates "llama-swap says nothing is loaded" from "llama-swap
# did not answer", and both callers depend on that: the interactive guard
# compares compute clients against a model count, so a failed query folded into
# zero would read our own llama-server as somebody else's GPU work and withdraw
# a healthy node.
#
# `.running` has to be present for the body to count as an answer — an error
# page, a truncated response or a future schema change all land in the
# no-reading branch rather than being read as an empty list.
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
# This is the heartbeat's question: which model is warm.
edge_running_models() {
    local body
    body=$(edge_running_body) || return 1
    printf '%s' "$body" | jq -r '.running[]? | select(.state != "stopping") | .model' 2>/dev/null
}

# Model ids whose llama-server process is still on the card, one per line,
# INCLUDING models in state `stopping`. This is the guard's question, and it is
# a different one: it compares this count against the number of KFD compute
# clients, and a model being unloaded still holds its process and its compute
# slot for as long as llama-swap says it is stopping. Counting it as gone would
# give compute_clients=1 against models=0 during every ordinary idle unload —
# the node's own work read as somebody else's, and a withdrawal for the whole
# release hold-down triggered by nothing but a model going cold on schedule.
#
# $1 (optional): passed straight through to edge_running_body as a --max-time
# override; see edge_curl_opts.
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
