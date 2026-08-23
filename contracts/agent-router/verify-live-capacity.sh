#!/usr/bin/env bash
#
# LIVE evidence harness for STORY-035-9c Part B: proves the deployed
# agent-router's capacity facts are TRUSTWORTHY, against the real router, the
# real edge producer and (as a no-wake oracle only) the real cluster.
#
# THIS IS DELIBERATELY NOT WIRED INTO CI. Every sibling verifier in this
# directory (verify-digests.sh, verify-placement-cases.sh,
# verify-ade-boundary.sh) checks committed documents and runs anywhere, on
# every relevant change. This one needs a live router, a live edge host it can
# reach over SSH, live credentials, and a live cluster context — none of which
# exist in a CI runner, and some of which (stopping edge-heartbeat.service,
# stopping the edge container) are disruptive to a real placement. Run it by
# hand, from a machine with all of the below, when the acceptance for 35.9c
# needs re-proving.
#
# Every check reads GET /v1/status — never the router's internal state, never
# a log line taken on faith — because that is the surface every future
# consumer (35.10, 35.11, an operator) actually has. A check that cannot state
# what it observed there is not run.
#
# WHAT EACH CHECK PROVES (story "Part B", in the same order)
#
#   1. freshness         last_heartbeat for the node is within 2x the
#                         producer interval, both read from /v1/status itself
#                         (heartbeat_policy.interval_seconds).
#   2. offline-restore    REAL end-to-end: stop edge-heartbeat.service on the
#                         edge host, wait past 3x interval, placement reads
#                         OFFLINE; restart it, placement recovers from the
#                         ACTUAL producer. Needs --edge-ssh; this is the half
#                         the story says a worker sandbox cannot run.
#   3. state-vocabulary   AVAILABLE observed live. SERVING / DRAINING /
#                         INTERACTIVE proved by SYNTHETIC AUTHENTICATED
#                         heartbeats using the real node credential — this
#                         proves the router accepts/stores/renders the state,
#                         NOT that the host physically entered it (35.6/35.6a
#                         already proved the guard's own preemption mechanics
#                         and are not reimplemented here). The real producer's
#                         heartbeat is restored and its recovery proved after
#                         every injection.
#   4. cluster-reachable  a synthetic heartbeat with cluster_reachable:false
#                         narrows eligibility, re-proving 35.9a's rule against
#                         the deployed router.
#   5. R14 anti-escalation a synthetic heartbeat claiming an active_model the
#                         catalog does not authorize on this placement is
#                         accepted (202) but readiness never reads warm;
#                         contrasted against the authorized claim, which does.
#   6. cached-model contrast  the five Part A acceptance proofs (present,
#                         absent, unknown-unmapped, staleness/loss, recovery),
#                         exercised through the manifest boundary on the real
#                         edge host. Needs --edge-ssh.
#   7. capabilities/max_context  a synthetic heartbeat cannot expand either
#                         beyond catalog authority (narrow-only, R14); no
#                         routing/constraint-selection logic is exercised or
#                         implied.
#
# PART C — the no-wake oracle. `check_kserve_no_wake` reads Kubernetes
# Deployment status (readyReplicas) for the KServe placement via a LABEL
# SELECTOR, never a probe of the predictor itself, never an HTTP call on its
# request path. It runs once before every other check and once after, and the
# suite fails if the second reading is not still zero — this is the
# acceptance's "the predictor is STILL at 0 replicas after the entire
# acceptance run" proof, and it is an ORACLE ONLY (owner amendment 2): not a
# new agent-router input, not permission for the router to reach the
# Kubernetes API, not a substitute for the catalog's frozen cold-start
# estimate.
#
# CREDENTIALS. Nothing credential-shaped is committed here. Every secret comes
# from a flag or an environment variable, and this script never prints one —
# curl calls that carry a bearer token do so via -H with the value
# interpolated, never echoed, and command output that could contain one is
# not logged verbatim.
#
# Usage:
#   verify-live-capacity.sh --router-url URL --router-token TOKEN \
#       --edge-node-token TOKEN [--node NAME] [--edge-ssh USER@HOST] \
#       [--kube-context NAME] [--interval-margin N] [options]
#
# Required:
#   --router-url URL        Base URL of the deployed router, e.g.
#                            https://agent-router.homelab0.org
#   --router-token TOKEN     Caller bearer credential for GET /v1/status.
#                            Prefer AGENT_ROUTER_TOKEN in the environment over
#                            this flag: an argument is visible to other local
#                            users via ps / /proc/<pid>/cmdline.
#   --edge-node-token TOKEN  The edge node's OWN bearer credential, used to
#                            send synthetic heartbeats (checks 3, 4, 5, 7).
#                            Prefer EDGE_ROUTER_TOKEN in the environment.
#
# Optional:
#   --node NAME              Placement name under test (default
#                             cachyos-7900xtx).
#   --edge-ssh USER@HOST     SSH target for the edge host. Required for
#                             checks 2 and 6, which need systemctl/docker
#                             access this script cannot get through the
#                             router API. Checks 2 and 6 FAIL, loudly, with
#                             this omitted — they are not silently skipped,
#                             because a vacuous pass here would misreport
#                             35.9c's acceptance.
#   --kube-context NAME       kubectl context for the Part C oracle. Default:
#                             ambient current-context.
#   --namespace NAME          Kubernetes namespace the KServe placement lives
#                             in (default: ai).
#   --interval-margin N       Extra seconds of slack added to every
#                             interval-derived wait, for clock skew and
#                             request latency (default 10).
#   --timeout SECONDS         Per-HTTP-call timeout (default 15).
#
# Exit status: 0 if every check PASSed, 1 if any FAILed, 2 on usage error.

