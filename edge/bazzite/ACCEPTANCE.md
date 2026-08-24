# bazzite-5090 — edge acceptance evidence

Captured 2026-08-24 on the Bazzite host itself, against prox-ops main
`0e16ab57a3fcf99a8c69b86e19a4d8b26b57f2e9` (branch `edge/bazzite-5090-bringup`).
Secret-free: no credential values, no private keys. The host's LAN address is
written `<edge-lan-ip>` here; its committed copy of record is
`kubernetes/apps/network/unifi-dns/app/dnsendpoint-bazzite.yaml`.

## Hardware / platform identity (measured)

| | |
|---|---|
| OS | Bazzite 44.20260820.0 (Kinoite), image `bazzite-dx-nvidia`, rpm-ostree |
| GPU | NVIDIA GeForce RTX 5090, compute capability 12.0 (`sm_120`) |
| driver | 610.57.04 (CUDA UMD 13.3), CDI spec present at /etc/cdi/nvidia.yaml |
| VRAM | **32607 MiB total measured** (31.8 GiB) — nameplate says 32 GB; catalog `capacity.vram_gb` should carry the measured 31.8 |
| idle desktop VRAM | ~1417 MiB (Steam client, browser, terminals open), 3–5% util |
| container runtime | rootless podman 5.8.4 + quadlet; docker daemon inaccessible to the login user (by design here — nothing needs sudo) |
| firewall | firewalld, zone `FedoraWorkstation` (TCP 1025–65535 open by default → published :8443 needs no rule change) |

## Runtime / model identity (pinned)

| | |
|---|---|
| image | `localhost/edge-llama-swap:b9917-swap250`, id `a08af794fdf1…` |
| base image | `ghcr.io/ggml-org/llama.cpp:server-cuda-b9917`, digest `sha256:19b4445dba0f3be11af40386a3409b8ac4bccb275f8ee7728560202f1fe76a6d` — same llama.cpp build number as the cluster's kserve-a5000 placement |
| llama-swap | v250 (runtime.version reports `llama-swap/v250 llama.cpp/b9917`) |
| model | `unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf` |
| sha256 | `4085665ee36d82a672a238a43f0e5643f2f0e39f2d7bd5d373f0ef10ecf53095` — verified after download; matches the HF LFS object id and the artifact every other qwen36-27b placement runs |
| flags | identical to the cluster invocation: `--n-gpu-layers 999 --ctx-size 65536 --parallel 1 --spec-type draft-mtp --spec-draft-n-max 2 --jinja --metrics` |

## TLS / auth (production shape, no --insecure anywhere)

- Dedicated per-host PKI issued on-host by `scripts/issue-edge-pki.sh`:
  CA `CN=homelab0 edge CA` (new keypair, not the CachyOS one), CA:TRUE
  pathlen:0, expires 2036-08-21, sha256 fingerprint `57:0D:D7:27:…:D8:F8:C6:6A`
  (full value in the committed ConfigMap `edge-bazzite-5090-ca`); leaf
  SAN `DNS:bazzite-5090.homelab0.org`, expires 2027-09-25. Keys live in
  `~/.config/edge-bazzite/pki` (0700), outside every checkout.
- Live-leaf validation: `openssl s_client -servername
  bazzite-5090.homelab0.org -CAfile edge-ca.crt` → `Verification: OK,
  Verify return code: 0`.
- Unauthenticated `GET /v1/models` → 401; unauthenticated
  `POST /v1/chat/completions` → 401.

## Conformance (edge/conformance.sh, unmodified)

```
PASS models_shape
PASS chat_completions
PASS sse_streaming
PASS tool_calling
PASS model_alias        'qwen3.6-27b' and 'qwen36-27b' both resolve to 'qwen3.6-27b'
PASS auth_enforced      rejected without a credential (models HTTP 401, chat HTTP 401)
PASS tls_validation     certificate chain and hostname SAN verified against the edge CA (no --insecure)
PASS=7 FAIL=0 SKIP=0
```

Run with `--ca-cert` against the dedicated CA and `--resolve` (no LAN DNS
record yet — the DNSEndpoint is in this branch). Note `model_alias` is a real
PASS here, not the llama-server echo SKIP: llama-swap resolves the alias
before dispatch, so both ids observably land on the same served model.

## Performance (measured on this host)

| | |
|---|---|
| cold start (nothing loaded → completed 64-token response) | **4.0 s** wall, ~22 GB into VRAM from local NVMe |
| warm generation | **114 tok/s** client-measured over a 1024-token completion (server-reported 116.5 predicted tok/s, 330 tok/s prompt) |
| VRAM with model loaded | 23529 MiB used total (~22.1 GB ours), ~8.9 GiB free alongside the desktop |
| context | 65536 as configured; matches the catalog's `max_context` fact |

Qwen3.6 is a reasoning model: small `max_tokens` budgets can be consumed by
reasoning_content before any assistant content is emitted (observed at
max_tokens=64). Conformance's 512/1024 budgets are sufficient.

## State machine (all transitions host-decided; the cluster commanded nothing)

