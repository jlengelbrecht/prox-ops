# STORY-036-1 — Evidence: gateway-backed local launch path for `local-code-standard`

**Type**: Validation (evidence-producing). No cluster change was made.
**Executed**: 2026-08-30, 04:21Z – 04:32Z (single window)
**Epic**: EPIC-036 · **Blocks**: STORY-036-2
**Catalog observed**: document version `1.5.0`, `catalog_version sha256:59781b92…eb36`
**Router build observed**: `sha-1595015e…`, `capacity_state: steady`

This record states *that* a requirement passed and *what was observed*. It does not
reproduce the wire capture. Raw captures (response headers, upstream request ids, full
bodies) are retained locally and uncited, per the AC-9 split.

The gateway key is referenced only as **1Password item `agw-claude-code`**. No credential
value appears in this file.

---

## Configuration under test

Four variables, set **per-process only** on the `claude` subprocess under test. Nothing
global was written: no `ANTHROPIC_API_KEY` was ever set, `CLAUDE_CONFIG_DIR` was never set,
`~/.claude/settings.json` was not modified (mtime unchanged across the window), and no
variable was exported into the operator's own shell.

```text
ANTHROPIC_BASE_URL       = https://ai-gateway.homelab0.org
ANTHROPIC_AUTH_TOKEN     = <1Password item agw-claude-code>
ANTHROPIC_MODEL          = qwen36-27b
ANTHROPIC_CUSTOM_HEADERS = "x-placement: <value copied verbatim from a live /v1/place result>"
```

Harness under test: **Claude Code 2.1.251**.

## Live preconditions observed at the start of the window

`GET /v1/status` on `https://agent-router.homelab0.org` at `2026-08-30T04:21:55Z`:

| Placement | catalog `status` / `selectable` | live `state` | `state_source` | `eligible` |
|---|---|---|---|---|
| `kserve-a5000` | available / true | AVAILABLE | static | **true** |
| `cachyos-7900xtx` | available / true | OFFLINE | silence | false |
| `bazzite-5090` | available / true | OFFLINE | silence | false |
| `laptop-rtx5000` | planned / false | OFFLINE | static | false |

Heartbeat policy: `interval_seconds: 30`, `offline_after_seconds: 90`. Both edge placements
had heartbeated at some point (`cachyos-7900xtx` last at `2026-08-28T07:11:02Z`;
`bazzite-5090` last at `2026-08-29T19:13:47Z`, its retained heartbeat body reporting
`state: AVAILABLE` with `qwen36-27b` cached), but both were past the 90 s offline window at
validation time and the router therefore held them ineligible.

**Consequence for AC-4 coverage**: exactly one placement was eligible in the window. This
meets the coordinator's stated minimum bar of one, and is recorded as one — not three.

---

## Numbered requirements

### R1 — (AC-0, observation) Unpinned Claude Code sends a body `model` outside the alias list

**Observed.** With `ANTHROPIC_BASE_URL` pointed at a local capture server and **no**
`ANTHROPIC_MODEL` set, Claude Code 2.1.251 sent `"model": "claude-opus-5"` in the
`/v1/messages` request body (two requests, both identical on this field). The gateway's
authorization CEL accepts `json(request.body).model in ['qwen36-27b', 'claude-sonnet-5']`.
`claude-opus-5` is **not** in that list.

The capture also confirmed: request path `/v1/messages?beta=true`, `anthropic-version:
2023-06-01`, user-agent `claude-cli/2.1.251 (external, sdk-cli)`, and that
`ANTHROPIC_AUTH_TOKEN` is conveyed as an `Authorization` header (bearer scheme) — the
`x-api-key` header was not used.

### R2 — (AC-0, observation) The unpinned path 403s end-to-end against the live gateway

**Observed.** A real `claude -p` session against `https://ai-gateway.homelab0.org` with the
key from `agw-claude-code` and **no** `ANTHROPIC_MODEL` returned
`is_error: true`, `"Failed to authenticate. API Error: 403 authorization failed"`. The
gateway access log for that request shows `route_rule: qwen36-27b-messages`,
`http.status: 403`, `error: "authorization failed"`, `reason: Authorization` — i.e. it was
refused by the body-consistency clause, exactly as R1 predicts.

