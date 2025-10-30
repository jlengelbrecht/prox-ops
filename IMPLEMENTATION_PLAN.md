# Prox-Ops Kubernetes Homelab - Comprehensive Implementation Plan

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Infrastructure Architecture](#2-infrastructure-architecture)
3. [Repository Structure](#3-repository-structure)
4. [Dependency Installation](#4-dependency-installation)
5. [Repository Setup](#5-repository-setup)
6. [Network Configuration](#6-network-configuration)
7. [Storage Configuration](#7-storage-configuration)
8. [Talos Configuration](#8-talos-configuration)
9. [Cloudflare Setup](#9-cloudflare-setup)
10. [Bootstrap Process](#10-bootstrap-process)
11. [GitOps and Application Deployment](#11-gitops-and-application-deployment)
12. [Verification and Testing](#12-verification-and-testing)
13. [Post-Installation Tasks](#13-post-installation-tasks)

---

## 1. Project Overview

### Goals
- Deploy a highly available Kubernetes cluster on Proxmox VMs running Talos Linux
- Implement multi-VLAN networking for workload isolation (DMZ VLAN 81, IoT VLAN 62)
- Integrate Proxmox Ceph for highly available persistent storage
- Use GitOps (Flux) for cluster and application management
- Leverage Cloudflare for external access and DNS

### Key Technologies
- **OS**: Talos Linux (immutable, API-driven Kubernetes OS)
- **Container Runtime**: containerd (built into Talos)
- **CNI**: Cilium (with multi-network support via Multus)
- **GitOps**: Flux v2
- **Storage**: Rook-Ceph (leveraging Proxmox Ceph cluster)
- **Ingress**: Envoy Gateway
- **External Access**: Cloudflare Tunnel
- **Secrets Management**: SOPS + Age encryption

### Infrastructure Details

**Talos VMs:**
- Control Plane: 10.20.67.1, 10.20.67.2, 10.20.67.3 (3 nodes for HA)
- Workers: 10.20.67.4 - 10.20.67.15 (12 nodes)

**Network:**
- Main Cluster Network: 10.20.67.0/24
- DMZ VLAN: 81 (for public-facing workloads)
- IoT VLAN: 62 (for IoT workloads like Home Assistant)
- Worker nodes have additional NICs for VLAN-specific traffic

**Storage:**
- Proxmox Ceph cluster (managed via Proxmox UI)
- Plan: Deploy Rook-Ceph in Kubernetes, connecting to external Proxmox Ceph cluster

---

## 2. Infrastructure Architecture

### Network Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Proxmox Cluster                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Talos Kubernetes Cluster                                 │  │
│  │                                                            │  │
│  │  Controllers (10.20.67.1-3)                               │  │
│  │  ┌──────┐ ┌──────┐ ┌──────┐                              │  │
│  │  │ CP-1 │ │ CP-2 │ │ CP-3 │  VIP: 10.20.67.10 (API)      │  │
│  │  └───┬──┘ └───┬──┘ └───┬──┘                              │  │
│  │      │        │        │                                  │  │
│  │      └────────┴────────┘                                  │  │
│  │             │                                             │  │
│  │  ┌──────────┴──────────┐                                 │  │
│  │  │                     │                                  │  │
│  │  │  Workers (10.20.67.4-15)                              │  │
│  │  │  ┌────────────┐  ┌────────────┐     ┌────────────┐  │  │
│  │  │  │  Worker-1  │  │  Worker-2  │ ... │  Worker-12 │  │  │
│  │  │  │            │  │            │     │            │  │  │
│  │  │  │ eth0: Main │  │ eth0: Main │     │ eth0: Main │  │  │
│  │  │  │ eth1: DMZ  │  │ eth1: DMZ  │     │ eth1: DMZ  │  │  │
│  │  │  │ eth2: IoT  │  │ eth2: IoT  │     │ eth2: IoT  │  │  │
│  │  │  └────────────┘  └────────────┘     └────────────┘  │  │
│  │  └──────────────────────────────────────────────────────┘  │
│  │                                                            │  │
│  │  Cilium (Primary CNI)                                     │  │
│  │  - Pod network: 10.42.0.0/16                             │  │
│  │  - Service network: 10.43.0.0/16                         │  │
│  │  - LoadBalancer support (L2 announcements)               │  │
│  │                                                            │  │
│  │  Multus (Secondary CNI)                                   │  │
│  │  - NetworkAttachmentDefinitions for DMZ/IoT              │  │
│  │  - Macvlan/bridge interfaces to VLANs                    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Ceph Storage Cluster                                     │  │
│  │  - Managed via Proxmox UI                                 │  │
│  │  - Rook-Ceph connects as external cluster                │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Storage Architecture

**Option A: External Ceph Cluster (Recommended)**
- Proxmox Ceph runs independently
- Rook-Ceph configured to connect to external Ceph cluster
- Pros: Lower resource usage in K8s, simpler management
- Cons: Need to configure external cluster connection

**Option B: Independent Rook-Ceph Cluster**
- Deploy separate Ceph cluster within Kubernetes
- Pros: Full GitOps management, independent of Proxmox
- Cons: Higher resource usage, complexity

**Recommendation**: Start with Option A (external Ceph) for better resource utilization in homelab environment.

### Service Load Balancer IPs

These will be allocated from the main cluster network (10.20.67.0/24):

- Kubernetes API VIP: 10.20.67.10
- K8s Gateway (Internal DNS): 10.20.67.20
- Internal Gateway (Envoy): 10.20.67.21
- External Gateway (Cloudflare): 10.20.67.22

---

## 3. Repository Structure

The following structure will be created in `/home/devbox/repos/jlengelbrecht/prox-ops/`:

```
prox-ops/
├── .github/
│   └── workflows/
│       ├── flux-local.yaml          # Flux manifest validation
│       └── label-sync.yaml          # GitHub label management
├── .taskfiles/
│   ├── bootstrap/
│   │   └── Taskfile.yaml            # Bootstrap tasks
│   ├── talos/
│   │   └── Taskfile.yaml            # Talos management tasks
│   └── template/
│       ├── Taskfile.yaml            # Template rendering tasks
│       └── resources/
│           ├── cluster.schema.cue   # Cluster config validation
│           └── nodes.schema.cue     # Nodes config validation
├── .vscode/
│   └── extensions.json              # Recommended VS Code extensions
├── bootstrap/
│   ├── helmfile.d/                  # Pre-Flux Helm deployments
│   ├── github-deploy-key.sops.yaml  # GitHub deploy key (encrypted)
│   └── sops-age.sops.yaml           # Age key for SOPS (encrypted)
├── kubernetes/
│   ├── apps/
│   │   ├── cert-manager/            # TLS certificate management
│   │   ├── flux-system/             # Flux operator and instance
│   │   ├── kube-system/             # Core system components
│   │   │   ├── cilium/              # Primary CNI
│   │   │   ├── multus/              # Multi-network CNI
│   │   │   ├── coredns/             # DNS
│   │   │   ├── metrics-server/      # Metrics API
│   │   │   ├── reloader/            # ConfigMap/Secret reloader
│   │   │   └── spegel/              # Local OCI mirror
│   │   ├── network/                 # Network components
│   │   │   ├── cloudflare-dns/      # External DNS
│   │   │   ├── cloudflare-tunnel/   # Cloudflare tunnel
│   │   │   ├── envoy-gateway/       # Ingress controller
│   │   │   └── k8s-gateway/         # Internal DNS
│   │   ├── rook-ceph/               # Storage orchestration
│   │   ├── monitoring/              # Prometheus, Grafana, etc.
│   │   ├── dmz/                     # DMZ VLAN workloads
│   │   └── iot/                     # IoT VLAN workloads
│   ├── components/
│   │   └── sops/
│   │       └── cluster-secrets.sops.yaml  # Cluster-wide secrets
│   └── flux/
│       └── cluster/
│           └── ks.yaml              # Root Kustomization
├── talos/
│   ├── clusterconfig/               # Generated Talos configs
│   ├── patches/
│   │   ├── global/                  # Patches for all nodes
│   │   ├── controller/              # Controller-specific patches
│   │   └── worker/                  # Worker-specific patches
│   ├── talconfig.yaml               # Talhelper configuration
│   ├── talenv.yaml                  # Talhelper environment
│   └── talsecret.sops.yaml          # Talos secrets (encrypted)
├── templates/                        # Jinja2 templates (temporary)
├── scripts/                          # Utility scripts
├── .editorconfig                     # Editor configuration
├── .gitattributes                    # Git attributes
├── .gitignore                        # Git ignore patterns
├── .mise.toml                        # Developer environment
├── .renovaterc.json5                 # Renovate configuration
├── .sops.yaml                        # SOPS configuration
├── age.key                           # Age encryption key (gitignored)
├── cluster.yaml                      # Cluster configuration
├── cloudflare-tunnel.json            # Cloudflare tunnel credentials
├── github-deploy.key                 # GitHub deploy key (gitignored)
├── github-push-token.txt             # GitHub webhook token (gitignored)
├── kubeconfig                        # Kubernetes config (gitignored)
├── makejinja.toml                    # Template rendering config
├── nodes.yaml                        # Node definitions
├── README.md                         # Main documentation
├── IMPLEMENTATION_PLAN.md            # This file
└── Taskfile.yaml                     # Main task automation
```

---

## 4. Dependency Installation

### Required Tools

The following tools are needed for cluster management:

| Tool | Version | Purpose |
|------|---------|---------|
| mise | latest | Developer environment manager |
| python | 3.14+ | Required for makejinja |
| makejinja | 2.8.1+ | Template rendering |
| talhelper | 3.0.38+ | Talos configuration helper |
| talosctl | 1.11.3+ | Talos CLI |
| kubectl | 1.34.0+ | Kubernetes CLI |
| flux | 2.7.2+ | Flux GitOps CLI |
| cilium-cli | 0.18.7+ | Cilium CLI |
| helm | 3.19.0+ | Helm package manager |
| helmfile | 1.1.7+ | Helm chart deployment tool |
| sops | 3.11.0+ | Secrets encryption |
| age | 1.2.1+ | Age encryption |
| gh | 2.82.0+ | GitHub CLI |
| cloudflared | 2025.10.0+ | Cloudflare tunnel |
| task | 3.45.4+ | Task automation |
| jq | 1.8.1+ | JSON processor |
| yq | 4.48.1+ | YAML processor |
| kustomize | 5.7.1+ | Kubernetes manifest customization |
| kubeconform | 0.7.0+ | Kubernetes manifest validation |
| cue | 0.14.2+ | Configuration validation |

### Currently Installed

Based on the system check:
- kubectl: ✓ (installed via Nix)
- helm: ✓ (installed via Nix)

### Missing Dependencies

The following need to be installed:
- mise (environment manager)
- All tools managed by mise (listed in .mise.toml)

### Installation Steps

#### Step 1: Install Mise via Nix

Mise is the central tool that will manage all other dependencies. It needs to be installed via Nix on CachyOS:

```bash
# Add mise to your Nix configuration or install directly
nix profile install nixpkgs#mise
```

**Alternative**: Install using curl (standalone):
```bash
curl https://mise.jdx.dev/install.sh | sh
```

#### Step 2: Configure Mise Shell Integration

Add to `~/.bashrc` or `~/.zshrc`:
```bash
# Mise activation
eval "$(mise activate bash)"  # or zsh
```

Reload shell:
```bash
source ~/.bashrc  # or ~/.zshrc
```

#### Step 3: Verify Mise Installation

```bash
mise --version
```

#### Step 4: Install Tool Dependencies via Mise

After copying the `.mise.toml` file to your prox-ops repo:

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
mise trust
pip install pipx  # If not already installed
mise install
```

This will install all tools defined in `.mise.toml`.

#### Step 5: Verify Tool Installation

```bash
# Check key tools
talosctl version
kubectl version --client
flux version
sops --version
age --version
task --version
```

### Optional: Manual Installation via Nix

If mise installation fails or you prefer Nix-managed packages:

```nix
# Add to your home-manager configuration
home.packages = with pkgs; [
  talosctl
  kubectl
  kubernetes-helm
  fluxcd
  sops
  age
  go-task
  jq
  yq
  kustomize
  cloudflared
  cilium-cli
  github-cli
  # Note: Some tools may not be in nixpkgs
];
```

---

## 5. Repository Setup

### Step 1: Copy Essential Files from Template

Navigate to your repository:
```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
```

Copy directory structure (excluding .git):
```bash
# Copy taskfiles
cp -r /home/devbox/repos/onedr0p/cluster-template/.taskfiles .

# Copy templates
cp -r /home/devbox/repos/onedr0p/cluster-template/templates .

# Copy scripts
cp -r /home/devbox/repos/onedr0p/cluster-template/scripts .

# Copy configuration files
cp /home/devbox/repos/onedr0p/cluster-template/.editorconfig .
cp /home/devbox/repos/onedr0p/cluster-template/.gitattributes .
cp /home/devbox/repos/onedr0p/cluster-template/.gitignore .
cp /home/devbox/repos/onedr0p/cluster-template/.mise.toml .
cp /home/devbox/repos/onedr0p/cluster-template/.renovaterc.json5 .
cp /home/devbox/repos/onedr0p/cluster-template/.shellcheckrc .
cp /home/devbox/repos/onedr0p/cluster-template/makejinja.toml .
cp /home/devbox/repos/onedr0p/cluster-template/Taskfile.yaml .

# Copy sample configs
cp /home/devbox/repos/onedr0p/cluster-template/cluster.sample.yaml .
cp /home/devbox/repos/onedr0p/cluster-template/nodes.sample.yaml .
```

### Step 2: Copy GitHub Workflows (Optional)

```bash
mkdir -p .github/workflows
cp /home/devbox/repos/onedr0p/cluster-template/.github/workflows/flux-local.yaml .github/workflows/
cp /home/devbox/repos/onedr0p/cluster-template/.github/workflows/label-sync.yaml .github/workflows/
```

### Step 3: Copy VS Code Configuration (Optional)

```bash
mkdir -p .vscode
cp -r /home/devbox/repos/onedr0p/cluster-template/.vscode/* .vscode/
```

### Step 4: Initialize Configuration Files

```bash
task init
```

This command will:
- Rename `cluster.sample.yaml` to `cluster.yaml`
- Rename `nodes.sample.yaml` to `nodes.yaml`
- Generate an Age encryption key (`age.key`)
- Generate a GitHub deploy key pair (`github-deploy.key`, `github-deploy.key.pub`)
- Generate a GitHub push token (`github-push-token.txt`)

### Step 5: Verify File Structure

```bash
tree -L 2 -a
```

Ensure you have:
- `.mise.toml`
- `Taskfile.yaml`
- `cluster.yaml`
- `nodes.yaml`
- `age.key`
- `makejinja.toml`
- `.taskfiles/`
- `templates/`
- `scripts/`

---

## 6. Network Configuration

### Overview

The cluster will use:
1. **Cilium** as the primary CNI for pod networking
2. **Multus** as a meta-CNI to attach additional network interfaces to pods
3. **MacVLAN** or **bridge** mode for VLAN interfaces

### Network Design

```
Pod Network Flow:

┌─────────────────────┐
│   Standard Pod      │
│  (default network)  │
└──────────┬──────────┘
           │ eth0 (Cilium)
           └─> 10.42.0.0/16 (Pod CIDR)

┌─────────────────────┐
│   DMZ Pod           │
│  (multi-network)    │
├─────────────────────┤
│ eth0: Cilium        │ Default route, cluster communication
│ net1: MacVLAN (DMZ) │ VLAN 81, public-facing
└─────────────────────┘

┌─────────────────────┐
│   IoT Pod           │
│  (multi-network)    │
├─────────────────────┤
│ eth0: Cilium        │ Default route, cluster communication
│ net1: MacVLAN (IoT) │ VLAN 62, IoT network
└─────────────────────┘
```

### Cilium Configuration

Cilium is configured with `exclusive: false` to allow Multus to function alongside it.

Key Cilium settings (already in template):
```yaml
cni:
  exclusive: false  # Allow Multus to work alongside Cilium
```

### Multus Installation

Multus will be deployed as a DaemonSet to enable multi-network support.

Create directory structure:
```bash
mkdir -p kubernetes/apps/kube-system/multus/app
```

### NetworkAttachmentDefinition for DMZ VLAN

**File**: `kubernetes/apps/kube-system/multus/app/dmz-network.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: dmz-vlan81
  namespace: kube-system
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth1",
      "mode": "bridge",
      "ipam": {
        "type": "dhcp"
      }
    }
```

**Notes**:
- `master: "eth1"` assumes worker nodes have DMZ VLAN on second NIC
- `mode: "bridge"` for L2 connectivity
- `ipam: "dhcp"` to get IPs from VLAN 81 DHCP server
- Alternatively, use static IPAM with IP ranges

### NetworkAttachmentDefinition for IoT VLAN

**File**: `kubernetes/apps/kube-system/multus/app/iot-network.yaml`

```yaml
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: iot-vlan62
  namespace: kube-system
spec:
  config: |
    {
      "cniVersion": "0.3.1",
      "type": "macvlan",
      "master": "eth2",
      "mode": "bridge",
      "ipam": {
        "type": "dhcp"
      }
    }
```

### Talos Worker Node Configuration

Worker nodes need additional network interfaces configured in Talos.

**File**: `talos/patches/worker/multi-nic.yaml`

```yaml
---
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth1
    dhcp: true
    vlans:
      - vlanId: 81
        dhcp: true
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth2
    dhcp: true
    vlans:
      - vlanId: 62
        dhcp: true
```

**Alternative**: If Proxmox already tags interfaces, omit VLAN configuration:

```yaml
---
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth1
    dhcp: true
- op: add
  path: /machine/network/interfaces/-
  value:
    interface: eth2
    dhcp: true
```

### Pod Annotation for Multi-Network

To attach a pod to a secondary network:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dmz-pod
  annotations:
    k8s.v1.cni.cncf.io/networks: dmz-vlan81
spec:
  containers:
  - name: app
    image: nginx
```

For multiple networks:
```yaml
annotations:
  k8s.v1.cni.cncf.io/networks: dmz-vlan81, iot-vlan62
```

### Implementation Timeline

1. **Phase 1**: Deploy base cluster with Cilium only
2. **Phase 2**: Add Multus CNI DaemonSet
3. **Phase 3**: Create NetworkAttachmentDefinitions
4. **Phase 4**: Deploy test pods with VLAN attachments
5. **Phase 5**: Deploy production workloads (DMZ/IoT)

---

## 7. Storage Configuration

### Storage Architecture Decision

**Recommendation: External Ceph Cluster Mode**

Since you already have Proxmox Ceph configured, the most efficient approach is to configure Rook-Ceph as an **external cluster consumer** rather than deploying a new Ceph cluster inside Kubernetes.

### Benefits of External Mode

1. **Resource Efficiency**: No Ceph daemons running in Kubernetes
2. **Simplified Management**: Manage Ceph via Proxmox UI
3. **Performance**: Direct access to Proxmox Ceph cluster
4. **Reliability**: Ceph cluster lifecycle independent of Kubernetes

### Architecture

```
┌─────────────────────────────────────────────┐
│          Kubernetes Cluster                  │
│  ┌────────────────────────────────────────┐ │
│  │  Rook-Ceph Operator                    │ │
│  │  - CephCluster (external: true)        │ │
│  │  - StorageClasses                      │ │
│  │  - PVC/PV Management                   │ │
│  └─────────────┬──────────────────────────┘ │
│                │                             │
│                │ Ceph Protocol               │
└────────────────┼─────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────┐
│      Proxmox Ceph Cluster                   │
│  ┌────────────────────────────────────────┐ │
│  │  Ceph MONs, OSDs, MGRs                 │ │
│  │  - RBD Pool for block storage          │ │
│  │  - CephFS for shared storage           │ │
│  └────────────────────────────────────────┘ │
└─────────────────────────────────────────────┘
```

### Prerequisites

1. **Ceph Cluster Information Needed**:
   - Ceph monitor addresses (MON IPs)
   - Ceph admin keyring
   - Pool names (RBD pool for block storage)
   - CephFS filesystem name (if using shared storage)

2. **Retrieve from Proxmox**:

```bash
# SSH into Proxmox node
ssh root@proxmox-host

# Get monitor addresses
ceph mon dump

# Get admin key
ceph auth get-key client.admin

# List pools
ceph osd pool ls

# Get cluster FSID
ceph fsid
```

### Rook-Ceph External Cluster Setup

#### Step 1: Create Namespace

**File**: `kubernetes/apps/rook-ceph/namespace.yaml`

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: rook-ceph
```

#### Step 2: Import External Cluster

Rook provides a script to import external cluster credentials.

**Manual Secret Creation**:

**File**: `kubernetes/apps/rook-ceph/external-cluster-secret.sops.yaml`

```yaml
---
apiVersion: v1
kind: Secret
metadata:
  name: rook-ceph-mon
  namespace: rook-ceph
type: Opaque
stringData:
  admin-secret: <CEPH_ADMIN_KEY>
  fsid: <CEPH_CLUSTER_FSID>
  mon-secret: <CEPH_MON_KEY>
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: rook-ceph-mon-endpoints
  namespace: rook-ceph
data:
  data: <MON1_IP>:6789,<MON2_IP>:6789,<MON3_IP>:6789
  mapping: |
    {
      "node": {
        "mon1": {"Name": "mon1", "Hostname": "mon1", "Address": "<MON1_IP>"},
        "mon2": {"Name": "mon2", "Hostname": "mon2", "Address": "<MON2_IP>"},
        "mon3": {"Name": "mon3", "Hostname": "mon3", "Address": "<MON3_IP>"}
      }
    }
```

Encrypt with SOPS:
```bash
sops --encrypt --in-place kubernetes/apps/rook-ceph/external-cluster-secret.sops.yaml
```

#### Step 3: Deploy Rook Operator

**File**: `kubernetes/apps/rook-ceph/rook-ceph/app/helmrelease.yaml`

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: rook-ceph-operator
  namespace: rook-ceph
spec:
  interval: 30m
  chart:
    spec:
      chart: rook-ceph
      version: v1.15.5  # Check latest version
      sourceRef:
        kind: HelmRepository
        name: rook-ceph
        namespace: flux-system
      interval: 30m
  values:
    crds:
      enabled: true
    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        memory: 256Mi
```

#### Step 4: Create CephCluster Resource (External Mode)

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/cephcluster.yaml`

```yaml
---
apiVersion: ceph.rook.io/v1
kind: CephCluster
metadata:
  name: rook-ceph
  namespace: rook-ceph
spec:
  external:
    enable: true
  dataDirHostPath: /var/lib/rook
```

#### Step 5: Create StorageClasses

**RBD StorageClass** (Block Storage):

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/storageclass-rbd.yaml`

```yaml
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-ceph-block
provisioner: rook-ceph.rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: <RBD_POOL_NAME>  # e.g., kubernetes-rbd
  imageFormat: "2"
  imageFeatures: layering
  csi.storage.k8s.io/provisioner-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-rbd-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph
  csi.storage.k8s.io/fstype: ext4
allowVolumeExpansion: true
reclaimPolicy: Delete
```

**CephFS StorageClass** (Shared Storage):

**File**: `kubernetes/apps/rook-ceph/rook-ceph-cluster/app/storageclass-cephfs.yaml`

```yaml
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-cephfs
provisioner: rook-ceph.cephfs.csi.ceph.com
parameters:
  clusterID: rook-ceph
  fsName: <CEPHFS_NAME>  # e.g., cephfs
  pool: <CEPHFS_DATA_POOL>
  csi.storage.k8s.io/provisioner-secret-name: rook-csi-cephfs-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-cephfs-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-cephfs-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph
allowVolumeExpansion: true
reclaimPolicy: Delete
```

### Testing Storage

Create a test PVC:

**File**: `test-pvc.yaml`

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-rbd-pvc
  namespace: default
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: rook-ceph-block
  resources:
    requests:
      storage: 1Gi
```

```bash
kubectl apply -f test-pvc.yaml
kubectl get pvc -w
```

### Backup Strategy

**Velero Integration** (Future):
- Use Velero with Rook-Ceph for backup/restore
- Configure Velero to use Ceph RBD snapshots
- Schedule automated backups

---

## 8. Talos Configuration

### Overview

Talos is an immutable, API-driven Linux distribution designed specifically for Kubernetes. All configuration is declarative and applied via the Talos API.

### Node Discovery

Before configuring, you need to gather information about each node.

#### Step 1: Discover Node Information

**Boot all Talos VMs** from Talos ISO and ensure they're on the network.

**Find nodes**:
```bash
nmap -Pn -n -p 50000 10.20.67.0/24 -vv | grep 'Discovered'
```

Expected output:
```
Discovered open port 50000/tcp on 10.20.67.1
Discovered open port 50000/tcp on 10.20.67.2
Discovered open port 50000/tcp on 10.20.67.3
Discovered open port 50000/tcp on 10.20.67.4
...
Discovered open port 50000/tcp on 10.20.67.15
```

#### Step 2: Get Disk Information

For each node, retrieve disk information:

```bash
# Controllers
talosctl disks --nodes 10.20.67.1 --insecure
talosctl disks --nodes 10.20.67.2 --insecure
talosctl disks --nodes 10.20.67.3 --insecure

# Workers (repeat for each)
talosctl disks --nodes 10.20.67.4 --insecure
# ... continue for 10.20.67.5 through 10.20.67.15
```

Note the disk device path (e.g., `/dev/sda`) or serial number for each node.

#### Step 3: Get MAC Addresses

For each node, retrieve network interface information:

```bash
# Controllers
talosctl get links --nodes 10.20.67.1 --insecure
talosctl get links --nodes 10.20.67.2 --insecure
talosctl get links --nodes 10.20.67.3 --insecure

# Workers (repeat for each)
talosctl get links --nodes 10.20.67.4 --insecure
# ... continue for 10.20.67.5 through 10.20.67.15
```

Note the MAC address of the primary network interface (usually eth0).

### Talos Image Factory

Visit https://factory.talos.dev/ to create a custom Talos image.

#### Required System Extensions

For Proxmox VMs, you typically need:
- **qemu-guest-agent**: For Proxmox integration

#### Optional System Extensions

- **iscsi-tools**: If using iSCSI storage
- **util-linux-tools**: Additional utilities

#### Create Schematic

1. Go to https://factory.talos.dev/
2. Select Talos version (e.g., 1.11.3)
3. Add system extensions: `qemu-guest-agent`
4. Click "Generate"
5. Note the **Schematic ID** (e.g., `376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba`)

If you need different configurations per node (e.g., workers have different extensions), create separate schematics.

### Configure cluster.yaml

Edit `/home/devbox/repos/jlengelbrecht/prox-ops/cluster.yaml`:

```yaml
---
# Network CIDR for the nodes
node_cidr: "10.20.67.0/24"

# DNS servers (Cloudflare by default)
node_dns_servers:
  - "1.1.1.1"
  - "1.0.0.1"

# NTP servers (Cloudflare by default)
node_ntp_servers:
  - "162.159.200.1"
  - "162.159.200.123"

# Default gateway (first IP in node_cidr)
node_default_gateway: "10.20.67.1"  # Adjust to your gateway

# Kubernetes API VIP (choose unused IP in node_cidr)
cluster_api_addr: "10.20.67.10"

# Additional SANs for API server (optional)
# cluster_api_tls_sans:
#   - "k8s.example.com"

# Pod CIDR (must not overlap with node_cidr)
cluster_pod_cidr: "10.42.0.0/16"

# Service CIDR (must not overlap with node_cidr or pod_cidr)
cluster_svc_cidr: "10.43.0.0/16"

# Load balancer IP for k8s_gateway (internal DNS)
cluster_dns_gateway_addr: "10.20.67.20"

# Load balancer IP for internal gateway (Envoy)
cluster_gateway_addr: "10.20.67.21"

# GitHub repository
repository_name: "jlengelbrecht/prox-ops"

# Repository branch (default: main)
repository_branch: "main"

# Repository visibility (public or private)
repository_visibility: "public"

# Cloudflare domain
cloudflare_domain: "yourdomain.com"  # REPLACE WITH YOUR DOMAIN

# Cloudflare API token (will be encrypted with SOPS)
cloudflare_token: "your-cloudflare-api-token"  # REPLACE

# Load balancer IP for external gateway (Cloudflare tunnel)
cloudflare_gateway_addr: "10.20.67.22"

# Cilium load balancer mode (dsr or snat)
cilium_loadbalancer_mode: "dsr"

# BGP configuration (optional - uncomment if using BGP)
# cilium_bgp_router_addr: "10.20.67.1"
# cilium_bgp_router_asn: "64513"
# cilium_bgp_node_asn: "64514"
```

### Configure nodes.yaml

Edit `/home/devbox/repos/jlengelbrecht/prox-ops/nodes.yaml`:

```yaml
---
nodes:
  # Controller Nodes
  - name: "k8s-ctrl-1"
    address: "10.20.67.1"
    controller: true
    disk: "/dev/sda"  # or serial number from talosctl disks
    mac_addr: "XX:XX:XX:XX:XX:01"  # from talosctl get links
    schematic_id: "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"  # Your schematic
    mtu: 1500

  - name: "k8s-ctrl-2"
    address: "10.20.67.2"
    controller: true
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:02"
    schematic_id: "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
    mtu: 1500

  - name: "k8s-ctrl-3"
    address: "10.20.67.3"
    controller: true
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:03"
    schematic_id: "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
    mtu: 1500

  # Worker Nodes (with additional NICs for VLANs)
  - name: "k8s-work-1"
    address: "10.20.67.4"
    controller: false
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:04"
    schematic_id: "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
    mtu: 1500

  - name: "k8s-work-2"
    address: "10.20.67.5"
    controller: false
    disk: "/dev/sda"
    mac_addr: "XX:XX:XX:XX:XX:05"
    schematic_id: "376567988ad370138ad8b2698212367b8edcb69b5fd68c80be1f2ec7d603b4ba"
    mtu: 1500

  # ... Continue for k8s-work-3 through k8s-work-12 (10.20.67.6-15)
  # Repeat the pattern above, incrementing addresses and MAC addresses
```

**Important**: Replace MAC addresses, disk paths, and schematic IDs with actual values from your nodes.

### Validate and Render Configurations

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
task configure
```

This will:
1. Validate `cluster.yaml` and `nodes.yaml` against schemas
2. Render Jinja2 templates to generate:
   - Talos configurations in `talos/`
   - Kubernetes manifests in `kubernetes/`
   - Bootstrap configurations in `bootstrap/`
3. Encrypt secrets with SOPS
4. Validate generated configurations

**Troubleshooting**: If errors occur, read the error messages carefully and adjust `cluster.yaml` or `nodes.yaml` accordingly.

---

## 9. Cloudflare Setup

### Prerequisites

- Cloudflare account
- Domain managed by Cloudflare
- Cloudflare API token with appropriate permissions

### Step 1: Create Cloudflare API Token

1. Log in to Cloudflare Dashboard
2. Go to **My Profile** > **API Tokens**
3. Click **Create Token**
4. Select **Edit zone DNS** template
5. Click **Use template**
6. Configure permissions:
   - **Zone - DNS - Edit**
   - **Account - Cloudflare Tunnel - Read**
7. Set zone resources (select your domain)
8. Click **Continue to Summary**
9. Click **Create Token**
10. **Save the token securely** - you'll need it for `cluster.yaml`

### Step 2: Create Cloudflare Tunnel

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/

# Login to Cloudflare (opens browser)
cloudflared tunnel login

# Create tunnel named "kubernetes"
cloudflared tunnel create --credentials-file cloudflare-tunnel.json kubernetes
```

This creates `cloudflare-tunnel.json` with tunnel credentials.

**Tunnel ID**: Note the tunnel ID from the output (or from the filename).

### Step 3: Update cluster.yaml

Edit `cluster.yaml` with:
- `cloudflare_domain`: Your domain (e.g., `example.com`)
- `cloudflare_token`: API token from Step 1

### Step 4: Re-render Configurations

After updating `cluster.yaml`:

```bash
task configure
```

This will encrypt the Cloudflare token into the generated manifests.

### Step 5: Configure DNS (Post-Bootstrap)

After cluster is running, you'll configure split DNS:
- **External DNS**: Point `*.example.com` to Cloudflare tunnel
- **Internal DNS**: Point `*.example.com` to internal gateway IP (10.20.67.21)

This is covered in Post-Installation section.

---

## 10. Bootstrap Process

### Overview

The bootstrap process happens in two phases:
1. **Talos Bootstrap**: Install Talos OS and Kubernetes on nodes
2. **Apps Bootstrap**: Deploy Cilium, CoreDNS, Flux, and initial applications

### Pre-Bootstrap Checklist

Verify the following before proceeding:

- [ ] All Talos VMs are running and accessible on network
- [ ] `cluster.yaml` and `nodes.yaml` are configured correctly
- [ ] `task configure` completed successfully without errors
- [ ] `cloudflare-tunnel.json` exists
- [ ] `age.key` exists
- [ ] All secrets are encrypted (check `*.sops.*` files)
- [ ] Git repository is initialized and pushed to GitHub

### Phase 1: Bootstrap Talos

```bash
cd /home/devbox/repos/jlengelbrecht/prox-ops/
task bootstrap:talos
```

**What this does**:
1. Generates Talos secrets (encrypted with SOPS)
2. Generates Talos configurations for each node using talhelper
3. Applies configurations to nodes (over insecure connection initially)
4. Bootstraps the Kubernetes cluster on the first controller
5. Retrieves kubeconfig and saves to `kubeconfig`

**Expected duration**: 5-10 minutes

**Monitoring**: You can watch the bootstrap progress:

```bash
# In another terminal, watch Talos logs
talosctl dmesg --follow --nodes 10.20.67.1 --talosconfig talos/clusterconfig/talosconfig

# Check node status
talosctl get members --nodes 10.20.67.1 --talosconfig talos/clusterconfig/talosconfig
```

**Verification**:

```bash
# Check kubeconfig was created
ls -la kubeconfig

# Verify you can connect to cluster
export KUBECONFIG=$(pwd)/kubeconfig
kubectl get nodes

# Nodes will show NotReady (no CNI yet) - this is expected
```

### Commit Encrypted Secrets

After bootstrap, commit the encrypted Talos secrets:

```bash
git add -A
git commit -m "chore: add talhelper encrypted secret"
git push
```

### Phase 2: Bootstrap Applications

```bash
task bootstrap:apps
```

**What this does**:
1. Deploys Cilium CNI (pods will now have networking)
2. Deploys CoreDNS (cluster DNS)
3. Deploys Spegel (local OCI mirror for faster image pulls)
4. Deploys Flux operator and instance
5. Flux syncs the repository and deploys remaining applications

**Expected duration**: 10-15 minutes

**Monitoring**:

```bash
# Watch all pods come up
kubectl get pods --all-namespaces --watch

# In another terminal, check Flux status
flux get sources git
flux get kustomizations
flux get helmreleases
```

**Common issues during bootstrap**:

1. **"couldn't get current server API group list"**: Normal during bootstrap, wait
2. **"no matches for kind"**: CRDs not installed yet, Flux will retry
3. **Nodes stay NotReady**: Cilium not running yet
4. **ImagePullBackOff**: Normal initially, Spegel needs to sync

**Success indicators**:

```bash
# All 15 nodes should be Ready
kubectl get nodes
# Should show all 3 controllers + 12 workers in Ready state

# Cilium should be healthy
cilium status

# Flux should be synced
flux check
```

### Troubleshooting Bootstrap

#### Talos bootstrap fails

```bash
# Check Talos logs
talosctl dmesg --nodes 10.20.67.1 --talosconfig talos/clusterconfig/talosconfig

# Check service status
talosctl services --nodes 10.20.67.1 --talosconfig talos/clusterconfig/talosconfig
```

#### Apps bootstrap fails

```bash
# Check bootstrap logs
kubectl logs -n kube-system -l app=cilium
kubectl logs -n flux-system -l app=flux

# Force Flux reconciliation
flux reconcile source git flux-system
flux reconcile kustomization flux-system
```

#### Reset cluster (if needed)

```bash
# This will wipe all nodes and require re-bootstrap
talosctl reset --graceful=false --reboot --nodes 10.20.67.1,10.20.67.2,10.20.67.3,10.20.67.4,10.20.67.5,10.20.67.6,10.20.67.7,10.20.67.8,10.20.67.9,10.20.67.10,10.20.67.11,10.20.67.12,10.20.67.13,10.20.67.14,10.20.67.15 --talosconfig talos/clusterconfig/talosconfig
```

---

## 11. GitOps and Application Deployment

### Flux Architecture

Flux uses a hierarchical Kustomization structure:

```
flux-system (root)
├── kubernetes/flux/cluster/ks.yaml (cluster apps)
│   ├── kube-system (core components)
│   │   ├── cilium
│   │   ├── multus
│   │   ├── coredns
│   │   ├── metrics-server
│   │   ├── reloader
│   │   └── spegel
│   ├── cert-manager (certificate management)
│   ├── network (networking components)
│   │   ├── cloudflare-dns
│   │   ├── cloudflare-tunnel
│   │   ├── envoy-gateway
│   │   └── k8s-gateway
│   ├── rook-ceph (storage)
│   ├── monitoring (observability)
│   ├── dmz (DMZ VLAN workloads)
│   └── iot (IoT VLAN workloads)
```

### Directory Structure for Apps

Each application follows this structure:

```
kubernetes/apps/<namespace>/<app>/
├── ks.yaml                    # Flux Kustomization (defines sync)
├── namespace.yaml             # Namespace definition
├── kustomization.yaml         # Kustomize overlay
└── app/
    ├── helmrelease.yaml       # Helm chart deployment
    ├── ocirepository.yaml     # OCI chart source
    ├── kustomization.yaml     # Resources to apply
    └── secret.sops.yaml       # Encrypted secrets
```

### Adding a New Application

#### Example: Deploy Home Assistant (IoT VLAN)

**Step 1: Create Directory Structure**

```bash
mkdir -p kubernetes/apps/iot/home-assistant/app
```

**Step 2: Create Namespace**

**File**: `kubernetes/apps/iot/namespace.yaml`

```yaml
---
apiVersion: v1
kind: Namespace
metadata:
  name: iot
  labels:
    pod-security.kubernetes.io/enforce: privileged
```

**Step 3: Create Kustomization (Root)**

**File**: `kubernetes/apps/iot/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - namespace.yaml
  - home-assistant/ks.yaml
```

**Step 4: Create Flux Kustomization**

**File**: `kubernetes/apps/iot/home-assistant/ks.yaml`

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: home-assistant
  namespace: flux-system
spec:
  interval: 30m
  path: ./kubernetes/apps/iot/home-assistant/app
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  wait: true
  timeout: 5m
```

**Step 5: Create HelmRelease**

**File**: `kubernetes/apps/iot/home-assistant/app/helmrelease.yaml`

```yaml
---
apiVersion: helm.toolkit.fluxcd.io/v2
kind: HelmRelease
metadata:
  name: home-assistant
  namespace: iot
  annotations:
    k8s.v1.cni.cncf.io/networks: iot-vlan62  # Attach IoT VLAN
spec:
  interval: 30m
  chart:
    spec:
      chart: home-assistant
      version: 13.4.2  # Check latest version
      sourceRef:
        kind: HelmRepository
        name: k8s-at-home
        namespace: flux-system
  values:
    image:
      repository: ghcr.io/home-assistant/home-assistant
      tag: 2024.1.0
    env:
      TZ: America/New_York
    service:
      main:
        type: LoadBalancer
        loadBalancerIP: 10.20.67.30  # Optional: specific IP
    ingress:
      main:
        enabled: true
        className: envoy
        hosts:
          - host: home.yourdomain.com
            paths:
              - path: /
                pathType: Prefix
    persistence:
      config:
        enabled: true
        storageClass: rook-ceph-block
        accessMode: ReadWriteOnce
        size: 5Gi
    resources:
      requests:
        cpu: 100m
        memory: 512Mi
      limits:
        memory: 2Gi
```

**Step 6: Create Kustomization (App)**

**File**: `kubernetes/apps/iot/home-assistant/app/kustomization.yaml`

```yaml
---
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - helmrelease.yaml
```

**Step 7: Update Root Kustomization**

Edit `kubernetes/flux/cluster/ks.yaml` to include IoT namespace:

```yaml
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: cluster-apps
  namespace: flux-system
spec:
  interval: 10m
  path: ./kubernetes/apps
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  decryption:
    provider: sops
    secretRef:
      name: sops-age
  postBuild:
    substituteFrom:
      - kind: Secret
        name: cluster-secrets
      - kind: ConfigMap
        name: cluster-settings
```

**Step 8: Commit and Push**

```bash
git add kubernetes/apps/iot/
git commit -m "feat: add home-assistant to IoT VLAN"
git push
```

**Step 9: Force Flux Reconciliation**

```bash
task reconcile
# Or manually:
flux reconcile source git flux-system
flux reconcile kustomization cluster-apps
```

**Step 10: Monitor Deployment**

```bash
kubectl get helmreleases -n iot
kubectl get pods -n iot
kubectl describe pod -n iot home-assistant-xxxx
```

### Multi-Network Pod Example

For pods requiring both cluster network and VLAN access:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dmz-nginx
  namespace: dmz
  annotations:
    k8s.v1.cni.cncf.io/networks: dmz-vlan81
spec:
  containers:
  - name: nginx
    image: nginx:latest
    resources:
      requests:
        cpu: 100m
        memory: 128Mi
      limits:
        memory: 256Mi
```

The pod will have:
- `eth0`: Cilium network (10.42.x.x) - default route
- `net1`: DMZ VLAN 81 (IP from VLAN DHCP) - for external access

### Network Policy for VLAN Isolation

**File**: `kubernetes/apps/dmz/network-policy.yaml`

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: dmz-isolation
  namespace: dmz
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          name: ingress
  - from:
    - podSelector: {}
  egress:
  - to:
    - namespaceSelector:
        matchLabels:
          name: kube-system
  - to:
    - podSelector: {}
```

---

## 12. Verification and Testing

### Post-Bootstrap Verification

#### Step 1: Check Node Status

```bash
kubectl get nodes -o wide
```

Expected output:
```
NAME          STATUS   ROLES           AGE   VERSION
k8s-ctrl-1    Ready    control-plane   10m   v1.31.0
k8s-ctrl-2    Ready    control-plane   10m   v1.31.0
k8s-ctrl-3    Ready    control-plane   10m   v1.31.0
k8s-work-1    Ready    worker          10m   v1.31.0
k8s-work-2    Ready    worker          10m   v1.31.0
...
k8s-work-12   Ready    worker          10m   v1.31.0
```

#### Step 2: Check Cilium Status

```bash
cilium status --wait
```

Expected output:
```
    /¯¯\
 /¯¯\__/¯¯\    Cilium:             OK
 \__/¯¯\__/    Operator:           OK
 /¯¯\__/¯¯\    Envoy DaemonSet:    OK
 \__/¯¯\__/    Hubble Relay:       disabled
    \__/       ClusterMesh:        disabled

Deployment             cilium-operator    Desired: 1, Ready: 1/1, Available: 1/1
DaemonSet              cilium             Desired: 5, Ready: 5/5, Available: 5/5
Containers:            cilium             Running: 5
                       cilium-operator    Running: 1
```

#### Step 3: Check Flux Status

```bash
flux check
```

Expected output:
```
► checking prerequisites
✔ Kubernetes 1.31.0 >=1.28.0-0
► checking version in cluster
✔ distribution: flux-v2.7.2
✔ bootstrapped: true
► checking controllers
✔ helm-controller: deployment ready
✔ kustomize-controller: deployment ready
✔ notification-controller: deployment ready
✔ source-controller: deployment ready
► checking components
✔ all checks passed
```

```bash
flux get sources git
flux get kustomizations
flux get helmreleases -A
```

#### Step 4: Check Core Services

```bash
kubectl get pods -n kube-system
kubectl get pods -n cert-manager
kubectl get pods -n network
kubectl get pods -n flux-system
```

All pods should be in `Running` state.

#### Step 5: Check Storage (if deployed)

```bash
kubectl get storageclass
kubectl get cephcluster -n rook-ceph
kubectl get cephblockpools -n rook-ceph
```

#### Step 6: Check Load Balancer IPs

```bash
kubectl get svc -A | grep LoadBalancer
```

Verify IPs match your configuration:
- k8s-gateway: 10.20.67.20
- internal-gateway: 10.20.67.21
- cloudflare-gateway: 10.20.67.22

### Network Testing

#### Test 1: Pod Connectivity

```bash
# Create test pod
kubectl run test-pod --image=nicolaka/netshoot -it --rm -- /bin/bash

# Inside pod:
# Test DNS resolution
nslookup kubernetes.default.svc.cluster.local

# Test external connectivity
curl -I https://google.com

# Test pod-to-pod connectivity
ping <another-pod-ip>
```

#### Test 2: VLAN Connectivity

Deploy a test pod with VLAN attachment:

**File**: `test-vlan-pod.yaml`

```yaml
---
apiVersion: v1
kind: Pod
metadata:
  name: test-vlan
  annotations:
    k8s.v1.cni.cncf.io/networks: dmz-vlan81
spec:
  containers:
  - name: netshoot
    image: nicolaka/netshoot
    command: ["sleep", "3600"]
```

```bash
kubectl apply -f test-vlan-pod.yaml
kubectl exec -it test-vlan -- /bin/bash

# Inside pod:
ip addr show  # Should see net1 interface with VLAN IP
ping <ip-in-vlan-81>  # Test VLAN connectivity
```

#### Test 3: Ingress Connectivity

```bash
# From your workstation:
curl -H "Host: echo.yourdomain.com" http://10.20.67.21

# Expected: Echo server response
```

#### Test 4: External Access via Cloudflare

```bash
curl https://echo.yourdomain.com
```

### Storage Testing

#### Test 1: Create PVC

```yaml
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-pvc
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: rook-ceph-block
  resources:
    requests:
      storage: 1Gi
```

```bash
kubectl apply -f test-pvc.yaml
kubectl get pvc test-pvc -w
```

Should transition to `Bound` state.

#### Test 2: Use PVC in Pod

```yaml
---
apiVersion: v1
kind: Pod
metadata:
  name: test-storage-pod
spec:
  containers:
  - name: test
    image: busybox
    command: ["sh", "-c", "echo 'Hello from Ceph' > /mnt/test.txt && sleep 3600"]
    volumeMounts:
    - name: storage
      mountPath: /mnt
  volumes:
  - name: storage
    persistentVolumeClaim:
      claimName: test-pvc
```

```bash
kubectl apply -f test-storage-pod.yaml
kubectl exec test-storage-pod -- cat /mnt/test.txt
# Expected: Hello from Ceph
```

### Performance Testing

#### CPU/Memory Usage

```bash
kubectl top nodes
kubectl top pods -A
```

#### Network Throughput (using iperf3)

Deploy iperf3 server and client to test network performance.

---

## 13. Post-Installation Tasks

### Task 1: Configure Split DNS

For internal access to services, configure your home DNS server (Pi-hole, Dnsmasq, etc.) to resolve your domain to the internal gateway.

**Option A: Wildcard DNS**

Add to your DNS server:
```
*.yourdomain.com IN A 10.20.67.21
```

**Option B: Specific Records**

Add individual records:
```
home.yourdomain.com IN A 10.20.67.21
grafana.yourdomain.com IN A 10.20.67.21
```

### Task 2: Configure GitHub Webhook

For faster Git syncs, configure a webhook to notify Flux of changes.

1. Go to your GitHub repository settings
2. Navigate to **Settings** > **Webhooks** > **Add webhook**
3. Configure:
   - **Payload URL**: `https://<cloudflare-gateway-addr>/hook/<receiver-token>`
   - **Content type**: `application/json`
   - **Secret**: (contents of `github-push-token.txt`)
   - **Events**: Just the push event
4. Click **Add webhook**

### Task 3: Set Up Monitoring

Deploy monitoring stack (Prometheus, Grafana, Loki):

```bash
# This would be a Flux Kustomization pointing to monitoring apps
# Example structure:
mkdir -p kubernetes/apps/monitoring/{prometheus,grafana,loki}
```

**Prometheus**: Metrics collection
**Grafana**: Visualization dashboards
**Loki**: Log aggregation

### Task 4: Backup Configuration

Regularly back up critical configurations:

```bash
# Backup age key
cp age.key ~/backups/prox-ops-age.key.backup

# Backup kubeconfig
cp kubeconfig ~/backups/prox-ops-kubeconfig.backup

# Backup Talos config
cp talos/clusterconfig/talosconfig ~/backups/prox-ops-talosconfig.backup

# Store securely (encrypted) off-site
```

### Task 5: Configure Renovate

Renovate will automatically create PRs for dependency updates.

The template includes `.renovaterc.json5` configuration. Ensure Renovate is enabled for your GitHub repository:

1. Go to https://github.com/apps/renovate
2. Click **Configure**
3. Select your repository
4. Renovate will start scanning for dependencies

### Task 6: Deploy Additional Applications

Based on your needs, deploy:

- **Ingress**: Traefik or Nginx (alternative to Envoy)
- **Monitoring**: Prometheus, Grafana, AlertManager
- **Logging**: Loki, Promtail
- **Backup**: Velero
- **Dashboard**: Kubernetes Dashboard or Headlamp
- **GitOps UI**: Flux UI or Weave GitOps
- **Service Mesh**: Istio or Linkerd (optional)

### Task 7: Harden Security

1. **Enable Pod Security Standards**:
   ```yaml
   apiVersion: v1
   kind: Namespace
   metadata:
     name: myapp
     labels:
       pod-security.kubernetes.io/enforce: restricted
       pod-security.kubernetes.io/audit: restricted
       pod-security.kubernetes.io/warn: restricted
   ```

2. **Implement Network Policies**: Restrict pod-to-pod communication

3. **Enable Audit Logging**: Configure Talos audit logging

4. **Rotate Secrets**: Periodically rotate Age keys and regenerate SOPS secrets

5. **RBAC Review**: Ensure least-privilege access

### Task 8: Document Your Setup

Update `README.md` with:
- Network topology
- IP allocations
- Access credentials (encrypted)
- Runbooks for common tasks
- Disaster recovery procedures

### Task 9: Test Disaster Recovery

Simulate failures:

1. **Node failure**: Drain a node and verify workloads migrate
   ```bash
   kubectl drain k8s-work-1 --ignore-daemonsets --delete-emptydir-data
   ```

2. **Controller failure**: Stop a controller and verify HA

3. **Storage failure**: Test Ceph failover

4. **Complete cluster rebuild**: Practice rebuilding from Git

### Task 10: Performance Tuning

Based on monitoring data:

1. Adjust resource requests/limits
2. Configure pod autoscaling (HPA)
3. Optimize Cilium settings for your workload
4. Tune Ceph performance parameters

---

## Appendix A: Common Commands Reference

### Talos Commands

```bash
# Get node status
talosctl get members --nodes <node-ip> --talosconfig <path>

# Get disks
talosctl disks --nodes <node-ip> --insecure

# Get network interfaces
talosctl get links --nodes <node-ip> --insecure

# View logs
talosctl dmesg --follow --nodes <node-ip> --talosconfig <path>

# Check services
talosctl services --nodes <node-ip> --talosconfig <path>

# Upgrade Talos
talosctl upgrade --nodes <node-ip> --image <image> --talosconfig <path>

# Reset node (WARNING: destroys all data)
talosctl reset --graceful=false --reboot --nodes <node-ip> --talosconfig <path>
```

### Kubectl Commands

```bash
# Get resources
kubectl get nodes
kubectl get pods -A
kubectl get svc -A
kubectl get pvc -A

# Describe resources
kubectl describe node <node-name>
kubectl describe pod <pod-name> -n <namespace>

# Logs
kubectl logs <pod-name> -n <namespace>
kubectl logs -f <pod-name> -n <namespace>  # Follow logs

# Execute commands in pod
kubectl exec -it <pod-name> -n <namespace> -- /bin/bash

# Port forwarding
kubectl port-forward -n <namespace> <pod-name> <local-port>:<remote-port>

# Resource usage
kubectl top nodes
kubectl top pods -A
```

### Flux Commands

```bash
# Check Flux status
flux check

# Get sources
flux get sources git
flux get sources helm

# Get Kustomizations
flux get kustomizations

# Get HelmReleases
flux get helmreleases -A

# Reconcile (force sync)
flux reconcile source git flux-system
flux reconcile kustomization <name>
flux reconcile helmrelease <name> -n <namespace>

# Suspend/Resume
flux suspend helmrelease <name> -n <namespace>
flux resume helmrelease <name> -n <namespace>

# Logs
flux logs --follow
```

### Cilium Commands

```bash
# Check status
cilium status

# Connectivity test
cilium connectivity test

# Monitor network traffic
cilium monitor

# BGP status (if enabled)
cilium bgp peers
cilium bgp routes

# Policy trace
cilium policy trace <pod-name>
```

### Task Commands

```bash
# List all tasks
task --list

# Initialize configuration
task init

# Render templates
task configure

# Bootstrap Talos
task bootstrap:talos

# Bootstrap apps
task bootstrap:apps

# Force Flux reconciliation
task reconcile

# Debug (gather resource info)
task template:debug
```

---

## Appendix B: Troubleshooting Guide

### Issue: Nodes stuck in NotReady

**Symptoms**: `kubectl get nodes` shows NotReady status

**Causes**:
- Cilium not deployed yet
- Network configuration issues
- Talos networking misconfigured

**Solutions**:
```bash
# Check Cilium status
cilium status

# Check Cilium pods
kubectl get pods -n kube-system -l app.kubernetes.io/name=cilium

# Check Cilium logs
kubectl logs -n kube-system -l app.kubernetes.io/name=cilium

# Check node network configuration
talosctl get links --nodes <node-ip> --talosconfig talos/clusterconfig/talosconfig
```

### Issue: Flux not syncing

**Symptoms**: Applications not deploying, Flux Kustomizations in failed state

**Causes**:
- GitHub authentication issues
- SOPS decryption failure
- Manifest validation errors

**Solutions**:
```bash
# Check Flux logs
flux logs --follow

# Check Flux Kustomization status
flux get kustomizations
kubectl describe kustomization <name> -n flux-system

# Check GitHub deploy key
kubectl get secret -n flux-system flux-system -o yaml

# Force reconciliation
flux reconcile source git flux-system
```

### Issue: PVCs stuck in Pending

**Symptoms**: PVCs not binding to PVs

**Causes**:
- StorageClass not available
- Rook-Ceph not ready
- Insufficient storage

**Solutions**:
```bash
# Check StorageClasses
kubectl get storageclass

# Check Rook-Ceph status
kubectl get cephcluster -n rook-ceph
kubectl get pods -n rook-ceph

# Check PVC events
kubectl describe pvc <pvc-name>

# Check Ceph health
kubectl exec -n rook-ceph -it <rook-ceph-tools-pod> -- ceph status
```

### Issue: Pods can't pull images

**Symptoms**: ImagePullBackOff errors

**Causes**:
- Network connectivity issues
- Spegel not running
- Image doesn't exist

**Solutions**:
```bash
# Check Spegel status
kubectl get pods -n kube-system -l app.kubernetes.io/name=spegel

# Test external connectivity from pod
kubectl run test --image=busybox -it --rm -- wget -O- https://google.com

# Check image pull logs
kubectl describe pod <pod-name>
```

### Issue: VLAN connectivity not working

**Symptoms**: Pods with VLAN annotations don't have secondary network

**Causes**:
- Multus not deployed
- NetworkAttachmentDefinition incorrect
- Worker node NIC not configured

**Solutions**:
```bash
# Check Multus installation
kubectl get ds -n kube-system multus

# Check NetworkAttachmentDefinitions
kubectl get network-attachment-definitions -A

# Check pod network status
kubectl exec <pod-name> -- ip addr show

# Check worker node interfaces
talosctl get links --nodes <worker-ip> --talosconfig talos/clusterconfig/talosconfig
```

---

## Appendix C: Resource Requirements

### Minimum Requirements (Testing)

| Component | CPU | Memory | Disk |
|-----------|-----|--------|------|
| Controller Node | 2 cores | 4GB | 32GB |
| Worker Node | 2 cores | 4GB | 32GB |

### Recommended Requirements (Homelab)

| Component | CPU | Memory | Disk |
|-----------|-----|--------|------|
| Controller Node | 4 cores | 8GB | 64GB SSD |
| Worker Node | 4 cores | 16GB | 128GB SSD |

### Production-like Requirements

| Component | CPU | Memory | Disk |
|-----------|-----|--------|------|
| Controller Node | 4 cores | 16GB | 256GB NVMe |
| Worker Node | 8 cores | 32GB | 512GB NVMe |

### Service Resource Allocations

Typical resource usage for core services:

| Service | CPU Request | Memory Request | CPU Limit | Memory Limit |
|---------|-------------|----------------|-----------|--------------|
| Cilium (per node) | 100m | 512Mi | 1000m | 2Gi |
| CoreDNS | 100m | 128Mi | 1000m | 256Mi |
| Flux controllers | 100m | 128Mi | 1000m | 1Gi |
| Cert-manager | 10m | 64Mi | 100m | 128Mi |
| Envoy Gateway | 100m | 128Mi | 500m | 512Mi |
| Rook-Ceph Operator | 100m | 128Mi | 500m | 256Mi |

---

## Appendix D: Network Design Reference

### IP Allocation Plan

| Purpose | IP Range | Notes |
|---------|----------|-------|
| Controller Nodes | 10.20.67.1-3 | Static IPs |
| Worker Nodes | 10.20.67.4-15 | Static IPs |
| Reserved | 10.20.67.16-19 | Future expansion |
| Kubernetes API VIP | 10.20.67.10 | Virtual IP |
| LoadBalancer IPs | 10.20.67.20-30 | Reserved for services |
| K8s Gateway | 10.20.67.20 | Internal DNS |
| Internal Gateway | 10.20.67.21 | Envoy ingress |
| External Gateway | 10.20.67.22 | Cloudflare tunnel |
| Future Services | 10.20.67.31-99 | Reserved |
| DHCP Pool | 10.20.67.100-200 | For VMs/clients |

### VLAN Configuration

| VLAN ID | Purpose | Subnet | Gateway |
|---------|---------|--------|---------|
| Default | Management/Cluster | 10.20.67.0/24 | 10.20.67.1 |
| 81 | DMZ (Public-facing) | <your-dmz-subnet> | <dmz-gateway> |
| 62 | IoT (Home Automation) | <your-iot-subnet> | <iot-gateway> |

### Port Requirements

**Talos**:
- 50000/tcp: Talos API
- 6443/tcp: Kubernetes API
- 51820/udp: WireGuard (if used)

**Kubernetes**:
- 6443/tcp: API server
- 2379-2380/tcp: etcd
- 10250/tcp: Kubelet API
- 10259/tcp: kube-scheduler
- 10257/tcp: kube-controller-manager

**Cilium**:
- 4240/tcp: Health checks
- 4244/tcp: Hubble server (if enabled)
- 4245/tcp: Hubble relay (if enabled)

**Ceph**:
- 6789/tcp: Ceph monitors
- 3300/tcp: Ceph managers
- 6800-7300/tcp: Ceph OSDs

---

## Summary

This implementation plan provides a comprehensive guide to deploying a production-grade Kubernetes homelab cluster with:

1. **High Availability**: 3-node control plane with Talos Linux
2. **Advanced Networking**: Multi-VLAN support using Cilium + Multus
3. **Persistent Storage**: Rook-Ceph integration with Proxmox Ceph
4. **GitOps**: Flux v2 for declarative cluster management
5. **Security**: SOPS encryption, network policies, RBAC
6. **Observability**: Ready for monitoring and logging stacks
7. **Scalability**: Designed for growth and additional workloads

Follow the steps in order, verify each phase before proceeding, and refer to the appendices for troubleshooting and reference information.

Good luck with your homelab cluster!
