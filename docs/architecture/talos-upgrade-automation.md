# Architecture: Talos Kubernetes Upgrade Automation

**Version**: 3.0 (Docker-Based External Runner)
**Status**: Approved
**Created**: 2025-11-19
**Updated**: 2025-11-20
**Architect**: Winston (BMad Architect)
**Related Epic**: EPIC-019

---

## Executive Summary

This document defines the architecture for fully automated Talos Linux and Kubernetes version upgrades using an **intelligent routing approach** that separates **cattle upgrades** (major/minor versions with infrastructure recreation) from **pets upgrades** (patch versions with in-place updates).

### Key Decisions

1. **External Runner**: Docker container in dedicated VM on Proxmox (outside cluster)
2. **Cattle Upgrades**: Terraform destroy/create for major/minor versions → Workers + control plane
3. **Pets Upgrades**: In-place `talosctl upgrade` for patch versions → All nodes (future implementation)
4. **Routing Logic**: Automated semver detection in router workflow
5. **Execution**: Zero workflow delays, runner always available

### Success Criteria

- ✅ Renovate detects new Talos versions and updates `terraform/variables.tf`
- ✅ Router workflow automatically detects version type (major/minor vs patch)
- ✅ Cattle workflow executes on external runner (no circular dependency)
- ✅ Zero manual intervention for version detection and routing
- ✅ Complete audit trail in Git history

---

## Problem Statement

### The Circular Dependency

**Original Approach**: Self-hosted GitHub Actions runners deployed in-cluster (ARC) executing cattle upgrade workflows that destroy/recreate worker nodes.

**Failure Mode**:
```
1. Workflow starts on runner pod running on k8s-work-1
2. Workflow drains k8s-work-1 (where runner is running)
3. Force drain kills runner pod
4. GitHub Actions DOES NOT re-queue job (ARC limitation)
5. Job becomes "orphaned" - tied to dead runner
6. Workflow hangs until timeout (15-30 minutes)
```

**Root Cause**: ARC ephemeral JIT runners don't trigger GitHub job re-queue when force-killed mid-execution. This is a known limitation (ARC issues #4148, #4203).

**Test Evidence**: Run 19514552854 showed runner force-evicted at 19:58:15Z, replacement runner created immediately, but GitHub API still showed `runner_name: "kwvfq"` (dead runner) at 20:05:00Z.

### Why Traditional Solutions Don't Work

| Solution | Why It Fails |
|----------|--------------|
| **Resilient runner architecture** (anti-affinity, PDB) | Infrastructure worked perfectly, but GitHub job scheduler doesn't re-queue |
| **Split runner pools** (A/B partitions) | Too complex, tight coupling to topology, difficult to maintain |
| **Control plane runners** | Defeats purpose of "cattle", creates "pets" |
| **Waiting for ARC fix** | No timeline, architectural limitation not a bug |
| **JIT ephemeral runners** | Adds 30+ seconds delay before workflow starts, overcomplicated |

---

## Architecture Overview

### Upgrade Strategy

```
┌─────────────────────────────────────────────────────────────┐
│                  Talos Version Update Types                  │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  Major/Minor (1.10.x → 1.11.x)     Patches (1.10.8 → 1.10.9) │
│  ────────────────────────────       ──────────────────────── │
│  • High disruption                  • Low disruption          │
│  • Clean infrastructure slate       • Incremental update      │
│  • Terraform destroy/create         • In-place talosctl      │
│  • Quarterly/annually               • Monthly                 │
│  • Workers + control plane          • All nodes               │
│                                                               │
│  ┌───────────────────────┐          ┌────────────────────┐  │
│  │  Cattle Workflow      │          │  Pets Workflow     │  │
│  │  (External Runner)    │          │  (External Runner) │  │
│  └───────────────────────┘          └────────────────────┘  │
│           │                                   │               │
│           ▼                                   ▼               │
│  upgrade-cattle.yaml                 upgrade-pets.yaml       │
│  (GitHub Actions)                    (GitHub Actions)        │
│                                       [NOT YET IMPLEMENTED]  │
└─────────────────────────────────────────────────────────────┘
```