**Recorded as a known limitation of the unpinned path, not a gate.** The launch contract
under validation pins `ANTHROPIC_MODEL=qwen36-27b`, which is unaffected. Whoever next
touches `local-model-policies.yaml` should note the maintenance comment already in that file
is now stale: Claude Code's zero-config default string has moved from `claude-sonnet-5` to
`claude-opus-5`.

### R3 — (AC-1) A real Claude Code completion was served by `qwen36-27b` through `/v1/messages` — **PASS**

Real `claude -p` session, four-variable configuration, `--output-format json`:

| Field | Value |
|---|---|
| `is_error` | `false` |
| `subtype` | `success` |
| `num_turns` | 1 |
| `duration_ms` | 39803 |
| `result` | `391` (the correct answer to the prompt "17 × 23") |
| `usage.input_tokens` | 30308 |
| `usage.output_tokens` | 39 |
| `usage.cache_read_input_tokens` | 0 |
| `modelUsage` key | `qwen36-27b` |

Corroborated gateway-side: `route_rule: qwen36-27b-messages-kserve`, `http.status: 200`,
`protocol: llm`, `gen_ai.provider.name: openai`, `gen_ai.request.model: qwen3.6-27b`,
`duration: 39572ms`.

### R4 — (AC-2) Served-model identity confirmed from live response data — **PASS**

Not taken from configuration. Three independent live signals:

1. **Session response bodies.** Streamed `assistant` events from a real session carry
   `message.model = "qwen3.6-27b"`. This is the **upstream** model string, which differs
   from the pinned request string `qwen36-27b` — so it cannot be an echo of what the client
   sent. It matches the `openai.model: qwen3.6-27b` declared on every provider in
   `local-model-backends.yaml`.
2. **Raw gateway responses.** Direct `/v1/messages` responses returned
   `"model": "qwen3.6-27b"` with an OpenAI-style completion id (`chatcmpl-…`), confirming an
   OpenAI-compatible llama.cpp upstream rather than a vendor Anthropic endpoint. The id
   values themselves are held in the raw capture file only.
3. **Gateway telemetry.** Access logs record `gen_ai.request.model: qwen3.6-27b` and
   `gen_ai.response.model: qwen3.6-27b` per request.

Cluster-side identity of the serving backend: the KServe predictor pod for `qwen36-27b` was
scheduled on the cluster's RTX A5000 GPU node (node labels
`gpu.nvidia.com/model: rtx-a5000`, `gpu.nvidia.com/class: professional`), which is the
`kserve-a5000` placement.

No gateway-emitted upstream request-id response header was present. Response headers carried
`x-envoy-upstream-service-time` and `server: envoy` only. Recorded as an observation: the
request-id half of AC-2 has no such header to capture on this path; identity is established
by the three signals above instead.

### R5 — (AC-3) Full `tool_use` → `tool_result` → synthesis round trip — **PASS**

Real `claude -p` session, `--allowedTools Read`, `--output-format stream-json`:

- **`tool_use`** emitted by the model: `{"name":"Read","input":{"file_path":"…/marker.txt"}}`
- **`tool_result`** returned to the model: `is_error: null`, content
  `"1\tTOOLPROOF-QWEN-7731\n2\t"`
- **Synthesis**: final assistant text reproduced the token exactly —
  ``The exact token in `marker.txt` is: TOOLPROOF-QWEN-7731``
- Result envelope: `is_error: false`, `subtype: success`, `num_turns: 2`,
  `duration_ms: 45941`

Two `/v1/messages` requests appear in the gateway log for this session (`43177ms` then
`2403ms`), which is the round trip: first call producing the tool call, second call
consuming the tool result. This licenses the `tools` capability claim **on the gateway
launch path specifically**.

### R6 — (AC-4) The placement name was obtained from a live `POST /v1/place` and used verbatim — **PASS**

`POST /v1/place` with `{"model_profile":"local-code-standard","placement_policy":"prefer-warm-local"}`
was called freshly before each attempt (four times across the window; `ttl_seconds: 30`, so
no result was reused across a long session). Every result:

```text
status:      placed
model:       qwen36-27b
placement:   kserve-a5000
readiness:   unknown
estimated_cold_start_s: 150
headers:     { "x-placement": "kserve-a5000" }
ttl_seconds: 30
reason.code: placed_only_candidate
```

