# Prox-Ops - Kubernetes Homelab on Proxmox

A production-grade Kubernetes homelab cluster running on Proxmox with Talos Linux, Flux GitOps, multi-VLAN networking, and Ceph storage integration.

## Overview

This repository contains the complete infrastructure-as-code for a highly available Kubernetes cluster with:

- **High Availability**: 3-node control plane with Talos Linux
- **Advanced Networking**: Cilium CNI with Multus for multi-VLAN support (DMZ, IoT)
- **Persistent Storage**: Rook-Ceph integration with Proxmox Ceph cluster
- **GitOps**: Flux v2 for declarative cluster and application management
- **Security**: SOPS encryption, network policies, RBAC
- **External Access**: Cloudflare Tunnel for secure external connectivity

## Architecture

### Infrastructure

```
Proxmox Cluster
├── Control Plane Nodes: 3 VMs (10.20.67.1-3)
├── Worker Nodes: 12 VMs (10.20.67.4-15)
├── Ceph Storage Cluster: Managed via Proxmox
└── Network: 10.20.67.0/24 (main) + VLAN 81 (DMZ) + VLAN 62 (IoT)
```

### Technology Stack

| Component | Technology | Purpose |
|-----------|------------|---------|
| **Operating System** | Talos Linux 1.11.3 | Immutable, API-driven Kubernetes OS |
| **Container Runtime** | containerd | Built into Talos |
| **CNI (Primary)** | Cilium | Pod networking, LoadBalancer, observability |
| **CNI (Secondary)** | Multus | Multi-network (VLAN) support |
| **GitOps** | Flux v2 | Continuous deployment from Git |
| **Storage** | Rook-Ceph | Persistent storage via Proxmox Ceph |
| **Ingress** | Envoy Gateway | Traffic routing and TLS termination |
| **External Access** | Cloudflare Tunnel | Secure external connectivity |
| **Secrets** | SOPS + Age | Encrypted secrets in Git |
| **DNS (Internal)** | k8s-gateway | Internal DNS for services |
| **DNS (External)** | external-dns | Cloudflare DNS automation |

## Network Architecture

```
┌─────────────────────────────────────────────────────┐
│  Kubernetes Cluster (10.20.67.0/24)                 │
│                                                      │
│  ┌───────────────────────────────────────────────┐  │
│  │  Standard Pods                                │  │
│  │  - eth0: Cilium network (10.42.0.0/16)       │  │
│  │  - Default: All cluster services             │  │
│  └───────────────────────────────────────────────┘  │
│                                                      │
│  ┌───────────────────────────────────────────────┐  │
│  │  DMZ Pods (Public-Facing)                     │  │
│  │  - eth0: Cilium (cluster communication)      │  │
│  │  - net1: VLAN 81 (external access)           │  │
│  └───────────────────────────────────────────────┘  │
│                                                      │
│  ┌───────────────────────────────────────────────┐  │
│  │  IoT Pods (Home Automation)                   │  │
│  │  - eth0: Cilium (cluster communication)      │  │
│  │  - net1: VLAN 62 (IoT devices)               │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

## Storage Architecture

```
Kubernetes Cluster (Rook-Ceph CSI)
           │
           │ Ceph Protocol
           ▼
Proxmox Ceph Cluster (External)
  ├── RBD Pool: kubernetes-rbd (Block storage)
  └── CephFS: kubernetes-cephfs (Shared storage)
```

## Repository Structure

```
prox-ops/
├── kubernetes/
│   ├── apps/                     # Application deployments
│   │   ├── cert-manager/         # TLS certificates
│   │   ├── flux-system/          # Flux operator
│   │   ├── kube-system/          # Core system components
│   │   │   ├── cilium/           # Primary CNI
│   │   │   ├── multus/           # Multi-network CNI
│   │   │   ├── coredns/          # DNS
│   │   │   └── ...
│   │   ├── network/              # Networking components
│   │   ├── rook-ceph/            # Storage orchestration
│   │   ├── dmz/                  # DMZ VLAN workloads
│   │   └── iot/                  # IoT VLAN workloads
│   ├── components/               # Shared components
│   └── flux/                     # Flux configuration
├── talos/                        # Talos configuration
│   ├── patches/                  # Talos patches
│   │   ├── global/               # All nodes
│   │   ├── controller/           # Controllers only
│   │   └── worker/               # Workers only
│   ├── talconfig.yaml            # Talhelper config
│   └── talsecret.sops.yaml       # Encrypted secrets
├── bootstrap/                    # Pre-Flux bootstrap
├── .github/workflows/            # CI/CD pipelines
├── cluster.yaml                  # Cluster configuration
├── nodes.yaml                    # Node definitions
├── Taskfile.yaml                 # Task automation
└── .mise.toml                    # Developer environment
```

## Quick Start

### Prerequisites

- 15 Talos Linux VMs running on Proxmox (10.20.67.1-15)
- Proxmox Ceph cluster configured
- Cloudflare account with domain
- CachyOS workstation (or Linux with Nix)

### Installation (90 minutes)

**Detailed guides available**:
- [QUICKSTART.md](./QUICKSTART.md) - Fast-track guide (experienced users)
- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) - Comprehensive step-by-step guide
- [DEPENDENCIES.md](./DEPENDENCIES.md) - Tool installation guide

**Quick summary**:

```bash
# 1. Install dependencies (5 min)
nix profile install nixpkgs#mise
eval "$(mise activate bash)"
cd /home/devbox/repos/jlengelbrecht/prox-ops/
mise trust && pip install pipx && mise install

