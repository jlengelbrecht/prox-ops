# Bazzite RTX 5090 edge worker

The `bazzite-5090` placement from the EPIC-035 model catalog: llama.cpp CUDA
serving Qwen3.6-27B, behind llama-swap, behind TLS and a bearer token, on the
LAN. Catalog status at bring-up: `status: planned`, `selectable: false`,
`blocked_by: ["35.13"]` — nothing in this directory changes that; promotion is
a separate, owner-approved catalog change once acceptance evidence supports it.

Contract: [`edge/EDGE-WORKER-CONTRACT.md`](../EDGE-WORKER-CONTRACT.md).
Checker: [`edge/conformance.sh`](../conformance.sh).
Reference implementation and full design history:
[`edge/cachyos/README.md`](../cachyos/README.md) — the architecture, the
shared-state directory, the guard-ready lease (fail-closed interlock), the
activation boundary, and every STORY-035-6a defect cycle apply here unchanged
and are not re-argued in this file. **This file documents what is different on
Bazzite and why**, plus the bring-up.

## Contract behavior specific to this host (§4)

Gaming/interactive use has **absolute priority**. Abrupt mid-request
withdrawal is expected and normal — no graceful drain is guaranteed. The
supervisor still drains politely when it can (SIGTERM closes the listener
first), but nothing here adds machinery to hide the abruptness: a mid-stream
withdrawal fails the stream, and ruling R2 says the gateway does not rescue it.

## Shape

Identical to the CachyOS node at the contract level:

```text
                    LAN (cluster VLAN)
                          │  https://bazzite-5090.homelab0.org:8443
                          │  bearer token, dedicated edge CA
                          ▼
  ┌────────────────────── container: edge-llama-swap (rootless podman) ──┐
  │  edge-supervisor.sh  (PID 1)                                          │
  │    └── llama-swap        :8443   TLS terminator, API-key enforcement  │
  │          └── llama-server 127.0.0.1:10001   never published, CUDA     │
  └───────────────────────────────────────────────────────────────────────┘
  ┌─ edge-interactive-guard ─┐        ┌─ edge-heartbeat ──────────────────┐
  │ (systemctl --user)       │        │ (systemctl --user)                │
  └──────────────────────────┘        └───────────────────────────────────┘
  shared state: ~/.local/state/edge-bazzite-state  ⇄  /edge/state
```

## What is different from CachyOS, and why

| | CachyOS 7900 XTX | Bazzite 5090 | Why |
|---|---|---|---|
| container runtime | docker compose | **rootless podman quadlet** | this host's docker daemon needs a group the user is not in; rootless podman needs no privilege at all. The quadlet is rendered from `systemd/edge-llama-swap.container.template` by `scripts/install.sh` so no LAN address enters Git. |
| GPU access | `/dev/kfd` + `/dev/dri` | **CDI: `nvidia.com/gpu=all`** + `SecurityLabelDisable=true` | NVIDIA CDI spec pre-generated at `/etc/cdi/nvidia.yaml`; with SELinux labeling on, NVML fails `Insufficient Permissions` (measured). |
| firewall | ufw drops host-process ports, docker DNAT bypasses it | **firewalld `FedoraWorkstation` zone opens TCP 1025–65535** | a rootless-podman published port is a real host socket and traverses INPUT — and the zone's default already admits 8443. No sudo, no rule change. |
| storage | docker named volumes | **plain host directories** | rootless podman gives the user direct ownership; nothing needs root to read. The supervisor's cache-manifest mechanism is kept anyway so both nodes share one heartbeat behavior. |
| image | llama.cpp **ROCm** `server-rocm-b9917` + llama-swap 250 | llama.cpp **CUDA** `server-cuda-b9917` + llama-swap 250 | same build number as the cluster's kserve-a5000 placement — cross-placement numbers stay comparable. |
| GPU sensor | `rocm-smi` (absolute path rule) | `nvidia-smi` (same rule, `NVIDIA_SMI=`) | STORY-035-6a cycle 5 inherited. |
| guard arithmetic | compute-vs-graphics (llama-server is the only compute client) | **ours-vs-foreign by container cgroup** | **measurably false premise here**: on NVIDIA-Wayland the compositor, browsers and Electron apps all hold CUDA compute contexts (~643 MiB "compute" at an idle desktop, measured 2026-08-24). See below. |
| credential copies | volume copy + host copy | **one file**, `~/.config/edge-bazzite/secrets/api-key`, mounted into the container and read by the units | host directories make the second copy pointless. |
| withdrawal wire signal | connection **refused** (docker DNAT vanishes with the listener) | connection **reset** during TLS handshake, measured 4 ms client-visible | pasta owns the host socket, so TCP accept still succeeds while the in-container listener is closed; the forwarded connection is RST immediately. Same contract class — an instant wire failure agentgateway's passive health acts on, not a 10 s deadline — but probes must be HTTP-level, not raw TCP connect (`testing/interactive-drill.sh` is). |