### Component Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     GitHub Repository                        │
│  ┌──────────────────┐         ┌──────────────────┐         │
│  │ Terraform        │         │ K8s Manifests    │         │
│  │ (Infrastructure) │         │ (Applications)   │         │
│  └────────┬─────────┘         └──────────┬───────┘         │
│           │                                │                 │
│  ┌────────▼────────────────────────────────▼──────────┐    │
│  │         GitHub Actions Workflows                    │    │
│  │  ┌────────────────┐   ┌──────────────────┐         │    │
│  │  │ Router         │   │ Cattle Workflow  │         │    │
│  │  │ (ubuntu-latest)│   │ (external runner)│         │    │
│  │  │ • Detects ver  │   │ • Terraform      │         │    │
│  │  │ • Routes logic │   │ • Destroy/Create │         │    │
│  │  └────────────────┘   └──────────────────┘         │    │
│  └──────────────────────────────────────────────────────┘   │
│           │ triggers                     │ runs on           │
└───────────┼──────────────────────────────┼───────────────────┘
            │                              │
            │                              ▼
            │                  ┌──────────────────────────────┐
            │                  │ PROXMOX (baldar)             │
            │                  │  ┌─────────────────────────┐ │
            │                  │  │ VM: cattle-runner       │ │
            │                  │  │ ────────────────────────│ │
            │                  │  │ Ubuntu 22.04 LTS        │ │
            │                  │  │ 2 vCPU, 4GB RAM         │ │
            │                  │  │                         │ │
            │                  │  │  ┌──────────────────┐  │ │
            │                  │  │  │ Docker Daemon    │  │ │
            │                  │  │  │  ┌─────────────┐ │  │ │
            │                  │  │  │  │ Runner      │ │  │ │
            │                  │  │  │  │ Container   │ │  │ │
            │                  │  │  │  │ • terraform │ │  │ │
            │                  │  │  │  │ • kubectl   │ │  │ │
            │                  │  │  │  │ • talosctl  │ │  │ │
            │                  │  │  │  │ • aws-cli   │ │  │ │
            │                  │  │  │  └─────────────┘ │  │ │
            │                  │  │  └──────────────────┘  │ │
            │                  │  └─────────────────────────┘ │
            │                  └──────────────────────────────┘
            │                              │
            │                              │ terraform/kubectl/talosctl
            │                              ▼
            │                  ┌──────────────────────────────┐
            │                  │ Kubernetes Cluster           │
            │                  │ • 15 nodes on Proxmox        │
            │                  │ • Terraform manages VMs      │
            │                  │ • Talos machine configs      │
            │                  │                              │
            │                  │  ┌─────────────────────────┐ │
            │                  │  │ In-Cluster ARC Runners  │ │
            │                  │  │ (for non-cattle tasks)  │ │
            │                  │  │ • CI/CD workflows       │ │
            │                  │  │ • Renovate bot          │ │
            │                  │  │ • Testing               │ │
            │                  │  └─────────────────────────┘ │
            │                  └──────────────────────────────┘
            │
            └─ Renovate updates terraform/variables.tf
```

---

## Architectural Decisions

### Decision 1: External Runner Implementation

**Decision**: Docker container running GitHub Actions runner in dedicated VM on Proxmox

**Alternatives Considered**:
| Option | Maintenance | Security | Complexity | Delays | Decision |
|--------|-------------|----------|------------|--------|----------|
| **Docker in VM** | Low (quarterly) | High (VM isolation) | Low | None | ✅ **SELECTED** |
| JIT ephemeral VM | Lowest | Highest | High | 30+ seconds | ❌ Rejected (delays) |
| Persistent VM + manual setup | Medium | High | Low | None | ⚠️ Acceptable fallback |
| Docker in LXC | Medium | Medium (shared kernel) | Low | None | ❌ Rejected (security) |
| On Proxmox host | Medium | LOW (no isolation) | Low | None | ❌ Rejected (huge risk) |

**Rationale**:
- **Simplicity**: Docker Compose manages runner lifecycle, auto-restarts on failure
- **Maintenance**: Quarterly image rebuild includes all tool updates (terraform, kubectl, talosctl)
- **Security**: VM provides kernel isolation from Proxmox host, Docker socket risk contained
- **No Delays**: Runner is always registered and idle, workflow starts immediately
- **Tool Management**: Dockerfile defines all dependencies, versioned in Git

**VM Specification**:
```yaml
Name: cattle-runner
Host: baldar (or separate Proxmox host from k8s nodes)
Resources:
  CPU: 2 vCPU
  Memory: 4GB RAM
  Disk: 40GB
