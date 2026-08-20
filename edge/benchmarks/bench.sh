#!/usr/bin/env bash
#
# Physical validation harness (EPIC-035 Story 35.7, AC1).
#
# Measures cold-start, warm first-token latency, prompt/generation throughput
# and tool-call behaviour against any OpenAI-compatible chat-completions
# endpoint (llama.cpp server, both the kserve-a5000 and cachyos-7900xtx
# placements run the same build). Parameterised by --endpoint; nothing here
# is hardcoded to one host.
#
# Usage:
#   bench.sh <cold|warm|sustained|tool-call> --endpoint URL --api-key KEY [options]
#   bench.sh stats --in FILE --phase cold|warm --field FIELD
#
# Subcommands:
#   cold        Run N trials, each expected to start from a cold (unloaded /
#               scaled-to-zero) model. Use --state-cmd to have the caller
#               confirm the precondition per trial (see below).
#   warm        Run N trials against an already-warm model. Sends one
#               untimed warm-up request first.
#   sustained   Run continuous requests for --duration-seconds, logging
#               per-request throughput for drift/throttle analysis.
#   tool-call   Send one fixed tool-definition request and check whether a
#               well-formed tool call comes back.
#   stats       Read a JSONL file written by cold/warm/sustained and print
#               N / min / median / p95 / max for one numeric --field,
#               excluding discarded trials.
#
# Common options:
#   --endpoint URL          Base URL, e.g. https://cachyos-7900xtx.homelab0.org:8443
#   --api-key KEY           Bearer credential. Prefer the BENCH_API_KEY
#                           environment variable: a credential passed as a
#                           CLI argument is visible to other local users via
#                           ps / /proc/<pid>/cmdline on a shared host.
#   --model NAME             Model id in the request body (default: qwen36-27b)
#   --resolve HOST:PORT:IP   Passed straight through to curl --resolve, for
#                           endpoints with no DNS record yet.
#   --ca-cert PATH           Verify against this CA bundle instead of the
#                           machine's trust store (curl --cacert).
#   --insecure               Disable TLS certificate validation. Never use
#                           against a production endpoint; downgrades the
#                           run to a debug shape.
#   --timeout SECONDS        Per-request timeout (default: 300; cold starts
#                           on either placement can run to several minutes).
#
# cold/warm options:
#   --trials N               Number of timed trials (default: 5; AC3 requires
#                           at least 5 per phase).
#   --max-tokens N            Generation length for every trial (default: 256).
#   --prompt-file PATH        File whose contents become the single user
#                           message. Same file must be used for both
#                           placements to keep the prompt set fixed (AC2).
#   --state-cmd CMD           Shell command run (via `sh -c`) immediately
#                           before each trial. Must print exactly "cold",
#                           "warm" or "unknown" on stdout. A mismatch against
#                           the subcommand's phase discards the trial rather
#                           than trusting the harness's own assumption.
#   --guard-phase-cmd CMD      Shell command run before AND after each trial,
#                           printing the edge interactive-guard phase (or
#                           "n/a" for a placement with no such concept, e.g.
#                           the cluster). A phase other than "serving" or
#                           "n/a" on either read discards the trial (binding
#                           constraint 4).
#   --out FILE                 JSONL output, appended (default: derived from
#                           --model and subcommand under ./results/).
#
# sustained options:
#   --duration-seconds N        Minimum wall-clock run length (default: 600 —
#                           AC4 requires at least 10 minutes).
#   --interval-seconds N        Gap between the end of one request and the
#                           start of the next (default: 2).
#   --max-tokens N               Generation length per request (default: 128).
#   --out FILE
#
# tool-call options:
#   --prompt STR                 User message expected to trigger the tool
#                           (default: a weather-lookup prompt).
#   --out FILE                    Raw response and verdict.
#
# Environment variables BENCH_ENDPOINT, BENCH_API_KEY, BENCH_MODEL,
# BENCH_CA_CERT are read as defaults for the flags above.
#
# Exit status: 0 on success, 1 if any trial's request failed outright
# (network/HTTP error, not a discard), 2 on usage/environment error.