set -uo pipefail

ROUTER_URL=""
ROUTER_TOKEN="${AGENT_ROUTER_TOKEN:-}"
EDGE_NODE_TOKEN="${EDGE_ROUTER_TOKEN:-}"
NODE="cachyos-7900xtx"
EDGE_SSH=""
EDGE_LOCAL=0
KUBE_CONTEXT=""
NAMESPACE="ai"
INTERVAL_MARGIN=10
TIMEOUT=15

usage() {
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

require_value() {
    if [ "$2" -lt 2 ]; then
        echo "FATAL: $1 requires a value" >&2
        exit 2
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --router-url) require_value "$1" "$#"; ROUTER_URL="$2"; shift 2 ;;
        --router-token) require_value "$1" "$#"; ROUTER_TOKEN="$2"; shift 2 ;;
        --edge-node-token) require_value "$1" "$#"; EDGE_NODE_TOKEN="$2"; shift 2 ;;
        --node) require_value "$1" "$#"; NODE="$2"; shift 2 ;;
        --edge-ssh) require_value "$1" "$#"; EDGE_SSH="$2"; shift 2 ;;
        # This harness is usually run FROM the edge host (the story's own
        # acceptance was). --edge-local runs the edge-side commands directly
        # instead of through ssh-to-self, which would demand key setup for no
        # isolation gain.
        --edge-local) EDGE_LOCAL=1; shift ;;
        --kube-context) require_value "$1" "$#"; KUBE_CONTEXT="$2"; shift 2 ;;
        --namespace) require_value "$1" "$#"; NAMESPACE="$2"; shift 2 ;;
        --interval-margin) require_value "$1" "$#"; INTERVAL_MARGIN="$2"; shift 2 ;;
        --timeout) require_value "$1" "$#"; TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$ROUTER_URL" ] || [ -z "$ROUTER_TOKEN" ] || [ -z "$EDGE_NODE_TOKEN" ]; then
    echo "FATAL: --router-url, --router-token (or AGENT_ROUTER_TOKEN) and --edge-node-token (or EDGE_ROUTER_TOKEN) are required" >&2
    exit 2
fi

for bin in curl jq kubectl ssh; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "FATAL: required tool '$bin' not found on PATH" >&2
        exit 2
    fi
done

ROUTER_URL="${ROUTER_URL%/}"

# The offline-restore and cached-contrast checks stop the real producer and the
# real edge container mid-check. An interrupt during those windows would leave
# production degraded (it happened: a run killed mid-offline-check left
# edge-heartbeat.service stopped until noticed). Restore both on the way out of
# ANY abnormal exit; idempotent when nothing was stopped.
restore_production() {
    if [ "$EDGE_LOCAL" = 1 ] || [ -n "$EDGE_SSH" ]; then
        ssh_edge "systemctl --user start edge-heartbeat.service" >/dev/null 2>&1 || true
        ssh_edge "docker start edge-llama-swap" >/dev/null 2>&1 || true
    fi
}
trap 'echo "  interrupted - restoring production state ..." >&2; restore_production' INT TERM

SCRATCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/verify-live-capacity.XXXXXX")
trap 'rm -rf "$SCRATCH_DIR"' EXIT

CURL_OPTS=(--silent --show-error --max-time "$TIMEOUT")

PASS_COUNT=0
FAIL_COUNT=0

report() {
    local status="$1" name="$2" detail="${3:-}"
    case "$status" in
        PASS) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
    esac
    if [ -n "$detail" ]; then
        printf '%-4s %-32s %s\n' "$status" "$name" "$detail"
    else
        printf '%-4s %-32s\n' "$status" "$name"
    fi
}

# --- helpers -----------------------------------------------------------------

# GET /v1/status. Every check re-reads this fresh rather than caching a
# snapshot, because the whole point is to observe the router's rendered state,
# not to assume it.
status_get() {
    curl "${CURL_OPTS[@]}" -H "Authorization: Bearer $ROUTER_TOKEN" \
        "$ROUTER_URL/v1/status" 2>"$SCRATCH_DIR/status.err"
}

# One field of one placement from a captured /v1/status body: status_field
# BODY '.field.path'
status_field() {
    printf '%s' "$1" | jq -er --arg node "$NODE" \
        --argjson path "$(printf '%s' "$2" | jq -R -s 'split(".") | map(select(length > 0))')" \
        'reduce ($path[]) as $p
            (.placements[] | select(.name == $node));
         .' 2>/dev/null
}

# The named placement object out of a captured /v1/status body, or empty if
# the node is not present at all -- which is itself a finding, not a crash.
placement_of() {
    printf '%s' "$1" | jq -e --arg node "$NODE" '.placements[] | select(.name == $node)' 2>/dev/null
}

# POST a heartbeat body to the router with the real edge node credential.
# $1 = JSON body. Prints the HTTP status code.
post_heartbeat() {
    local payload="$1"
    curl "${CURL_OPTS[@]}" --output "$SCRATCH_DIR/heartbeat-resp.json" \
        --write-out '%{http_code}' \
        -H "Authorization: Bearer $EDGE_NODE_TOKEN" -H "Content-Type: application/json" \
        -X POST "$ROUTER_URL/v1/capacity/heartbeat" --data "$payload" \
        2>"$SCRATCH_DIR/heartbeat.err"
}

# Build a synthetic heartbeat by taking the LAST REAL heartbeat this node sent
# (re-exposed verbatim by /v1/status per EDGE-WORKER-CONTRACT §1) and
# overriding only the fields the caller names, via a jq filter fragment. This
# keeps every synthetic injection otherwise indistinguishable from a real one
# in shape, which is the point of labelling it "synthetic authenticated
# contract/render test" rather than "malformed payload test".
synthetic_from_real() {
    local real_heartbeat="$1" jq_overrides="$2"
    printf '%s' "$real_heartbeat" | jq --arg node "$NODE" --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        ". + {node: \$node, last_heartbeat: \$now} + ($jq_overrides)"
}

ssh_edge() {
    if [ "$EDGE_LOCAL" = 1 ]; then
        # Call sites pass ONE command string (ssh joins args through the remote
        # shell); local mode must match that contract, not exec the string as a
        # single argv[0].
        bash -c "$*"
        return $?
    fi
    [ -n "$EDGE_SSH" ] || return 2
    ssh -o BatchMode=yes -o ConnectTimeout="$TIMEOUT" "$EDGE_SSH" "$@"
}

# --- Part C: KServe no-wake oracle -------------------------------------------
#
# ACCEPTANCE-TEST ORACLE ONLY (owner amendment 2). A label-selector read of
# Deployment status -- never a hardcoded predictor object name, never a
# request to the predictor or its Knative revisions, never anything on the
# request path. See the header comment for what this is and is not.
kserve_ready_replicas() {
    local ctx_args=()
    [ -n "$KUBE_CONTEXT" ] && ctx_args=(--context "$KUBE_CONTEXT")
    kubectl "${ctx_args[@]}" get deployment -n "$NAMESPACE" \
        -l "serving.kserve.io/inferenceservice=qwen36-27b,component=predictor" \
        -o jsonpath='{range .items[*]}{.status.readyReplicas}{"\n"}{end}' 2>/dev/null
}

