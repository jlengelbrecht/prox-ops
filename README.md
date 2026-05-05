<div align="center">

<img src=".github/assets/header.gif" alt="Prox-Ops" width="500"/>

_Production-grade Kubernetes homelab — Proxmox VE • Talos Linux • Flux GitOps • Cilium • Rook-Ceph • GPU-accelerated AI._

</div>

<div align="center">

[![Talos Latest](https://img.shields.io/github/v/release/siderolabs/talos?label=Talos%20Latest&logo=talos&logoColor=white&color=blue&style=for-the-badge)](https://github.com/siderolabs/talos/releases)
[![Talos Pinned](https://img.shields.io/badge/dynamic/yaml?url=https://raw.githubusercontent.com/jlengelbrecht/prox-ops/main/talos/talenv.yaml&query=$.talosVersion&label=Talos%20Pinned&logo=talos&logoColor=white&color=blue&style=for-the-badge)](./talos/talenv.yaml)
[![Kubernetes Latest](https://img.shields.io/github/v/release/siderolabs/kubelet?label=K8s%20Latest&logo=kubernetes&logoColor=white&color=blue&style=for-the-badge)](https://github.com/siderolabs/kubelet/releases)
[![Kubernetes Pinned](https://img.shields.io/badge/dynamic/yaml?url=https://raw.githubusercontent.com/jlengelbrecht/prox-ops/main/talos/talenv.yaml&query=$.kubernetesVersion&label=K8s%20Pinned&logo=kubernetes&logoColor=white&color=blue&style=for-the-badge)](./talos/talenv.yaml)

</div>

<div align="center">

[![Flux Local](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/flux-local.yaml?branch=main&label=flux-local&logo=flux&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/flux-local.yaml)
[![Security Gate](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/security-gate.yaml?branch=main&label=security&logo=github&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/security-gate.yaml)
[![Release](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/release.yaml?branch=main&label=release&logo=github&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/release.yaml)
[![Renovate](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/renovate.yaml?branch=main&label=renovate&logo=renovatebot&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/renovate.yaml)
[![Cattle Upgrade](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/upgrade-cattle.yaml?branch=main&label=cattle&logo=talos&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/upgrade-cattle.yaml)
[![Pets Upgrade](https://img.shields.io/github/actions/workflow/status/jlengelbrecht/prox-ops/upgrade-pets.yaml?branch=main&label=pets&logo=talos&logoColor=white&style=flat-square)](https://github.com/jlengelbrecht/prox-ops/actions/workflows/upgrade-pets.yaml)

</div>

<div align="center">

[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](#-license)
[![Last Commit](https://img.shields.io/github/last-commit/jlengelbrecht/prox-ops/main?style=flat-square&logo=github)](https://github.com/jlengelbrecht/prox-ops/commits/main)
[![Top Language](https://img.shields.io/github/languages/top/jlengelbrecht/prox-ops?style=flat-square&logo=github)](https://github.com/jlengelbrecht/prox-ops)

</div>

---

## 📖 Overview

Prox-Ops is the complete infrastructure-as-code definition for a 15-node Kubernetes cluster running on a four-host Proxmox VE cluster. It runs media services, home automation, observability tooling, and a self-hosted AI/RAG stack on consumer GPUs — all reconciled from this repository by Flux.

The design goals are:

- **Reproducibility** — every node, network, and workload is declared in Git. The cluster can be rebuilt from a clean Proxmox install.
- **Cattle, not pets** — Talos nodes are immutable and replaced rather than patched. Image Factory schematics are the single source of truth for kernel args and drivers.
- **GitOps end-to-end** — Flux v2 reconciles every workload from `main`. Direct cluster mutation is reserved for emergency rollback.
- **Encrypted by default** — secrets live in Git as SOPS+age ciphertext, fed into the cluster via External Secrets Operator + 1Password Connect.

## ✨ Features

- **Talos Linux** on every Kubernetes node — immutable, API-driven, no SSH
- **Cilium** as the only CNI — kube-proxy replacement, L2 LoadBalancer, network policy
- **Multus** for VLAN-attached pods — DMZ workloads and home-automation pods get their own L2 segments via macvlan
- **Rook-Ceph** consuming an external Proxmox-managed Ceph cluster for both block (RBD) and shared (CephFS) storage
- **Envoy Gateway** as the in-cluster ingress, fronted by **Cloudflare Tunnel** for WAN access (zero open inbound ports)
- **Renovate** keeps Helm charts, container images, and Talos versions current — versions ship to the cluster via PRs, not direct `talosctl upgrade`
- **NVIDIA RTX A5000 + A2000** for AI inference; GPU drivers are baked into a dedicated Image Factory schematic so non-GPU nodes stay lean
- **CodeRabbit + Gitleaks + GitGuardian + Flux local validation** on every PR
- **Self-hosted GitHub Actions runners** (`gha-runner-scale-set`) for CI

## 🔧 Hardware

### Proxmox Hosts

| Host | Model | CPU | Threads | RAM | Role |
| --- | --- | --- | --- | --- | --- |
| **baldar** | Dell PowerEdge R730xd | 2× Xeon E5-2697 v3 @ 2.60 GHz | 56 | 128 GB | Compute |
| **heimdall** | Dell PowerEdge R730xd | 2× Xeon E5-2697 v3 @ 2.60 GHz | 56 | 256 GB | Compute + GPU passthrough |
| **odin** | Dell PowerEdge R740xd | 2× Xeon Gold 6148 @ 2.40 GHz | 80 | 128 GB | Compute |
| **thor** | Dell PowerEdge R740xd | 2× Xeon Gold 6148 @ 2.40 GHz | 80 | 256 GB | Compute + GPU passthrough |

### Kubernetes VM Layout

Each Proxmox host runs a slice of the cluster. The mapping is intentionally symmetric so any single host can be evacuated for maintenance without losing quorum.

| Host | Control plane | Workers | GPU |
| --- | --- | --- | --- |
| `baldar` | `k8s-ctrl-1` | `k8s-work-1`, `k8s-work-2`, `k8s-work-3` | — |
| `heimdall` | `k8s-ctrl-2` | `k8s-work-4`, `k8s-work-5`, `k8s-work-6` | NVIDIA RTX A2000 → `k8s-work-4` |
| `odin` | `k8s-ctrl-3` | `k8s-work-7`, `k8s-work-8`, `k8s-work-9` | — |
| `thor` | — | `k8s-work-10`, `k8s-work-11`, `k8s-work-12` | NVIDIA RTX A5000 → `k8s-work-10` |

GPU drivers and the NVIDIA Container Toolkit are baked into a dedicated Image Factory schematic — only GPU workers use it. All other nodes run the lean base schematic.

## 🌐 Networking

- **CNI** — Cilium replaces kube-proxy and handles L2 LoadBalancer announcement onto the home LAN.
- **Secondary CNI** — Multus attaches pods to additional VLANs via macvlan when a workload needs an externally-routable address (DMZ services, IoT integrations).
- **Internal DNS** — `k8s-gateway` answers cluster service hostnames on the LAN; UniFi forwards selected zones to it.
- **External DNS** — `external-dns` reconciles a Cloudflare zone for public records.
- **Ingress** — Envoy Gateway (Gateway API) provides both internal (LAN) and external (WAN-via-Cloudflare-Tunnel) listeners.
- **WAN access** — every public service is reached through a Cloudflare Tunnel; the homelab has no inbound ports open.
- **Service mesh / observability** — Cilium Hubble for flow visibility; Prometheus + Loki for metrics and logs.

## 📦 Applications

<details>
<summary><b>Click to expand the full per-namespace inventory</b></summary>

### Core (`kube-system`)

| App | Purpose |
| --- | --- |
| Cilium | CNI, kube-proxy replacement, L2 LB, network policy |
| Multus | Secondary CNI for VLAN-attached pods |
| CoreDNS | Cluster DNS |
| Metrics Server | Resource metrics for HPA / `kubectl top` |
| Reloader | Automatic rollout on ConfigMap/Secret change |
| Spegel | In-cluster peer-to-peer OCI registry mirror |
| NVIDIA device plugin + DCGM exporter | GPU scheduling and metrics |
| Knative Serving | Serverless runtime (used by KServe) |
| Tetragon (+ cluster policies) | eBPF runtime security |

### GitOps & Secrets

| App | Purpose |
| --- | --- |
| Flux Operator + Flux Instance | GitOps controller |
| External Secrets Operator | Pulls secrets from external stores |
| 1Password Connect | Backing store for ExternalSecrets |
| `cert-manager` | TLS certificate issuance (Let's Encrypt) |

### Networking (`network`)

| App | Purpose |
| --- | --- |
| Envoy Gateway (operator + config) | Gateway API ingress |
| Cloudflare Tunnel | External access without open ports |
| Cloudflare DNS / external-dns | Cloudflare zone automation |
| `k8s-gateway` | Internal DNS for cluster services |
| UniFi DNS integration | LAN DNS sync |
| WireGuard gateway | Site-to-site / egress tunnel |
| Network attachments | DMZ + IoT VLAN definitions for Multus |

### Storage (`rook-ceph`)

| App | Purpose |
| --- | --- |
| Rook-Ceph operator | CSI driver lifecycle |
| Rook-Ceph cluster (external) | RBD + CephFS storage classes backed by Proxmox Ceph |

### Observability (`observability`)

| App | Purpose |
| --- | --- |
| kube-prometheus-stack | Prometheus, Alertmanager, Grafana, exporters |
| Loki | Log aggregation |
| UnPoller | UniFi controller metrics → Prometheus |
| Zabbix | Long-running infrastructure monitoring |

### Security (`security`, `security-system`)

| App | Purpose |
| --- | --- |
| Authentik | SSO / OIDC identity provider |
| Tetragon policies (in `media`) | Runtime security policies for media stack |

### AI & RAG (`ai`, `database`, `mcp`)

| App | Purpose |
| --- | --- |
| Open WebUI | Chat interface |
| LiteLLM (+ Postgres) | OpenAI-compatible LLM gateway |
| KServe | Model serving runtime |
| Model cache | Shared model artifact cache |
| moltbot (OpenClaw) | Claude Code agent runner |
| SearXNG | Search backend for RAG |
| Voice bridge | Voice I/O front-end |
| Qdrant | Vector database |
| FalkorDB | Graph database (Graphiti backend) |
| CloudNative-PG | PostgreSQL operator + cluster |
| Valkey | Redis-compatible cache |
| ToolHive (+ UI, gateway, registry) | MCP server runtime |

### Media (`media`, `downloads`)

| App | Purpose |
| --- | --- |
| Plex | Media server |
| Sonarr / Radarr / Prowlarr | TV, movies, indexer management |
| Overseerr | Media request portal |
| Tautulli | Plex analytics |
| Maintainerr / Cleanuparr | Library hygiene |
| FileFlows | Transcoding orchestration |
| Notifiarr | Notification fan-out |
| Newtarr / Wizarr | Arr companions |
| Bookstack | Self-hosted wiki |
| Uptime Kuma | Service monitoring + public status page |
| qBittorrent / SABnzbd | Torrent + Usenet clients (VPN-isolated) |

### Home Automation (`iot`)

| App | Purpose |
| --- | --- |
| Home Assistant | Home automation platform on a dedicated VLAN |

### Health (`health`)

| App | Purpose |
| --- | --- |
| GlycemicGPT | Personal Dexcom + GPT integration |

### Tools (`tools`, `infra-proxies`)

| App | Purpose |
| --- | --- |
| Homepage | Dashboard for cluster services |
| n8n | Workflow automation |
| GlycemicGPT Discord bot | Notifications front-end |
| Infra proxies (Proxmox, TrueNAS, offsite-backup) | Internal-only proxies for upstream gear |

### CI/CD (`github-actions`)

| App | Purpose |
| --- | --- |
| GitHub Actions runner scale set + controller | Self-hosted Actions runners |

</details>

## 📁 Repository Layout

```text
prox-ops/
├── kubernetes/
│   ├── apps/                # GitOps-managed workloads (one dir per namespace)
│   │   ├── ai/              # Open WebUI, LiteLLM, KServe, MCP, ...
│   │   ├── cache/           # Valkey
│   │   ├── cert-manager/    # cert-manager operator + ClusterIssuers
│   │   ├── database/        # CNPG, Qdrant, FalkorDB
│   │   ├── downloads/       # qBittorrent, SABnzbd
│   │   ├── external-secrets/
│   │   ├── flux-system/     # Flux operator + instance
│   │   ├── github-actions/  # ARC self-hosted runners
│   │   ├── health/          # GlycemicGPT
│   │   ├── infra-proxies/   # Internal proxies for Proxmox, TrueNAS, etc.
│   │   ├── iot/             # Home Assistant
│   │   ├── kube-system/     # Cilium, Multus, GPU plugins, Tetragon, ...
│   │   ├── mcp/             # ToolHive MCP platform
│   │   ├── media/           # Plex + arr stack + Uptime Kuma
│   │   ├── network/         # Envoy, Cloudflare Tunnel, k8s-gateway
│   │   ├── observability/   # Prometheus, Loki, Grafana, UnPoller, Zabbix
│   │   ├── rook-ceph/       # External Ceph CSI
│   │   ├── security/        # Authentik
│   │   └── tools/           # Homepage, n8n
│   ├── components/          # Shared Kustomize components
│   └── flux/                # Flux bootstrap + cluster Kustomization
├── talos/
│   ├── patches/             # Per-role and per-node Talos config patches
│   ├── talconfig.yaml       # talhelper input
│   ├── talenv.yaml          # Talos + Kubernetes versions (Renovate-managed)
│   └── talsecret.sops.yaml  # Encrypted Talos secrets
├── terraform/               # Proxmox VM templates + node provisioning
├── bootstrap/               # Pre-Flux bootstrap manifests
├── scripts/                 # Operational helpers
├── .taskfiles/, Taskfile.yaml  # Task automation
├── .github/                 # Actions workflows, Renovate, CodeRabbit
└── .mise.toml               # Tool version pinning
```

## 🛠️ Day-to-Day

```bash
# Cluster status
kubectl get nodes
kubectl get pods -A
cilium status
flux check

# Force a Flux reconciliation
task reconcile

# Talos
talosctl --nodes <node> dashboard
talosctl --nodes <node> dmesg --follow

# Logs
flux logs --follow
kubectl logs -n <namespace> <pod>

# List all available Tasks
task --list
```

## 🔐 Security Posture

- All secrets committed to Git are encrypted with [SOPS](https://github.com/getsops/sops) using age. Plaintext secret values never reach `main`.
- Runtime secrets are fetched from 1Password via the External Secrets Operator + 1Password Connect — application manifests reference `existingSecret`, never literals.
- WAN ingress is exclusively via Cloudflare Tunnel; the homelab has no inbound ports open to the internet.
- Authentik provides SSO for internal services. cert-manager issues TLS for everything via Let's Encrypt.
- Tetragon enforces eBPF-based runtime policies on sensitive namespaces.
- The repository is public; pushes are gated by Gitleaks, GitGuardian, Flux local validation, and a mandatory pre-push security review.

## 🚀 Bootstrapping a New Cluster

The bootstrap flow follows the [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template) pattern this layout was originally derived from. At a high level:

1. **Provision Proxmox** with a Ceph cluster reachable from your Talos VMs.
2. **Generate Talos VMs** via `terraform/` (creates Image Factory templates and clones VMs from them).
3. **Render Talos configs** with `task configure` (uses `talconfig.yaml` + `talenv.yaml` + `nodes.yaml`).
4. **Bootstrap Talos**: `task bootstrap:talos`.
5. **Bootstrap apps**: `task bootstrap:apps` — installs Flux and seeds the cluster from this repository.
6. **Verify**: `kubectl get nodes`, `cilium status`, `flux check`.

Per-node specifics (MACs, install disks, schematic IDs, addresses) are kept in `nodes.yaml`, which is intentionally **not** committed — see `.gitignore` for the local-only files you'll need to populate.

## 🔄 GitOps Workflow

Every change to a workload is a pull request:

1. Edit YAML under `kubernetes/`.
2. Open a PR — CI runs Flux local validation, secret scanning, and CodeRabbit review.
3. Merge to `main` → Flux reconciles within ~30 seconds.
4. Renovate keeps Helm charts, container images, and Talos versions current via automated PRs.

Direct `kubectl` against the cluster is limited to read-only inspection (`get`, `describe`, `logs`); the only state-mutating operation that bypasses Git is an emergency rollback, and only with explicit approval.

## 🙏 Credits

- Cluster layout, Renovate configuration, and Taskfile patterns are adapted from [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template).
- README structure inspired by [`onedr0p/home-ops`](https://github.com/onedr0p/home-ops).
- Talos image schematics are produced via the [Sidero Image Factory](https://factory.talos.dev/).

External documentation: [Talos](https://www.talos.dev/) · [Kubernetes](https://kubernetes.io/) · [Flux](https://fluxcd.io/) · [Cilium](https://docs.cilium.io/) · [Rook](https://rook.io/) · [Multus](https://github.com/k8snetworkplumbingwg/multus-cni) · [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) · [SOPS](https://github.com/getsops/sops) · [1Password Connect](https://developer.1password.com/docs/connect/).

## 📜 License

Published under the MIT License — the same terms as the upstream [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template) it was derived from.
