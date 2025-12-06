# Automated Talos Cattle Upgrade Strategy - Brownfield Architecture

**Document Version**: 1.0.0
**Created**: 2025-11-16
**Author**: Winston (Architect Agent)
**Project**: prox-ops Homelab Infrastructure
**Purpose**: Document architecture for automated Talos Kubernetes cluster upgrade workflows

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Enhancement Scope and Integration Strategy](#2-enhancement-scope-and-integration-strategy)
3. [Technology Stack](#3-technology-stack)
4. [Component Architecture](#4-component-architecture)
5. [Infrastructure & Deployment](#5-infrastructure--deployment)
6. [Testing Strategy and Quality Assurance](#6-testing-strategy-and-quality-assurance)
7. [Operational Considerations and Monitoring](#7-operational-considerations-and-monitoring)
8. [Future Enhancements](#8-future-enhancements)
9. [Conclusion](#9-conclusion)

---

## 1. Introduction

### 1.1 Project Context

The **prox-ops** repository manages a **15-node Talos Kubernetes homelab cluster** (3 control plane, 12 workers) running on Proxmox VE virtualization platform. The cluster currently operates with **90% infrastructure maturity**, featuring:

- **Flux GitOps** for application deployment
- **Terraform** for infrastructure-as-code (local state)
- **Multi-VLAN networking** (native 10.20.66.0/23, IoT VLAN 62, DMZ VLAN 81)
- **GPU passthrough** on k8s-work-4 (RTX A2000) and k8s-work-14 (RTX A5000)
- **Rook-Ceph CSI** for persistent storage (external Ceph cluster on Proxmox)

**Current Problem**: Cluster upgrades are **manual, time-consuming (4-5 hours), and error-prone**, requiring significant user intervention for each of 15 nodes. Recent GPU cattle upgrade failures (eBPF incompatibility, missing NICs) demonstrate the need for automated validation and deployment.

### 1.2 Enhancement Objectives

**Primary Goal**: Achieve **fully automated Talos cluster upgrades** triggered by Renovate pull requests, requiring only user approval (PR merge) to execute.

**Success Criteria**:
- ✅ **Zero manual intervention** after PR merge
- ✅ **Dual-workflow support**: Pets (in-place) for patch releases, Cattle (destroy/recreate) for minor/major releases
- ✅ **90% effort reduction**: From 4-5 hours manual work to automated execution
- ✅ **Zero configuration drift**: Cattle upgrades ensure clean state
- ✅ **GPU node safety**: Conditional patch application with validation
- ✅ **Network preservation**: Multi-NIC VLAN configuration maintained

### 1.3 Existing Constraints and Considerations

**Infrastructure Constraints**:
- **Terraform local state**: Must migrate to S3 backend for CI/CD access
- **Private network**: Homelab on 10.20.66.0/23, unreachable by GitHub-hosted runners
- **MAC address preservation**: Each node requires specific MAC for DHCP reservation
- **Multi-VLAN workers**: Workers have 3 NICs (eth0 native, eth1 VLAN 62, eth2 VLAN 81)
- **Controller single-NIC**: Controllers only have eth0 (native VLAN)
- **GPU patch sensitivity**: NVIDIA patches must use `/var/` paths (NOT `/etc/`), require `no-cgroups = true`

**Operational Constraints**:
- **ETCD quorum preservation**: Cannot upgrade >1 controller simultaneously
- **Sequential minor version upgrades**: Cannot skip Talos minor versions (must upgrade 1.11.x → 1.12.x → 1.13.x, not 1.11.x → 1.13.x directly)
- **External dependencies**: Factory.talos.dev for images, GitHub for Actions, S3 for state
- **GitOps workflow**: All changes must go through Flux reconciliation

### 1.4 Trade-Offs and Design Choices

| Decision | Choice | Rationale | Trade-Off |
|----------|--------|-----------|-----------|
| **Pets vs Cattle** | Dual-workflow approach | Pets faster for patch releases, Cattle ensures zero drift for minor/major releases | Added complexity (2 workflows) |
| **Credential Management** | GitHub Actions Secrets | Simple, built-in, sufficient for homelab | Less sophisticated than SOPS, no GitOps |
| **State Backend** | S3 | Standard Terraform backend, widely supported | Dependency on external S3 service |
| **Runner Location** | In-cluster self-hosted | Only option (GitHub-hosted can't reach homelab) | Runner pod eviction risk |
| **Job Orchestration** | Kubernetes Jobs | Jobs survive runner pod death | More complex than direct workflow execution |
| **Controller Upgrades** | Sequential (max-parallel: 1) | Preserves ETCD quorum | Slower than parallel (~6 min overhead) |

### 1.5 Key Assumptions

1. **Talos Factory Availability**: `factory.talos.dev` remains available for image downloads
2. **Schematic Stability**: Custom schematics remain accessible after creation
3. **S3 Backend Reliability**: S3 service has 99.9%+ uptime for state access
4. **GitHub Actions Availability**: GitHub Actions service available during upgrade windows
5. **Network Stability**: 10.20.66.0/23 network stable during upgrades
6. **Proxmox API Uptime**: Proxmox API available for Terraform operations

### 1.6 Areas Requiring Validation

**Pre-Production Validation**:
- [ ] Single-node pets upgrade on non-critical worker (k8s-work-16)
- [ ] Single-node cattle upgrade on non-critical worker (k8s-work-16)
- [ ] GPU patch application on k8s-work-4
- [ ] NIC configuration preservation after destroy/recreate
- [ ] ETCD quorum preservation during controller upgrades
- [ ] Emergency rollback procedures

**Ongoing Monitoring**:
- Terraform state lock contention (DynamoDB)
- S3 backend latency impact on upgrades
- Job scheduling anti-affinity effectiveness
- Network policy egress restrictions

---

## 2. Enhancement Scope and Integration Strategy

### 2.1 In-Scope Features

**Core Automation**:
- ✅ Automated pets upgrade workflow (patch releases: 1.11.0 → 1.11.1, pure `talosctl upgrade`)
- ✅ Automated cattle upgrade workflow (minor/major releases: 1.11.x → 1.12.x, Terraform destroy/recreate)
- ✅ Renovate integration for version detection and PR creation
- ✅ Self-hosted GitHub Actions runners in Kubernetes cluster
- ✅ Kubernetes Job-based upgrade orchestration for cattle workflow (runner-independent)

**Validation Gates**:
- ✅ Pre-flight GPU patch validation (paths, no-cgroups config)
- ✅ Post-creation NIC count validation (prevent 27-hour outage)
- ✅ ETCD quorum health checks (before/after controller upgrades)
- ✅ Talos image availability verification

**Infrastructure**:
- ✅ S3 backend for Terraform state (enable CI/CD access)
- ✅ GitHub Actions Secrets for credential management
- ✅ Network policies restricting runner egress
- ✅ RBAC for runner service account (node/pod management)

**Operational**:
- ✅ Workflow monitoring via GitHub Actions UI
- ✅ Job status tracking via kubectl
- ✅ Emergency rollback runbooks

### 2.2 Out-of-Scope Features

**Explicitly Not Included**:
- ❌ Automated Renovate PR merge (user must manually approve)
- ❌ Blue-green cluster upgrades (parallel cluster creation)
- ❌ Automated rollback workflows (manual scripts only)
- ❌ Cross-version compatibility validation (rely on Talos docs)
- ❌ Canary testing (deploy test workload before full rollout)
- ❌ Slack/Discord notifications (Prometheus metrics only)
- ❌ Multi-cluster upgrades (single cluster only)

### 2.3 Integration Points

**Existing Systems**:
| System | Integration Method | Purpose |
|--------|-------------------|---------|
| **Renovate** | Custom regex managers | Detect Talos version changes in `terraform/variables.tf` |
| **Flux GitOps** | Namespace/RBAC deployment | Deploy self-hosted runners via HelmRelease |
| **Terraform** | S3 backend migration | Enable CI/CD access to infrastructure state |
| **Proxmox VE** | API token authentication | VM destroy/create operations |
| **Talos API** | Talosconfig in Secrets | Node upgrade, patch, validation commands |
| **Kubernetes API** | Kubeconfig in Secrets | Node cordon/drain/uncordon, Job management |

**External Dependencies**:
- **factory.talos.dev**: Talos installer images (critical path)
- **GitHub API**: Actions runner registration, workflow triggers
- **S3 endpoint**: Terraform state read/write (critical path)
- **Proxmox API** (10.20.66.4:8006): VM management (critical path)

---

## 3. Technology Stack

### 3.1 Core Technologies

| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| **OS** | Talos Linux | 1.11.5 → 1.12.x | Immutable Kubernetes OS |
| **Container Runtime** | containerd | Embedded in Talos | Container execution |
| **Orchestration** | Kubernetes | 1.32.x | Cluster orchestration |
| **IaC** | Terraform | 1.9.8 | Proxmox VM provisioning |
| **GitOps** | Flux CD | 2.x | Application deployment |
| **CI/CD** | GitHub Actions | N/A | Workflow automation |
| **State Backend** | S3 (AWS/MinIO) | N/A | Terraform state storage |
| **Dependency Automation** | Renovate | N/A | Version update PRs |

### 3.2 Infrastructure Components

**Proxmox Virtualization**:
- **Proxmox VE**: 8.x cluster (4 nodes: baldar, heimdall, loki, thor)
- **Network Bridge**: vmbr1 (10Gig bond, native VLAN 10.20.66.0/23)
- **VLAN Tagging**: VLAN 62 (IoT), VLAN 81 (DMZ) on worker eth1/eth2

**Talos Schematics**:
- **Controller Schematic**: `366fd68945d42e0b6428f65068c83c2c3c08e3afb0e3bc3f00e04b28e2ad0ce2`
- **Worker Schematic**: `990731763242a6b3cf735e49d0f550ce4068b4d0e7f4dfbb49a31799b698877e`

**GPU Hardware**:
- **k8s-work-4**: NVIDIA RTX A2000 (12GB VRAM)
- **k8s-work-14**: NVIDIA RTX A5000 (24GB VRAM)

**Talos Versioning Semantics**:
- **Semantic Versioning**: Talos follows standard semver (MAJOR.MINOR.PATCH)
- **Patch releases**: 1.11.0 → 1.11.1 (bug fixes, security patches)
- **Minor releases**: 1.11.x → 1.12.x (new features, some breaking changes)
- **Major releases**: 1.x.x → 2.x.x (significant breaking changes)
- **Upgrade requirement**: Must upgrade sequentially through all minor versions (cannot skip)
- **Example path**: 1.11.x → 1.12.x → 1.13.x (each step required)
- **Official support**: Talos supports in-place upgrades via `talosctl upgrade` for all version types

### 3.3 Renovate Configuration

**Custom Managers** for Talos version detection:

```json5
// .github/renovate.json5
{
  "customManagers": [
    {
      "customType": "regex",
      "fileMatch": ["^terraform/variables\\.tf$"],
      "matchStrings": [
        "talos_version\\s*=\\s*\"(?<currentValue>.*?)\""
      ],
      "datasourceTemplate": "github-releases",
      "depNameTemplate": "siderolabs/talos",
      "extractVersionTemplate": "^v(?<version>.*)$"
    },
    {
      "customType": "regex",
      "fileMatch": ["^terraform/variables\\.tf$"],
      "matchStrings": [
        "controller_schematic_id\\s*=\\s*\"(?<currentDigest>.*?)\"",
        "worker_schematic_id\\s*=\\s*\"(?<currentDigest>.*?)\""
      ],
      "datasourceTemplate": "docker",
      "depNameTemplate": "factory.talos.dev/installer",
      "currentValueTemplate": "{{currentDigest}}",
      "autoReplaceStringTemplate": "{{newDigest}}"
    }
  ],
  "packageRules": [
    {
      "matchDatasources": ["github-releases"],
      "matchPackageNames": ["siderolabs/talos"],
      "matchUpdateTypes": ["patch"],
      "labels": ["upgrade-pets", "dependencies", "talos"],
      "automerge": false,
      "schedule": ["on saturday"]
    },
    {
      "matchDatasources": ["github-releases"],
      "matchPackageNames": ["siderolabs/talos"],
      "matchUpdateTypes": ["minor", "major"],
      "labels": ["upgrade-cattle", "dependencies", "talos"],
      "automerge": false,
      "schedule": ["on saturday"]
    }
  ]
}
```

**Label Assignment Logic**:
- **`upgrade-pets`**: Automatically added for patch releases (1.11.0 → 1.11.1, 1.11.1 → 1.11.2)
- **`upgrade-cattle`**: Automatically added for minor/major releases (1.11.x → 1.12.x, 1.x.x → 2.x.x)

---

## 4. Component Architecture

### 4.1 Dual-Workflow Strategy

The architecture implements **two distinct upgrade workflows** based on Talos version change type:

#### Workflow 1: Pets Upgrade (Patch Releases)

**Trigger**: PR merged with `upgrade-pets` label
**Use Case**: Patch releases (1.11.0 → 1.11.1, 1.11.1 → 1.11.2)
**Method**: Pure in-place `talosctl upgrade` command (NO Terraform)
**Duration**: ~45 minutes for 15 nodes (~3 min/node)
**Risk Level**: LOW (no infrastructure changes, preserves node identity)

**Workflow Diagram**:
```
Renovate detects patch release (1.11.0 → 1.11.1)
  ↓
Opens PR with `upgrade-pets` label
  ↓
User reviews and merges PR
  ↓
GitHub Actions workflow triggers
  ↓
Validate: Talos image availability at factory.talos.dev
  ↓
Backup: ETCD snapshot on k8s-ctrl-1
  ↓
Upgrade Workers (sequential, max-parallel: 1)
  ├─ kubectl cordon $NODE
  ├─ kubectl drain $NODE --timeout=5m
  ├─ talosctl upgrade --nodes $NODE_IP --image factory.talos.dev/installer/$SCHEMATIC:$VERSION
  ├─ Wait for node Ready (talosctl will auto-preserve, A-B rollback available)
  └─ kubectl uncordon $NODE
  ↓
Upgrade Controllers (sequential, ETCD quorum preserved)
  ├─ Check ETCD quorum ≥2/3 members healthy
  ├─ kubectl cordon $NODE
  ├─ kubectl drain $NODE --timeout=5m
  ├─ talosctl upgrade --nodes $NODE_IP --image factory.talos.dev/installer/$SCHEMATIC:$VERSION
  ├─ Wait for ETCD member rejoin (Talos auto-serializes if multiple triggered)
  ├─ Wait 2 minutes (ETCD stabilization)
  └─ kubectl uncordon $NODE
  ↓
Post-upgrade validation: all nodes Ready, ETCD quorum 3/3, versions match
```

**Key Safety Measures**:
- **Pure `talosctl upgrade`**: No Terraform, no VM destruction, preserves node identity
- **A-B Image Scheme**: Automatic rollback if upgrade fails to boot
- **Sequential execution** (`max-parallel: 1`): Prevents cluster instability
- **Workers first**: Less critical than controllers
- **ETCD quorum protection**: Talos refuses upgrades that would break quorum
- **Automatic controller serialization**: Even if triggered simultaneously, Talos upgrades controllers one at a time

**Talos Features Leveraged**:
- `--preserve` flag automatically applied (Talos 1.8+)
- `--stage` flag available if file locks prevent normal upgrade
- Automatic rollback to previous kernel/OS if new version fails to boot

#### Workflow 2: Cattle Upgrade (Minor/Major Releases)

**Trigger**: PR merged with `upgrade-cattle` label
**Use Case**: Minor/major releases (1.11.x → 1.12.x, 1.x.x → 2.x.x)
**Method**: Terraform destroy + recreate VMs (full infrastructure refresh)
**Duration**: ~90-120 minutes for 15 nodes (~6-8 min/node)
**Risk Level**: MEDIUM (infrastructure recreation)

**⚠️ CRITICAL REQUIREMENT**: Cannot skip Talos minor versions. Must upgrade sequentially through intermediate releases:
- ✅ **CORRECT**: 1.11.x → 1.12.x → 1.13.x (sequential)
- ❌ **WRONG**: 1.11.x → 1.13.x (skipping 1.12.x not allowed)

**Rationale for Cattle Approach**:
- Talos officially supports in-place upgrades for minor/major releases
- We CHOOSE cattle (destroy/recreate) for **zero configuration drift** and clean slate
- Ensures GPU patches, NIC configurations, and all infrastructure settings are validated from scratch

**Workflow Diagram**:
```
Renovate detects minor/major release (1.11.x → 1.12.x)
  ↓
Opens PR with `upgrade-cattle` label
  ↓
User reviews and merges PR
  ↓
GitHub Actions workflow triggers
  ↓
Validate: GPU patches (/var/ paths, no-cgroups config)
  ↓
Create upgrade credentials Secret (Talos, Kube, Proxmox, S3)
  ↓
Create Kubernetes Jobs for each node (with anti-affinity)
  ↓
Jobs Execute (parallel for workers, sequential for controllers)
  ├─ Cordon target node
  ├─ Drain workloads
  ├─ Terraform destroy old VM
  ├─ Terraform apply new VM (updated Talos version from variables.tf)
  ├─ Wait for node to join cluster
  ├─ Wait for node Ready
  ├─ Apply GPU patches (if GPU node: k8s-work-4, k8s-work-14)
  ├─ Validate NIC count (3 for workers, 1 for controllers)
  ├─ Validate VLAN interfaces (eth1 VLAN 62, eth2 VLAN 81)
  └─ Uncordon node
  ↓
Monitor Job completion (detect failures early)
  ↓
Post-upgrade validation: all nodes Ready, versions match, GPU working
  ↓
Cleanup: Delete Jobs, delete credentials Secret
```

**Key Innovations**:
- **Kubernetes Job Pattern**: Jobs execute upgrades independently of runner pod lifecycle
- **Pod Anti-Affinity**: Jobs schedule on different nodes than upgrade target (prevents eviction)
- **Conditional GPU Patching**: Bash logic applies patches only to k8s-work-4 and k8s-work-14
- **NIC Count Validation**: Fails upgrade if worker has ≠3 NICs (prevents 27-hour outage)

### 4.2 Job-Based Orchestration

**Problem**: Self-hosted GitHub Actions runners run as pods in the cluster. When a runner's node is drained during upgrade, the runner pod is evicted, terminating the workflow mid-execution.

**Solution**: Use Kubernetes Jobs to execute actual upgrade operations:

```
GitHub Actions Workflow (orchestrator)
  ↓
Creates Kubernetes Job manifests
  ↓
Jobs have pod anti-affinity (schedule on different node than target)
  ↓
kubectl apply Job manifests
  ↓
Jobs execute upgrades autonomously
  ↓
Workflow monitors Job status via kubectl
  ↓
If runner pod dies, Jobs continue running
  ↓
Workflow can resume monitoring from new runner pod
```

**Job Manifest Example**:
```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: upgrade-k8s-work-5
  namespace: github-actions
spec:
  backoffLimit: 0  # Fail fast
  ttlSecondsAfterFinished: 3600  # Keep for debugging
  template:
    spec:
      restartPolicy: Never
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              # CRITICAL: Do NOT schedule on target node
              - key: kubernetes.io/hostname
                operator: NotIn
                values: ["k8s-work-5"]
      containers:
      - name: upgrade
        image: ghcr.io/jlengelbrecht/talos-upgrade-operator:v1.0.0
        env:
        - name: TARGET_NODE
          value: "k8s-work-5"
        - name: IS_GPU
          value: "false"
        command:
        - /bin/bash
        - -c
        - |
          # Upgrade logic here
          kubectl cordon $TARGET_NODE
          kubectl drain $TARGET_NODE --timeout=300s
          terraform destroy -target='module.worker_nodes["$TARGET_NODE"]'
          terraform apply -target='module.worker_nodes["$TARGET_NODE"]'
          kubectl wait --for=condition=Ready node/$TARGET_NODE
          # Validate NICs
          [ $(talosctl get links | grep -c "^eth") -eq 3 ] || exit 1
```

### 4.3 Critical Validation Gates

#### GPU Patch Validation (Pre-Flight)

**Problem**: GPU patches using `/etc/` paths cause boot loops. NVIDIA container runtime requires `no-cgroups = true` to bypass eBPF incompatibility.

**Validation Steps**:
```yaml
- name: Validate GPU patches
  run: |
    # Check k8s-work-4 patch
    grep -q "/var/cri" talos/patches/k8s-work-4/nvidia-gpu.yaml
    grep -q "/var/etc/nvidia-container-runtime" talos/patches/k8s-work-4/nvidia-gpu.yaml
    grep -q "no-cgroups.*true" talos/patches/k8s-work-4/nvidia-gpu.yaml

    # Check k8s-work-14 patch (same requirements)
    grep -q "/var/cri" talos/patches/k8s-work-14/nvidia-gpu.yaml
    grep -q "/var/etc/nvidia-container-runtime" talos/patches/k8s-work-14/nvidia-gpu.yaml
    grep -q "no-cgroups.*true" talos/patches/k8s-work-14/nvidia-gpu.yaml

    # CRITICAL: Ensure NO /etc/ paths (causes boot loops)
    if grep -q "path.*:/etc/" talos/patches/k8s-work-*/nvidia-gpu.yaml; then
      echo "ERROR: GPU patches use /etc/ paths"
      exit 1
    fi
```

**Required GPU Patch Configuration**:
```yaml
# talos/patches/k8s-work-4/nvidia-gpu.yaml
machine:
  files:
  - path: /var/cri/conf.d/20-customization.part
    content: |
      [plugins."io.containerd.grpc.v1.cri".containerd.runtimes.nvidia]
        runtime_type = "io.containerd.runc.v2"

  - path: /var/etc/nvidia-container-runtime/config.toml
    content: |
      [nvidia-container-cli]
      no-cgroups = true  # CRITICAL: Bypass eBPF incompatibility
```

#### NIC Count Validation (Post-Creation)

**Problem**: Terraform misconfiguration can result in workers missing VLAN NICs (eth1, eth2), breaking Multus CNI and causing 27-hour Home Assistant outages.

**Validation Steps**:
```bash
# In Job container, after terraform apply
NIC_COUNT=$(talosctl -n $NODE_IP get links | grep -c "^eth" || true)

if [[ "$TARGET_NODE" == k8s-ctrl-* ]]; then
  # Controllers must have exactly 1 NIC (eth0 native VLAN)
  [ "$NIC_COUNT" -eq 1 ] || {
    echo "ERROR: Controller has $NIC_COUNT NICs (expected 1)"
    exit 1
  }
else
  # Workers must have exactly 3 NICs (eth0, eth1 VLAN 62, eth2 VLAN 81)
  [ "$NIC_COUNT" -eq 3 ] || {
    echo "ERROR: Worker has $NIC_COUNT NICs (expected 3)"
    echo "Missing NICs will break Multus CNI and VLAN workloads"
    exit 1
  }

  # Validate VLAN tags
  talosctl -n $NODE_IP get links | grep "eth1.*62" || {
    echo "ERROR: IoT VLAN 62 missing on eth1"
    exit 1
  }
  talosctl -n $NODE_IP get links | grep "eth2.*81" || {
    echo "ERROR: DMZ VLAN 81 missing on eth2"
    exit 1
  }
fi
```

#### ETCD Quorum Preservation

**Problem**: Upgrading >1 controller simultaneously can break ETCD quorum (need 2/3 members).

**Safety Measures**:
```yaml
# Sequential controller upgrades (max-parallel: 1)
strategy:
  max-parallel: 1
  matrix:
    node: [k8s-ctrl-1, k8s-ctrl-2, k8s-ctrl-3]

# Pre-upgrade check
- name: Check ETCD quorum
  run: |
    MEMBERS=$(talosctl -n $NODE_IP etcd members | grep -c "Healthy")
    if [ "$MEMBERS" -lt 2 ]; then
      echo "ERROR: ETCD quorum lost (only $MEMBERS healthy)"
      exit 1
    fi

# Post-upgrade wait
- name: Wait for ETCD stabilization
  run: sleep 120  # 2 minutes between controllers
```

### 4.4 Network Architecture Implementation

**Terraform Configuration** (`terraform/modules/talos-node/main.tf`):

```hcl
# eth0: Main network (native VLAN, NO tag)
network_device {
  bridge      = "vmbr1"  # 10Gig bond on all Proxmox hosts
  mac_address = var.mac_address  # CRITICAL: Preserve for DHCP reservation
  model       = "virtio"
  # NO vlan_id - uses native VLAN (10.20.66.0/23)
}

# eth1: IoT VLAN 62 (workers only)
dynamic "network_device" {
  for_each = var.is_controlplane ? [] : [1]  # Empty for controllers
  content {
    bridge   = "vmbr1"
    vlan_id  = 62  # IoT devices (Home Assistant, ESPHome, etc.)
    model    = "virtio"
    # MAC auto-generated (not preserved across recreates)
  }
}

# eth2: DMZ VLAN 81 (workers only)
dynamic "network_device" {
  for_each = var.is_controlplane ? [] : [1]  # Empty for controllers
  content {
    bridge   = "vmbr1"
    vlan_id  = 81  # DMZ services (Plex, external-facing apps)
    model    = "virtio"
    # MAC auto-generated (not preserved across recreates)
  }
}
```

**Network Summary**:
| Node Type | NICs | VLAN Tags | Purpose |
|-----------|------|-----------|---------|
| **Controllers** | 1 | None (native) | Cluster management traffic only |
| **Workers** | 3 | None, 62, 81 | Cluster + IoT + DMZ workloads |

---

## 5. Infrastructure & Deployment

### 5.1 Kubernetes Infrastructure

**Namespace and RBAC**:

```yaml
# kubernetes/apps/github-actions/namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: github-actions
  labels:
    pod-security.kubernetes.io/enforce: baseline

---
# kubernetes/apps/github-actions/rbac.yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: github-actions-runner
  namespace: github-actions

---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: github-actions-runner
rules:
# Node management
- apiGroups: [""]
  resources: ["nodes"]
  verbs: ["get", "list", "patch", "update"]

# Pod eviction for draining
- apiGroups: [""]
  resources: ["pods", "pods/eviction"]
  verbs: ["get", "list", "delete", "create"]

# Job management
- apiGroups: ["batch"]
  resources: ["jobs"]
  verbs: ["get", "list", "create", "delete", "watch"]

# Secret management
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "create", "delete"]

---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: github-actions-runner
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: ClusterRole
  name: github-actions-runner
subjects:
- kind: ServiceAccount
  name: github-actions-runner
  namespace: github-actions
```

### 5.2 Self-Hosted GitHub Actions Runner

```yaml
# kubernetes/apps/github-actions/runner-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: github-actions-runner
  namespace: github-actions
spec:
  replicas: 3  # 3 runners for parallel worker upgrades
  selector:
    matchLabels:
      app: github-actions-runner
  template:
    metadata:
      labels:
        app: github-actions-runner
    spec:
      serviceAccountName: github-actions-runner

      # Spread runners across nodes
      affinity:
        podAntiAffinity:
          preferredDuringSchedulingIgnoredDuringExecution:
          - weight: 100
            podAffinityTerm:
              labelSelector:
                matchLabels:
                  app: github-actions-runner
              topologyKey: kubernetes.io/hostname

      containers:
      - name: runner
        image: ghcr.io/actions/actions-runner:latest
        env:
        - name: REPO_URL
          value: "https://github.com/jlengelbrecht/prox-ops"
        - name: RUNNER_TOKEN
          valueFrom:
            secretKeyRef:
              name: github-runner-token
              key: token
        - name: LABELS
          value: "self-hosted,kubernetes,talos-upgrade"

        resources:
          requests:
            cpu: "500m"
            memory: "512Mi"
          limits:
            cpu: "2000m"
            memory: "2Gi"
```

### 5.3 Upgrade Operator Container Image

**Dockerfile**:
```dockerfile
# docker/talos-upgrade-operator/Dockerfile
FROM alpine:3.19

# Install required tools
RUN apk add --no-cache bash curl jq git openssh-client ca-certificates

# Install kubectl
RUN curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl" && \
    chmod +x kubectl && mv kubectl /usr/local/bin/

# Install talosctl
ARG TALOS_VERSION=v1.11.5
RUN curl -sL https://github.com/siderolabs/talos/releases/download/${TALOS_VERSION}/talosctl-linux-amd64 -o /usr/local/bin/talosctl && \
    chmod +x /usr/local/bin/talosctl

# Install Terraform
ARG TERRAFORM_VERSION=1.9.8
RUN curl -LO https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip && \
    unzip terraform_${TERRAFORM_VERSION}_linux_amd64.zip && \
    mv terraform /usr/local/bin/ && \
    rm terraform_${TERRAFORM_VERSION}_linux_amd64.zip

WORKDIR /workspace
CMD ["/bin/bash"]
```

### 5.4 Secret Management

**GitHub Actions Secrets** (configured at repository level):

| Secret Name | Description | Example Value |
|-------------|-------------|---------------|
| `TALOS_CONFIG` | Talosconfig (base64) | `Y2x1c3RlcjogcHJveC1vcH...` |
| `KUBECONFIG` | Kubeconfig (base64) | `YXBpVmVyc2lvbjogdjEKY...` |
| `PROXMOX_API_TOKEN` | Proxmox API token | `root@pam!terraform=abc123...` |
| `TF_STATE_S3_ACCESS_KEY` | S3 access key | `AKIAIOSFODNN7EXAMPLE` |
| `TF_STATE_S3_SECRET_KEY` | S3 secret key | `wJalrXUtnFEMI/K7MDENG/...` |
| `TF_STATE_S3_BUCKET` | S3 bucket name | `prox-ops-terraform-state` |
| `TF_STATE_S3_REGION` | S3 region | `us-east-1` |
| `TF_STATE_S3_ENDPOINT` | S3 endpoint URL | `https://s3.us-east-1.amazonaws.com` |

### 5.5 Terraform S3 Backend

```hcl
# terraform/backend.tf
terraform {
  backend "s3" {
    bucket         = "prox-ops-terraform-state"
    key            = "talos-cluster/terraform.tfstate"
    region         = "us-east-1"
    endpoint       = "https://s3.us-east-1.amazonaws.com"
    encrypt        = true
    dynamodb_table = "prox-ops-terraform-locks"
  }
}
```

**S3 Backend Setup**:
```bash
#!/bin/bash
# scripts/setup-s3-backend.sh

BUCKET="prox-ops-terraform-state"
REGION="us-east-1"
TABLE="prox-ops-terraform-locks"

# Create S3 bucket
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"

# Enable versioning
aws s3api put-bucket-versioning \
  --bucket "$BUCKET" \
  --versioning-configuration Status=Enabled

# Enable encryption
aws s3api put-bucket-encryption \
  --bucket "$BUCKET" \
  --server-side-encryption-configuration '{
    "Rules": [{
      "ApplyServerSideEncryptionByDefault": {
        "SSEAlgorithm": "AES256"
      }
    }]
  }'

# Block public access
aws s3api put-public-access-block \
  --bucket "$BUCKET" \
  --public-access-block-configuration \
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

# Create DynamoDB table for locking
aws dynamodb create-table \
  --table-name "$TABLE" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --provisioned-throughput ReadCapacityUnits=5,WriteCapacityUnits=5 \
  --region "$REGION"

echo "✅ S3 backend setup complete"
```

### 5.6 Network Policies

```yaml
# kubernetes/apps/github-actions/network-policy.yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: github-actions-runner-egress
  namespace: github-actions
spec:
  endpointSelector:
    matchLabels:
      app: github-actions-runner

  egress:
  # Allow DNS
  - toEndpoints:
    - matchLabels:
        k8s:io.kubernetes.pod.namespace: kube-system
        k8s:app.kubernetes.io/name: coredns
    toPorts:
    - ports:
      - port: "53"
        protocol: UDP

  # Allow GitHub API
  - toFQDNs:
    - matchPattern: "*.github.com"
    - matchPattern: "*.githubusercontent.com"
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP

  # Allow Kubernetes API
  - toEndpoints:
    - matchLabels:
        component: kube-apiserver
    toPorts:
    - ports:
      - port: "6443"
        protocol: TCP

  # Allow Proxmox API
  - toCIDR:
    - 10.20.66.4/32
    toPorts:
    - ports:
      - port: "8006"
        protocol: TCP

  # Allow Talos API (entire cluster network)
  - toCIDR:
    - 10.20.66.0/23
    toPorts:
    - ports:
      - port: "50000"  # Talos API
        protocol: TCP

  # Allow S3
  - toFQDNs:
    - matchPattern: "*.amazonaws.com"
    toPorts:
    - ports:
      - port: "443"
        protocol: TCP
```

---

## 6. Testing Strategy and Quality Assurance

### 6.1 Pre-Production Testing

**Test Plan**:
1. ✅ Single-node pets upgrade (k8s-work-16) - patch release test
2. ✅ Single-node cattle upgrade (k8s-work-16) - minor release test
3. ✅ GPU patch validation (k8s-work-4)
4. ✅ NIC configuration preservation
5. ✅ ETCD quorum preservation
6. ✅ Emergency rollback procedures

**Test Automation**:
```bash
# scripts/test-gpu-configuration.sh
GPU_NODES=("k8s-work-4" "k8s-work-14")

for NODE in "${GPU_NODES[@]}"; do
  # Test 1: NVIDIA extensions loaded
  talosctl -n $NODE get extensions | grep nvidia

  # Test 2: GPU allocatable resources
  kubectl get node $NODE -o json | jq '.status.allocatable["nvidia.com/gpu"]'

  # Test 3: No-cgroups configuration
  talosctl -n $NODE read /var/etc/nvidia-container-runtime/config.toml | grep "no-cgroups.*true"

  # Test 4: GPU test pod
  kubectl run gpu-test-$NODE --rm -i \
    --image=nvidia/cuda:11.8.0-base-ubuntu22.04 \
    --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"'$NODE'"},"containers":[{"name":"test","image":"nvidia/cuda:11.8.0-base-ubuntu22.04","command":["nvidia-smi"],"resources":{"limits":{"nvidia.com/gpu":"1"}}}]}}'
done
```

### 6.2 Performance Benchmarking

**Benchmark Results** (projected):
```
Pets Upgrade (patch release, single node):     ~3 minutes (talosctl upgrade)
Cattle Upgrade (minor release, single node):   ~6 minutes (destroy/recreate)

Full Cluster (15 nodes):
  Pets workflow (patch):    45 minutes  (3 min × 15 nodes sequential)
  Cattle workflow (minor):  90 minutes  (6 min × 15 nodes sequential)
```

---

## 7. Operational Considerations and Monitoring

### 7.1 Workflow Observability

**GitHub Actions Monitoring**:
```bash
# List recent workflow runs
gh run list --workflow=talos-pets-upgrade.yml --limit 10

# Watch running workflow
gh run watch <run-id>

# View workflow logs
gh run view <run-id> --log
```

**Kubernetes Job Monitoring**:
```bash
# Watch Job progress
watch -n 5 kubectl get jobs -n github-actions -l app=talos-cattle-upgrade

# View Job logs
kubectl logs -n github-actions job/upgrade-k8s-work-5 --tail=100
```

### 7.2 Runbook for Common Scenarios

**Scenario 1: Single Node Upgrade Failure**
```bash
# 1. Identify failed node
kubectl get nodes -o wide

# 2. Check Job logs
FAILED_JOB=$(kubectl get jobs -n github-actions -l app=talos-cattle-upgrade -o jsonpath='{.items[?(@.status.failed>=1)].metadata.name}')
kubectl logs -n github-actions job/$FAILED_JOB

# 3. Manual intervention (if NIC issue)
# Verify Terraform config, manually add missing NICs via Proxmox UI

# 4. Retry upgrade
kubectl delete job -n github-actions $FAILED_JOB
# Re-run workflow
```

**Scenario 2: ETCD Quorum Lost**
```bash
# 1. Check ETCD status
talosctl -n k8s-ctrl-1 etcd members

# 2. Stop controller upgrades immediately
# Cancel GitHub Actions workflow

# 3. Recover ETCD from backup
talosctl -n k8s-ctrl-1 etcd snapshot /tmp/etcd-backup-latest.db
```

### 7.3 Rollback Procedures

**Emergency Rollback (Pets)**:
```bash
#!/bin/bash
# scripts/emergency-rollback-pets.sh

ROLLBACK_VERSION="v1.11.4"

for NODE in k8s-work-{1..16} k8s-ctrl-{1..3}; do
  kubectl cordon $NODE
  kubectl drain $NODE --timeout=180s
  talosctl -n $NODE upgrade --image factory.talos.dev/installer/...:$ROLLBACK_VERSION --preserve
  kubectl wait --for=condition=Ready node/$NODE
  kubectl uncordon $NODE
done
```

---

## 8. Future Enhancements

### 8.1 Planned Improvements

1. **Blue-Green Upgrade Strategy** (v2.0.0)
   - Create parallel cluster with new version
   - Migrate workloads gradually
   - Zero-downtime upgrades

2. **Automated Canary Testing** (v1.1.0)
   - Deploy test workload to upgraded node
   - Auto-rollback on test failures

3. **Renovate Auto-Labeling** (v1.2.0)
   - Automatically apply `upgrade-pets` or `upgrade-cattle` based on semver

4. **Advanced GPU Handling** (v1.3.0)
   - Drain GPU workloads to other GPU nodes
   - Validate GPU migration success

5. **Flux Integration** (v1.4.0)
   - Pause Flux during upgrades
   - Validate Flux resources post-upgrade

### 8.2 Known Limitations

1. **Manual PR Merge Required**: User must approve upgrades
2. **Sequential Controllers**: Adds ~6 minutes vs parallel
3. **No Cross-Version Validation**: Relies on Talos docs
4. **Network Dependency**: Requires internet access
5. **No Rollback Automation**: Manual scripts only

---

## 9. Conclusion

### 9.1 Architecture Summary

This brownfield architecture defines a **comprehensive dual-workflow automation strategy** for Talos cluster upgrades, achieving:

**Primary Goals**:
✅ **Zero Manual Intervention**: User only merges Renovate PR
✅ **Dual-Workflow Support**: Pets (patch releases) and Cattle (minor/major releases)
✅ **90% Effort Reduction**: From 4-5 hours to 45-120 minutes
✅ **GPU Node Safety**: Conditional patching with validation
✅ **Network Preservation**: Multi-NIC VLAN configuration maintained
✅ **ETCD Quorum Safety**: Sequential controller upgrades
✅ **Sequential Minor Upgrades**: Enforces upgrade through intermediate Talos releases

**Key Innovations**:
1. **Dual-Workflow Design**: Pets (talosctl) for patches, Cattle (Terraform) for minor/major
2. **Job-Based Orchestration**: Survives runner pod eviction (cattle workflow)
3. **Pod Anti-Affinity**: Jobs schedule on different nodes
4. **Conditional GPU Patching**: GPU-specific validation
5. **Multi-Phase Validation**: Pre/in/post-flight checks
6. **S3 Backend Integration**: CI/CD state access

**Design Choice Rationale**:
- Talos officially supports in-place upgrades for ALL version types (patch, minor, major)
- We use **cattle for minor/major** by choice (not requirement) for zero configuration drift
- We use **pets for patch** for speed and simplicity (preserves node identity)

**Risk Mitigation**:
- R7 (NIC Configuration): NIC count validation
- R16 (MAC Preservation): Terraform variables
- R17 (GPU Patches): Bash conditionals
- R18 (VLAN Tags): Dynamic Terraform blocks

### 9.2 Success Metrics

**Efficiency**: 90% manual work reduction
**Reliability**: Zero drift via cattle upgrades
**Safety**: ETCD quorum preservation, validation gates
**Observability**: Prometheus metrics, Job monitoring

### 9.3 Deployment Roadmap

**Phase 1** (Week 1): S3 backend, runners, secrets
**Phase 2** (Week 2): Workflows, Renovate, network policies
**Phase 3** (Week 3): Testing (pets, cattle, GPU, rollback)
**Phase 4** (Week 4): Production rollout
**Phase 5** (Ongoing): Continuous improvement

### 9.4 Final Recommendations

1. Start with pets workflow testing
2. Monitor first upgrade closely
3. Keep backup state before cattle
4. Test rollback before production
5. Document all incidents

**This architecture provides production-ready foundation for fully automated Talos cluster lifecycle management.**

---

**Document End**