### Human-use detection on this host

The question is unchanged — *is anything other than our inference using the
GPU* — but the observable that answers it is different. `nvidia-smi` cannot
split compute-vs-graphics the way rocm-smi's KFD table does (desktop processes
are compute clients here), so the guard splits **ours-vs-foreign** instead:

- **ours** = VRAM held by processes whose `/proc/<pid>/cgroup` places them in
  the `edge-llama-swap.service` cgroup. Cgroup, not process name, on purpose:
  a foreign llama-server someone runs by hand must count as foreign
  (`testing/interactive-drill.sh` proves exactly this), and a renamed binary
  inside our container must still count as ours.
- **foreign** = `vram_used − ours`.

Trip rules (see `scripts/edge-interactive-guard.sh`):

1. foreign VRAM above the measured idle baseline (`EDGE_DESKTOP_BASELINE_MB`,
   1500 MB measured 2026-08-24) by more than `EDGE_INTERACTIVE_VRAM_MB`
   (2048) — games, video editors, LM Studio. The primary gaming detector.
2. any single foreign compute client over `EDGE_FOREIGN_COMPUTE_MB`
   (1024 MB) — real GPU work by absolute size; idle desktop clients measure
   77–250 MB each.
3. GPU busy over `EDGE_INTERACTIVE_GPU_PCT` (25%) while llama-swap
   **verifiably** reports zero loaded models — compute-heavy, VRAM-light
   foreign work. Requires a real zero; an `unknown` reading skips the rule
   (missing telemetry must not manufacture a claim).

Unlike the 24 GB CachyOS card, a loaded Qwen3.6-27B leaves **~12 GB free**
here, so the thresholds are not squeezed by headroom — but the same two limits
hold: detection is fast, not instantaneous, and VRAM/compute-light interactive
work is invisible. `edge-interactive-guard.sh claim` from a launcher hook or
by hand *before* the GPU is wanted remains the primary mechanism; the detector
is the backstop. A manual claim is sticky and never released by the detector.

The guard-ready lease, its fail-closed semantics, the renewal-only-on-valid-
sample rule, and the withdrawal bound (`EDGE_GUARD_LEASE_TTL +
EDGE_POLL_INTERVAL`, 7 s at defaults) are inherited unchanged.

## Files

| Path | What it is |
|---|---|
| `Containerfile` | llama.cpp's CUDA server image + the llama-swap release binary |
| `systemd/edge-llama-swap.container.template` | quadlet template; `install.sh` renders it with the deployed env |
| `systemd/*.service`, `systemd/edge-heartbeat.service.d/` | the two host-side daemons + router-token drop-in |
| `llama-swap.yaml` | model definition, TTL, API-key enforcement |
| `env.example` | template for the untracked host config |
| `model-id-map.json` | runtime identity → catalog `model_id` (contract §1) |
| `scripts/edge-common.sh` | NVIDIA GPU sampling, ours-vs-foreign attribution |
| `scripts/edge-interactive-guard.sh` | Bazzite trip rules; claims the GPU |
| `scripts/edge-supervisor.sh` | container PID 1 — byte-identical to the CachyOS copy |
| `scripts/edge-heartbeat.sh` | contract §1 payload, `vendor: nvidia`, node `bazzite-5090` |
| `scripts/issue-edge-pki.sh` | byte-identical to the CachyOS copy |
| `scripts/install.sh` | idempotent install/refresh + quadlet render |
| `testing/interactive-drill.sh` | times detection/withdrawal/recovery using a foreign llama-server as the load |