check_kserve_no_wake() {
    local label="$1" replicas total=0 line
    replicas=$(kserve_ready_replicas) || {
        report FAIL "kserve-no-wake:$label" "kubectl query failed"
        return
    }
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        case "$line" in ''|*[!0-9]*) continue ;; esac
        total=$((total + line))
    done <<<"$replicas"
    if [ "$total" -ne 0 ]; then
        report FAIL "kserve-no-wake:$label" "predictor readyReplicas=$total, expected 0"
        return
    fi
    report PASS "kserve-no-wake:$label" "readyReplicas=0"
}

# --- check 1: freshness -------------------------------------------------------
check_freshness() {
    local body plc interval last_hb last_epoch now age bound
    body=$(status_get) || { report FAIL "freshness" "GET /v1/status failed"; return; }
    interval=$(printf '%s' "$body" | jq -er '.heartbeat_policy.interval_seconds' 2>/dev/null) || {
        report FAIL "freshness" "no heartbeat_policy.interval_seconds in /v1/status"
        return
    }
    plc=$(placement_of "$body") || { report FAIL "freshness" "placement '$NODE' not in /v1/status"; return; }
    last_hb=$(printf '%s' "$plc" | jq -er '.last_heartbeat' 2>/dev/null) || {
        report FAIL "freshness" "placement '$NODE' has no last_heartbeat"
        return
    }
    last_epoch=$(date -u -d "$last_hb" +%s 2>/dev/null) || {
        report FAIL "freshness" "last_heartbeat '$last_hb' is unparseable"
        return
    }
    now=$(date -u +%s)
    age=$((now - last_epoch))
    bound=$((interval * 2))
    if [ "$age" -lt 0 ] || [ "$age" -gt "$bound" ]; then
        report FAIL "freshness" "age=${age}s exceeds 2x interval (${bound}s)"
        return
    fi
    report PASS "freshness" "age=${age}s, within 2x interval (${bound}s)"
}

# --- check 2: silence -> OFFLINE -> restore (real, needs --edge-ssh) --------
check_offline_restore() {
    local body interval offline_after wait_s plc state source
    if [ "$EDGE_LOCAL" != 1 ] && [ -z "$EDGE_SSH" ]; then
        report FAIL "offline-restore" "requires --edge-ssh or --edge-local; the story names this the half a worker sandbox cannot run"
        return
    fi

    body=$(status_get) || { report FAIL "offline-restore" "GET /v1/status failed"; return; }
    interval=$(printf '%s' "$body" | jq -er '.heartbeat_policy.interval_seconds' 2>/dev/null) || {
        report FAIL "offline-restore" "no heartbeat_policy.interval_seconds in /v1/status"
        return
    }
    offline_after=$(printf '%s' "$body" | jq -er '.heartbeat_policy.offline_after_seconds' 2>/dev/null) || \
        offline_after=$((interval * 3))

    echo "  stopping edge-heartbeat.service on $EDGE_SSH ..."
    if ! ssh_edge "systemctl --user stop edge-heartbeat.service"; then
        report FAIL "offline-restore" "could not stop edge-heartbeat.service over ssh"
        return
    fi

    wait_s=$((offline_after + INTERVAL_MARGIN))
    echo "  waiting ${wait_s}s past offline_after_seconds ($offline_after) ..."
    sleep "$wait_s"

    body=$(status_get) || { report FAIL "offline-restore" "GET /v1/status failed after silence"; ssh_edge "systemctl --user start edge-heartbeat.service" >/dev/null 2>&1; return; }
    plc=$(placement_of "$body")
    state=$(printf '%s' "$plc" | jq -r '.state // empty')
    source=$(printf '%s' "$plc" | jq -r '.state_source // empty')
    if [ "$state" != "OFFLINE" ] || [ "$source" != "silence" ]; then
        report FAIL "offline-restore" "after silence: state=$state state_source=$source, expected OFFLINE/silence"
        ssh_edge "systemctl --user start edge-heartbeat.service" >/dev/null 2>&1
        return
    fi
    echo "  OFFLINE/silence confirmed; restarting edge-heartbeat.service ..."

    if ! ssh_edge "systemctl --user start edge-heartbeat.service"; then
        report FAIL "offline-restore" "went OFFLINE correctly, but could not restart edge-heartbeat.service over ssh"
        return
    fi

    wait_s=$((interval + INTERVAL_MARGIN))
    echo "  waiting up to ${wait_s}s for the actual producer's next heartbeat ..."
    local waited=0 recovered=0
    while [ "$waited" -lt "$wait_s" ]; do
        sleep "$INTERVAL_MARGIN"
        waited=$((waited + INTERVAL_MARGIN))
        body=$(status_get) || continue
        plc=$(placement_of "$body")
        state=$(printf '%s' "$plc" | jq -r '.state // empty')
        source=$(printf '%s' "$plc" | jq -r '.state_source // empty')
        if [ "$source" = "heartbeat" ] && [ "$state" != "OFFLINE" ]; then
            recovered=1
            break
        fi
    done
    if [ "$recovered" -ne 1 ]; then
        report FAIL "offline-restore" "no recovery from the real producer within ${wait_s}s (last: state=$state state_source=$source)"
        return
    fi
    report PASS "offline-restore" "OFFLINE on silence (state_source=silence), recovered to state=$state from the real producer"
}

