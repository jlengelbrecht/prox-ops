#!/usr/bin/env bash
#
# Edge worker conformance checker (EPIC-035 Story 35.3, AC5).
#
# Runs the checklist from edge/EDGE-WORKER-CONTRACT.md against any
# OpenAI-compatible chat-completions endpoint and prints PASS/FAIL per
# capability. Exits non-zero if any capability fails.
#
# Usage:
#   conformance.sh --endpoint URL --api-key KEY [options]
#
# Required:
#   --endpoint URL        Base URL, e.g. https://ai-gateway.homelab0.org
#   --api-key KEY         Bearer credential (sent as "Authorization: Bearer KEY").
#                         On a shared host prefer the EDGE_API_KEY environment
#                         variable: a credential passed as a CLI argument is
#                         visible to other local users via ps / /proc/<pid>/cmdline.
#
# Optional:
#   --model NAME           Model id used for the primary requests (default: qwen36-27b)
#   --alias-model NAME     A second model id expected to route identically to
#                          --model (e.g. a gateway alias). Both must report the
#                          same resolved model id. Skipped if unset.
#   --resolve HOST:PORT:IP Passed straight through to curl --resolve, for
#                          endpoints with no DNS record yet.
#   --ca-cert PATH         Verify the endpoint against this CA bundle instead of
#                          the machine's trust store (curl --cacert), for an edge
#                          host issued by a dedicated edge CA. This is the
#                          conforming way to test such a host: verification stays
#                          on, and the system trust store is left alone.
#   --insecure              Disable TLS certificate validation. Downgrades the
#                          TLS-validation capability to SKIP with a warning —
#                          this shape does not conform to EDGE-WORKER-CONTRACT
#                          rule R4 and must never be used against production.
#                          It also overrides --ca-cert: curl verifies nothing,
#                          so the two together are a debug shape, never a test
#                          of the CA.
#   --timeout SECONDS       Per-request timeout (default: 180, cold starts run ~150s)
#
# Environment variables EDGE_ENDPOINT, EDGE_API_KEY, EDGE_MODEL, EDGE_ALIAS_MODEL
# and EDGE_CA_CERT are read as defaults for the flags above.
#
# Exit status: 0 if every capability PASSed (SKIP does not fail the run),
# 1 if any capability FAILed, 2 on usage/environment error.

set -uo pipefail

ENDPOINT="${EDGE_ENDPOINT:-}"
API_KEY="${EDGE_API_KEY:-}"
MODEL="${EDGE_MODEL:-qwen36-27b}"
ALIAS_MODEL="${EDGE_ALIAS_MODEL:-}"
CA_CERT="${EDGE_CA_CERT:-}"
RESOLVE=""
INSECURE=0
TIMEOUT=180

# Model id reported in the primary (non-streaming) completion, used by the
# alias check to prove both ids land on the same served model.
PRIMARY_RESOLVED_MODEL=""

usage() {
    # Print the contiguous comment block that follows the shebang, stopping at
    # the first non-comment line. Deriving the range instead of hardcoding it
    # keeps --help complete when this header grows.
    awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

# Guard for flags that take a value: $1 is the flag, $2 is the remaining
# argument count including the flag itself.
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
        --alias-model) require_value "$1" "$#"; ALIAS_MODEL="$2"; shift 2 ;;
        --resolve) require_value "$1" "$#"; RESOLVE="$2"; shift 2 ;;
        --ca-cert) require_value "$1" "$#"; CA_CERT="$2"; shift 2 ;;
        --insecure) INSECURE=1; shift ;;
        --timeout) require_value "$1" "$#"; TIMEOUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if [ -z "$ENDPOINT" ] || [ -z "$API_KEY" ]; then
    echo "FATAL: --endpoint and --api-key (or EDGE_ENDPOINT/EDGE_API_KEY) are required" >&2
    exit 2
fi

if [ -n "$CA_CERT" ] && [ ! -r "$CA_CERT" ]; then
    echo "FATAL: --ca-cert '$CA_CERT' is not a readable file" >&2
    exit 2
fi

if [ -n "$CA_CERT" ] && [ "$INSECURE" -eq 1 ]; then
    echo "WARNING: --insecure overrides --ca-cert; certificates are not verified at all" >&2
fi

for bin in curl jq; do
    if ! command -v "$bin" >/dev/null 2>&1; then
        echo "FATAL: required tool '$bin' not found on PATH" >&2
        exit 2
    fi
done

ENDPOINT="${ENDPOINT%/}"