# 2. Initialize repository (10 min)
task init

# 3. Discover node information (30 min)
nmap -Pn -n -p 50000 10.20.67.0/24
# For each node: talosctl disks/links --nodes <ip> --insecure

# 4. Create Talos schematic at https://factory.talos.dev/ (5 min)

# 5. Configure cluster (20 min)
# Edit cluster.yaml and nodes.yaml
task configure

# 6. Create Cloudflare tunnel (10 min)
cloudflared tunnel login
cloudflared tunnel create --credentials-file cloudflare-tunnel.json kubernetes

# 7. Commit and bootstrap (30 min)
git add -A && git commit -m "chore: initial configuration" && git push
task bootstrap:talos
git add -A && git commit -m "chore: add encrypted secrets" && git push
task bootstrap:apps

# 8. Verify cluster (5 min)
kubectl get nodes
cilium status
flux check
```

## Advanced Features

### Multi-VLAN Networking

Deploy workloads to specific VLANs for network isolation:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dmz-app
  annotations:
    k8s.v1.cni.cncf.io/networks: dmz-vlan81  # Attach to DMZ VLAN
spec:
  containers:
  - name: app
    image: nginx
```

See [VLAN_SETUP.md](./VLAN_SETUP.md) for detailed configuration.

### Persistent Storage

Request storage from Proxmox Ceph cluster:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-data
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: rook-ceph-block
  resources:
    requests:
      storage: 10Gi
```

See [STORAGE_SETUP.md](./STORAGE_SETUP.md) for Rook-Ceph configuration.

### GitOps Application Deployment

Add applications by committing Kubernetes manifests:

```bash
# 1. Create app structure
mkdir -p kubernetes/apps/myapp/myapp/app

# 2. Add Flux Kustomization (ks.yaml)
# 3. Add HelmRelease or manifests
# 4. Commit and push

git add kubernetes/apps/myapp/
git commit -m "feat: add myapp"
git push

# 5. Flux automatically deploys
task reconcile
```

## Included Applications

After bootstrap, the following are deployed:

**Core Infrastructure**:
- Cilium (CNI, LoadBalancer, NetworkPolicy)
- CoreDNS (Cluster DNS)
- Metrics Server (Resource metrics)
- Spegel (Local OCI mirror)
- Reloader (ConfigMap/Secret auto-reload)

**Certificates**:
- cert-manager (TLS certificate management)
- ClusterIssuers for Let's Encrypt

**Networking**:
- Envoy Gateway (Ingress controller)
- k8s-gateway (Internal DNS)
- external-dns (Cloudflare DNS automation)
- Cloudflare Tunnel (External access)

**GitOps**:
- Flux Operator (Flux management)
- Flux Instance (Git synchronization)

## Customization

### Add New Application

1. Create directory: `kubernetes/apps/<namespace>/<app>/app/`
2. Add manifests (HelmRelease, Deployment, etc.)
3. Create Flux Kustomization: `kubernetes/apps/<namespace>/<app>/ks.yaml`
4. Update namespace kustomization: `kubernetes/apps/<namespace>/kustomization.yaml`
5. Commit and push

### Add Monitoring Stack

See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md#task-3-set-up-monitoring) for Prometheus, Grafana, and Loki setup.

### Configure Backups

Deploy Velero for cluster and volume backups. See storage guide for details.

## Maintenance

### Update Cluster

```bash
# Update Flux
flux install --export > kubernetes/flux/install.yaml
git commit -am "chore: update flux" && git push

# Update Talos
talosctl upgrade --nodes <nodes> --image <new-image>