set -uo pipefail

SUBCOMMAND="${1:-}"
[ -n "$SUBCOMMAND" ] && shift

ENDPOINT="${BENCH_ENDPOINT:-}"
API_KEY="${BENCH_API_KEY:-}"
MODEL="${BENCH_MODEL:-qwen36-27b}"
CA_CERT="${BENCH_CA_CERT:-}"
RESOLVE=""
INSECURE=0
TIMEOUT=300

TRIALS=5
MAX_TOKENS=256
PROMPT_FILE=""
STATE_CMD=""
GUARD_PHASE_CMD=""
OUT_FILE=""
# Counts trials whose request failed outright, so the documented exit status
# (0 clean / 1 if any request failed) is actually honoured — a benchmark that
# exits 0 through failed requests silently reports a partial dataset as complete.
FAILED_TRIALS=0

DURATION_SECONDS=600
INTERVAL_SECONDS=2

TOOL_PROMPT="What is the current weather in Portland, Oregon? Use the get_weather tool."

STATS_IN=""
STATS_PHASE=""
STATS_FIELD=""

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
        --endpoint) require_value "$1" "$#"; ENDPOINT="$2"; shift 2 ;;
        --api-key) require_value "$1" "$#"; API_KEY="$2"; shift 2 ;;
        --model) require_value "$1" "$#"; MODEL="$2"; shift 2 ;;
        --resolve) require_value "$1" "$#"; RESOLVE="$2"; shift 2 ;;
        --ca-cert) require_value "$1" "$#"; CA_CERT="$2"; shift 2 ;;
        --insecure) INSECURE=1; shift ;;
        --timeout) require_value "$1" "$#"; TIMEOUT="$2"; shift 2 ;;
        --trials) require_value "$1" "$#"; TRIALS="$2"; shift 2 ;;
        --max-tokens) require_value "$1" "$#"; MAX_TOKENS="$2"; shift 2 ;;
        --prompt-file) require_value "$1" "$#"; PROMPT_FILE="$2"; shift 2 ;;
        --state-cmd) require_value "$1" "$#"; STATE_CMD="$2"; shift 2 ;;
        --guard-phase-cmd) require_value "$1" "$#"; GUARD_PHASE_CMD="$2"; shift 2 ;;
        --out) require_value "$1" "$#"; OUT_FILE="$2"; shift 2 ;;
        --duration-seconds) require_value "$1" "$#"; DURATION_SECONDS="$2"; shift 2 ;;
        --interval-seconds) require_value "$1" "$#"; INTERVAL_SECONDS="$2"; shift 2 ;;
        --prompt) require_value "$1" "$#"; TOOL_PROMPT="$2"; shift 2 ;;
        --in) require_value "$1" "$#"; STATS_IN="$2"; shift 2 ;;
        --phase) require_value "$1" "$#"; STATS_PHASE="$2"; shift 2 ;;
        --field) require_value "$1" "$#"; STATS_FIELD="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

case "$SUBCOMMAND" in
    cold|warm|sustained|tool-call|stats) ;;
    "") echo "FATAL: a subcommand is required" >&2; usage; exit 2 ;;
    *) echo "FATAL: unknown subcommand '$SUBCOMMAND'" >&2; usage; exit 2 ;;
esac

for bin in curl jq; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "FATAL: required tool '$bin' not found on PATH" >&2
        exit 2
    fi
done

iso_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