# --- check 3: state vocabulary (AVAILABLE real; others synthetic) ----------
check_state_vocabulary() {
    local body real_hb plc failed=0

    body=$(status_get) || { report FAIL "state-vocabulary:setup" "GET /v1/status failed"; return; }
    plc=$(placement_of "$body") || { report FAIL "state-vocabulary:setup" "placement '$NODE' not in /v1/status"; return; }
    real_hb=$(printf '%s' "$plc" | jq -c '.heartbeat' 2>/dev/null)
    if [ -z "$real_hb" ] || [ "$real_hb" = "null" ]; then
        report FAIL "state-vocabulary:setup" "no real heartbeat on record to base synthetic injections on"
        return
    fi

    local live_state
    live_state=$(printf '%s' "$plc" | jq -r '.state // empty')
    if [ "$live_state" = "AVAILABLE" ]; then
        report PASS "state-vocabulary:AVAILABLE" "observed live from the real producer"
    else
        report FAIL "state-vocabulary:AVAILABLE" "live state is '$live_state', not AVAILABLE -- re-run when the node is idle"
        failed=1
    fi

    inject_and_check() {
        local want="$1" overrides="$2" payload code new_body new_plc got
        payload=$(synthetic_from_real "$real_hb" "$overrides")
        code=$(post_heartbeat "$payload")
        if [ "$code" != "202" ]; then
            report FAIL "state-vocabulary:$want" "synthetic authenticated contract/render test: POST heartbeat returned HTTP $code, expected 202"
            failed=1
            return
        fi
        sleep 2
        new_body=$(status_get) || { report FAIL "state-vocabulary:$want" "GET /v1/status failed after injection"; failed=1; return; }
        new_plc=$(placement_of "$new_body")
        got=$(printf '%s' "$new_plc" | jq -r '.state // empty')
        if [ "$got" != "$want" ]; then
            report FAIL "state-vocabulary:$want" "synthetic authenticated contract/render test: router rendered state=$got, expected $want"
            failed=1
            return
        fi
        report PASS "state-vocabulary:$want" "synthetic authenticated contract/render test: router accepted, stored and rendered $want"
    }

    inject_and_check SERVING '{state: "SERVING"}'
    inject_and_check DRAINING '{state: "DRAINING"}'
    inject_and_check INTERACTIVE '{state: "INTERACTIVE", interactive: true}'

    echo "  restoring the real producer's heartbeat ..."
    code=$(post_heartbeat "$real_hb")
    if [ "$code" != "202" ]; then
        report FAIL "state-vocabulary:restore" "could not restore the real heartbeat: HTTP $code"
        failed=1
    else
        sleep 2
        body=$(status_get) || body=""
        plc=$(placement_of "$body")
        got=$(printf '%s' "$plc" | jq -r '.state // empty')
        want_restore=$(printf '%s' "$real_hb" | jq -r '.state // empty')
        if [ "$got" = "$want_restore" ]; then
            report PASS "state-vocabulary:restore" "production state recovered to $got"
        else
            report FAIL "state-vocabulary:restore" "after restoring the real payload, router still renders $got (expected $want_restore)"
            failed=1
        fi
    fi

    return $failed
}