OS: Ubuntu 22.04 LTS
Network: Management VLAN (10.20.66.x)
Startup: Auto-start on Proxmox boot
```

**Docker Container Specification**:
```yaml
Base Image: myoung34/github-runner:latest
Extended With:
  - terraform (pinned to 1.6.x)
  - kubectl (matches cluster version)
  - talosctl (matches cluster version)
  - aws-cli (for S3 Terraform state)
  - gh (GitHub CLI)
Runner Labels:
  - cattle-runner
  - external-runner
Restart Policy: unless-stopped (persistent)
Lifecycle: Always running, single runner
```

---

### Decision 2: Cattle vs Pets Routing Logic

**Decision**: Automated semver-based routing in `talos-version-router.yaml` workflow

**Router Logic**:
```bash
# Parse versions
OLD_MAJOR.OLD_MINOR.OLD_PATCH  # From HEAD^ commit
NEW_MAJOR.NEW_MINOR.NEW_PATCH  # From HEAD commit

# Decision
if OLD_MAJOR != NEW_MAJOR OR OLD_MINOR != NEW_MINOR:
  UPGRADE_TYPE="cattle"  # Major or minor version change
  → Call upgrade-cattle.yaml workflow
else:
  UPGRADE_TYPE="pets"    # Patch version change
  → Call upgrade-pets.yaml workflow (future)
```

**Examples**:
| Old Version | New Version | Type | Workflow | Reason |
|-------------|-------------|------|----------|--------|
| 1.10.8 | 1.10.9 | Patch | Pets | Same major.minor |
| 1.10.8 | 1.11.0 | Minor | Cattle | Minor changed |
| 1.10.8 | 2.0.0 | Major | Cattle | Major changed |

**Rationale**:
- **Automatic**: No manual decision required
- **Predictable**: Semver rules are universal
- **Safe**: Major/minor upgrades get clean infrastructure (cattle)
- **Efficient**: Patches get fast in-place updates (pets)

---

### Decision 3: Update Strategy

**Decision**: Lazy quarterly updates aligned with cattle upgrade schedule

**Update Frequency**:
| Component | Update Method | Frequency | Effort |
|-----------|---------------|-----------|--------|
| **Docker base image** | `docker-compose pull && up` | Quarterly | 2 min |
| **Tools (terraform, kubectl, talosctl)** | Rebuild Dockerfile | Quarterly | 3 min |
| **Ubuntu security patches** | `unattended-upgrades` | Daily (auto) | 0 min |
| **Docker daemon** | `apt upgrade docker-ce` | Quarterly | 1 min |

**Quarterly Update Workflow** (before cattle upgrade):
```bash
# 1. SSH to cattle-runner VM
ssh ubuntu@cattle-runner

# 2. Update Dockerfile versions
cd ~/cattle-runner
vim Dockerfile.cattle-runner
# Update: KUBECTL_VERSION=1.34.3
# Update: TALOS_VERSION=1.11.0

# 3. Rebuild image and restart
docker-compose down
docker build -t cattle-runner:latest -f Dockerfile.cattle-runner .
docker-compose up -d

# 4. Verify versions
docker exec cattle-runner terraform version
docker exec cattle-runner kubectl version --client
docker exec cattle-runner talosctl version --client