# Update applications (via Renovate PR)
# Merge Renovate PRs to update Helm charts and images
```

### Troubleshooting

**Nodes not Ready**:
```bash
kubectl describe node <node>
kubectl logs -n kube-system -l app.kubernetes.io/name=cilium
```

**Flux not syncing**:
```bash
flux logs --follow
flux reconcile source git flux-system
```

**Storage issues**:
```bash
kubectl logs -n rook-ceph -l app=csi-rbdplugin
kubectl describe pvc <pvc-name>
```

See [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md#appendix-b-troubleshooting-guide) for comprehensive troubleshooting.

## Network Allocations

| Purpose | IP/Range | Type |
|---------|----------|------|
| Control Nodes | 10.20.67.1-3 | Static |
| Worker Nodes | 10.20.67.4-15 | Static |
| Kubernetes API | 10.20.67.10 | VIP |
| K8s Gateway (DNS) | 10.20.67.20 | LoadBalancer |
| Internal Gateway | 10.20.67.21 | LoadBalancer |
| External Gateway | 10.20.67.22 | LoadBalancer |
| Service IPs | 10.20.67.23-99 | LoadBalancer Pool |
| DHCP Pool (Optional) | 10.20.67.100-200 | Dynamic |
| Pod Network | 10.42.0.0/16 | CIDR |
| Service Network | 10.43.0.0/16 | CIDR |

## Security

- **Secrets**: All secrets encrypted with SOPS using Age encryption
- **Network Policies**: Namespace isolation via NetworkPolicies
- **RBAC**: Least-privilege access controls
- **Pod Security**: Pod Security Standards enforced per namespace
- **TLS**: Automated TLS certificates via cert-manager
- **External Access**: Secured via Cloudflare Tunnel (no open ports)

## Backup Strategy

1. **Git Repository**: All configurations in version control
2. **SOPS Age Key**: Backed up securely offline (required to decrypt secrets)
3. **Talos Config**: `talosconfig` backed up (required to manage nodes)
4. **Kubeconfig**: Generated from cluster (recoverable)
5. **Volume Snapshots**: CSI snapshots for persistent volumes
6. **Velero**: Full cluster backups (optional, to be deployed)

## Documentation

- [QUICKSTART.md](./QUICKSTART.md) - Fast setup guide (90 minutes)
- [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) - Comprehensive guide with architecture details
- [DEPENDENCIES.md](./DEPENDENCIES.md) - Tool installation and troubleshooting
- [VLAN_SETUP.md](./VLAN_SETUP.md) - Multi-VLAN networking configuration
- [STORAGE_SETUP.md](./STORAGE_SETUP.md) - Rook-Ceph and Proxmox integration

## Useful Commands

```bash
# Task automation
task --list                    # List all tasks
task reconcile                 # Force Flux sync

# Cluster status
kubectl get nodes              # Node status
kubectl get pods -A            # All pods
cilium status                  # Cilium health
flux check                     # Flux status

# Talos
talosctl dashboard             # Node dashboard
talosctl dmesg --follow        # System logs

# Logs
flux logs --follow             # Flux logs
kubectl logs -n <ns> <pod>     # Pod logs
```

## Credits and References

This cluster is based on the excellent [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template) architecture, adapted for:
- Multi-VLAN networking (DMZ/IoT separation)
- Proxmox Ceph integration
- Custom homelab requirements

**External Documentation**:
- [Talos Linux](https://www.talos.dev/)
- [Flux](https://fluxcd.io/)
- [Cilium](https://docs.cilium.io/)
- [Rook](https://rook.io/)
- [Multus CNI](https://github.com/k8snetworkplumbingwg/multus-cni)

## License

This repository: MIT License

Template based on [onedr0p/cluster-template](https://github.com/onedr0p/cluster-template) which is also MIT licensed.

## Support

For issues and questions:
1. Check [IMPLEMENTATION_PLAN.md](./IMPLEMENTATION_PLAN.md) troubleshooting section
2. Review Flux/Talos/Cilium logs
3. Consult official documentation for respective tools
4. Open an issue in this repository

## Project Status

- [x] Base cluster deployment (Talos, Kubernetes, Flux)
- [x] Core networking (Cilium)
- [x] Certificate management (cert-manager)
- [x] External access (Cloudflare Tunnel)
- [ ] Multi-VLAN networking (Multus) - Documentation ready, pending deployment
- [ ] Storage (Rook-Ceph) - Documentation ready, pending deployment
- [ ] Monitoring (Prometheus, Grafana, Loki) - Planned
- [ ] Backups (Velero) - Planned
- [ ] CI/CD pipelines - Planned

---

**Built with**: Talos Linux • Kubernetes • Flux • Cilium • Rook • Proxmox