The header value was extracted programmatically from the result's `headers` object
(`jq -r '.headers["x-placement"]'`) and passed straight into the request — never hand-typed.

### R7 — (AC-4) `x-placement: kserve-a5000` pinning is honored and lands on the intended backend — **PASS**

Confirmed at the routing layer, which is the only place the claim can actually be settled:
gateway access logs show these requests selected `route_rule: qwen36-27b-messages-kserve` —
the placement-scoped route section, distinct from the headerless default section
`qwen36-27b-messages`. That section's `backendRefs` names the `qwen36-27b` backend whose
group-0 provider is `kserve-a5000`, and the logged upstream endpoint was the in-cluster
KServe predictor Service for `qwen36-27b`.

First such request was a cold start: `HTTP 200` in **128.9 s** total,
`x-envoy-upstream-service-time: 128796`, against the route's `timeouts.request: 240s`. The
`estimated_cold_start_s: 150` in the place result was therefore conservative but the right
order of magnitude. Subsequent warm requests on the same section completed in 0.7–4.7 s.

### R8 — (AC-4) Placements demonstrated, and the late-binding behavior for the rest — **PARTIAL, by design**

| Placement | Demonstrated in this window? | Basis |
|---|---|---|
| `kserve-a5000` | **Yes** | R6 + R7 + R9 |
| `cachyos-7900xtx` | **No** | Router held it ineligible (`offline`) for the whole window; `/v1/place` never emitted it |
| `bazzite-5090` | **No** | Same |

STORY-036-2 may claim `kserve-a5000` only. The other two are not claimed and must be added
later by data-only catalog PRs when those machines are next up.

**The non-emission is positive evidence, recorded as demonstration rather than as a gap.**
Two live results show late binding working as specified:

1. Under `prefer-warm-local`, the result was `status: placed` /
   `reason.code: placed_only_candidate` on `kserve-a5000`, with an `alternatives` array
   listing both edges as `eligible: false`, `reason.code: offline`. The router named what it
   rejected and why, rather than silently narrowing.
2. Under `edge-only`, the result was `status: unavailable`, `placement: null`,
   `headers: {}`, `reason.code: all_candidates_withdrawn`, with the same two alternatives
   marked offline. HTTP 200, not an error. **The router declined to fall through to a cluster
   placement the caller had excluded** — which is the behavior the contract makes normative
   and the thing that would be most damaging if it silently did otherwise.

Eligibility was read from live runtime state, not from the catalog: all three placements
read `status: available` / `selectable: true` in catalog 1.5.0 while only one was actually
eligible.

### R9 — (AC-4b) `x-placement` conveyed from inside a real Claude Code session — **PASS**

This is the criterion that mattered most, and the header path worked; the agreed degraded
fallback was not needed.

Two-stage confirmation:

1. **Wire-level.** With `ANTHROPIC_CUSTOM_HEADERS="x-placement: kserve-a5000"` and
   `ANTHROPIC_MODEL=qwen36-27b` set on the subprocess and the base URL aimed at a local
   capture server, the captured request carried header `x-placement: kserve-a5000` and body
   `"model": "qwen36-27b"`. Both variables convey as intended, with no quoting or
   normalization surprises.
2. **End-to-end.** Real `claude -p` sessions (R3 and R5) with the same variable set against
   the live gateway were logged gateway-side as `route_rule: qwen36-27b-messages-kserve` —
   the placement-scoped section. A session that had *not* emitted the header could not have
   matched that section; the headerless sessions in R10 landed on `qwen36-27b-messages`
   instead. The harness therefore demonstrably drives placement selection, not just `curl`.

Note for the kit: Claude Code emits `/v1/messages?beta=true` (query string present). Gateway
API `Exact` path matching ignores the query, so this does not interfere with the
`/v1/messages` section matching — confirmed live.

### R10 — (AC-5) A headerless request lands on the frozen grouped default — **PASS**

Two forms, both confirmed:

- **`curl`**: `HTTP 200` in 4.77 s; gateway log `route_rule: qwen36-27b-messages` (the
  default section), upstream = the in-cluster KServe predictor Service.
