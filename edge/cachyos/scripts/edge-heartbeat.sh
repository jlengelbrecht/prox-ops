#!/usr/bin/env bash
#
# Heartbeat producer for the CachyOS RX 7900 XTX edge worker.
#
# Emits the EDGE-WORKER-CONTRACT §1 payload for this node and, when a router
# URL is configured, POSTs it to /v1/capacity/heartbeat. `agent-router` does not
# exist yet (35.9), so with no router configured this prints the payload and
# exits — which is also how it is tested.
#
# The part that is NOT deferrable, and the reason this script exists at 35.6
# rather than at 35.9: the translation from runtime identity to catalog
# model_id.
#
#   EDGE-WORKER-CONTRACT §1: "active_model" and "cached_models" carry the
#   catalog model_id (`qwen36-27b`), never the upstream_model_id
#   (`qwen3.6-27b`) that agentgateway sends to the server. "Translation is the
#   edge producer's job. A runtime knows its own identity, not the catalog's:
#   llama.cpp reports whatever --alias it was started with. The daemon emitting
#   the heartbeat maps that runtime identity onto the catalog model_id before
#   sending. The router does not guess, does not keep an alias table, and does
#   not fall back to fuzzy matching."
#
# So: llama-swap says `qwen3.6-27b`, this script sends `qwen36-27b`, and an id
# in neither column of model-id-map.json is dropped with a warning rather than
# guessed at. Run with --self-test to exercise exactly that.
#
# capabilities and max_context describe what this deployment can SERVE, so they
# come from the models it is configured with (model-id-map.json's
# runtime_to_catalog, whose facts come from the catalog), never from
# cached_models. A node that cannot see its own model store is not a node that
# cannot do chat. The router intersects what is advertised here with the real
# catalog under R14, so this is an advertisement and not an authority.
#
# cached_models (STORY-035-9c Part A): the model store is a docker volume this
# host daemon cannot read directly -- its mountpoint requires root. Rather than
# grant this daemon Docker control-plane authority (docker exec was considered
# and REJECTED, owner security ruling: a process that can drive Docker can
# start a privileged container), cached_models is derived from a
# cache-observation manifest that scripts/edge-supervisor.sh writes into the
# shared EDGE_STATE_DIR from its own already-mounted /models:ro -- the same
# boundary this daemon already reads claim/lease/phase over, carrying one more
# file. The manifest publishes relative artifact PATHS, never a catalog id;
# translating them through model-id-map.json's artifact_to_catalog is this
# script's job, exactly as it already translates llama-swap's runtime model
# ids. See derive_cached_models() below and README.md "Heartbeat and the
# model-id translation".
#
# Fail-to-empty, always: a missing, unreadable, malformed, incomplete or stale
# (older than EDGE_CACHE_MANIFEST_MAX_AGE) manifest reports cached_models as
# empty rather than reusing whatever was last observed. Loss of observability
# is never promoted to a stale claim of presence.
#
# One line of the payload deserves its own warning. `runtime.endpoint` is
# observational status metadata only (§2). It is not service discovery, it
# never becomes a routing target, and it must never trigger gateway
# reconfiguration: whoever holds an edge credential must not be able to
# redirect a placement's traffic by advertising an address of their choosing.
# agentgateway backend hostnames stay Git-managed.
#
# Usage:
#   edge-heartbeat.sh [--once] [--interval SECONDS] [--self-test]
#
#   --once        emit one heartbeat and exit (default)
#   --interval N  emit every N seconds until stopped. The router declares a
#                 node OFFLINE after 3 x interval, so whatever is chosen here
#                 has to match what the router is told.
#   --self-test   run the translation unit tests and exit
#
# Environment (see env.example):
#   EDGE_NODE_ID              placement name (default cachyos-7900xtx)
#   EDGE_ENDPOINT             https://<edge-hostname>:<port>
#   EDGE_API_KEY              bearer credential for llama-swap
#   EDGE_CA_CERT              dedicated edge CA bundle
#   EDGE_CURL_RESOLVE         optional curl --resolve, while LAN DNS is missing
#   EDGE_MODEL_MAP            model-id-map.json (default: next to this script)
#   EDGE_STATE_DIR            state directory shared with the container
#   EDGE_CACHE_MANIFEST_MAX_AGE
#                             seconds a cache-observation manifest stays fresh
#                             (default 180 -- 3x edge-supervisor.sh's default
#                             EDGE_CACHE_SCAN_INTERVAL of 60s, the same 3x
#                             relationship the router uses for offline
#                             detection). Older, missing, unreadable, malformed
#                             or incomplete => cached_models reports empty.
#   EDGE_LLAMACPP_BUILD       llama.cpp build id, for runtime.version
#   EDGE_ROUTER_URL           agent-router base URL; unset = print only
#   EDGE_ROUTER_TOKEN         bearer credential for the router
#   EDGE_CLUSTER_PROBE_URL    URL proving this host reaches the cluster
#   EDGE_SERVING_GPU_PCT      busy-% above which a loaded model counts as
#                             SERVING rather than AVAILABLE (default 20)
#   ROCM_SMI                  rocm-smi command (default: rocm-smi)

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=edge-common.sh
. "$SCRIPT_DIR/edge-common.sh"