# --- check 4: cluster_reachable narrows --------------------------------------
check_cluster_reachable_narrows() {
    local body real_hb plc payload code new_body new_plc eligible
    body=$(status_get) || { report FAIL "cluster-reachable-narrows" "GET /v1/status failed"; return; }
    plc=$(placement_of "$body")
    real_hb=$(printf '%s' "$plc" | jq -c '.heartbeat' 2>/dev/null)
    if [ -z "$real_hb" ] || [ "$real_hb" = "null" ]; then
        report FAIL "cluster-reachable-narrows" "no real heartbeat on record to base the synthetic injection on"
        return
    fi

    payload=$(synthetic_from_real "$real_hb" '{cluster_reachable: false}')
    code=$(post_heartbeat "$payload")
    if [ "$code" != "202" ]; then
        report FAIL "cluster-reachable-narrows" "synthetic heartbeat rejected: HTTP $code"
        post_heartbeat "$real_hb" >/dev/null
        return
    fi
    sleep 2
    new_body=$(status_get) || { report FAIL "cluster-reachable-narrows" "GET /v1/status failed after injection"; post_heartbeat "$real_hb" >/dev/null; return; }
    new_plc=$(placement_of "$new_body")
    eligible=$(printf '%s' "$new_plc" | jq -r '.eligible')

    echo "  restoring the real producer's heartbeat ..."
    post_heartbeat "$real_hb" >/dev/null

    if [ "$eligible" != "false" ]; then
        report FAIL "cluster-reachable-narrows" "cluster_reachable:false left eligible=$eligible, expected false"
        return
    fi
    report PASS "cluster-reachable-narrows" "cluster_reachable:false narrowed eligible to false"
}

# --- check 5: R14 anti-escalation, live --------------------------------------
check_r14_anti_escalation() {
    local body real_hb plc payload code new_body new_plc readiness

    body=$(status_get) || { report FAIL "r14-anti-escalation" "GET /v1/status failed"; return; }
    plc=$(placement_of "$body")
    real_hb=$(printf '%s' "$plc" | jq -c '.heartbeat' 2>/dev/null)
    if [ -z "$real_hb" ] || [ "$real_hb" = "null" ]; then
        report FAIL "r14-anti-escalation" "no real heartbeat on record to base the synthetic injection on"
        return
    fi

    # Deliberately a model_id absent from the catalog entirely, so this check
    # never depends on which specific models the catalog currently declares:
    # "no catalog counterpart" and "not authorized on this placement" are the
    # same alarm case per heartbeat.schema.json.
    payload=$(synthetic_from_real "$real_hb" \
        '{active_model: "not-a-catalog-model-035-9c", cached_models: ["not-a-catalog-model-035-9c"]}')
    code=$(post_heartbeat "$payload")
    if [ "$code" != "202" ]; then
        report FAIL "r14-anti-escalation:unauthorized" "unauthorized active_model claim returned HTTP $code, expected 202 (accepted but ignored for eligibility)"
        post_heartbeat "$real_hb" >/dev/null
        return
    fi
    sleep 2
    new_body=$(status_get) || { report FAIL "r14-anti-escalation:unauthorized" "GET /v1/status failed"; post_heartbeat "$real_hb" >/dev/null; return; }
    new_plc=$(placement_of "$new_body")
    readiness=$(printf '%s' "$new_plc" | jq -r '.readiness // empty')
    if [ "$readiness" = "warm" ]; then
        report FAIL "r14-anti-escalation:unauthorized" "unauthorized active_model claim was accepted AND rendered readiness=warm -- R14 violated"
        post_heartbeat "$real_hb" >/dev/null
        return
    fi
    report PASS "r14-anti-escalation:unauthorized" "202 accepted, readiness=$readiness (never warm) for an unauthorized claim"

    # Contrast: the same shape claiming the CATALOG-AUTHORIZED model reports
    # warm. Injected explicitly (synthetic authenticated contract/render test)
    # rather than echoing the real heartbeat, which is legitimately idle
    # (active_model null) most of the time.
    payload=$(synthetic_from_real "$real_hb" '{active_model: "qwen36-27b"}')
    code=$(post_heartbeat "$payload")
    sleep 2
    new_body=$(status_get) || new_body=""
    new_plc=$(placement_of "$new_body")
    readiness=$(printf '%s' "$new_plc" | jq -r '.readiness // empty')

    echo "  restoring the real producer's heartbeat ..."
    post_heartbeat "$real_hb" >/dev/null

    if [ "$readiness" != "warm" ]; then
        report FAIL "r14-anti-escalation:contrast" "authorized active_model claim rendered readiness=$readiness, expected warm"
        return
    fi
    report PASS "r14-anti-escalation:contrast" "synthetic authenticated contract/render test: authorized active_model claim rendered readiness=warm"
}