SCRATCH_DIR=$(mktemp -d "${TMPDIR:-/tmp}/edge-conformance.XXXXXX")
trap 'rm -rf "$SCRATCH_DIR"' EXIT

CURL_OPTS=(--silent --show-error --max-time "$TIMEOUT")
[ -n "$RESOLVE" ] && CURL_OPTS+=(--resolve "$RESOLVE")
[ -n "$CA_CERT" ] && CURL_OPTS+=(--cacert "$CA_CERT")
[ "$INSECURE" -eq 1 ] && CURL_OPTS+=(--insecure)

PASS_COUNT=0
FAIL_COUNT=0
SKIP_COUNT=0

report() {
    local status="$1" name="$2" detail="${3:-}"
    case "$status" in
        PASS) PASS_COUNT=$((PASS_COUNT + 1)) ;;
        FAIL) FAIL_COUNT=$((FAIL_COUNT + 1)) ;;
        SKIP) SKIP_COUNT=$((SKIP_COUNT + 1)) ;;
    esac
    if [ -n "$detail" ]; then
        printf '%-4s %-28s %s\n' "$status" "$name" "$detail"
    else
        printf '%-4s %-28s\n' "$status" "$name"
    fi
}

# --- capability: /v1/models shape ------------------------------------------
check_models_shape() {
    local body status
    body=$(curl "${CURL_OPTS[@]}" --write-out '\n%{http_code}' \
        -H "Authorization: Bearer $API_KEY" \
        "$ENDPOINT/v1/models" 2>"$SCRATCH_DIR/models.err") || {
        report FAIL "models_shape" "request failed: $(cat "$SCRATCH_DIR/models.err" 2>/dev/null)"
        return
    }
    status="${body##*$'\n'}"
    body="${body%$'\n'*}"
    if [ "$status" != "200" ]; then
        report FAIL "models_shape" "HTTP $status"
        return
    fi
    if ! echo "$body" | jq -e '.data | type == "array" and length > 0 and all(.[]; has("id"))' >/dev/null 2>&1; then
        report FAIL "models_shape" "response is not a {data:[{id:...}]} list"
        return
    fi
    report PASS "models_shape"
}

# --- capability: /v1/chat/completions non-streaming -------------------------
check_chat_completions() {
    local resp status
    resp=$(curl "${CURL_OPTS[@]}" --write-out '\n%{http_code}' \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$MODEL" '{model: $model, messages: [{role: "user", content: "Reply with the single word: pong"}], max_tokens: 512}')" \
        2>"$SCRATCH_DIR/chat.err") || {
        report FAIL "chat_completions" "request failed: $(cat "$SCRATCH_DIR/chat.err" 2>/dev/null)"
        return
    }
    status="${resp##*$'\n'}"
    resp="${resp%$'\n'*}"
    if [ "$status" != "200" ]; then
        report FAIL "chat_completions" "HTTP $status"
        return
    fi
    if ! echo "$resp" | jq -e '.choices[0].message.content | type == "string" and length > 0' >/dev/null 2>&1; then
        report FAIL "chat_completions" "no choices[0].message.content in response"
        return
    fi
    PRIMARY_RESOLVED_MODEL=$(echo "$resp" | jq -r '.model // empty' 2>/dev/null)
    report PASS "chat_completions"
}