# Read by edge_log() in edge-common.sh, and inherited by anything this script
# spawns, so a stray child's output is attributable too.
export EDGE_LOG_TAG=heartbeat

NODE_ID="${EDGE_NODE_ID:-cachyos-7900xtx}"
MODEL_MAP="${EDGE_MODEL_MAP:-$SCRIPT_DIR/../model-id-map.json}"
CACHE_MANIFEST_MAX_AGE="${EDGE_CACHE_MANIFEST_MAX_AGE:-180}"
SERVING_GPU_PCT="${EDGE_SERVING_GPU_PCT:-20}"

MODE=once
INTERVAL=30

while [ "$#" -gt 0 ]; do
    case "$1" in
        --once) MODE=once; shift ;;
        --interval)
            [ "$#" -ge 2 ] || edge_die "--interval requires a value" 2
            MODE=loop; INTERVAL="$2"; shift 2 ;;
        --self-test) MODE=selftest; shift ;;
        -h|--help)
            awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
            exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

edge_require_tools jq curl date

# --- translation ------------------------------------------------------------
# The two functions below are the whole of the contract's "translation is the
# edge producer's job", and are what --self-test covers.

# runtime id -> catalog model_id. Prints nothing and returns 1 when the id is
# not in the map, which is the deliberate no-guess path.
translate_runtime_id() {
    local runtime_id="$1" catalog_id
    catalog_id=$(jq -er --arg id "$runtime_id" '.runtime_to_catalog[$id] // empty' "$MODEL_MAP" 2>/dev/null) || return 1
    [ -n "$catalog_id" ] || return 1
    printf '%s' "$catalog_id"
}

# Cache-observation manifest -> catalog model_ids, one per line. "cached" in
# the contract means the artifact is present so no download is needed, which is
# a question about the filesystem, not about llama-swap's configuration — a
# model can be configured and its GGUF absent, and reporting that as cached
# would promise the router a load time it cannot deliver.
#
# This host cannot read the model store itself (STORY-035-9c Part A: it is a
# docker volume that requires root, and `docker exec` was rejected as an
# observation mechanism -- see the header comment). scripts/edge-supervisor.sh
# publishes a filenames/existence scan of that store into
# EDGE_CACHE_MANIFEST_FILE, over the same shared-state boundary this script
# already reads claim/lease/phase over, and this function is the translation
# half: relative artifact path -> catalog model_id, via model-id-map.json's
# artifact_to_catalog, exactly as translate_runtime_id() does for llama-swap's
# runtime ids. An artifact path with no catalog counterpart is dropped with a
# warning, never guessed at.
#
# Fail-to-empty on every form of lost observability, and NEVER a stale
# last-known-good claim: a missing/unreadable file, a malformed one, one whose
# scan did not complete (complete != true), or one older than
# CACHE_MANIFEST_MAX_AGE all report no cached models. A manifest timestamped in
# the future (clock skew) is treated exactly like the guard-lease check in
# scripts/edge-supervisor.sh treats it: not fresh, so as not to fail open on a
# clock instead of on the scan itself.
derive_cached_models() {
    local manifest complete scanned_at scanned_epoch now age rel catalog_id

    manifest="$EDGE_CACHE_MANIFEST_FILE"
    if [ ! -r "$manifest" ]; then
        edge_log "WARN cache manifest '$manifest' not readable; reporting no cached models"
        return 0
    fi
    if ! jq -e 'type == "object"
            and (.scanned_at | type == "string")
            and (.complete   | type == "boolean")
            and (.artifacts  | type == "array" and all(type == "string"))' \
        "$manifest" >/dev/null 2>&1; then
        edge_log "WARN cache manifest '$manifest' is malformed; reporting no cached models"
        return 0
    fi

    complete=$(jq -r '.complete' "$manifest" 2>/dev/null)
    if [ "$complete" != "true" ]; then
        edge_log "WARN cache manifest reports an incomplete or failed scan; reporting no cached models"
        return 0
    fi

    scanned_at=$(jq -r '.scanned_at' "$manifest" 2>/dev/null)
    scanned_epoch=$(date -u -d "$scanned_at" +%s 2>/dev/null) || {
        edge_log "WARN cache manifest scanned_at '$scanned_at' is unparseable; reporting no cached models"
        return 0
    }
    now=$(date -u +%s)
    age=$((now - scanned_epoch))
    if [ "$age" -lt 0 ]; then
        edge_log "WARN cache manifest scanned_at is ${age#-}s in the FUTURE (clock skew between container and host?); reporting no cached models"
        return 0
    fi
    if [ "$age" -gt "$CACHE_MANIFEST_MAX_AGE" ]; then
        edge_log "WARN cache manifest is stale (age ${age}s > ${CACHE_MANIFEST_MAX_AGE}s); reporting no cached models"
        return 0
    fi

    while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        catalog_id=$(jq -er --arg p "$rel" '.artifact_to_catalog[$p] // empty' "$MODEL_MAP" 2>/dev/null) || catalog_id=""
        if [ -n "$catalog_id" ]; then
            printf '%s\n' "$catalog_id"
        else
            edge_log "WARN cache manifest artifact '$rel' has no catalog mapping; ignoring"
        fi
    done < <(jq -er '.artifacts[]? // empty' "$manifest" 2>/dev/null)
}