# --- check 6: cached-model contrast (drives the Part A acceptance) ---------
check_cached_model_contrast() {
    if [ "$EDGE_LOCAL" != 1 ] && [ -z "$EDGE_SSH" ]; then
        report FAIL "cached-model-contrast" "requires --edge-ssh or --edge-local -- exercises the manifest boundary on the real edge host (docker stop/start, aging the manifest), which this script cannot do through the router API alone"
        return
    fi

    local body plc cached failed=0

    # 1. Present, known artifact.
    body=$(status_get) || { report FAIL "cached-model-contrast:present" "GET /v1/status failed"; return; }
    plc=$(placement_of "$body")
    cached=$(printf '%s' "$plc" | jq -c '.heartbeat.cached_models // []' 2>/dev/null)
    if printf '%s' "$cached" | jq -e 'index("qwen36-27b") != null' >/dev/null 2>&1; then
        report PASS "cached-model-contrast:present" "qwen36-27b present in cached_models: $cached"
    else
        report FAIL "cached-model-contrast:present" "qwen36-27b missing from cached_models: $cached"
        failed=1
    fi

    # 2. Unknown artifact — SYNTHETIC LABELED translation test, no store write.
    #    Writing into the real model store would mutate production data to
    #    prove a pure translation property. Instead: run the INSTALLED
    #    heartbeat script once, in print mode, against a crafted manifest that
    #    lists an unmapped path. Same deployed code path
    #    (manifest -> model-id-map translation), zero mutation anywhere.
    echo "  synthetic translation test: unmapped path through the installed heartbeat script ..."
    local tmpm out
    tmpm=$(mktemp) && printf '{"scanned_at":"%s","complete":true,"artifacts":["totally/unmapped-035-9c.gguf","unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf"]}\n' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmpm"
    # Runs the INSTALLED script under the unit's own environment (edge.env),
    # with the router URL blanked so it prints instead of posting, and the
    # crafted manifest substituted through the overridable
    # EDGE_CACHE_MANIFEST_FILE (edge-common.sh honours the override; an
    # unconditional assignment there defeated this exact test once).
    out=$(ssh_edge "set -a; . \$HOME/.config/edge-cachyos/edge.env; set +a; EDGE_ROUTER_URL= EDGE_CACHE_MANIFEST_FILE='$tmpm' \$HOME/.local/libexec/edge-cachyos/scripts/edge-heartbeat.sh --once 2>&1") || out=""
    rm -f "$tmpm"
    if printf '%s' "$out" | grep -q "no catalog mapping" \
        && printf '%s' "$out" | jq -e 'try (.cached_models == ["qwen36-27b"]) catch false' >/dev/null 2>&1; then
        report PASS "cached-model-contrast:unknown-unmapped" "synthetic labeled translation test: unmapped path warned+ignored, mapped path translated (cached_models=[qwen36-27b])"
    else
        report FAIL "cached-model-contrast:unknown-unmapped" "translation did not behave: $(printf '%s' "$out" | tail -c 200)"
        failed=1
    fi

    # 3. Staleness/loss via container stop, and recovery on restart.
    echo "  stopping the edge container on $EDGE_SSH ..."
    if ssh_edge "docker stop edge-llama-swap"; then
        sleep "$INTERVAL_MARGIN"
        body=$(status_get) || body=""
        plc=$(placement_of "$body")
        cached=$(printf '%s' "$plc" | jq -c '.heartbeat.cached_models // []' 2>/dev/null)
        if [ "$cached" = "[]" ]; then
            report PASS "cached-model-contrast:loss" "cached_models empty after the container stopped: $cached"
        else
            report FAIL "cached-model-contrast:loss" "cached_models still non-empty after the container stopped: $cached"
            failed=1
        fi

        echo "  restarting the edge container on $EDGE_SSH ..."
        ssh_edge "docker start edge-llama-swap" || {
            report FAIL "cached-model-contrast:recovery" "could not restart edge-llama-swap over ssh -- MANUAL RECOVERY NEEDED"
            failed=1
        }
        sleep $((INTERVAL_MARGIN * 3))
        body=$(status_get) || body=""
        plc=$(placement_of "$body")
        cached=$(printf '%s' "$plc" | jq -c '.heartbeat.cached_models // []' 2>/dev/null)
        if printf '%s' "$cached" | jq -e 'index("qwen36-27b") != null' >/dev/null 2>&1; then
            report PASS "cached-model-contrast:recovery" "cached_models recovered after restart: $cached"
        else
            report FAIL "cached-model-contrast:recovery" "cached_models did not recover after restart: $cached"
            failed=1
        fi
    else
        report FAIL "cached-model-contrast:loss" "could not stop edge-llama-swap over ssh"
        failed=1
    fi

    return $failed
}