# Time: 5 minutes total
```

**Rationale**:
- **Aligned with usage**: Cattle upgrades are quarterly, update runner quarterly
- **Low burden**: 5 minutes every 3 months
- **Security**: `unattended-upgrades` handles critical security patches between manual updates
- **Version matching**: kubectl/talosctl versions match cluster versions

---

## Component Design

### Component 1: External Docker Runner

**Purpose**: Execute cattle/pets workflows outside cluster to avoid circular dependency

**Responsibilities**:
- Execute `.github/workflows/upgrade-cattle.yaml`
- Execute `.github/workflows/upgrade-pets.yaml` (future)
- Terraform operations (destroy/apply VMs)
- Talosctl operations (apply machine configs, in-place upgrades)
- Kubectl operations (drain/uncordon, health checks)
- AWS S3 operations (backup Terraform state)

**Installation**: See "Implementation: Docker-Based Runner" section below

**Tool Versions**:
```
terraform: 1.6.x (pinned for Terraform state compatibility)
kubectl: Matches cluster Kubernetes version (e.g., 1.34.2)
talosctl: Matches cluster Talos version (e.g., 1.10.8)
aws-cli: Latest (fully backwards compatible)
gh: Latest (fully backwards compatible)
```

**Security**:
- All credentials via GitHub Secrets (no local storage)
- VM isolated from Proxmox host (full kernel isolation)
- Docker socket mounted (allows Docker-in-Docker workflows if needed)
- Auto-updates: `unattended-upgrades` enabled for security patches
- Monitoring: Health check via Docker Compose

**Resource Usage**:
- Idle: ~200MB RAM, 5% CPU (waiting for jobs)
- Active (Terraform): ~2GB RAM, 80% CPU (VM operations)
- Duration: 3-4 hours per cattle upgrade (quarterly)

---

### Component 2: Router Workflow

**Purpose**: Detect Talos version changes and route to appropriate upgrade workflow

**File**: `.github/workflows/talos-version-router.yaml`

**Trigger**:
- Push to main branch
- Path: `terraform/variables.tf` changes
- Manual dispatch (workflow_dispatch)

**Execution**:
```
1. Checkout repository (fetch-depth: 2 for comparison)
2. Extract old version (HEAD^ commit)
3. Extract new version (HEAD commit)
4. Validate semver format (X.Y.Z)
5. Parse major, minor, patch components
6. Decision logic:
   - Major/minor change → route-to-cattle job
   - Patch change → route-to-pets job
7. Call appropriate workflow via workflow_call
```

**Runner**: `ubuntu-latest` (GitHub-hosted, no dependencies)

**Outputs**:
- `version_changed`: true/false
- `old_version`: X.Y.Z
- `new_version`: X.Y.Z
- `upgrade_type`: cattle/pets

---

### Component 3: Cattle Workflow

**Purpose**: Automate major/minor Talos version upgrades via Terraform destroy/create

**File**: `.github/workflows/upgrade-cattle.yaml`

**Trigger**:
- Called by router workflow (`workflow_call`)
- Manual dispatch (`workflow_dispatch`)

**Runner**: `cattle-runner` (external Docker runner)

**Phases**:
```
Phase 1: Rebuild Templates (if version changed)
  ├─ Destroy 8 old templates (4 hosts × 2 roles)
  ├─ Create 8 new templates (Talos v{new_version})
  └─ Validate templates exist (Terraform state check)

Phase 2: Upgrade Control Plane (sequential, 1 at a time)
  ├─ For each controller (k8s-ctrl-1, k8s-ctrl-2, k8s-ctrl-3):
  │  ├─ Validate etcd quorum health (≥2 healthy members)
  │  ├─ Cordon node
  │  ├─ Drain node (graceful 240s, force fallback)
  │  ├─ Backup Terraform state (S3)
  │  ├─ terraform destroy (delete VM)
  │  ├─ terraform apply (recreate VM from new template)
  │  ├─ Apply machine configuration (SOPS-encrypted secrets)
  │  ├─ Wait for node Ready
  │  ├─ Verify version
  │  └─ Uncordon node

Phase 3: Upgrade Workers (sequential, 1 at a time)
  ├─ For each worker (k8s-work-1 to k8s-work-16):
  │  ├─ Pre-drain health check (CoreDNS, Rook-Ceph)
  │  ├─ Cordon node
  │  ├─ Drain node (graceful 240s, force fallback)
  │  ├─ Backup Terraform state (S3)
  │  ├─ terraform destroy (delete VM)
  │  ├─ terraform apply (recreate VM from new template)
  │  ├─ Apply machine configuration
  │  ├─ Wait for node Ready
  │  ├─ Verify version
  │  ├─ Apply global patches (kubelet, network, sysctls, time)
  │  ├─ Apply GPU patches (if k8s-work-4 or k8s-work-14)
  │  ├─ Uncordon node
  │  └─ Post-upgrade validation (deployments, Ceph)

Phase 4: Cluster Validation
  ├─ Check all nodes upgraded to target version
  ├─ Validate GPU nodes (nvidia.com/gpu.present label)
  ├─ Validate workload health (HelmReleases, pods)
  └─ Final summary report
