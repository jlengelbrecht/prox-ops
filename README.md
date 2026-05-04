<div align="center">

# 🚀 &nbsp; Prox-Ops &nbsp; 🏠

_A production-grade Kubernetes homelab on Proxmox — Talos Linux, Flux GitOps, Cilium, Rook-Ceph, and GPU-accelerated AI._

</div>

---

## 📖 Overview

Prox-Ops is the complete infrastructure-as-code definition for a 15-node Kubernetes cluster running on a four-host Proxmox VE cluster. It is the operational backbone of a homelab that runs media services, home automation, observability tooling, and a self-hosted AI/RAG stack on consumer GPUs.

The repository's design goals are:

- **Reproducibility** — every node, network, and workload is declared in Git. The cluster can be rebuilt from a clean Proxmox install.
- **Cattle, not pets** — Talos nodes are immutable and replaced rather than patched. Image Factory schematics are the single source of truth for kernel args and drivers.
- **GitOps end-to-end** — Flux v2 reconciles every workload from this repository. Direct `kubectl apply` is reserved for emergency rollback only.
- **Encrypted by default** — secrets live in Git as SOPS+age ciphertext, fed into the cluster via External Secrets Operator + 1Password Connect.

## 🧱 Stack at a Glance

| Layer | Technology | Notes |
| --- | --- | --- |
| Hypervisor | **Proxmox VE 8.x** | 4 hosts: `baldar`, `heimdall`, `odin`, `thor` |
| Node OS | **Talos Linux v1.12.4** | UKI/SDBoot, Image Factory schematics |
| Kubernetes | **v1.34.1** | 3 control-plane + 12 workers (HA via VIP) |
| GitOps | **Flux v2** | Reconciles `kubernetes/apps` from `main` |
| CNI (primary) | **Cilium** | kube-proxy replacement, L2 LoadBalancer |
| CNI (secondary) | **Multus** | Per-pod VLAN attachments (DMZ, IoT) |
| Ingress | **Envoy Gateway** | Internal + external HTTP routes |
| Storage | **Rook-Ceph** (external) | Talks to Proxmox-managed Ceph |
| External access | **Cloudflare Tunnel** | No inbound ports on the WAN |
| Internal DNS | **k8s-gateway** | LAN-resolvable cluster hostnames |
| External DNS | **external-dns** | Cloudflare zone automation |
| Secrets | **SOPS + age**, **External Secrets + 1Password** | Encrypted-at-rest, fetched at deploy time |
| GPU runtime | **NVIDIA device plugin + DCGM** | Time-slicing on RTX A5000, A2000 |
| Image cache | **Spegel** | Peer-to-peer OCI mirror |

## 🖥️ Hardware

### Proxmox Hosts

| Host | Role |
| --- | --- |
| `baldar` | Compute — runs `k8s-ctrl-1`, `k8s-work-1..3` |
| `heimdall` | Compute + GPU passthrough — runs `k8s-ctrl-2`, `k8s-work-4` (RTX A2000), `k8s-work-5..6` |
| `odin` | Compute — runs `k8s-ctrl-3`, `k8s-work-7..9` |
| `thor` | Compute + GPU passthrough — runs `k8s-work-10` (RTX A5000), `k8s-work-11..12` |

### Kubernetes Nodes

The cluster runs 3 control-plane nodes (`k8s-ctrl-1..3`) and 12 worker nodes (`k8s-work-1..12`) on `10.20.67.0/24`. The Kubernetes API is served on a VIP at `10.20.67.20:6443`.

### GPU Resources

| Node | GPU | VRAM | Primary Use |
| --- | --- | --- | --- |
| `k8s-work-4` | NVIDIA RTX A2000 | 12 GB | Lightweight inference, embeddings |
| `k8s-work-10` | NVIDIA RTX A5000 | 24 GB | Ollama, LiteLLM, voice models |

GPU drivers and the NVIDIA Container Toolkit are baked into a dedicated Image Factory schematic — only the GPU nodes use it. All other workers run the lean base schematic.

## 🌐 Network

### VLANs