# --- capability: SSE streaming with correct framing --------------------------
check_sse_streaming() {
    local raw has_data has_done has_content bad_json payload line
    # max_tokens is generous because a reasoning model can spend a lot of the
    # budget in reasoning_content before it emits any assistant content, and
    # this check requires real content below.
    raw=$(curl "${CURL_OPTS[@]}" \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$MODEL" '{model: $model, messages: [{role: "user", content: "Count from 1 to 3."}], max_tokens: 1024, stream: true}')" \
        2>"$SCRATCH_DIR/sse.err") || {
        report FAIL "sse_streaming" "request failed: $(cat "$SCRATCH_DIR/sse.err" 2>/dev/null)"
        return
    }
    # SSE lines may end CRLF, which is legal and which end-of-line-anchored
    # matches would otherwise reject. Drop the trailing CR once so every match
    # below is CR-tolerant. A bare CR inside a frame would be invalid JSON, so
    # nothing legitimate is lost.
    raw=$(printf '%s' "$raw" | sed $'s/\r$//')
    has_data=$(printf '%s' "$raw" | grep -c '^data: ' || true)
    has_done=$(printf '%s' "$raw" | grep -c '^data: \[DONE\]$' || true)
    if [ "$has_data" -eq 0 ]; then
        report FAIL "sse_streaming" "no 'data: ' frames in response"
        return
    fi
    if [ "$has_done" -eq 0 ]; then
        report FAIL "sse_streaming" "missing terminal 'data: [DONE]' frame"
        return
    fi
    bad_json=0
    has_content=0
    while IFS= read -r line; do
        payload="${line#data: }"
        [ "$payload" = "[DONE]" ] && continue
        # A trailing usage-only frame with an empty "choices" array is valid
        # (observed against llama.cpp's OpenAI-compatible streaming) — only
        # require that "choices" itself is present and is an array.
        if ! printf '%s' "$payload" | jq -e '.choices | type == "array"' >/dev/null 2>&1; then
            bad_json=1
        fi
        # Correct framing around an empty stream is not a working stream: at
        # least one frame has to carry generated assistant content.
        if printf '%s' "$payload" | jq -e '[.choices[]? | .delta.content? | select(type == "string" and length > 0)] | length > 0' >/dev/null 2>&1; then
            has_content=1
        fi
    done < <(printf '%s' "$raw" | grep '^data: ')
    if [ "$bad_json" -eq 1 ]; then
        report FAIL "sse_streaming" "a data frame did not parse as {choices: [...]}"
        return
    fi
    if [ "$has_content" -eq 0 ]; then
        report FAIL "sse_streaming" "framing is correct but no frame carried choices[].delta.content"
        return
    fi
    report PASS "sse_streaming"
}

# --- capability: tool-calling round trip -------------------------------------
check_tool_calling() {
    local resp status name
    resp=$(curl "${CURL_OPTS[@]}" --write-out '\n%{http_code}' \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$MODEL" '{
            model: $model,
            messages: [{role: "user", content: "What is the weather in Paris?"}],
            tools: [{type: "function", function: {
                name: "get_weather",
                description: "Get the current weather for a city",
                parameters: {type: "object", properties: {city: {type: "string"}}, required: ["city"]}
            }}],
            tool_choice: {type: "function", function: {name: "get_weather"}},
            max_tokens: 512
        }')" 2>"$SCRATCH_DIR/tools.err") || {
        report FAIL "tool_calling" "request failed: $(cat "$SCRATCH_DIR/tools.err" 2>/dev/null)"
        return
    }
    status="${resp##*$'\n'}"
    resp="${resp%$'\n'*}"
    if [ "$status" != "200" ]; then
        report FAIL "tool_calling" "HTTP $status"
        return
    fi
    name=$(echo "$resp" | jq -r '.choices[0].message.tool_calls[0].function.name // empty' 2>/dev/null)
    if [ "$name" != "get_weather" ]; then
        report FAIL "tool_calling" "no matching tool_calls[0].function.name in response"
        return
    fi
    report PASS "tool_calling"
}

# --- capability: model-alias semantics identical to the KServe path ---------
check_model_alias() {
    if [ -z "$ALIAS_MODEL" ]; then
        report SKIP "model_alias" "no --alias-model given"
        return
    fi
    local resp status content resolved
    resp=$(curl "${CURL_OPTS[@]}" --write-out '\n%{http_code}' \
        -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$ALIAS_MODEL" '{model: $model, messages: [{role: "user", content: "Reply with the single word: pong"}], max_tokens: 512}')" \
        2>"$SCRATCH_DIR/alias.err") || {
        report FAIL "model_alias" "request failed: $(cat "$SCRATCH_DIR/alias.err" 2>/dev/null)"
        return
    }
    status="${resp##*$'\n'}"
    resp="${resp%$'\n'*}"
    if [ "$status" != "200" ]; then
        report FAIL "model_alias" "alias '$ALIAS_MODEL' HTTP $status (must route like '$MODEL')"
        return
    fi
    content=$(echo "$resp" | jq -e '.choices[0].message.content | type == "string" and length > 0' 2>/dev/null)
    if [ "$content" != "true" ]; then
        report FAIL "model_alias" "alias '$ALIAS_MODEL' did not return a normal chat completion"
        return
    fi
    # HTTP 2xx only proves the alias is accepted somewhere. The contract claims
    # the alias behaves identically to --model, so compare the model id each
    # request actually resolved to (a required field of the completion object).
    resolved=$(echo "$resp" | jq -r '.model // empty' 2>/dev/null)
    if [ -z "$PRIMARY_RESOLVED_MODEL" ]; then
        report FAIL "model_alias" "cannot compare routing: '$MODEL' reported no 'model' id"
        return
    fi
    if [ -z "$resolved" ]; then
        report FAIL "model_alias" "alias '$ALIAS_MODEL' response carries no 'model' id to compare"
        return
    fi
    # Servers that echo the request body's model id back (llama.cpp's
    # llama-server does this) carry no routing information in this field, so
    # neither PASS nor FAIL would be honest — report it as not observable.
    if [ "$resolved" = "$ALIAS_MODEL" ] && [ "$PRIMARY_RESOLVED_MODEL" = "$MODEL" ]; then
        report SKIP "model_alias" "endpoint echoes the requested model id; alias routing is not observable from the response"
        return
    fi
    if [ "$resolved" != "$PRIMARY_RESOLVED_MODEL" ]; then
        report FAIL "model_alias" "alias resolved to '$resolved', '$MODEL' resolved to '$PRIMARY_RESOLVED_MODEL'"
        return
    fi
    report PASS "model_alias" "'$ALIAS_MODEL' and '$MODEL' both resolve to '$resolved'"
}