- **Real session**: `claude -p` with the base URL, key and `ANTHROPIC_MODEL` set but
  `ANTHROPIC_CUSTOM_HEADERS` **unset** returned `is_error: false`, `subtype: success`,
  `result: "HEADERLESS-DEFAULT-OK"`, `duration_ms: 46154`; gateway log
  `route_rule: qwen36-27b-messages`, `http.status: 200`.

It landed on the `kserve-a5000` backend. It did not land on a human's desktop — neither edge
provider was contacted on either request (upstream service time 4.7 s / 45.9 s with no
connection-failure delay, versus the ~10 s penalty seen in R12 when an edge *is* attempted).

### R11 — (AC-5) A stale or unknown `x-placement` falls through to the same default — **PASS**

| `x-placement` sent | Result | `route_rule` selected |
|---|---|---|
| `laptop-rtx5000` (a real catalog placement, `selectable: false`, no route section) | HTTP 200, 1.09 s | `qwen36-27b-messages` |
| `totally-not-a-placement-9999` (garbage) | HTTP 200, 0.76 s | `qwen36-27b-messages` |

Both fell through to the frozen default and were served normally. This confirms the
consequence the story anticipates for an expired `ttl_seconds: 30` place result: a stale
header value degrades to the safe default rather than failing the request.

### R12 — (AC-6) Observed behavior with an edge unavailable — **PASS (observation recorded; no guarantee asserted)**

Both edges were genuinely unavailable throughout the window, so this was measured against
real conditions rather than simulated.

| `x-placement` sent | Route section matched | Caller-observed outcome | Total | Upstream service time |
|---|---|---|---|---|
| `cachyos-7900xtx` | `qwen36-27b-messages-edge` | **HTTP 200**, valid completion | 10.87 s | 0.72 s |
| `bazzite-5090` | `qwen36-27b-messages-bazzite` | **HTTP 200**, valid completion | 10.84 s | 0.71 s |

**What was observed**: the caller got a correct answer, with roughly **10.1 s of added
latency** spent failing to reach the offline edge before the request was served. The gateway
log records `retry.attempt: 1` on both, `http.status: 200`, and an upstream endpoint of the
in-cluster KServe predictor Service — i.e. the request was ultimately answered by the
group-1 provider (`kserve-a5000`) of the edge-preferred backend, not by the requested edge.

**What is *not* asserted.** This is a single observation under one failure mode (a host that
is down and does not answer at the transport layer). The route configuration for context is
`retry.attempts: 1`, `backoff: 50ms`, `codes: [503]`, `timeouts.request: 240s` — note that
`codes: [503]` means the route-level retry is scoped to 503 responses, and no 503 was
returned here, so what was observed is the backend's provider-group ordering, not the route
retry rule. A different failure mode (an edge that accepts the connection then hangs, or one
mid-interactive-withdrawal) could behave differently and was not tested. Router-side
fallback is explicitly out of scope (35.9).

The ~10 s penalty is worth carrying into STORY-036-2 as a note: pinning a placement that has
gone offline is not free, even though it is safe.

### R13 — (AC-7) The key is scoped to `qwen36-27b`, and other models are refused — **PASS**

The ExternalSecret template for `agentgateway-claude-code` stamps
`metadata.models = "qwen36-27b"` (single model), against the Open WebUI key's `"any"`.
Live negative tests, all with the `agw-claude-code` key:

| Request | Result |
|---|---|
| `/v1/messages`, body `"model":"claude-opus-5"` | **403** `authorization failed` |
| `/v1/chat/completions`, body `"model":"qwen-coder"` | **403** `authorization failed` |
| `GET /api/hello` (pilot catch-all, different credential set) | **401** `invalid credentials` |
| `/v1/messages`, no credential at all | **401** `no API Key found` |

The first case is notable: `claude-opus-5` is precisely the string an unpinned Claude Code
sends (R1), so the scoping check refuses the one model name most likely to be sent by
accident. The 401 on the catch-all confirms the key does not authenticate outside its
credential set.

### R14 — (AC-7) Zero vendor spend — **PASS**