# Catalog model_ids this deployment is CONFIGURED to serve, one per line — the
# right-hand side of runtime_to_catalog, which mirrors the models llama-swap.yaml
# defines. Not a filesystem question and deliberately not the same question as
# cached_models.
served_catalog_ids() {
    jq -er '[ (.runtime_to_catalog // {}) | values[] ] | unique | .[]' "$MODEL_MAP" 2>/dev/null
}

# capabilities + max_context for a JSON array of catalog model_ids, as
# {"capabilities": [...], "max_context": N}.
#
# These describe what this deployment is able to SERVE, so they are derived from
# the configured models and never from cached_models. Deriving them from the
# cache was wrong in both directions: the model store is a docker volume this
# host daemon cannot read directly, so a cache-derived answer advertised a
# warm, serving node as capable of nothing with a zero-length context window,
# which is an eligibility answer of "never pick me". A node that cannot see
# its own disk is not a node that cannot do chat.
#
# This is an advertisement, not an authority: the router intersects it with the
# real catalog under R14, so an id whose facts here drifted from the catalog
# narrows placement rather than widening it. The facts themselves come from
# model-id-map.json, whose source of truth is the catalog ConfigMap.
derive_served_facts() {
    jq -e --argjson ids "$1" '
        (.catalog_facts // {}) as $facts
        | { capabilities: ([ $ids[] | $facts[.].capabilities // [] ] | add // [] | unique),
            max_context:  ([ $ids[] | $facts[.].max_context  // empty ] | max // 0) }
    ' "$MODEL_MAP" 2>/dev/null
}

# --- self test --------------------------------------------------------------
run_self_test() {
    local failures=0 tmp got served_json NOW_RFC3339 STALE_RFC3339

    check() {
        local what="$1" want="$2" have="$3"
        if [ "$want" = "$have" ]; then
            printf 'PASS %-42s %s\n' "$what" "$have"
        else
            printf 'FAIL %-42s want=%s have=%s\n' "$what" "$want" "$have"
            failures=$((failures + 1))
        fi
    }

    [ -r "$MODEL_MAP" ] || edge_die "model map '$MODEL_MAP' is not readable" 2

    # 1. The translation the contract names explicitly.
    got=$(translate_runtime_id "qwen3.6-27b" || echo "<none>")
    check "runtime qwen3.6-27b -> catalog" "qwen36-27b" "$got"

    # 2. The catalog id must not be silently accepted as a runtime id: the map
    #    is one-directional on purpose, so a producer that started reporting
    #    the wrong column fails loudly here instead of quietly in the router.
    got=$(translate_runtime_id "qwen36-27b" || echo "<none>")
    check "catalog id is not a runtime id" "<none>" "$got"

    # 3. No guessing. An unmapped id is dropped, not passed through.
    got=$(translate_runtime_id "qwen3.6-27b-instruct-q4" || echo "<none>")
    check "unmapped runtime id is dropped" "<none>" "$got"

    # 4. No manifest at all -- the container has never scanned, or the state
    #    directory this daemon reads is not the one the supervisor writes.
    tmp=$(mktemp -d "${TMPDIR:-/tmp}/edge-hb-test.XXXXXX")
    EDGE_CACHE_MANIFEST_FILE="$tmp/cache-manifest.json"
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "no manifest is not cached" "" "$got"

    # A manifest is exactly what scripts/edge-supervisor.sh's
    # write_cache_manifest() produces: scanned_at, complete, artifacts[].
    write_manifest() {
        local scanned_at="$1" complete="$2" artifacts_json="$3"
        jq -n --arg scanned_at "$scanned_at" --argjson complete "$complete" \
            --argjson artifacts "$artifacts_json" \
            '{scanned_at: $scanned_at, complete: $complete, artifacts: $artifacts}' \
            >"$EDGE_CACHE_MANIFEST_FILE"
    }
    NOW_RFC3339=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # 5. A fresh, complete manifest naming the real artifact translates to the
    #    catalog id -- the manifest's whole purpose.
    write_manifest "$NOW_RFC3339" true \
        '["unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf"]'
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "manifest artifact maps to catalog id" "qwen36-27b," "$got"

    # 6. No guessing here either: an artifact the manifest reports that
    #    model-id-map.json does not recognise is dropped, not passed through.
    write_manifest "$NOW_RFC3339" true '["unsloth/some-other-model/weights.gguf"]'
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "unmapped manifest artifact is dropped" "" "$got"

    # 7. complete: false is the supervisor's own signal that its scan did not
    #    finish (STORY-035-9c Part A) -- honoured even when the timestamp is
    #    fresh and the artifact list is otherwise correct, because a
    #    known-incomplete scan is not evidence of anything.
    write_manifest "$NOW_RFC3339" false \
        '["unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf"]'
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "incomplete scan reports nothing cached" "" "$got"

    # 8. Loss/staleness: an old scan is never treated as still true. Chosen
    #    comfortably past the default EDGE_CACHE_MANIFEST_MAX_AGE (180s).
    STALE_RFC3339=$(date -u -d "@$(( $(date -u +%s) - 3600 ))" +%Y-%m-%dT%H:%M:%SZ)
    write_manifest "$STALE_RFC3339" true \
        '["unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf"]'
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "stale manifest reports nothing cached" "" "$got"
    rm -rf "$tmp"

    # 9. Every catalog id the map can produce must have catalog facts, or the
    #    heartbeat would advertise a model with no capabilities or context.
    got=$(jq -er '
        [ (.runtime_to_catalog | values[]), (.artifact_to_catalog | values[]) ]
        | unique
        | map(select(. as $id | ($ARGS.named.facts | has($id) | not)))
        | join(",")
    ' --argjson facts "$(jq -c '.catalog_facts' "$MODEL_MAP")" "$MODEL_MAP" 2>/dev/null)
    check "every mapped catalog id has facts" "" "$got"

    # 10. What this deployment advertises it can serve comes from the models it
    #     is configured with, not from anything on disk.
    got=$(served_catalog_ids | tr '\n' ',')
    check "served ids are the configured models" "qwen36-27b," "$got"

    served_json=$(served_catalog_ids | jq -R . | jq -sc 'unique')

    # 11. The regression this pair of tests exists for. capabilities/max_context
    #     used to be derived from cached_models, so an unobservable cache —
    #     no manifest, exactly test 4's fixture — advertised a warm, serving
    #     node as capable of nothing with a zero-length context window, which
    #     the router can only read as "never pick me". An unobservable cache is
    #     not an incapable node.
    EDGE_CACHE_MANIFEST_FILE="${TMPDIR:-/tmp}/edge-hb-test-no-manifest.$$"
    got=$(derive_cached_models 2>/dev/null | tr '\n' ',')
    check "no manifest caches nothing" "" "$got"
    got=$(derive_served_facts "$served_json" | jq -c . 2>/dev/null)
    check "no manifest keeps capabilities" \
        '{"capabilities":["chat","tools"],"max_context":65536}' "$got"

    echo
    if [ "$failures" -eq 0 ]; then
        echo "self-test OK"
        return 0
    fi
    echo "self-test FAILED ($failures)"
    return 1
}

# --- payload ----------------------------------------------------------------
ac_power() {
    # A desktop has no battery, so `true` is a fact rather than an assumption.
    # On a host that does have one the contract's laptop rule applies (§4:
    # ac_power false disqualifies laptop placements) and the answer has to come
    # from the hardware, not from a default.
    local ps type has_battery=0 mains_online=0
    for ps in /sys/class/power_supply/*; do
        [ -r "$ps/type" ] || continue
        type=$(cat "$ps/type" 2>/dev/null)
        case "$type" in
            Battery) has_battery=1 ;;
            Mains) [ "$(cat "$ps/online" 2>/dev/null)" = "1" ] && mains_online=1 ;;
            *) : ;;
        esac
    done
    if [ "$has_battery" -eq 1 ]; then
        [ "$mains_online" -eq 1 ] && echo true || echo false
        return
    fi
    echo "${EDGE_AC_POWER:-true}"
}

cluster_reachable() {
    # Reported false when it cannot be demonstrated. Eligibility implications
    # are one-way: under-claiming can only make this placement less attractive,
    # while a cheerful `true` from a host that cannot reach the cluster sends
    # the router work that will never land.
    #
    # The honest long-term answer is "did my last heartbeat POST arrive", so
    # once EDGE_ROUTER_URL is set (35.9) that is what gets probed. Until then
    # EDGE_CLUSTER_PROBE_TCP is a plain TCP connect to something cluster-side,
    # which avoids needing a credential or a trust anchor just to answer a
    # reachability question — and avoids calling the AI gateway, which this
    # story is not allowed to touch.
    local url="${EDGE_CLUSTER_PROBE_URL:-${EDGE_ROUTER_URL:-}}" hostport host port
    if [ -n "$url" ]; then
        if curl --silent --output /dev/null --max-time 4 "$url" 2>/dev/null; then
            echo true
        else
            echo false
        fi
        return
    fi
    hostport="${EDGE_CLUSTER_PROBE_TCP:-}"
    if [ -n "$hostport" ]; then
        host="${hostport%:*}"
        port="${hostport##*:}"
        if timeout 4 bash -c "exec 3<>/dev/tcp/$host/$port" 2>/dev/null; then
            echo true
        else
            echo false
        fi
        return
    fi
    edge_log "WARN no EDGE_ROUTER_URL, EDGE_CLUSTER_PROBE_URL or EDGE_CLUSTER_PROBE_TCP; reporting cluster_reachable=false"
    echo false
}

runtime_version() {
    local body swap_version
    edge_curl_opts
    body=$(curl "${EDGE_CURL_OPTS[@]}" -H "Authorization: Bearer ${EDGE_API_KEY:-}" \
        "${EDGE_ENDPOINT%/}/api/version" 2>/dev/null) || body=""
    swap_version=$(printf '%s' "$body" | jq -er '.version // empty' 2>/dev/null) || swap_version=""
    # llama.cpp's own build id is deliberately NOT read from the upstream
    # server: /props would force a cold start just to answer a version
    # question, and pinning a 24 GB model into VRAM to fill in a string is the
    # exact behaviour invariant 7 exists to prevent. It comes from the image
    # tag instead.
    printf 'llama-swap/%s llama.cpp/%s' \
        "${swap_version:-unknown}" "${EDGE_LLAMACPP_BUILD:-unknown}"
}

derive_state() {
    local phase claim alive loaded gpu_pct
    phase="$1"; claim="$2"; alive="$3"; loaded="$4"; gpu_pct="$5"

    # Order matters: a claimed GPU outranks everything, because the host has
    # already decided and the endpoint is on its way down or already down.
    if [ "$claim" = "yes" ]; then
        case "$phase" in
            withdrawn) echo INTERACTIVE ;;
            *) echo DRAINING ;;
        esac
        return
    fi
    [ "$phase" = "draining" ] && { echo DRAINING; return; }
    [ "$alive" = "yes" ] || { echo OFFLINE; return; }
    if [ "$loaded" -gt 0 ] && [ "$gpu_pct" -ge "$SERVING_GPU_PCT" ]; then
        echo SERVING
        return
    fi
    echo AVAILABLE
}

emit_heartbeat() {
    local gpu total used pct total_gib free_gib identity gpu_model gpu_arch
    local phase claim alive running active_runtime active_model cached state loaded
    local served served_facts

    if gpu=$(edge_gpu_sample); then
        # Field 4 is compute-client VRAM, which only the interactive guard
        # needs; vram_free_gb is about the whole card.
        read -r total used pct _ <<<"$gpu"
        total_gib=$(awk -v b="$total" 'BEGIN { printf "%.1f", b / 1073741824 }')
        free_gib=$(awk -v t="$total" -v u="$used" 'BEGIN { printf "%.1f", (t - u) / 1073741824 }')
    else
        edge_log "WARN no GPU sample; reporting gpu fields as unmeasured"
        total_gib=null; free_gib=null; pct=0
    fi

    gpu_model=unmeasured
    gpu_arch=unmeasured
    if identity=$(edge_gpu_identity); then
        # Tab-separated: the card series has spaces in it.
        IFS=$'\t' read -r gpu_model gpu_arch <<<"$identity"
    fi

    phase=$(edge_phase)
    claim=no
    edge_claim_present && claim=yes
    alive=no
    edge_endpoint_alive && alive=yes

    running=$(edge_running_models 2>/dev/null || true)
    loaded=0
    active_model=""
    while IFS= read -r active_runtime; do
        [ -n "$active_runtime" ] || continue
        loaded=$((loaded + 1))
        if [ -z "$active_model" ]; then
            if ! active_model=$(translate_runtime_id "$active_runtime"); then
                active_model=""
                edge_log "ALARM llama-swap reports runtime model '$active_runtime' with no catalog mapping; omitting active_model"
            fi
        fi
    done <<<"$running"

    cached=$(derive_cached_models | jq -R . | jq -sc 'unique')
    served=$(served_catalog_ids | jq -R . | jq -sc 'unique')
    served_facts=$(derive_served_facts "$served") || served_facts=''
    if [ -z "$served_facts" ]; then
        edge_log "ALARM no catalog facts for the configured models; advertising no capabilities"
        served_facts='{"capabilities":[],"max_context":0}'
    fi
    state=$(derive_state "$phase" "$claim" "$alive" "$loaded" "${pct:-0}")

    jq -n \
        --arg node "$NODE_ID" \
        --arg state "$state" \
        --arg gpu_model "$gpu_model" \
        --arg gpu_arch "$gpu_arch" \
        --argjson vram_total "$total_gib" \
        --argjson vram_free "$free_gib" \
        --argjson util "${pct:-0}" \
        --arg runtime_version "$(runtime_version)" \
        --arg endpoint "${EDGE_ENDPOINT:-}" \
        --arg active "$active_model" \
        --argjson cached "$cached" \
        --argjson served_facts "$served_facts" \
        --argjson preemptible "${EDGE_PREEMPTIBLE:-true}" \
        --argjson interactive "$([ "$claim" = yes ] && echo true || echo false)" \
        --argjson ac_power "$(ac_power)" \
        --argjson cluster_reachable "$(cluster_reachable)" \
        --arg last_heartbeat "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        '
        {
            node: $node,
            state: $state,
            gpu: {
              vendor: "amd",
              model: $gpu_model,
              arch: $gpu_arch,
              vram_total_gb: $vram_total,
              vram_free_gb: $vram_free,
              utilization_pct: $util
            },
            runtime: {
              kind: "llama-swap+llama.cpp",
              version: $runtime_version,
              endpoint: $endpoint
            },
            active_model: (if $active == "" then null else $active end),
            cached_models: $cached,
            preemptible: $preemptible,
            interactive: $interactive,
            ac_power: $ac_power,
            cluster_reachable: $cluster_reachable,
            last_heartbeat: $last_heartbeat,
            capabilities: $served_facts.capabilities,
            max_context: $served_facts.max_context
          }
        '
}

post_heartbeat() {
    local payload="$1" status
    status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
        -H "Authorization: Bearer ${EDGE_ROUTER_TOKEN:-}" \
        -H "Content-Type: application/json" \
        -X POST "${EDGE_ROUTER_URL%/}/v1/capacity/heartbeat" \
        --data "$payload" 2>/dev/null) || status=000
    case "$status" in
        2*) : ;;
        *) edge_log "WARN router returned HTTP $status" ;;
    esac
}

case "$MODE" in
    selftest)
        run_self_test
        exit $?
        ;;
    once)
        payload=$(emit_heartbeat) || exit 1
        if [ -n "${EDGE_ROUTER_URL:-}" ]; then
            post_heartbeat "$payload"
        fi
        printf '%s\n' "$payload"
        ;;
    loop)
        while true; do
            payload=$(emit_heartbeat) || payload=""
            if [ -n "$payload" ]; then
                if [ -n "${EDGE_ROUTER_URL:-}" ]; then
                    post_heartbeat "$payload"
                else
                    printf '%s\n' "$payload"
                fi
            fi
            sleep "$INTERVAL"
        done
        ;;
esac