if [ "$SUBCOMMAND" = "stats" ]; then
    if [ -z "$STATS_IN" ] || [ -z "$STATS_PHASE" ] || [ -z "$STATS_FIELD" ]; then
        echo "FATAL: stats requires --in, --phase and --field" >&2
        exit 2
    fi
    if [ ! -r "$STATS_IN" ]; then
        echo "FATAL: --in '$STATS_IN' is not a readable file" >&2
        exit 2
    fi
    mapfile -t VALUES < <(jq -r --arg phase "$STATS_PHASE" --arg field "$STATS_FIELD" \
        'select(.phase == $phase and (.discarded // false) == false) | .[$field] // empty' \
        "$STATS_IN" | sort -n)
    N=${#VALUES[@]}
    if [ "$N" -eq 0 ]; then
        echo "N=0 min=NA median=NA p95=NA max=NA field=$STATS_FIELD phase=$STATS_PHASE"
        exit 0
    fi
    MIN="${VALUES[0]}"
    MAX="${VALUES[$((N - 1))]}"
    # Median: average of the two middle values for even N, the middle value
    # for odd N (integer arithmetic on the index, values themselves are floats).
    if [ $((N % 2)) -eq 1 ]; then
        MEDIAN="${VALUES[$((N / 2))]}"
    else
        MEDIAN=$(awk -v a="${VALUES[$((N / 2 - 1))]}" -v b="${VALUES[$((N / 2))]}" 'BEGIN { printf "%.4f", (a + b) / 2 }')
    fi
    # p95 via nearest-rank on the sorted sample (ceil(0.95*N), 1-indexed).
    # With N=5 this equals the max; a larger N narrows that (see Threats to
    # validity in the report this feeds).
    P95_IDX=$(awk -v n="$N" 'BEGIN { r = n * 0.95; c = int(r); if (r > c) c++; if (c < 1) c = 1; print c }')
    P95="${VALUES[$((P95_IDX - 1))]}"
    echo "N=$N min=$MIN median=$MEDIAN p95=$P95 max=$MAX field=$STATS_FIELD phase=$STATS_PHASE"
    exit 0
fi

if [ -z "$ENDPOINT" ] || [ -z "$API_KEY" ]; then
    echo "FATAL: --endpoint and --api-key (or BENCH_ENDPOINT/BENCH_API_KEY) are required" >&2
    exit 2
fi

if [ -n "$CA_CERT" ] && [ ! -r "$CA_CERT" ]; then
    echo "FATAL: --ca-cert '$CA_CERT' is not a readable file" >&2
    exit 2
fi

if [ -n "$CA_CERT" ] && [ "$INSECURE" -eq 1 ]; then
    echo "WARNING: --insecure overrides --ca-cert; certificates are not verified at all" >&2
fi

ENDPOINT="${ENDPOINT%/}"

if [ -z "$OUT_FILE" ]; then
    mkdir -p results
    # tool-call writes a single JSON object, not JSONL — give it the matching extension.
    if [ "$SUBCOMMAND" = "tool-call" ]; then
        OUT_FILE="results/${MODEL}-tool-call.json"
    else
        OUT_FILE="results/${MODEL}-${SUBCOMMAND}.jsonl"
    fi
fi
mkdir -p "$(dirname "$OUT_FILE")"

SCRATCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-bench.XXXXXX")
trap 'rm -rf "$SCRATCH_DIR"' EXIT

CURL_OPTS=(--silent --show-error --max-time "$TIMEOUT")
[ -n "$RESOLVE" ] && CURL_OPTS+=(--resolve "$RESOLVE")
[ -n "$CA_CERT" ] && CURL_OPTS+=(--cacert "$CA_CERT")
[ "$INSECURE" -eq 1 ] && CURL_OPTS+=(--insecure)

# Reads guard-phase, prints "n/a" when no --guard-phase-cmd was given —
# distinct from a hook that errors, which is a real signal the trial should
# not have been trusted.
guard_phase() {
    if [ -z "$GUARD_PHASE_CMD" ]; then
        echo "n/a"
        return 0
    fi
    sh -c "$GUARD_PHASE_CMD" 2>/dev/null || echo "unknown"
}

# Prints "unknown" when no --state-cmd was given, which never matches the
# expected phase and so is treated as a discard reason rather than silently
# assumed correct.
observed_state() {
    if [ -z "$STATE_CMD" ]; then
        echo "unknown"
        return 0
    fi
    sh -c "$STATE_CMD" 2>/dev/null || echo "unknown"
}

# Runs one streaming chat-completions request, timed client-side. Writes a
# JSONL line to $OUT_FILE. $1 = phase (cold|warm|sustained), $2 = trial index.
run_one_trial() {
    local phase="$1" trial="$2"
    local prompt_content body_file curl_meta http_code ttft total
    local guard_before guard_after state_before
    local prompt_tokens completion_tokens token_method
    local prompt_tok_s gen_tok_s discarded discard_reason
    local server_prompt_tok_s server_gen_tok_s draft_n draft_n_accepted

    if [ -n "$PROMPT_FILE" ]; then
        prompt_content=$(cat "$PROMPT_FILE")
    else
        prompt_content="Explain, in a few sentences, what a cold start means for an LLM inference server."
    fi

    state_before=$(observed_state)
    guard_before=$(guard_phase)

    body_file="$SCRATCH_DIR/trial-${phase}-${trial}.sse"
    curl_meta=$(curl "${CURL_OPTS[@]}" -o "$body_file" \
        --write-out '%{http_code} %{time_starttransfer} %{time_total}' \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$MODEL" --arg content "$prompt_content" --argjson max_tokens "$MAX_TOKENS" \
            '{model: $model, messages: [{role: "user", content: $content}], max_tokens: $max_tokens, temperature: 0, stream: true, stream_options: {include_usage: true}}')" \
        2>"$SCRATCH_DIR/trial-${phase}-${trial}.err")
    CURL_STATUS=$?

    guard_after=$(guard_phase)

    if [ "$CURL_STATUS" -ne 0 ]; then
        jq -n --arg ts "$(iso_now)" --arg phase "$phase" --argjson trial "$trial" \
            --arg err "request failed (curl exit $CURL_STATUS): $(cat "$SCRATCH_DIR/trial-${phase}-${trial}.err" 2>/dev/null)" \
            '{ts: $ts, phase: $phase, trial: $trial, discarded: true, discard_reason: $err}' >> "$OUT_FILE"
        echo "TRIAL $phase#$trial: FAILED (curl exit $CURL_STATUS)" >&2
        return 1
    fi

    http_code="${curl_meta%% *}"
    ttft=$(echo "$curl_meta" | awk '{print $2}')
    total=$(echo "$curl_meta" | awk '{print $3}')

    discarded=false
    discard_reason=""

    if [ "$http_code" != "200" ]; then
        discarded=true
        discard_reason="HTTP $http_code"
    fi

    if [ "$phase" = "cold" ] || [ "$phase" = "warm" ]; then
        if [ "$state_before" != "unknown" ] && [ "$state_before" != "$phase" ]; then
            discarded=true
            discard_reason="${discard_reason:+$discard_reason; }state-cmd reported '$state_before' before a $phase trial"
        elif [ "$state_before" = "unknown" ] && [ -n "$STATE_CMD" ]; then
            discarded=true
            discard_reason="${discard_reason:+$discard_reason; }state-cmd did not report a usable state"
        fi
    fi

    if { [ "$guard_before" != "serving" ] && [ "$guard_before" != "n/a" ]; } || \
       { [ "$guard_after" != "serving" ] && [ "$guard_after" != "n/a" ]; }; then
        discarded=true
        discard_reason="${discard_reason:+$discard_reason; }guard phase not 'serving' throughout (before=$guard_before after=$guard_after)"
    fi

    # Prefer the exact OpenAI-style usage frame (stream_options.include_usage);
    # fall back to counting non-empty content chunks, which is a one-chunk-
    # per-token approximation for llama.cpp's unbatched streaming and is
    # flagged as such via token_method. The same frame, when present, also
    # carries llama.cpp's own server-reported "timings" (prompt_per_second /
    # predicted_per_second) and MTP draft-acceptance counters (draft_n /
    # draft_n_accepted) — captured alongside the client-measured figures
    # below, not in place of them (AC2: state which is which, never mix).
    local final_frame
    final_frame=$(grep '^data: ' "$body_file" | sed 's/^data: //' | grep -v '^\[DONE\]$' | \
        jq -s -e '[.[] | select(.usage != null)] | last' 2>/dev/null)
    prompt_tokens=$(echo "$final_frame" | jq -e '.usage.prompt_tokens' 2>/dev/null)
    completion_tokens=$(echo "$final_frame" | jq -e '.usage.completion_tokens' 2>/dev/null)
    server_prompt_tok_s=$(echo "$final_frame" | jq -e '.timings.prompt_per_second // null' 2>/dev/null)
    server_gen_tok_s=$(echo "$final_frame" | jq -e '.timings.predicted_per_second // null' 2>/dev/null)
    draft_n=$(echo "$final_frame" | jq -e '.timings.draft_n // null' 2>/dev/null)
    draft_n_accepted=$(echo "$final_frame" | jq -e '.timings.draft_n_accepted // null' 2>/dev/null)
    [ -z "$server_prompt_tok_s" ] && server_prompt_tok_s="null"
    [ -z "$server_gen_tok_s" ] && server_gen_tok_s="null"
    [ -z "$draft_n" ] && draft_n="null"
    [ -z "$draft_n_accepted" ] && draft_n_accepted="null"

    if [ -n "$prompt_tokens" ] && [ "$prompt_tokens" != "null" ] && [ -n "$completion_tokens" ] && [ "$completion_tokens" != "null" ]; then
        token_method="usage"
    else
        prompt_tokens="null"
        completion_tokens=$(grep '^data: ' "$body_file" | sed 's/^data: //' | grep -v '^\[DONE\]$' | \
            jq -s '[.[] | select((.choices[0].delta.content // "") != "")] | length')
        token_method="chunk_count_approx"
        discarded=true
        discard_reason="${discard_reason:+$discard_reason; }no usage frame in stream; prompt_tokens unavailable (token_method=chunk_count_approx)"
    fi

    prompt_tok_s="null"
    gen_tok_s="null"
    if [ "$token_method" = "usage" ]; then
        prompt_tok_s=$(awk -v p="$prompt_tokens" -v t="$ttft" 'BEGIN { if (t > 0) printf "%.2f", p / t; else print "null" }')
        gen_tok_s=$(awk -v c="$completion_tokens" -v ttft="$ttft" -v tot="$total" \
            'BEGIN { d = tot - ttft; if (d > 0 && c > 1) printf "%.2f", (c - 1) / d; else print "null" }')
    fi

    jq -n \
        --arg ts "$(iso_now)" --arg phase "$phase" --argjson trial "$trial" \
        --argjson http_code "$http_code" --argjson ttft_s "$ttft" --argjson total_s "$total" \
        --argjson prompt_tokens "$prompt_tokens" --argjson completion_tokens "$completion_tokens" \
        --arg token_method "$token_method" \
        --arg prompt_tok_s "$prompt_tok_s" --arg gen_tok_s "$gen_tok_s" \
        --arg server_prompt_tok_s "$server_prompt_tok_s" --arg server_gen_tok_s "$server_gen_tok_s" \
        --arg draft_n "$draft_n" --arg draft_n_accepted "$draft_n_accepted" \
        --arg guard_before "$guard_before" --arg guard_after "$guard_after" \
        --arg state_before "$state_before" \
        --argjson discarded "$discarded" --arg discard_reason "$discard_reason" \
        '{ts: $ts, phase: $phase, trial: $trial, http_code: $http_code, ttft_s: $ttft_s, total_s: $total_s,
          prompt_tokens: $prompt_tokens, completion_tokens: $completion_tokens, token_method: $token_method,
          prompt_tok_s_client: ($prompt_tok_s | if . == "null" then null else tonumber end),
          gen_tok_s_client: ($gen_tok_s | if . == "null" then null else tonumber end),
          prompt_tok_s_server: ($server_prompt_tok_s | if . == "null" then null else tonumber end),
          gen_tok_s_server: ($server_gen_tok_s | if . == "null" then null else tonumber end),
          draft_n: ($draft_n | if . == "null" then null else tonumber end),
          draft_n_accepted: ($draft_n_accepted | if . == "null" then null else tonumber end),
          guard_phase_before: $guard_before, guard_phase_after: $guard_after, state_before: $state_before,
          discarded: $discarded, discard_reason: $discard_reason}' >> "$OUT_FILE"

    if [ "$discarded" = "true" ]; then
        echo "TRIAL $phase#$trial: DISCARDED ($discard_reason)" >&2
    else
        echo "TRIAL $phase#$trial: ok ttft=${ttft}s total=${total}s prompt_tok/s=${prompt_tok_s} gen_tok/s=${gen_tok_s}" >&2
    fi
    return 0
}

