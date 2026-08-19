# Edge Worker Contract

Status: contract slice (Story 35.3). This document is the mechanically checkable target
for 35.6 (edge runtime bring-up) and every future edge node. A node that fails
`conformance.sh` never enters the model catalog.

Source: EPIC-035 §5 (Edge worker contract), §10b (owner rulings R3, R4, R5).

## 1. Heartbeat payload

Edge → router, `POST /v1/capacity/heartbeat` (EPIC-035 §4). This is also the shape
`GET /v1/status` re-exposes for each edge placement. Auth is a per-node bearer credential
(1Password → ExternalSecret cluster-side, `op read` host-side) — see §2.

```json
{
  "node": "<node-id>",
  "state": "AVAILABLE|SERVING|DRAINING|INTERACTIVE|OFFLINE",
  "gpu": {
    "vendor": "amd",
    "model": "RX 7900 XTX",
    "arch": "gfx1100",
    "vram_total_gb": 24,
    "vram_free_gb": 21.9,
    "utilization_pct": 3
  },
  "runtime": {
    "kind": "llama-swap+llama.cpp",
    "version": "…",
    "endpoint": "https://<edge-host>:<edge-port>"
  },
  "active_model": "qwen3.6-27b",
  "cached_models": ["qwen3.6-27b"],
  "preemptible": true,
  "interactive": false,
  "ac_power": true,
  "cluster_reachable": true,
  "last_heartbeat": "RFC3339",
  "capabilities": ["chat", "tools"],
  "max_context": 65536
}
```

| Field | Type | Notes |
|---|---|---|
| `node` | string | stable node identifier |
| `state` | enum | one of the five states below |
| `gpu.vendor` | string | `amd` \| `nvidia` \| `unknown` |
| `gpu.model` | string | may be `unmeasured` — see §4 (laptop) |
| `gpu.arch` | string | e.g. `gfx1100`; may be `unmeasured` |
| `gpu.vram_total_gb` / `gpu.vram_free_gb` | number | may be `null` when unmeasured |
| `gpu.utilization_pct` | number | 0–100 |
| `runtime.kind` | string | e.g. `llama-swap+llama.cpp` |
| `runtime.version` | string | runtime/daemon version string |
| `runtime.endpoint` | string | reachable-from-cluster endpoint, e.g. `https://<edge-host>:<edge-port>` |
| `active_model` | string \| null | currently loaded model, if any |
| `cached_models` | string[] | models resident without a reload |
| `preemptible` | bool | true if local interactive use can evict AI work |
| `interactive` | bool | true while a human is actively using the host |
| `ac_power` | bool | false disqualifies laptop placements — see §4 |
| `cluster_reachable` | bool | node's own view of reaching the cluster |
| `last_heartbeat` | RFC3339 | stamped by the edge host, not the router |
| `capabilities` | string[] | e.g. `chat`, `tools`, `vision` |
| `max_context` | number | tokens |

### 1.1 States

| State | Meaning |
|---|---|
| `AVAILABLE` | idle, ready to accept work |
| `SERVING` | actively handling one or more inference requests |
| `DRAINING` | finishing in-flight requests, refusing new ones |
| `INTERACTIVE` | local human use has claimed the GPU; not eligible for placement |
| `OFFLINE` | no heartbeat inside `3 × interval`, or the node reports it explicitly |

### 1.2 Ownership rules

- **State ownership is local.** The host decides its own state. The cluster **never**
  forces a node into `SERVING` — `agent-router`'s `/v1/place` response is
  authoritative-with-alternatives for placement *intent*, but the edge host is what
  actually accepts or refuses the inference request.
- **Withdrawal is by silence and by 503.** Entering `INTERACTIVE` or `DRAINING` means the
  inference endpoint itself stops accepting new work. The gateway's health weighting
  removes the backend without any control-plane round trip — there is no "please drain"
  RPC from the cluster to the edge. A missed heartbeat (silence) and a `503` from the
  inference endpoint are the only two withdrawal signals the cluster acts on.
- The router marks a node `OFFLINE` once it has gone `3 ×` the heartbeat interval without
  a heartbeat (EPIC-035 §4).

## 2. Transport (owner ruling R4 — production shape, non-negotiable)

- **HTTPS required.** No plaintext, even inside the VLAN trust boundary — this traffic
  carries a long-lived shared secret.
- **Bearer/API-key authentication required** on every request to the edge inference
  endpoint.
- **No `insecureSkipVerify`** in the production shape. It was proven to work in the 35.4
  spike (form a7, HTTP 200) and must not ship — it turns the CA into decoration.
- **agentgateway validates a real CA and a hostname SAN.** The trust anchor must have
  `basicConstraints: CA:TRUE`. A self-signed leaf used as its own anchor is rejected with
  `invalid peer certificate: Other(OtherError(CaUsedAsEndEntity))` (measured in the 35.4
  spike). A cert-manager-issued leaf plus its issuer CA satisfies this; a bare self-signed
  cert does not. The certificate's SAN must match the `host` value used in the backend
  reference — use the hostname form, not a bare IP.
- **Dedicated host certificate and private key.** Never reuse the Kubernetes wildcard
  private key for an edge host cert.
- **Secret material lives outside Git** — the edge bearer token and the host key/cert live
  in 1Password / cluster `ExternalSecret`s, never committed.