Every inference request in this validation is accounted for in the gateway access log, and
every one shows `gen_ai.provider.name: openai` with an upstream endpoint inside the cluster
(the KServe predictor Service) or an offline edge attempt that fell back to it. No request
reached a vendor endpoint. No `ANTHROPIC_API_KEY` was set at any point, and the Anthropic Max
subscription path was not exercised: every session was pointed at
`https://ai-gateway.homelab0.org` and would have failed loudly if it had not been.

**One caveat, recorded so nobody misreads the artifact.** Claude Code's own result envelope
reports `total_cost_usd` (e.g. `0.152515`, `0.152035`) and a `modelUsage` entry with
`provider: "firstParty"`. This is a **client-side estimate computed by the CLI from its
internal price table**, not observed spend — the same envelope reports
`costBasis: "unknown"` for the model. No money was charged; the local model has no price.
Any downstream tooling that sums `total_cost_usd` across local-path sessions will
over-report and should ignore the field for `hosting: local` profiles.

### R15 — (AC-7) No metered path is reachable with this credential — **PASS**

Structural argument, checked against live `/v1/status` rather than the Git catalog:

- `local-code-standard` carries `entitlements: [{pool: null, cost_class: free}]` — local
  execution draws on no entitlement pool.
- All four seeded pools (`anthropic-max`, `openai-plus`, `devin-free`, `minimax-max`) report
  `spillover: none` (README rule 23). Nothing can spill.
- No entitlement with `cost_class: metered` exists in the live catalog at all (rule 21) —
  the pools are `subscription`, `subscription`, `free`, `subscription`. There is no metered
  target to spill into.
- Empirically, the credential is refused everywhere except the `qwen36-27b` sections
  (R13). It cannot reach any other backend on this gateway.

### R16 — (AC-8) No warm-pinning side effect was introduced — **PASS**

Live `InferenceService qwen36-27b` after the window: `minReplicas: 0`, `maxReplicas: 1`,
`autoscaling.knative.dev/scale-to-zero-pod-retention-period: 30m` — byte-identical to what
Git declares. Nothing was patched, scaled, or applied: `git status` shows no modification
under `kubernetes/`, and no `kubectl apply|patch|edit|scale` was issued at any point in this
validation.

The predictor did scale 0 → 1 during the window. That is the ordinary
scale-from-zero-on-request lifecycle (a 128.9 s cold start, R7), not a pin: `minReplicas`
remains `0` and the unchanged 30 m retention window governs the scale back down. Invariant 7
concerns the router never *probing* the KServe backend to observe readiness — and it did
not: every `/v1/place` result in this window returned `readiness: "unknown"` for
`kserve-a5000`, which is the router declining to probe. The predictor was woken by a
deliberate inference request from the validator, which is the normal caller path.

**Scale-down verified empirically.** The last inference request completed at
`2026-08-30T04:31:14Z`. A once-per-minute replica poll on the predictor deployment ran from
`04:33Z` and observed `replicas=1` continuously until **`replicas=0` at
`2026-08-30T05:03:12Z`** — 31 m 58 s after the last request, consistent with the unchanged
30 m retention plus Knative's stabilization delay. A confirming check at `05:03:20Z` found no
`qwen36-27b` predictor pod running and the autoscale spec still reading
`minReplicas: 0 / maxReplicas: 1 / retention: 30m`.

The A5000 returned to zero on its own, on the schedule Git declares, and the GPU was freed.
Nothing in this validation held it warm.

### R17 — (AC-9) Evidence retained under the split this story specifies — **PASS**

- This file — sanitized, numbered, citable per line by a catalog row.
- Raw captures (response headers, upstream completion ids, full bodies, place-result JSON,
  gateway log lines) — retained locally under `.claude/.ai-docs/stories/`, gitignored,
  uncited.