| proof | result |
|---|---|
| idle | heartbeat `state: AVAILABLE`, inference accepted (conformance above) |
| generation in flight | `state: SERVING`, `active_model: "qwen36-27b"` (catalog id, translated from the runtime id) |
| manual claim (`edge-interactive-guard.sh claim`) | phase `withdrawn` <1 s, heartbeat `INTERACTIVE` + `interactive: true`; HTTP client sees connection reset in **4 ms** |
| release | serving/`AVAILABLE` again in 1 s |
| guard killed (fail-closed lease) | endpoint withdrawn at **t+7 s** (= TTL 6 s + poll 1 s bound) with no cluster round-trip; heartbeat `OFFLINE`; guard restart → serving in 1 s |
| container stopped | heartbeat `OFFLINE`, `cached_models: []` (manifest removed on graceful stop — fail-to-empty); restart → `AVAILABLE`, cache re-observed |
| container restarted with guard dead (reboot race) | supervisor logs `no fresh guard lease at startup; staying withdrawn until the guard proves it is alive` — starts CLOSED; serving resumes only after the guard renews |

### Interactive drill (testing/interactive-drill.sh, competing foreign llama-server, ngl=14)

```
t+0s   baseline: endpoint accepting, phase=serving, no claim
t+8s   GUARD CLAIMED the GPU            (detection latency 8s)
t+9s   ENDPOINT REFUSING connections    (withdrawal latency 9s, phase=draining)
t+31s  competing workload finished
t+92s  guard released the claim         (30-sample × 2 s hold-down)
t+93s  ENDPOINT ACCEPTING again         DRILL PASSED
```

The competing workload was a *foreign llama-server in its own container* —
also proving the cgroup-based ours/foreign attribution: a process named
llama-server outside `edge-llama-swap.service` is treated as a human-priority
foreign consumer, not as our own inference.

### Withdrawal wire signal (divergence from CachyOS, same contract class)

rootless podman's pasta forwarder owns the host socket, so a withdrawn
endpoint accepts TCP and immediately RSTs during the TLS handshake —
measured **4 ms** client-visible (`curl: (35) Connection reset by peer`),
never a hanging connect. An instant wire failure for agentgateway's passive
health, exactly like CachyOS's connection-refused; probes must be HTTP-level.

## Heartbeat (edge/EDGE-WORKER-CONTRACT.md §1)

`edge-heartbeat.sh --self-test`: 12/12 PASS (catalog-id translation,
one-directional map, no-guess drop, manifest fail-to-empty, capability
derivation). Live payload at idle:

```json
{"node":"bazzite-5090","state":"AVAILABLE",
 "gpu":{"vendor":"nvidia","model":"NVIDIA GeForce RTX 5090","arch":"sm_120",
        "vram_total_gb":31.8,"vram_free_gb":30.5,"utilization_pct":3},
 "runtime":{"kind":"llama-swap+llama.cpp","version":"llama-swap/v250 llama.cpp/b9917",
            "endpoint":"https://bazzite-5090.homelab0.org:8443"},
 "active_model":null,"cached_models":["qwen36-27b"],
 "preemptible":true,"interactive":false,"ac_power":true,
 "cluster_reachable":true,"capabilities":["chat","tools"],"max_context":65536}
```

`cluster_reachable: true` is a real probe of `agent-router.homelab0.org`
(reachable from this host, TLS-valid, 401 without a token as expected). The
heartbeat is NOT yet POSTing: `EDGE_ROUTER_URL` stays empty until the router
credential exists (below).

## Outstanding — blocked on owner 1Password actions

The op CLI on this host has no active session, so neither credential of
record could be read or created. Running now with a clearly-labeled
TEMPORARY bring-up token (prefix `sk-TEMP-bringup-`) in
`~/.config/edge-bazzite/secrets/api-key` — it protects the endpoint for the
drills above and MUST be replaced before any cluster wiring:

1. Create 1Password item **`edge-bazzite-5090`** in vault **`Automation`**:
   - field `credential` — inference bearer (protects traffic INTO this node;
     read by the host and by `ExternalSecret/agentgateway-pilot-edge-bazzite-5090`)
   - field `router-credential` — heartbeat bearer (lets this node report
     capacity TO agent-router; read by the host drop-in and by the
     `bazzite-5090` entry in `ExternalSecret/agent-router-node-credentials`)
   Two different values. Never reuse one for the other.
2. On this host: `op signin`, then re-seed per README steps 3/3a, set
   `EDGE_ROUTER_URL=https://agent-router.homelab0.org` in edge.env, restart
   the two units and the container.
3. Only after (1): merge/apply the agent-router ExternalSecret change (a
   missing item blocks the WHOLE node-credentials Secret refresh, cachyos
   included) and the agentgateway ExternalSecret.

## Catalog promotion (separate, owner-approved)

Not performed here. When acceptance is accepted, the promotion PR flips
`placements.bazzite-5090` to `status: available`, `selectable: true`,
drops `blocked_by`, sets `runtime: llama-swap fronting llama.cpp (CUDA)`,
`capacity: {vram_gb: 31.8, source: measured}` (rule 13),
`cold_start_s_estimate: 4` with `cold_start_source: measured` +
`cold_start_evidence` pointing here, adds `bazzite-5090` to
`models.qwen36-27b.placements` and the relevant `profiles[].physical`,
updates `prefer_order`/`resolves_today` as decided, bumps `version`/
`updated` — and updates `services/agent-router/internal/catalog/catalog_test.go:102`,
which currently asserts qwen36-27b is NOT authorized on bazzite-5090.