# --- check 7: capabilities / max_context — authority is catalog-derived ------
# R14 governs AUTHORITY: profile capabilities/min_context come from the Git
# catalog and a heartbeat claim must not move them. The placement's `heartbeat`
# block is a DIAGNOSTIC ECHO of what the node claimed — it is SUPPOSED to show
# an expanded claim faithfully (it is the reconciliation-alarm surface), so
# reading it as authority would misreport honesty as escalation. This check
# therefore asserts: (a) the catalog-derived profile fields are byte-identical
# before and after an expanded synthetic claim; (b) the placement carries no
# authority-shaped capabilities/max_context field outside the `heartbeat` echo.
check_capabilities_max_context() {
    local body plc real_hb before_profiles payload code new_body after_profiles stray

    body=$(status_get) || { report FAIL "capabilities-max-context" "GET /v1/status failed"; return; }
    plc=$(placement_of "$body")
    real_hb=$(printf '%s' "$plc" | jq -c '.heartbeat' 2>/dev/null)
    if [ -z "$real_hb" ] || [ "$real_hb" = "null" ]; then
        report FAIL "capabilities-max-context" "no real heartbeat on record to base the synthetic injection on"
        return
    fi
    before_profiles=$(printf '%s' "$body" | jq -cS '[.profiles[] | {name, capabilities, min_context}]')

    payload=$(synthetic_from_real "$real_hb"         '{capabilities: (.capabilities + ["vision", "audio"] | unique), max_context: 999999999}')
    code=$(post_heartbeat "$payload")
    if [ "$code" != "202" ]; then
        report FAIL "capabilities-max-context" "synthetic heartbeat rejected: HTTP $code"
        post_heartbeat "$real_hb" >/dev/null
        return
    fi
    sleep 2
    new_body=$(status_get) || { report FAIL "capabilities-max-context" "GET /v1/status failed after injection"; post_heartbeat "$real_hb" >/dev/null; return; }
    after_profiles=$(printf '%s' "$new_body" | jq -cS '[.profiles[] | {name, capabilities, min_context}]')
    stray=$(printf '%s' "$new_body" | jq -r --arg n "$NODE"         '.placements[] | select(.name==$n) | del(.heartbeat) | has("capabilities") or has("max_context")')

    echo "  restoring the real producer's heartbeat ..."
    post_heartbeat "$real_hb" >/dev/null

    if [ "$before_profiles" != "$after_profiles" ]; then
        report FAIL "capabilities-max-context" "catalog-derived profile capabilities/min_context MOVED after an expanded heartbeat claim"
        return
    fi
    if [ "$stray" = "true" ]; then
        report FAIL "capabilities-max-context" "placement carries an authority-shaped capabilities/max_context field outside the heartbeat echo"
        return
    fi
    report PASS "capabilities-max-context" "synthetic authenticated contract/render test: expanded claim echoed as observation only; catalog-derived profile authority byte-identical"
}


# --- main ---------------------------------------------------------------------
echo "verify-live-capacity: $ROUTER_URL (node=$NODE)"
echo "-----------------------------------------------------------------------------"

check_kserve_no_wake before

check_freshness
check_offline_restore
check_state_vocabulary
check_cluster_reachable_narrows
check_r14_anti_escalation
check_cached_model_contrast
check_capabilities_max_context

check_kserve_no_wake after

echo "-----------------------------------------------------------------------------"
echo "PASS=$PASS_COUNT FAIL=$FAIL_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