**FINDING F1 — RESOLVED.** This record was first written to
`docs/evidence/STORY-036-1-local-launch-validation.md`, the path the story names. That path is
unusable: `.gitignore` line 136 contains `/docs/` ("High-level documentation - contains
internal infrastructure details") and no file under `docs/` is tracked anywhere in this
repository, so a catalog row citing it would point at a file that is not in Git —
reproducing exactly the failure mode AC-9 exists to prevent (the STORY-035-23 citation
pointing at a file neither host can find).

The validator flagged this rather than fixing it, because the 2025-11-07 incident in this
repository was caused by a `.gitignore` negation rule and adding one unreviewed is not a
validation story's call. The decision taken downstream was the safer of the two options: the
record was **relocated to this tracked path under `contracts/`**, leaving `/docs/` ignored and
adding no negation rule. Catalog rows must cite
`contracts/agent-router/evidence/STORY-036-1-local-launch-validation.md`. The story's own
literal gate string (`test -f docs/evidence/…`) is therefore stale and is superseded by this
path.

---

## Verdict summary

| AC | Verdict | One line |
|---|---|---|
| AC-0 | Observation recorded (not a gate) | Unpinned Claude Code 2.1.251 sends `claude-opus-5`, outside the alias list; the unpinned path 403s live. Pinned contract unaffected. (R1, R2) |
| AC-1 | **PASS** | Real completion, `is_error: false`, correct answer, usage captured. (R3) |
| AC-2 | **PASS** | Served model `qwen3.6-27b` confirmed from live session and raw response bodies plus gateway telemetry; no upstream request-id header exists on this path. (R4) |
| AC-3 | **PASS** | Full `tool_use` → `tool_result` → synthesis, two-call round trip on the gateway path. (R5) |
| AC-4 | **PARTIAL** — one of three, which meets the stated minimum bar | `kserve-a5000` demonstrated end-to-end from a live `/v1/place` header. The two edges were ineligible all window and are **not claimed**; the router's refusal to emit them is captured as positive late-binding evidence. (R6, R7, R8) |
| AC-4b | **PASS** | `ANTHROPIC_CUSTOM_HEADERS` conveys `x-placement` from a real session; confirmed at the wire and by gateway route-section selection. Degraded fallback not needed. (R9) |
| AC-5 | **PASS** | Headerless and stale/unknown both land on the frozen default section, `curl` and real session alike. (R10, R11) |
| AC-6 | **PASS** (observation, no guarantee asserted) | With both edges genuinely down, a pinned-edge request still returned HTTP 200, paying ~10.1 s of added latency. Single failure mode only. (R12) |
| AC-7 | **PASS** | Key scoped to `qwen36-27b` and other models refused 403; zero vendor spend; no metered path exists or is reachable. CLI `total_cost_usd` is a client-side estimate, not spend. (R13, R14, R15) |
| AC-8 | **PASS** | `minReplicas: 0` and 30 m retention unchanged; nothing applied or patched; router never probed; predictor observed returning to zero at `05:03:12Z`, GPU freed. (R16) |
| AC-9 | **PASS** | Split retained as specified. The citable half lives at a tracked path under `contracts/` because `/docs/` is gitignored; F1 resolved without a negation rule. (R17) |

**Definition of done**: AC-1 through AC-8 pass, with AC-4 passing at the coordinator's stated
one-placement minimum and its coverage limits recorded explicitly. STORY-036-2 may document
the four-variable launch contract exactly as validated above, and may claim `kserve-a5000`
only.

## Findings carried forward

1. **F1 — resolved.** `docs/` is gitignored, so the citable record was relocated under
   `contracts/` rather than unignored; see R17. Cite that path, not the one in the story text.
2. **F2** — The maintenance note in `local-model-policies.yaml` naming `claude-sonnet-5` as
   Claude Code's zero-config default is stale; 2.1.251 sends `claude-opus-5`. The pinned
   contract does not depend on this, so it is context for whoever next touches that file,
   not a change this story makes.
3. **F3** — Claude Code does not recognize `qwen36-27b` as a model id and assumes a 200k
   context window for auto-compaction, emitting `[claude-code:unrecognized_model]` on every
   launch. The real window is 65536 (`min_context` on the profile; `max_context` on the edge
   heartbeats). The kit should set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` or a `modelOverrides`
   entry so long sessions compact at the right boundary rather than overrunning the model.
   Not a failure of any AC here — the validation sessions were far too short to reach it.
4. **F4** — `total_cost_usd` is reported non-zero on the free local path (R14). Anything
   aggregating session cost must exclude `hosting: local`.
5. **F5** — Pinning an offline placement costs ~10 s per request (R12). Callers holding a
   place result past its `ttl_seconds: 30` will pay this rather than fail, which is safe but
   not free.