# --- capability: auth actually enforced --------------------------------------
check_auth_enforced() {
    local list_status chat_status
    list_status=$(curl "${CURL_OPTS[@]}" --output /dev/null --write-out '%{http_code}' \
        "$ENDPOINT/v1/models" 2>"$SCRATCH_DIR/auth.err") || {
        report FAIL "auth_enforced" "request failed: $(cat "$SCRATCH_DIR/auth.err" 2>/dev/null)"
        return
    }
    # The listing path and the inference path can carry different policies, and
    # section 2 is about the inference path, so probe both. max_tokens is 1 so a
    # wrongly-open endpoint costs a token rather than a generation.
    chat_status=$(curl "${CURL_OPTS[@]}" --output /dev/null --write-out '%{http_code}' \
        -H "Content-Type: application/json" \
        -X POST "$ENDPOINT/v1/chat/completions" \
        -d "$(jq -n --arg model "$MODEL" '{model: $model, messages: [{role: "user", content: "ping"}], max_tokens: 1}')" \
        2>"$SCRATCH_DIR/auth-chat.err") || {
        report FAIL "auth_enforced" "no-credential inference request failed: $(cat "$SCRATCH_DIR/auth-chat.err" 2>/dev/null)"
        return
    }
    case "$list_status" in
        401|403) ;;
        *) report FAIL "auth_enforced" "GET /v1/models without a credential returned HTTP $list_status"; return ;;
    esac
    case "$chat_status" in
        401|403) ;;
        *) report FAIL "auth_enforced" "POST /v1/chat/completions without a credential returned HTTP $chat_status"; return ;;
    esac
    report PASS "auth_enforced" "rejected without a credential (models HTTP $list_status, chat HTTP $chat_status)"
}

# --- capability: TLS validation, when applicable -----------------------------
check_tls_validation() {
    if [ "${ENDPOINT%%://*}" != "https" ]; then
        report SKIP "tls_validation" "endpoint is not https"
        return
    fi
    if [ "$INSECURE" -eq 1 ]; then
        report SKIP "tls_validation" "--insecure given${CA_CERT:+ (overriding --ca-cert)}; this shape violates contract rule R4 and must never ship"
        return
    fi
    # Deliberately rebuilt without --insecure. --ca-cert is kept: verifying
    # against a dedicated edge CA is conforming, trusting nothing is not.
    local strict_opts=(--silent --show-error --max-time "$TIMEOUT" --output /dev/null --write-out '%{http_code}')
    [ -n "$RESOLVE" ] && strict_opts+=(--resolve "$RESOLVE")
    [ -n "$CA_CERT" ] && strict_opts+=(--cacert "$CA_CERT")
    local status
    status=$(curl "${strict_opts[@]}" -H "Authorization: Bearer $API_KEY" "$ENDPOINT/v1/models" 2>"$SCRATCH_DIR/tls.err") || {
        report FAIL "tls_validation" "TLS handshake/verification failed: $(cat "$SCRATCH_DIR/tls.err" 2>/dev/null)"
        return
    }
    if [ "$status" != "200" ]; then
        report FAIL "tls_validation" "HTTP $status with certificate validation on"
        return
    fi
    report PASS "tls_validation" "certificate chain and hostname SAN verified against ${CA_CERT:-the system trust store} (no --insecure)"
}

echo "Edge worker conformance: $ENDPOINT (model=$MODEL${ALIAS_MODEL:+, alias=$ALIAS_MODEL})"
echo "----------------------------------------------------------------------"

check_models_shape
check_chat_completions
check_sse_streaming
check_tool_calling
check_model_alias
check_auth_enforced
check_tls_validation

echo "----------------------------------------------------------------------"
echo "PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 1
fi
exit 0