## Bring-up

Everything runs as the login user. Nothing needs sudo.

```sh
cd edge/bazzite

# 1. model artifact (same GGUF as every other qwen36-27b placement)
mkdir -p ~/.local/share/edge-bazzite/models/unsloth/Qwen3.6-27B-MTP-GGUF
curl -sSL --retry 8 --retry-all-errors -C - \
  -o ~/.local/share/edge-bazzite/models/unsloth/Qwen3.6-27B-MTP-GGUF/Qwen3.6-27B-UD-Q4_K_XL.gguf \
  https://huggingface.co/unsloth/Qwen3.6-27B-MTP-GGUF/resolve/main/Qwen3.6-27B-UD-Q4_K_XL.gguf
sha256sum ~/.local/share/edge-bazzite/models/unsloth/Qwen3.6-27B-MTP-GGUF/*.gguf
# 4085665ee36d82a672a238a43f0e5643f2f0e39f2d7bd5d373f0ef10ecf53095
# (the HF LFS object id — a match also proves the artifact is the one the
# repo advertises; same figure as edge/cachyos/README.md step 1)

# 2. PKI (host openssl; keys never enter Git)
mkdir -p ~/.config/edge-bazzite/pki && chmod 700 ~/.config/edge-bazzite/pki
scripts/issue-edge-pki.sh --hostname bazzite-5090.homelab0.org \
  --out ~/.config/edge-bazzite/pki

# 3. bearer credential — copy of record is 1Password (vault Automation,
#    item edge-bazzite-5090, field `credential`)
mkdir -p ~/.config/edge-bazzite/secrets
( umask 077
  op read 'op://Automation/edge-bazzite-5090/credential' |
    tr -d '\r\n' > ~/.config/edge-bazzite/secrets/api-key )

# 3a. router credential — a DIFFERENT authority (capacity reporting TO
#     agent-router), field `router-credential` on the same item. Never reuse
#     one for the other.
( umask 077
  printf 'EDGE_ROUTER_TOKEN=%s\n' \
    "$(op read 'op://Automation/edge-bazzite-5090/router-credential')" \
    > ~/.config/edge-bazzite/router.env )

# 4. configure
cp env.example ~/.config/edge-bazzite/edge.env && chmod 600 ~/.config/edge-bazzite/edge.env
$EDITOR ~/.config/edge-bazzite/edge.env    # address, paths: replace /home/<user>

# 5. build, install, enable
podman build -t edge-llama-swap:b9917-swap250 -f Containerfile .
scripts/install.sh
systemctl --user enable --now edge-interactive-guard.service edge-heartbeat.service
systemctl --user start edge-llama-swap.service
loginctl enable-linger "$USER"
```

Verify the shared directory and then run the contract checker exactly as the
CachyOS README's step 7 does, substituting this node's names:

```sh
EDGE_API_KEY=$(op read 'op://Automation/edge-bazzite-5090/credential') \
../conformance.sh \
  --endpoint https://bazzite-5090.homelab0.org:8443 \
  --ca-cert ~/.config/edge-bazzite/pki/edge-ca.crt \
  --model qwen36-27b --alias-model qwen3.6-27b \
  --resolve bazzite-5090.homelab0.org:8443:<edge-lan-ip> \
  --timeout 300
```

`--resolve` until the unifi-dns `DNSEndpoint` for this hostname lands.
`install.sh` prints `ACTIVATION REQUIRED` after refreshes — the activation
boundary is inherited unchanged (see the CachyOS README).

## What this directory deliberately does not do

- It does not make `bazzite-5090` catalog-selectable, and it does not touch
  anything under `kubernetes/` — the `AgentgatewayBackend` provider, the CA
  ConfigMap, the ExternalSecrets, the DNSEndpoint and the catalog promotion
  are the control-plane change, gated on acceptance evidence.
- It does not expose llama-server, and `runtime.endpoint` in the heartbeat
  stays observational metadata, never a routing target.
- It does not add model distribution or any second model; the MVP model is
  the catalog's `qwen36-27b`, full stop.