| VLAN | Purpose | Subnet | Gateway |
| --- | --- | --- | --- |
| native | Cluster nodes | `10.20.66.0/23` | `10.20.66.1` |
| 81 | DMZ — public-facing pods (macvlan via Multus) | `10.20.81.0/24` | `10.20.81.1` |
| 62 | IoT — Home Assistant + smart devices | `10.20.62.0/23` | `10.20.62.1` |

### Cluster CIDRs

| Range | Purpose |
| --- | --- |
| `10.42.0.0/16` | Pod network |
| `10.43.0.0/16` | Service network |

### Named Service IPs

| Service | IP | Type |
| --- | --- | --- |
| Kubernetes API | `10.20.67.20` | Talos VIP |
| `k8s-gateway` (internal DNS) | `10.20.67.21` | Cilium LB |
| Envoy Gateway (internal) | `10.20.67.22` | Cilium LB |
| Envoy Gateway (external / Cloudflare Tunnel) | `10.20.67.23` | Cilium LB |

### LoadBalancer Pools

| Pool | Range |
| --- | --- |
| Native | `10.20.66.0/23` (services pull from here by default) |
| DMZ (VLAN 81) | `10.20.81.100`–`10.20.81.150` |
| IoT (VLAN 62) | `10.20.62.100`–`10.20.62.150` |

## 📦 Applications

<details>
<summary><b>Click to expand the full inventory</b></summary>

### Core Infrastructure (`kube-system`)

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
| Home Assistant | Home automation platform on VLAN 62 |

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
│   │   ├── iot/             # Home Assistant (VLAN 62)
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
talosctl --nodes 10.20.67.1 dashboard
talosctl --nodes 10.20.67.1 dmesg --follow

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

This repo is opinionated to a specific homelab, but the bootstrap flow is the same one documented by [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template) — the project this layout was originally derived from. At a high level:

1. **Provision Proxmox** with a Ceph cluster reachable from your Talos VMs.
2. **Generate Talos VMs** via `terraform/` (creates Image Factory templates and clones VMs from them).
3. **Render Talos configs** with `task configure` (uses `talconfig.yaml` + `talenv.yaml` + `nodes.yaml`).
4. **Bootstrap Talos**: `task bootstrap:talos`.
5. **Bootstrap apps**: `task bootstrap:apps` — installs Flux and seeds the cluster from this repository.
6. **Verify**: `kubectl get nodes`, `cilium status`, `flux check`.

Per-node specifics (MACs, install disks, schematic IDs) are kept in `nodes.yaml`, which is intentionally **not** committed — see `.gitignore` for the local-only files you'll need to populate.

## 🔄 GitOps Workflow

Every change to a workload is a pull request:

1. Edit YAML under `kubernetes/`.
2. Open a PR — CI runs Flux local validation, secret scanning, and CodeRabbit review.
3. Merge to `main` → Flux reconciles within ~30 seconds.
4. Renovate keeps Helm charts, container images, and Talos versions current via automated PRs.

Direct `kubectl` against the cluster is limited to read-only inspection (`get`, `describe`, `logs`); the only state-mutating operation that bypasses Git is an emergency rollback, and only with explicit approval.

## 🙏 Credits

- Cluster layout, Renovate configuration, and Taskfile patterns are adapted from [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template).
- Talos image schematics are produced via the [Sidero Image Factory](https://factory.talos.dev/).
- Inspiration for the README structure: [`onedr0p/home-ops`](https://github.com/onedr0p/home-ops).

External documentation: [Talos](https://www.talos.dev/) · [Kubernetes](https://kubernetes.io/) · [Flux](https://fluxcd.io/) · [Cilium](https://docs.cilium.io/) · [Rook](https://rook.io/) · [Multus](https://github.com/k8snetworkplumbingwg/multus-cni) · [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) · [SOPS](https://github.com/getsops/sops) · [1Password Connect](https://developer.1password.com/docs/connect/).

## 📜 License

This repository is published under the MIT License — the same terms as the upstream [`onedr0p/cluster-template`](https://github.com/onedr0p/cluster-template) it was derived from.