case "$SUBCOMMAND" in
    cold)
        for i in $(seq 1 "$TRIALS"); do
            run_one_trial cold "$i" || FAILED_TRIALS=$((FAILED_TRIALS + 1))
        done
        echo "cold trials complete, appended to $OUT_FILE" >&2
        ;;
    warm)
        echo "warm-up request (untimed)..." >&2
        run_one_trial warmup 0 >/dev/null 2>&1 || true
        for i in $(seq 1 "$TRIALS"); do
            run_one_trial warm "$i" || FAILED_TRIALS=$((FAILED_TRIALS + 1))
        done
        echo "warm trials complete, appended to $OUT_FILE" >&2
        ;;
    sustained)
        START_EPOCH=$(date -u +%s)
        i=0
        MAX_TOKENS_SAVED="$MAX_TOKENS"
        while :; do
            NOW_EPOCH=$(date -u +%s)
            ELAPSED=$((NOW_EPOCH - START_EPOCH))
            if [ "$ELAPSED" -ge "$DURATION_SECONDS" ]; then
                break
            fi
            i=$((i + 1))
            MAX_TOKENS="$MAX_TOKENS_SAVED"
            run_one_trial sustained "$i" || FAILED_TRIALS=$((FAILED_TRIALS + 1))
            sleep "$INTERVAL_SECONDS"
        done
        echo "sustained run complete: $i requests over ${DURATION_SECONDS}s+, appended to $OUT_FILE" >&2
        ;;
    tool-call)
        TOOLS_JSON='[{"type":"function","function":{"name":"get_weather","description":"Get the current weather for a location","parameters":{"type":"object","properties":{"location":{"type":"string","description":"City and state, e.g. Portland, Oregon"}},"required":["location"]}}}]'
        BODY_FILE="$SCRATCH_DIR/tool-call.json"
        CURL_META=$(curl "${CURL_OPTS[@]}" -o "$BODY_FILE" --write-out '%{http_code}' \
            -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
            -X POST "$ENDPOINT/v1/chat/completions" \
            -d "$(jq -n --arg model "$MODEL" --arg prompt "$TOOL_PROMPT" --argjson tools "$TOOLS_JSON" \
                '{model: $model, messages: [{role: "user", content: $prompt}], tools: $tools, max_tokens: 512, temperature: 0}')" \
            2>"$SCRATCH_DIR/tool-call.err")
        HTTP_CODE="$CURL_META"
        VERDICT="FAIL"
        DETAIL=""
        if [ "$HTTP_CODE" != "200" ]; then
            DETAIL="HTTP $HTTP_CODE: $(cat "$SCRATCH_DIR/tool-call.err" 2>/dev/null)"
        elif jq -e '.choices[0].message.tool_calls[0].function.name == "get_weather"' "$BODY_FILE" >/dev/null 2>&1 && \
             jq -e '.choices[0].message.tool_calls[0].function.arguments | fromjson | .location | type == "string"' "$BODY_FILE" >/dev/null 2>&1; then
            VERDICT="PASS"
            DETAIL="well-formed get_weather call with a string location argument"
        else
            DETAIL="no well-formed tool_calls[0] naming get_weather with a valid JSON 'location' argument"
        fi
        jq -n --arg ts "$(iso_now)" --arg verdict "$VERDICT" --arg detail "$DETAIL" \
            --argjson http_code "$HTTP_CODE" --slurpfile response "$BODY_FILE" \
            '{ts: $ts, verdict: $verdict, detail: $detail, http_code: $http_code, response: $response[0]}' > "$OUT_FILE"
        echo "TOOL-CALL: $VERDICT ($DETAIL) — written to $OUT_FILE" >&2
        [ "$VERDICT" = "PASS" ]
        ;;
esac

if [ "$FAILED_TRIALS" -gt 0 ]; then
    echo "$FAILED_TRIALS trial(s) failed outright — dataset is incomplete" >&2
    exit 1
fi