- `policies.tls.mtlsCertificateRef` (mutual TLS) is a viable upgrade path if an edge host
  ever fronts something more sensitive than this, but it needs a client cert issued to the
  gateway and an edge daemon willing to verify it — llama.cpp does not do this today, so
  bearer-over-TLS is the right amount of mechanism for 35.8.
- **Certificate provisioning may be documented as provisional at 35.6** if no automated
  non-Kubernetes PKI flow exists yet — that gap does not block the contract, it blocks
  calling 35.6 "done."

## 3. Architecture (owner ruling R3)

- **llama-swap is the LAN-facing edge inference service.** It is the only edge process any
  cluster traffic reaches — it supports API-key enforcement and native TLS, which is what
  §2 requires.
- **llama-server sits behind llama-swap and is never LAN-exposed.** llama-swap proxies to
  it locally; llama-server has no independent network exposure.
- **llama.cpp ROCm is the initial Qwen runtime** on the 7900 XTX edge host. This closes the
  35.4 open question of whether the edge daemon can require a token — it can, because
  llama-swap sits in front of it and owns that concern.
- **ufw finding (35.4):** the devbox's `ufw` drops inbound TCP to a plain host process while
  letting docker-published ports through — DNAT rules for published ports sit in
  `PREROUTING`/`DOCKER-USER` and never traverse `ufw`'s `INPUT` chain. A host-process
  llama-swap binding directly to a LAN interface is silently dropped from the cluster's
  perspective; it must either run in a container with a published port, or ship an explicit
  `ufw` allow rule scoped to the cluster's VLAN. This is the one thing that will silently
  not work if skipped.

## 4. Per-host preemption rules

| Host | GPU | Preemption behavior |
|---|---|---|
| Bazzite | RTX 5090 (confirmed) | `preemptible: true`. Gaming/interactive use has **absolute priority**. Abrupt mid-request withdrawal is expected and normal — no graceful drain is guaranteed. |
| CachyOS | RX 7900 XTX | Opportunistic. The machine's interactive desktop work outranks AI; AI work is expected to yield, not fight for the GPU. |
| Laptop | RTX 5000 — **SKU, generation, and VRAM UNMEASURED** | Advertises into the catalog **only when all of**: `cluster_reachable: true` **and** `ac_power: true` **and** GPU idle **and** thermally healthy. Model viability on this host is **unknown until the hardware is actually detected** at runtime — **no profile may assume the laptop is viable** for any given model, and no catalog entry may hardcode capability numbers for it ahead of detection. |

## 5. Conformance script

`edge/conformance.sh` takes an endpoint (`--endpoint`) and a bearer credential
(`--api-key`), and prints one `PASS`/`FAIL`/`SKIP` line per capability:

- **`models_shape`** — `GET /v1/models` returns `{"data": [{"id": ...}, ...]}`.
- **`chat_completions`** — `POST /v1/chat/completions` (non-streaming) returns a
  `choices[0].message.content`.
- **`sse_streaming`** — the same endpoint with `stream: true` returns correctly framed SSE:
  one or more `data: {...}` frames each parseable as `{choices[0].delta: ...}`, terminated
  by a literal `data: [DONE]` frame, **and at least one frame carrying non-empty
  `choices[].delta.content`** — framing around an empty stream is not a working stream.
  Both `LF` and `CRLF` line endings are accepted, since SSE permits either.
- **`tool_calling`** — a forced-tool-choice request round-trips a matching
  `choices[0].message.tool_calls[0].function.name`.
- **`model_alias`** — when `--alias-model` is given, the alias must return a normal
  completion **and report the same resolved `model` id as `--model` did**, which is how the
  script proves the two ids land on the same served model rather than merely both being
  accepted. This is what proves an edge node's alias handling matches the KServe path's
  model-alias semantics (34.11's authz binds body-model to routed-model — an alias that
  diverges here is a routing bug, not a cosmetic difference).
- **`auth_enforced`** — with no `Authorization` header, both `GET /v1/models` and
  `POST /v1/chat/completions` must be rejected (`401`/`403`), never `200`. The inference
  path is checked separately because it is the one §2 protects, and a node can guard model
  listing while leaving inference open.
- **`tls_validation`** — for `https` endpoints, the script connects **without** `-k`/
  `--insecure` and requires success, proving the certificate chain and hostname SAN are
  actually valid (§2). Passing `--insecure` downgrades this check to `SKIP` with an explicit
  warning that the tested shape violates rule R4 and must never be the production shape.

The script exits non-zero if any capability `FAIL`s. `SKIP` (e.g. no `--alias-model` given,
or `--insecure` requested) does not fail the run — it is a narrower test, not a failure.

## 6. Proof against KServe `qwen36-27b`

See the Dev Agent Record in `STORY-035-3-edge-worker-contract.md` for the captured run
output and the timestamped, deliberate GPU wake. The script was pointed at the existing
`agentgateway` route (`https://ai-gateway.homelab0.org`, pinned via `--resolve` because LAN
DNS for that hostname does not exist yet — 34.19b is unstarted) with `--model qwen36-27b`
and `--alias-model claude-sonnet-5` (the accepted gateway alias), using the production
`agentgateway-claude-code` credential.

## 7. Explicitly out of scope

**`x-placement` is a scheduling signal only (owner ruling R5).** It carries no
authorization meaning whatsoever. Model authorization is, and remains, the per-model CEL
policy binding the request body's `model` field to the routed backend model (34.11's
shape). Nothing in this contract, in `conformance.sh`, or in any edge node's request
handling may let `x-placement` influence an authorization decision — a design that does so
is rejected on sight.