```

**Duration**: ~3-4 hours (15 nodes × 15 min/node)

**Security**: 5-layer machine config security (see existing workflow for details)

---

### Component 4: Pets Workflow (Future Implementation)

**Purpose**: Automate patch Talos version upgrades via in-place `talosctl upgrade`

**File**: `.github/workflows/upgrade-pets.yaml` (NOT YET IMPLEMENTED)

**Trigger**:
- Called by router workflow (`workflow_call`)
- Manual dispatch (`workflow_dispatch`)

**Runner**: `cattle-runner` (same external runner, different workflow)

**Proposed Workflow**:
```
Phase 1: Upgrade Control Plane (sequential, 1 at a time)
  ├─ For each controller:
  │  ├─ Validate etcd quorum health
  │  ├─ talosctl upgrade --nodes <node> --image factory.talos.dev/...
  │  ├─ Wait for node Ready (node reboots during upgrade)
  │  └─ Verify version

Phase 2: Upgrade Workers (batched, 2-3 at a time)
  ├─ For each batch of workers:
  │  ├─ talosctl upgrade --nodes <node1>,<node2>,<node3> --image factory.talos.dev/...
  │  ├─ Wait for all nodes Ready
  │  └─ Verify versions

Phase 3: Validation
  └─ Check all nodes upgraded
```

**Duration**: ~75 minutes (15 nodes, ~5 min/node, some parallelization)

**Benefits over Cattle**:
- Faster (no VM destroy/create)
- Lower disruption (in-place reboot)
- Suitable for frequent patches

---

## Implementation: Docker-Based Runner

### 1. Create Extended Docker Image

**File**: `Dockerfile.cattle-runner`

```dockerfile
FROM myoung34/github-runner:latest

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ca-certificates \
    curl \
    jq \
    unzip \
    gnupg \
    lsb-release \
    && rm -rf /var/lib/apt/lists/*

# Install Terraform
RUN curl -fsSL https://apt.releases.hashicorp.com/gpg | apt-key add - && \
    apt-add-repository "deb [arch=amd64] https://apt.releases.hashicorp.com $(lsb_release -cs) main" && \
    apt-get update && \
    apt-get install -y terraform=1.6.* && \
    rm -rf /var/lib/apt/lists/*

# Install kubectl (match your cluster version)
ARG KUBECTL_VERSION=1.34.2
RUN curl -LO "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" && \
    install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl && \
    rm kubectl

# Install talosctl (match your cluster version)
ARG TALOS_VERSION=1.10.8
RUN curl -sL "https://github.com/siderolabs/talos/releases/download/v${TALOS_VERSION}/talosctl-linux-amd64" \
    -o /tmp/talosctl && \
    install -o root -g root -m 0755 /tmp/talosctl /usr/local/bin/talosctl && \
    rm /tmp/talosctl

# Install AWS CLI (for S3 Terraform state)
RUN curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install && \
    rm -rf aws awscliv2.zip

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Verify installations
RUN terraform version && \
    kubectl version --client && \
    talosctl version --client && \
    aws --version && \
    gh --version

# Metadata
LABEL maintainer="jlengelbrecht"
LABEL description="GitHub Actions runner with cattle upgrade tools"
LABEL version="1.0"
```

### 2. Docker Compose Configuration

**File**: `docker-compose.yml`

```yaml
version: '3.8'

services:
  cattle-runner:
    image: cattle-runner:latest
    container_name: cattle-runner
    restart: unless-stopped

    environment:
      # Runner configuration
      RUNNER_NAME: k8s-cattle
      ACCESS_TOKEN: ${DYNAMIC_RUNNER_TOKEN}
      RUNNER_WORKDIR: /tmp/runner/work
      RUNNER_SCOPE: 'repo'
      REPO_URL: https://github.com/jlengelbrecht/prox-ops
      LABELS: cattle-runner,external-runner

      # Disable auto-updates (managed via image rebuild)
      DISABLE_AUTO_UPDATE: 'true'

    volumes:
      # Docker-in-Docker (if workflows need docker)
      - '/var/run/docker.sock:/var/run/docker.sock'

      # Runner work directory (persistent across container restarts)
      - './runner-data:/tmp/runner'

    security_opt:
      # Disable SELinux labeling (if on SELinux system)
      - label:disable

    # Resource limits
    mem_limit: 4g
    cpus: 2

    # Health check
    healthcheck:
      test: ["CMD", "pgrep", "-f", "Runner.Listener"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
```

**File**: `.env`

```bash
# GitHub Personal Access Token or GitHub App token
# Scope: repo (full control)
# Generate: GitHub → Settings → Developer settings → Personal access tokens
# Or use GitHub App for better security

DYNAMIC_RUNNER_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### 3. VM Setup on Proxmox

**Option A: Via Proxmox GUI**
1. Create VM: Name=`cattle-runner`, Node=`baldar`
2. OS: Ubuntu 22.04 cloud image
3. Resources: 2 vCPU, 4GB RAM, 40GB disk
4. Network: vmbr0, VLAN 66 (management)
5. Start VM

**Option B: Via Proxmox CLI**
```bash
# Create VM from Ubuntu cloud image
# (Assumes cloud image already downloaded to Proxmox)
qm create 200 --name cattle-runner --memory 4096 --cores 2 --net0 virtio,bridge=vmbr0,tag=66
qm importdisk 200 /var/lib/vz/template/iso/ubuntu-22.04-server-cloudimg-amd64.img local-lvm
qm set 200 --scsihw virtio-scsi-pci --scsi0 local-lvm:vm-200-disk-0
qm set 200 --boot c --bootdisk scsi0
qm set 200 --ide2 local-lvm:cloudinit
qm set 200 --serial0 socket --vga serial0
qm set 200 --agent enabled=1
qm start 200
```

### 4. Runner Deployment

```bash
# 1. SSH into cattle-runner VM
ssh ubuntu@<cattle-runner-ip>

# 2. Install Docker
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 3. Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/download/v2.23.0/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# 4. Install unattended-upgrades (auto security patches)
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades

# 5. Create runner directory
mkdir -p ~/cattle-runner
cd ~/cattle-runner

# 6. Create files
# - Dockerfile.cattle-runner (see above)
# - docker-compose.yml (see above)
# - .env (with DYNAMIC_RUNNER_TOKEN)

# 7. Generate GitHub token
# GitHub → Settings → Developer settings → Personal access tokens
# Scopes: repo (full control)
# Copy token to .env file

# 8. Build custom image
docker build -t cattle-runner:latest -f Dockerfile.cattle-runner .

# 9. Start runner
docker-compose up -d

# 10. Verify runner registered
docker logs -f cattle-runner
# Should see: "Runner successfully added"
# Check: https://github.com/jlengelbrecht/prox-ops/settings/actions/runners
```

### 5. Update Workflows

**File**: `.github/workflows/upgrade-cattle.yaml`

```yaml
# Change all job runners from:
runs-on: gha-runner-scale-set

# To:
runs-on: cattle-runner
```

**Required Changes**:
- Line 51: `rebuild-templates` job
- Line 251: `upgrade-control-plane` job
- Line 663: `upgrade-workers` job
- Line 1233: `validate-cluster` job

---

## Maintenance Procedures

### Quarterly Update (Before Cattle Upgrade)

**Time**: 5 minutes
**Frequency**: Before each cattle upgrade (quarterly/annually)

```bash
# 1. SSH to cattle-runner VM
ssh ubuntu@cattle-runner

# 2. Update Dockerfile versions
cd ~/cattle-runner
vim Dockerfile.cattle-runner

# Update versions to match cluster:
# KUBECTL_VERSION=1.34.3  (match cluster Kubernetes version)
# TALOS_VERSION=1.11.0    (match cluster Talos version)

# 3. Rebuild image
docker-compose down
docker build -t cattle-runner:latest -f Dockerfile.cattle-runner .
docker-compose up -d

# 4. Verify versions
docker exec cattle-runner terraform version
docker exec cattle-runner kubectl version --client
docker exec cattle-runner talosctl version --client

# 5. Verify runner registered
docker logs -f cattle-runner
# Should see: "Listening for Jobs"
```

### Automatic Security Patches

**Configuration**: `/etc/apt/apt.conf.d/50unattended-upgrades` (on VM host)

```
Unattended-Upgrade::Allowed-Origins {
    "${distro_id}:${distro_codename}-security";
};
Unattended-Upgrade::AutoFixInterruptedDpkg "true";
Unattended-Upgrade::MinimalSteps "true";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";  # Manual reboot control
```

**What This Does**:
- Automatically installs security patches daily
- Removes old kernels automatically
- Does NOT auto-reboot (you control when to reboot)
- 95% of security vulnerabilities auto-patched

**Manual Reboot** (monthly or after kernel updates):
```bash
ssh ubuntu@cattle-runner
sudo reboot
# Runner container auto-restarts via Docker Compose
```

---

## Security Considerations

### External Runner Security

**Threat Model**:
- **Compromise of runner container**: Attacker gains access to Docker host (VM)
- **Compromise of VM**: Attacker gains access to Proxmox API, Talos API, K8s API
- **Credential theft**: GitHub Secrets exposed if runner compromised

**Mitigations**:

1. **Credential Management**:
   - All credentials via GitHub Secrets (no local storage)
   - Secrets masked in workflow logs
   - Short-lived tokens where possible (GitHub App recommended)

2. **VM Isolation**:
   - Runner VM on separate Proxmox host (or isolated from k8s nodes)
   - Firewall rules: Allow only necessary outbound (GitHub, Proxmox, K8s API, AWS)
   - No inbound connections except SSH from admin workstation

3. **Docker Security**:
   - Container runs as non-root (myoung34/github-runner handles this)
   - Docker socket mounted (necessary for Docker-in-Docker workflows)
   - Resource limits prevent DoS (4GB RAM, 2 vCPU)

4. **Access Control**:
   - GitHub Actions: Repository admins only can trigger workflows
   - VM SSH: Key-based auth only, disable password auth
   - GitHub token: Repo-scoped PAT or GitHub App (least privilege)

5. **Monitoring**:
   - Docker health check (detects crashed runner)
   - GitHub runner status (visible in repo settings)
   - Optional: Prometheus node_exporter on VM

**Risk Assessment**: MEDIUM
- External runner has elevated privileges (terraform, kubectl, talosctl)
- Acceptable for homelab automation
- Recommend periodic security audits (quarterly)

---

## Success Metrics

### Technical Metrics

| Metric | Target | Current |
|--------|--------|---------|
| Upgrade automation | 100% (zero manual steps) | TBD |
| Upgrade success rate | >95% | TBD |
| Time to upgrade (cattle) | <4 hours (15 nodes) | TBD |
| Rollback success rate | 100% | TBD |
| Zero data loss | 100% | TBD |

### Operational Metrics

| Metric | Target | Current |
|--------|--------|---------|
| User intervention required | 0 (automated) | TBD |
| Runner availability | >99% | TBD |
| Workflow start delay | <5 seconds | TBD |
| Maintenance effort | <10 min/quarter | TBD |

---

## References

### Documentation
- Talos Linux: https://www.talos.dev/
- GitHub Actions Self-Hosted Runners: https://docs.github.com/en/actions/hosting-your-own-runners
- Docker Runner Image: https://github.com/myoung34/docker-github-actions-runner
- Terraform Proxmox Provider: https://registry.terraform.io/providers/bpg/proxmox/latest/docs

### Related Documents
- `.claude/.ai-docs/stories/CURRENT_STATE_2025-11-19.md` - Problem analysis
- `.claude/.ai-docs/epics/EPIC-019-automated-cattle-upgrade-strategy.md` - Epic definition
- `.claude/.ai-docs/stories/CATTLE_WORKFLOW_STATUS.md` - Original problem discovery
- `.github/workflows/talos-version-router.yaml` - Router workflow
- `.github/workflows/upgrade-cattle.yaml` - Cattle workflow

### GitHub Issues
- ARC #4148: Ephemeral runners not triggering job re-queue
- ARC #4203: Orphaned jobs when runners terminated mid-execution

---

## Changelog

| Version | Date | Changes | Author |
|---------|------|---------|--------|
| 1.0 | 2025-11-16 | Initial architecture (in-cluster runners, 3-layer resilience) | BMad PM |
| 2.0 | 2025-11-19 | Hybrid architecture (external runner + tuppr) after circular dependency discovery | Winston (BMad Architect) |
| 3.0 | 2025-11-20 | Docker-based external runner, removed JIT/tuppr complexity | Winston (BMad Architect) |

---

**Status**: Architecture approved, ready for implementation
**Next Steps**:
1. Provision cattle-runner VM on Proxmox
2. Deploy Docker runner with extended image
3. Update workflows to use `cattle-runner` label
4. Test cattle upgrade on 2 worker nodes
5. Roll out to production

**Approval**: User approval required before execution

---
