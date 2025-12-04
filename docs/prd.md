# prox-ops Brownfield Enhancement PRD
# Automated Talos Cattle Upgrade Strategy

**Product Requirements Document**
**Version**: 1.0
**Date**: 2025-11-16
**Author**: Claude Code Product Manager
**Project**: prox-ops (Talos Kubernetes Homelab)

---

## Document Control

| Change | Date | Version | Description | Author |
|--------|------|---------|-------------|--------|
| Initial PRD creation | 2025-11-16 | 1.0 | Created brownfield PRD from Opus deployment guide + EPIC-019 | Claude Code PM |

---

## Table of Contents

1. [Intro Project Analysis and Context](#1-intro-project-analysis-and-context)
2. [Requirements](#2-requirements)
3. [Technical Constraints and Integration Requirements](#3-technical-constraints-and-integration-requirements)
4. [Epic and Story Structure](#4-epic-and-story-structure)

---

## 1. Intro Project Analysis and Context

### 1.1 Existing Project Overview

#### Analysis Source

- ✅ **PROJECT_STATE.md available** at `.claude/.ai-docs/PROJECT_STATE.md` (comprehensive cluster state document, 1136 lines)
- ✅ **EPIC-019 design document available** at `.claude/.ai-docs/epics/EPIC-019-automated-cattle-upgrade-strategy.md` (v2.0.0, detailed implementation plan)
- ✅ **Opus deployment guide available** at `.claude/.ai-docs/terraform/AUTOMATED_DEPLOYMENT_GUIDE.md` (production-ready Terraform automation)

#### Current Project State

**Infrastructure**: Production-ready Talos Linux Kubernetes cluster

- **Cluster**: 15 nodes (3 control plane, 12 workers) on 4-node Proxmox cluster
- **OS**: Talos Linux v1.11.3, Kubernetes v1.34.1
- **Maturity**: 90% infrastructure maturity score
- **Health**: GREEN status (27/27 HelmReleases operational, 100% node capacity)
- **GitOps**: Flux-managed deployments with instant sync (<30s)
- **Automation**: Renovate Bot active (5+ PRs merged/week, automated dependency updates)

**Current Upgrade Process (Manual)**:

- Manual detection of Talos/K8s releases
- Manual Terraform operations (template creation, VM destroy/create)
- Manual health validation between node upgrades
- **Time**: Hours of manual effort per upgrade cycle
- **Risk**: Human error, no automated rollback, configuration drift

**Validated Cattle Strategy**:

- ✅ **Proven**: 5-6 minute node replacement time per node
- ✅ **Zero-drift**: Fresh node creation from templates eliminates configuration drift
- ✅ **Production-ready**: Terraform modules operational (8 templates + 15 VMs)

---

### 1.2 Available Documentation Analysis

#### Documentation Status

Using existing project analysis from PROJECT_STATE.md, EPIC-019, and Opus's deployment guide:

- ✅ **Tech Stack Documentation** - PROJECT_STATE.md (Talos, Kubernetes, Proxmox, Terraform)
- ✅ **Source Tree/Architecture** - EPIC-019 architecture diagrams, component breakdown
- ✅ **Coding Standards** - GitOps workflow documented in CLAUDE.md
- ✅ **API Documentation** - Proxmox API, Talos API, GitHub API integrations documented
- ✅ **External API Documentation** - Factory.talos.dev schematics, GitHub Actions API
- ❌ **UX/UI Guidelines** - Not applicable (infrastructure project)
- ✅ **Technical Debt Documentation** - PROJECT_STATE.md identifies blocker (local Terraform state)
- **Other**: Comprehensive security design (GitHub Actions Secrets + 1Password ExternalSecrets)

**Documentation Quality**: EXCELLENT - No need to run document-project task, all critical information available.

---

### 1.3 Enhancement Scope Definition

#### Enhancement Type

- ✅ **New Feature Addition** (Automated upgrade workflow - completely new capability)
- ✅ **Integration with New Systems** (GitHub Actions runners in-cluster, Renovate bot, S3 Terraform backend)
- ✅ **Performance/Scalability Improvements** (90% reduction in upgrade time)
- ✅ **Technology Stack Upgrade** (Enables automated Talos/K8s version upgrades)

#### Enhancement Description

**What**: Implement fully automated, zero-downtime Talos Linux "cattle upgrade" workflow triggered by Renovate PRs, executed via self-hosted GitHub Actions runners deployed in the Kubernetes cluster, using GitOps principles for cluster version management.

**Why**: Currently, upgrading 15 Talos nodes requires hours of manual Terraform operations, health checks, and validation. The validated cattle strategy (destroy/recreate nodes) eliminates configuration drift but needs automation to be practical. This enhancement transforms manual, error-prone upgrades into a self-service, auditable process.

**How**: Renovate detects new Talos/K8s versions → Creates PR → User merges → GitHub Actions workflow (running on in-cluster self-hosted runners) executes rolling cattle upgrades node-by-node → Automated health checks between each node → Automatic rollback on failure → Notifications via Discord.

#### Impact Assessment

- ✅ **Significant Impact** (substantial existing code changes)
  - **Terraform**: Add remote S3 backend, update state management
  - **Infrastructure**: Deploy GitHub Actions runner namespace, RBAC, HelmRelease
  - **GitOps**: Add .github/workflows for cattle upgrades
  - **Secrets Management**: Integrate GitHub Actions Secrets + 1Password ExternalSecrets
  - **Renovate**: Custom managers for Talos/K8s version detection in Terraform files
- ✅ **Architectural changes required**
  - New namespace: `github-actions`
  - New infrastructure component: Actions Runner Controller
  - Remote Terraform state (S3/Terraform Cloud)
  - Workflow orchestration layer (GitHub Actions)

---

### 1.4 Goals and Background Context

#### Goals

1. **Eliminate 90% of manual upgrade effort** - Reduce human time from hours to minutes (approval only)
2. **Zero configuration drift** - Cattle strategy ensures fresh, template-based nodes every upgrade
3. **Automated rollback capability** - Failed upgrades automatically revert to previous version
4. **Continuous security patching** - Stay current with Talos/K8s CVE fixes within days of release
5. **Complete audit trail** - All upgrades tracked in Git history with PR approval workflow
6. **Self-service operation** - Single PR merge triggers entire upgrade workflow
7. **Production-grade safety** - Health checks, PodDisruptionBudget compliance, node-by-node progression

#### Background Context

The prox-ops cluster is a production-ready homelab infrastructure serving 2 production workloads (Home Assistant, Plex) with full observability, multi-VLAN networking, and GPU support. The cluster operates on Talos Linux, an immutable, API-driven Kubernetes distribution that embraces the "cattle not pets" philosophy.

**Why This Enhancement Is Needed**:

- **Current State**: Manual upgrades are time-consuming and error-prone. With Renovate already detecting version updates and creating PRs, the detection layer exists but execution requires manual Terraform operations.

- **Cattle Strategy Validation**: Recent testing confirms node replacement completes in 5-6 minutes, but doing this manually for 15 nodes (75-90 minutes of focused attention) is impractical.

- **Blocker Identified**: Terraform state is currently stored locally, preventing CI/CD automation. Migrating to remote state (S3 or Terraform Cloud) unblocks GitHub Actions workflows.

- **Existing Foundation**: Renovate Bot (Epic-016) already operational, 1Password ExternalSecrets (Epic-005) deployed, GitOps workflow mature. This enhancement leverages existing infrastructure.

- **Strategic Value**: Automated upgrades enable rapid response to security vulnerabilities, eliminate human error, and demonstrate infrastructure-as-code maturity. Aligns with project goal of "production-grade, self-healing, GitOps-managed Kubernetes homelab."

---

## 2. Requirements

### 2.1 Functional Requirements

**FR1: Version Detection and Pull Request Creation**

- The system SHALL automatically detect new Talos Linux and Kubernetes releases via Renovate Bot custom managers
- Renovate SHALL create separate PRs for patch, minor, and major version updates
- PRs SHALL include version delta analysis and automatically apply upgrade strategy labels (`upgrade-pets` or `upgrade-cattle`)
- Detection SHALL occur within 24 hours of upstream release

**FR2: Upgrade Strategy Decision Logic**

- The system SHALL automatically determine upgrade strategy based on semantic version delta:
  - Patch versions (1.11.3 → 1.11.4): Pets strategy (in-place talosctl upgrade)
  - Minor versions (1.11.x → 1.12.x): Cattle strategy (Terraform destroy/recreate)
  - Major versions (1.x → 2.x): Cattle strategy with manual approval gate
- Decision logic SHALL be visible in PR description with rationale
- Special cases (security CVEs, breaking changes) SHALL override default logic with manual approval

**FR3: Self-Hosted GitHub Actions Runner Deployment**

- The system SHALL deploy Actions Runner Controller in the `github-actions` namespace via Flux GitOps
- Runners SHALL register with the GitHub repository using 1Password ExternalSecret for GitHub App credentials
- Runners SHALL use Kubernetes ServiceAccount for in-cluster kubectl access (no kubeconfig required)
- Runner pods SHALL be ephemeral (destroyed after job completion)
- Runner scale set SHALL support concurrent workflows (minimum 2 runners)

**FR4: Terraform Remote State Management**

- Terraform state SHALL be migrated from local storage to S3-compatible backend OR Terraform Cloud
- State locking SHALL be enabled to prevent concurrent modifications
- GitHub Actions workflows SHALL access remote state via AWS credentials (stored in GitHub Actions Secrets)
- State SHALL be versioned with backup retention (minimum 30 days)

**FR5: Cattle Upgrade Workflow Execution**

- The system SHALL execute node-by-node cattle upgrades when `upgrade-cattle` label is present and PR is merged
- Workflow SHALL:
  1. Cordon and drain target node (respecting PodDisruptionBudgets, 5-minute timeout)
  2. Destroy old VM via Terraform (`terraform destroy -target`)
  3. Create new VM from updated template via Terraform (`terraform apply -target`)
  4. Wait for node Ready status (10-minute timeout)
  5. Uncordon node and validate pod scheduling
  6. Proceed to next node only after validation passes
- Control plane nodes SHALL upgrade before worker nodes
- Maximum 1 control plane node offline at any time (maintain quorum)
- Worker upgrades SHALL respect PodDisruptionBudgets for stateful workloads

**FR6: Pets Upgrade Workflow Execution**

- The system SHALL execute rolling in-place upgrades when `upgrade-pets` label is present and PR is merged
- Workflow SHALL use `talosctl upgrade` command with factory.talos.dev installer image
- Upgrades SHALL wait for node Ready status before proceeding to next node

**FR7: Automated Health Validation**

- The system SHALL execute comprehensive health checks after each node upgrade:
  - Node readiness status (kubectl wait for Ready condition)
  - All namespace pod health (no CrashLoopBackOff, no Pending > 5 min)
  - Ceph storage cluster health (HEALTH_OK status)
  - Flux GitOps reconciliation status (all Kustomizations Applied)
  - DNS resolution tests (internal cluster DNS)
  - Network connectivity tests (pod-to-pod, pod-to-service)
  - GPU availability tests (if upgrading GPU nodes work-4, work-14)
- Health checks SHALL complete within 5 minutes per node
- Failed health checks SHALL trigger automatic rollback

**FR8: Automatic Rollback on Failure**

- The system SHALL automatically rollback if:
  - Node fails to reach Ready status within 10 minutes
  - Health validation fails 3 consecutive times
  - Critical pods fail to schedule (kube-system, flux-system, rook-ceph)
- Rollback SHALL restore previous Terraform state and recreate node with old version
- Rollback events SHALL trigger Discord notifications with failure details

**FR9: Notification and Observability**

- The system SHALL send Discord notifications for:
  - Upgrade workflow start (total nodes, estimated time)
  - Per-node completion (success/failure, duration)
  - Upgrade workflow completion (summary stats)
  - Rollback events (failure reason, affected node)
- Notifications SHALL include node name, upgrade strategy, version (old → new), status
- All workflow executions SHALL be logged in GitHub Actions UI with full command output

**FR10: Secret Management Integration**

- GitHub Actions workflows SHALL access infrastructure credentials via GitHub Actions Secrets (UI-managed, never in repository):
  - `PROXMOX_USERNAME`, `PROXMOX_PASSWORD` (Terraform Proxmox provider)
  - `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` (S3 Terraform backend)
  - `TALOSCONFIG` (base64-encoded talosconfig for talosctl commands)
  - `DISCORD_WEBHOOK_URL` (notification delivery)
- Self-hosted runner registration SHALL use 1Password ExternalSecret for GitHub App credentials
- NO credentials SHALL be stored in Git repository, committed to version control, or logged in workflow output

---

### 2.2 Non-Functional Requirements

**NFR1: Performance - Upgrade Time Reduction**

- The system SHALL reduce human time investment by 90% (from 4-6 hours to < 30 minutes approval/monitoring)
- Cattle upgrade SHALL complete per-node replacement in ≤ 6 minutes (destroy + create + validate)
- Full 15-node cluster upgrade SHALL complete in ≤ 2 hours elapsed time (sequential with validation)
- Terraform template creation SHALL complete in ≤ 15 minutes (8 templates across 4 Proxmox nodes)

**NFR2: Reliability - Zero-Downtime Upgrades**

- The system SHALL maintain cluster availability during upgrades (minimum 2/3 control plane nodes operational)
- Workloads SHALL experience zero unplanned downtime (respects PodDisruptionBudgets)
- Flux GitOps SHALL continue reconciling during upgrades (no reconciliation failures)
- Storage (Ceph) SHALL remain HEALTH_OK throughout upgrade process

**NFR3: Security - Credential Protection**

- GitHub Actions Secrets SHALL never be exposed in logs, Git history, or workflow artifacts
- Secrets SHALL be rotated every 90 days with documented rotation procedure
- Self-hosted runner pods SHALL run with minimal privileges (non-root, read-only root filesystem)
- Terraform backend access SHALL use time-limited STS tokens (if using AWS S3)
- All secret access SHALL be audited via GitHub audit log

**NFR4: Maintainability - Infrastructure as Code**

- All automation components SHALL be deployed via Flux GitOps (HelmReleases, Kustomizations)
- GitHub Actions workflows SHALL be version-controlled in `.github/workflows/`
- Terraform modules SHALL remain unchanged (reuse existing talos-template, talos-node modules)
- Renovate configuration SHALL be version-controlled in `.github/renovate.json5`
- Documentation SHALL include runbooks for manual intervention scenarios

**NFR5: Auditability - Complete Upgrade History**

- Every upgrade SHALL create a Git commit with version change and timestamp
- PR approval workflow SHALL capture who authorized the upgrade and when
- GitHub Actions execution logs SHALL be retained for 90 days minimum
- Discord notifications SHALL provide permanent audit trail of upgrade events

**NFR6: Resilience - Graceful Degradation**

- If self-hosted runners are unavailable, manual upgrades SHALL remain possible via existing Terraform workflow
- If remote Terraform state is unavailable, workflow SHALL fail-safe (no destructive operations)
- If health checks fail, workflow SHALL halt and preserve cluster state (no further upgrades)
- Network partitions SHALL not cause split-brain scenarios (Terraform state locking prevents concurrent operations)

**NFR7: Scalability - Future Cluster Growth**

- The system SHALL support cluster expansion from 15 to 30+ nodes without workflow changes
- Parallel worker upgrades SHALL be configurable (default: sequential, optional: 2-3 parallel with safety checks)
- Template creation SHALL scale to additional Proxmox nodes (current: 4 nodes, future: 6-8 nodes)

---

### 2.3 Compatibility Requirements

**CR1: Existing Terraform Infrastructure Compatibility**

- Automation SHALL reuse existing Terraform modules (`talos-template`, `talos-node`) without modification
- VM resource allocations SHALL remain unchanged (control: 4 CPU/16GB RAM, workers: 16 CPU/32GB RAM)
- Proxmox storage pool (`vms-ceph`) and network bridge (`vmbr1`) SHALL remain unchanged
- GPU PCI mappings (`thor-gpu`, `heimdall-gpu`) SHALL remain functional
- Template ID ranges (9000-9007) and VM ID ranges (8001-8015) SHALL remain unchanged

**CR2: Existing GitOps Workflow Compatibility**

- Flux reconciliation SHALL continue operating during GitHub Actions workflow execution
- HelmRelease deployments SHALL not be disrupted by node upgrades (respects PodDisruptionBudgets)
- Existing `.github/renovate.json5` configuration SHALL be extended (not replaced)
- Git commit message format SHALL follow existing convention (conventional commits: `type(scope): description`)

**CR3: Existing Secret Management Compatibility**

- 1Password Connect ClusterSecretStore SHALL be reused for runner registration (no new secret infrastructure)
- Existing SOPS-encrypted secrets SHALL remain functional (workflow does not interact with SOPS)
- External Secrets Operator SHALL continue syncing application secrets (unaffected by runner namespace)

**CR4: Existing Observability Stack Compatibility**

- Prometheus SHALL continue scraping metrics during upgrades (node-exporter, kube-state-metrics)
- Grafana dashboards SHALL reflect node upgrades (node restarts visible in metrics)
- Loki SHALL collect GitHub Actions runner pod logs (if promtail DaemonSet configured)
- AlertManager SHALL fire alerts if node upgrades cause pod disruptions (existing alert rules)

**CR5: Existing Multi-VLAN Networking Compatibility**

- Upgraded nodes SHALL maintain multi-NIC configuration (eth0: cluster, eth1: IoT VLAN 62, eth2: DMZ VLAN 81)
- Cilium L2 LoadBalancer announcements SHALL continue functioning during upgrades
- NetworkAttachmentDefinitions (iot-vlan62, dmz-vlan81) SHALL remain attached to upgraded nodes
- Multus CNI SHALL re-apply network configurations to new nodes automatically

**CR6: Existing GPU Support Compatibility**

- Upgraded GPU nodes (k8s-work-4, k8s-work-14) SHALL maintain PCI passthrough configurations
- NVIDIA device plugin SHALL rediscover GPUs after node recreation (DaemonSet automatic rollout)
- GPU workloads (Plex transcoding) SHALL reschedule to upgraded nodes within 2 minutes
- RTX A2000 and RTX A5000 resource availability SHALL be verified in health checks

---

## 3. Technical Constraints and Integration Requirements

### 3.1 Existing Technology Stack

**From PROJECT_STATE.md - Current Infrastructure (90% Maturity)**

#### Core Platform

- **Operating System**: Talos Linux v1.11.3 (immutable, API-driven)
- **Kubernetes**: v1.34.1 (15-node cluster, 100% capacity)
- **Container Runtime**: containerd 2.1.4
- **Kernel**: 6.12.52-talos

#### Infrastructure Layer

- **Virtualization**: Proxmox VE 8.x (4-node cluster: Baldar, Heimdall, Odin, Thor)
- **Infrastructure as Code**: Terraform v1.6+ (bpg/proxmox provider v0.67.0)
- **Terraform Backend**: **LOCAL** (terraform.tfstate in repository) - **BLOCKER: Must migrate to remote**
- **Storage**: External Ceph cluster (17.46 TiB, HEALTH_OK, 84.6% free)
- **Networking**: Cilium v1.18.3 CNI, Multus v1.0.1 (multi-NIC), 3 VLAN pools (cluster/IoT/DMZ)

#### GitOps & Automation

- **GitOps Platform**: Flux v0.32.0 (5 controllers, webhook-driven instant sync)
- **Dependency Management**: Renovate Bot (active, 5+ PRs/week merged)
- **Secret Management**:
  - External Secrets Operator v0.10.7 (3 deployments)
  - 1Password Connect v1.15.1 (3 replicas HA)
  - SOPS with Age encryption (for infrastructure secrets)
- **Certificate Management**: cert-manager v1.19.1 (wildcard *.homelab0.org)

#### Observability

- **Metrics**: Prometheus (kube-prometheus-stack v68.2.2)
- **Logs**: Loki v6.23.0 (400 GiB storage) + Promtail v6.16.6 (15/15 nodes)
- **Dashboards**: Grafana (LoadBalancer 10.20.67.24, HTTPS access)
- **Alerting**: AlertManager (StatefulSet, 5 GiB storage)

#### Version Control & CI/CD (Current State)

- **Repository**: github.com/jlengelbrecht/prox-ops (public)
- **CI/CD**: **NONE** - No GitHub Actions workflows currently deployed
- **Runners**: **NONE** - No self-hosted runners (this enhancement adds them)

#### Existing Terraform Modules

```
terraform/
├── modules/
│   ├── talos-template/    # Template creation (SSH provisioners)
│   └── talos-node/        # VM deployment (Proxmox provider)
├── main.tf                # 8 templates + 15 VMs definition
├── variables.tf           # Version config, credentials
├── versions.tf            # Provider config (multi-provider setup)
└── terraform.tfvars       # User configuration (gitignored)
```

---

### 3.2 Integration Approach

#### Infrastructure Integration Strategy

**Terraform Remote State Migration**:

- **Current**: State stored locally at `terraform/terraform.tfstate` (gitignored)
- **Target**: S3-compatible backend OR Terraform Cloud workspace
- **Migration Path**:
  1. Create S3 bucket with versioning + encryption OR Terraform Cloud workspace
  2. Add backend configuration to `terraform/versions.tf`
  3. Run `terraform init -migrate-state` to transfer state
  4. Verify state locking operational
  5. Update CLAUDE.md with remote state access procedures
- **Credentials**: AWS credentials stored in GitHub Actions Secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- **Rollback Plan**: Preserve local state backup before migration, can revert via `terraform init -reconfigure`

**GitHub Actions Runner Integration**:

- **Deployment Method**: Flux GitOps HelmRelease (follows existing pattern)
- **Namespace**: New `github-actions` namespace
- **Chart**: `actions-runner-controller` (official GitHub chart)
- **Registration**: GitHub App authentication via 1Password ExternalSecret
- **ServiceAccount**: `github-actions-runner` with cluster-admin permissions (required for kubectl operations during upgrades)
- **Network**: Runners need egress to:
  - GitHub API (github.com:443)
  - Proxmox API (10.20.66.10-13:8006)
  - S3 backend endpoint (port 443)
  - Factory.talos.dev (factory.talos.dev:443)

**Renovate Bot Integration**:

- **Current State**: Already operational (Epic-016 complete)
- **Extension Required**: Add custom managers to `.github/renovate.json5`:
  - Regex manager for `talos_version` in `terraform/variables.tf`
  - Regex manager for `kubernetes_version` in `terraform/variables.tf`
  - Package rules for separate major/minor/patch PRs
- **No Breaking Changes**: Existing Renovate config preserved, only extended

#### API Integration Strategy

**Proxmox API Integration**:

- **Current**: Terraform uses bpg/proxmox provider with API token authentication
- **New**: GitHub Actions workflows will use same authentication via environment variables
- **Credentials Flow**:
  ```
  GitHub Actions Secret (PROXMOX_USERNAME/PASSWORD)
    → Workflow env vars (TF_VAR_proxmox_username/password)
    → Terraform Proxmox provider
    → Proxmox API (https://10.20.66.X:8006)
  ```
- **No Changes Required**: Existing Terraform modules remain unchanged

**Talos API Integration**:

- **Current**: Manual talosctl commands via local ~/.talos/config
- **New**: Workflows use base64-encoded TALOSCONFIG from GitHub Actions Secret
- **Operations**:
  - Health checks: `talosctl health --nodes <IP>`
  - Version verification: `talosctl version --nodes <IP>`
  - (Pets strategy only): `talosctl upgrade --nodes <IP> --image <URL>`

**Kubernetes API Integration**:

- **Current**: kubectl via local ~/.kube/config
- **New**: Runners use in-cluster ServiceAccount (no kubeconfig needed)
- **RBAC**: ServiceAccount with cluster-admin role (required for node management, pod eviction)
- **Operations**:
  - Node management: `kubectl cordon`, `kubectl drain`, `kubectl uncordon`
  - Health checks: `kubectl get nodes`, `kubectl get pods -A`
  - Flux status: `kubectl get kustomizations -n flux-system`

**GitHub API Integration**:

- **Registration**: Runners register via GitHub App API (app ID + installation ID + private key)
- **Workflow Triggers**: PRs with specific labels trigger workflows
- **Notifications**: Workflow status updates visible in GitHub UI

#### Testing Integration Strategy

**Pre-Merge Testing**:

- Terraform validation: `terraform validate` in CI (GitHub Actions)
- Workflow syntax: `actionlint` in CI (validates .github/workflows/*.yml)
- Dry-run capabilities: `terraform plan -out=plan.tfplan` (manual review)

**Post-Deployment Testing**:

- Health check framework executes after each node upgrade
- Integration tests run on dev/staging nodes before production rollout
- Rollback testing: Simulate failures to validate automatic rollback logic

---

### 3.3 Code Organization and Standards

#### File Structure Approach

**Existing Structure (Preserved)**:

```
/home/devbox/repos/jlengelbrecht/prox-ops/
├── terraform/                      # Existing Terraform (unchanged)
├── kubernetes/                     # Flux Kustomizations
│   ├── apps/
│   │   ├── github-actions/        # NEW: Runner deployment
│   │   │   ├── namespace.yaml
│   │   │   ├── ks.yaml           # Kustomization for Flux
│   │   │   └── app/
│   │   │       ├── helmrelease.yaml          # Actions Runner Controller
│   │   │       ├── externalsecret.yaml       # GitHub App credentials from 1Password
│   │   │       ├── rbac.yaml                 # ServiceAccount, ClusterRoleBinding
│   │   │       └── kustomization.yaml
│   │   ├── flux-system/           # Existing (unchanged)
│   │   ├── kube-system/           # Existing (unchanged)
│   │   └── ...
├── .github/
│   ├── workflows/                 # NEW: Automation workflows
│   │   ├── upgrade-cattle.yml    # Cattle strategy workflow
│   │   ├── upgrade-pets.yml      # Pets strategy workflow
│   │   └── health-check.yml      # Reusable health check workflow
│   └── renovate.json5            # EXTENDED: Add Talos/K8s managers
├── .claude/
│   └── .ai-docs/
│       ├── PROJECT_STATE.md      # UPDATED: Track automation deployment
│       ├── epics/
│       │   └── EPIC-019-...      # Existing epic document
│       └── stories/              # NEW: Work item tracking for this PRD
└── CLAUDE.md                     # UPDATED: Document new workflows
```

#### Naming Conventions

**GitHub Actions Workflows**:

- Pattern: `upgrade-<strategy>.yml` (e.g., `upgrade-cattle.yml`, `upgrade-pets.yml`)
- Workflow names: Title case with strategy (e.g., "Cattle Strategy Upgrade")
- Job names: Lowercase hyphenated (e.g., `cattle-upgrade`, `health-check`)
- Step names: Sentence case describing action (e.g., "Cordon and Drain Node")

**Kubernetes Resources**:

- Namespace: `github-actions`
- ServiceAccount: `github-actions-runner`
- ClusterRoleBinding: `github-actions-runner-admin`
- Secret (from 1Password): `github-runner-registration`
- HelmRelease: `actions-runner-controller`

**Git Commit Messages**:

- Follow existing conventional commits pattern: `type(scope): description`
- Examples:
  - `feat(ci): add cattle upgrade workflow for automated node replacement`
  - `chore(terraform): migrate state to S3 backend`
  - `fix(workflows): correct health check timeout for GPU nodes`

#### Coding Standards

**Terraform**:

- Follow existing module structure (no changes to `talos-template` or `talos-node` modules)
- Variables in `terraform.tfvars` (gitignored)
- Sensitive values via environment variables (`TF_VAR_*` prefix)
- State backend configuration in `versions.tf`

**GitHub Actions**:

- Use official actions from `actions/*` org (e.g., `actions/checkout@v4`)
- HashiCorp official actions for Terraform (`hashicorp/setup-terraform@v3`)
- Matrix strategy for parallel node upgrades (when safe)
- Reusable workflows for common tasks (health checks, notifications)

**YAML Formatting**:

- 2-space indentation (consistent with existing Flux manifests)
- Explicit string quoting for values with special characters
- Comments for non-obvious configuration choices

#### Documentation Standards

**Required Documentation**:

1. **CLAUDE.md Updates**:
   - Add "Automated Cattle Upgrade Workflow" section
   - Document GitHub Actions Secrets setup procedure
   - Document manual intervention procedures (rollback, emergency stop)
   - Update agent delegation section (when to use homelab-infra-architect for upgrades)

2. **Runbook Creation** (`.claude/.ai-docs/runbooks/`):
   - `CATTLE_UPGRADE_RUNBOOK.md` - Step-by-step upgrade procedures
   - `ROLLBACK_PROCEDURES.md` - Manual rollback instructions
   - `TROUBLESHOOTING_GITHUB_ACTIONS.md` - Runner debugging guide

3. **PROJECT_STATE.md Updates**:
   - Add GitHub Actions runner deployment to service inventory
   - Update automation coverage metrics (GitOps: 100% → include CI/CD)
   - Document Terraform backend migration milestone

---

### 3.4 Deployment and Operations

#### Build Process Integration

**Terraform Workflow** (existing, unchanged):

1. Template creation: Downloads Talos images → Uploads to Proxmox nodes → Creates templates (10-15 min)
2. VM deployment: Clones from templates → Configures hardware → Starts VMs (5-10 min)

**GitHub Actions Workflow** (new):

1. Trigger: PR merge with `upgrade-cattle` or `upgrade-pets` label
2. Runner assignment: Self-hosted runner in cluster picks up job
3. Terraform init: Downloads providers, configures S3 backend
4. Node upgrade loop: Cordon → Drain → Destroy → Create → Validate → Uncordon
5. Notification: Discord webhook with completion status

#### Deployment Strategy

**Cattle Upgrade Deployment Strategy** (this enhancement):

**Phase 1: Foundation Setup** (Manual, One-Time)

- Deploy Actions Runner Controller via Flux
- Set up GitHub Actions Secrets in repository settings
- Migrate Terraform state to S3/Terraform Cloud
- Configure Renovate custom managers
- **Deployment Method**: GitOps (Flux applies HelmRelease)
- **Rollback**: Scale runner deployment to 0 replicas if issues occur

**Phase 2: Workflow Testing** (Dev/Staging)

- Test cattle workflow on single worker node (k8s-work-1)
- Validate health checks detect failures correctly
- Test rollback mechanism with intentional failure
- **Deployment Method**: Merge workflow YAML to main branch (Flux not involved)
- **Rollback**: Revert Git commit to remove workflow

**Phase 3: Production Rollout** (Automated)

- Renovate creates PR for Talos/K8s update
- User reviews PR, approves and merges
- GitHub Actions workflow executes automatically
- **Deployment Method**: Event-driven (PR merge trigger)
- **Rollback**: Automatic (workflow-managed) or manual (kubectl scale deployment --replicas=0)

**Rolling Upgrade Sequence** (defined by workflow):

1. Control plane nodes first: k8s-ctrl-1 → k8s-ctrl-2 → k8s-ctrl-3 (sequential, 5-min validation between)
2. Worker nodes second: k8s-work-1 through k8s-work-16 (sequential by default, optional 2-3 parallel)
3. GPU nodes treated as standard workers (no special sequence)
4. Total duration: ~90 minutes for 15 nodes (6 min/node × 15)

#### Monitoring and Logging

**GitHub Actions Execution Monitoring**:

- **UI**: GitHub repository → Actions tab shows workflow runs
- **Logs**: Full command output (terraform, kubectl, talosctl) retained 90 days
- **Notifications**: Discord channel receives real-time updates
- **Metrics**: Workflow duration, success rate tracked in GitHub Insights

**Cluster Health Monitoring** (existing, unchanged):

- **Prometheus**: Node metrics (node-exporter), cluster metrics (kube-state-metrics)
- **Grafana**: Dashboards show node restarts, pod rescheduling during upgrades
- **Loki**: Logs from runner pods (if promtail configured for github-actions namespace)
- **AlertManager**: Fires alerts if upgrades cause prolonged pod disruptions

**Terraform State Monitoring**:

- **S3 Backend**: State versions tracked, can revert to previous version if corruption occurs
- **State Locking**: DynamoDB table (if AWS) or Terraform Cloud shows lock status
- **Drift Detection**: `terraform plan` in workflow shows unexpected infrastructure changes

#### Configuration Management

**Terraform Configuration**:

- **Managed By**: Git (version-controlled in `terraform/`)
- **Sensitive Values**: Environment variables from GitHub Actions Secrets
- **State**: Remote backend (S3 or Terraform Cloud)
- **Changes**: Pull request workflow (same as application code)

**GitHub Actions Secrets**:

- **Managed By**: GitHub UI (Settings → Secrets and variables → Actions)
- **Rotation**: 90-day rotation policy (documented in runbook)
- **Audit**: GitHub audit log tracks secret access
- **Backup**: Secrets documented in 1Password vault (encrypted, separate from Git)

**1Password Vault**:

- **Managed By**: 1Password admin (user)
- **Synced To**: Kubernetes via External Secrets Operator
- **Items**: GitHub App credentials (app ID, installation ID, private key)
- **Refresh**: Every 5 minutes (ExternalSecret refresh interval)

---

### 3.5 Risk Assessment and Mitigation

**From EPIC-019 + PROJECT_STATE.md - Integrated Risk Analysis**

#### Technical Risks

**Risk 1: Terraform State Corruption** (HIGH IMPACT, LOW LIKELIHOOD)

- **Scenario**: S3 backend fails during state write, corrupts state file
- **Impact**: Cannot manage infrastructure via Terraform, manual Proxmox intervention required
- **Mitigation**:
  - Enable S3 versioning (retain 30 previous state versions)
  - State locking prevents concurrent modifications
  - Backup local state before migration (stored in 1Password secure notes)
  - Runbook for state recovery from backup
- **Detection**: Terraform init fails with state corruption error
- **Rollback**: Restore previous state version from S3, re-run terraform init

**Risk 2: Node Fails to Join Cluster After Recreation** (MEDIUM IMPACT, MEDIUM LIKELIHOOD)

- **Scenario**: Cattle upgrade creates new VM, but Talos config application fails or network issue prevents join
- **Impact**: Cluster operates with N-1 nodes, degraded capacity
- **Mitigation**:
  - 10-minute timeout for node Ready status (workflow waits)
  - Automatic rollback destroys failed node, recreates with old version
  - Control plane maintains quorum (2/3 nodes minimum)
  - Manual intervention runbook for stuck nodes
- **Detection**: `kubectl wait --for=condition=Ready` times out
- **Rollback**: Workflow automatically destroys failed node, recreates from previous template version

**Risk 3: GitHub Actions Runner Unavailability** (LOW IMPACT, LOW LIKELIHOOD)

- **Scenario**: Runner pods crash or scale to 0, workflows cannot execute
- **Impact**: Automated upgrades blocked, must use manual Terraform workflow
- **Mitigation**:
  - Deploy 2 runner replicas minimum (HA)
  - Liveness/readiness probes detect unhealthy runners
  - Manual upgrade path documented (existing Terraform workflow still functional)
  - Alert if runner pods unavailable > 10 minutes
- **Detection**: Workflow queued but never starts execution
- **Rollback**: Scale runner deployment manually, or use manual Terraform upgrade

**Risk 4: Health Check False Negatives** (LOW IMPACT, MEDIUM LIKELIHOOD)

- **Scenario**: Health check reports success but cluster actually degraded (e.g., missing critical pods)
- **Impact**: Workflow proceeds to next node, compounding problems
- **Mitigation**:
  - Comprehensive health checks (not just node Ready, but pod health, Ceph, Flux)
  - Manual review of Discord notifications before approving multi-node upgrades
  - Emergency stop procedure (cancel GitHub Actions workflow)
  - Post-upgrade validation window (5 minutes observation before next node)
- **Detection**: User notices issues in Grafana/Discord notifications
- **Rollback**: Cancel workflow run, manually rollback affected nodes

#### Integration Risks

**Risk 5: Renovate Creates Incorrect PRs** (LOW IMPACT, LOW LIKELIHOOD)

- **Scenario**: Regex custom manager parses version incorrectly, creates PR with wrong version
- **Impact**: Workflow upgrades to wrong version, potential breakage
- **Mitigation**:
  - Renovate PR includes clear version delta (1.11.3 → 1.12.0)
  - User reviews PR before merge (manual approval gate)
  - Test Renovate regex against mock terraform.tfvars files
  - Workflow validates version matches expected pattern before proceeding
- **Detection**: User notices version mismatch in PR description
- **Rollback**: Close PR, fix Renovate regex, create new PR

**Risk 6: Flux and GitHub Actions Conflict** (LOW IMPACT, LOW LIKELIHOOD)

- **Scenario**: Flux tries to reconcile while GitHub Actions modifies infrastructure
- **Impact**: Flux marks resources as out-of-sync, potential rollback fight
- **Mitigation**:
  - Flux manages runner deployment, workflows manage Proxmox VMs (separate domains)
  - Terraform state locking prevents concurrent Terraform operations
  - Flux reconciliation interval (5 min) allows workflow to complete
  - No Flux automation for Terraform-managed VMs (Flux only deploys apps)
- **Detection**: Flux logs show reconciliation conflicts
- **Rollback**: Suspend Flux Kustomization for github-actions namespace during troubleshooting

#### Deployment Risks

**Risk 7: Control Plane Quorum Loss** (CRITICAL IMPACT, LOW LIKELIHOOD)

- **Scenario**: Two control plane nodes fail simultaneously during upgrade
- **Impact**: etcd loses quorum, cluster API unavailable, full outage
- **Mitigation**:
  - **HARD REQUIREMENT**: Only 1 control plane node upgraded at a time
  - 5-minute validation after each control plane node upgrade
  - Control plane nodes upgraded BEFORE workers (workers tolerate brief API unavailability)
  - Emergency procedure: Manual etcd restore from backup (not automated)
- **Detection**: kubectl commands fail with "connection refused"
- **Rollback**: Halt workflow immediately, manually recover failed control plane node

**Risk 8: PodDisruptionBudget Violation** (MEDIUM IMPACT, LOW LIKELIHOOD)

- **Scenario**: kubectl drain evicts too many pods, violates PDB, application downtime
- **Impact**: Stateful workloads (databases, Ceph) experience downtime
- **Mitigation**:
  - kubectl drain respects PDBs (--disable-eviction flag NOT used)
  - 5-minute drain timeout (fails if PDB prevents eviction)
  - Workflow halts if drain fails 3 times
  - Critical workloads have PDBs defined (Ceph, Prometheus, Flux)
- **Detection**: kubectl drain exits with non-zero code
- **Rollback**: Uncordon node, skip that node, proceed to next

**Risk 9: Secret Exposure in Logs** (CRITICAL IMPACT, LOW LIKELIHOOD)

- **Scenario**: Workflow accidentally logs GitHub Actions Secret value (e.g., via set -x or echo)
- **Impact**: Proxmox credentials, AWS keys exposed in public GitHub logs
- **Mitigation**:
  - GitHub automatically masks secret values in logs
  - Workflow scripts avoid `set -x` or `echo` of variables containing secrets
  - Security-guardian review of workflows before merge
  - Secrets rotated immediately if exposure suspected
  - GitHub audit log monitors secret access
- **Detection**: Security scanning of workflow logs, GitHub secret scanning alerts
- **Rollback**: Rotate all exposed secrets immediately (Proxmox token, AWS keys), revoke GitHub App credentials

#### Overall Risk Posture: MEDIUM-LOW

**Mitigation Summary**:

- 9 identified risks, 6 have automated detection and rollback
- Critical risks (control plane quorum, secret exposure) have hard requirements preventing occurrence
- Medium risks (node join failure, health check accuracy) have both automated and manual mitigation
- Low risks have acceptable degradation (manual fallback available)

**Risk Acceptance**:

- User must accept residual risk: Control plane upgrades carry inherent risk (mitigated but not eliminated)
- Testing phase on worker nodes first validates workflow before touching control plane
- Emergency stop always available (cancel GitHub Actions workflow, manual intervention)

---

## 4. Epic and Story Structure

### 4.1 Epic Approach

**Epic Structure Decision**: **Single Epic with Phased Delivery**

**Rationale**:

This brownfield enhancement is a cohesive, tightly-coupled feature (automated cattle upgrades) that requires all components working together to deliver value. While it spans multiple technical domains (Terraform, GitHub Actions, Kubernetes, secret management), splitting into multiple epics would create artificial boundaries and dependency management overhead.

**Why Single Epic**:

- **Unified Goal**: All stories contribute to single outcome (automated cluster upgrades)
- **Sequential Dependencies**: Stories 1-3 are prerequisites for Stories 4-9 (cannot execute workflows without runners)
- **Integration Complexity**: Testing requires full stack (Renovate → GitHub Actions → Terraform → Cluster)
- **Delivery Model**: Incremental value delivery within single epic (foundation → automation → validation)

**Phased Approach Within Epic**:

- **Phase 1 (Foundation)**: Stories 1-3 - Infrastructure setup (state migration, secrets, runners)
- **Phase 2 (Automation)**: Stories 4-7 - Workflow implementation (Renovate, decision logic, upgrade strategies)
- **Phase 3 (Validation)**: Stories 8-9 - Health checks and testing

---

### Epic 1: Automated Talos Cattle Upgrade Strategy

**Epic Goal**: Enable fully automated, zero-downtime Talos Linux and Kubernetes upgrades via GitHub Actions workflows, reducing human time investment by 90% while eliminating configuration drift through validated cattle strategy (destroy/recreate nodes).

**Integration Requirements**:

- **PRESERVE**: All 27 existing HelmRelease deployments must remain operational during and after upgrades
- **EXTEND**: Renovate Bot configuration with custom managers (Talos/K8s version detection)
- **INTEGRATE**: GitHub Actions runners with existing 1Password ExternalSecrets infrastructure
- **MAINTAIN**: Existing Terraform module structure (no modifications to talos-template/talos-node modules)
- **RESPECT**: Existing multi-VLAN networking, GPU passthrough, and Ceph storage configurations

---

### Story 1.1: Terraform Remote State Migration

**Story**:

As a **cluster operator**,
I want **Terraform state migrated from local storage to remote S3-compatible backend**,
so that **GitHub Actions workflows can access cluster state for automated infrastructure operations**.

#### Acceptance Criteria

1. **S3 Backend Provisioned**:
   - S3 bucket created with name `prox-ops-terraform-state` (or equivalent)
   - Versioning enabled (retain 30 previous state versions minimum)
   - Server-side encryption enabled (AES-256 or KMS)
   - Bucket policy restricts access to terraform automation credentials only

2. **Backend Configuration Added**:
   - `terraform/versions.tf` updated with S3 backend configuration block
   - DynamoDB table created for state locking (if using AWS S3)
   - Backend variables documented in `terraform/README.md`

3. **State Migration Completed**:
   - Local state backed up to `.claude/.ai-docs/backups/terraform.tfstate.backup.<timestamp>`
   - `terraform init -migrate-state` executed successfully
   - Remote state verified with `terraform state list` (matches local state)
   - Local terraform.tfstate deleted from repository

4. **State Locking Verified**:
   - Test concurrent `terraform plan` operations (second operation waits for lock)
   - Lock timeout configured (300 seconds)
   - Lock acquisition logged in Terraform output

5. **GitHub Actions Access Configured**:
   - AWS credentials created for GitHub Actions (IAM user or STS role)
   - Credentials stored in GitHub Actions Secrets (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
   - Test workflow successfully runs `terraform init` against remote state

6. **Documentation Updated**:
   - CLAUDE.md updated with remote state access procedures
   - Runbook created for state recovery from backup
   - Terraform backend configuration documented in `terraform/README.md`

#### Integration Verification

**IV1: Existing Terraform Operations Continue Functioning**

- Manually run `terraform plan` from local machine → Output shows no infrastructure changes
- Manually run `terraform apply` on single test resource → Successfully creates resource
- Verify state updates appear in S3 bucket with new version number

**IV2: State Integrity Validated**

- Compare resource count before migration: `terraform state list | wc -l`
- Compare resource count after migration: Should match exactly (23 resources: 8 templates + 15 VMs)
- Spot-check 3 critical resources (control plane node, GPU worker, template) for data integrity

**IV3: No Cluster Impact**

- All 15 nodes remain Ready status throughout migration
- All 27 HelmReleases remain in "Ready: True" state
- No pod restarts or evictions during state migration (read-only operation)

---

### Story 1.2: GitHub Actions Secrets Configuration

**Story**:

As a **cluster operator**,
I want **all infrastructure credentials stored securely in GitHub Actions Secrets**,
so that **workflows can authenticate to Proxmox, S3, and Talos APIs without exposing credentials in Git repository**.

#### Acceptance Criteria

1. **GitHub Actions Secrets Created**:
   - Navigate to repository Settings → Secrets and variables → Actions
   - Create 9 repository-level secrets (not environment-specific):
     - `PROXMOX_USERNAME` (value: `root@pam!terraform` or API token user)
     - `PROXMOX_PASSWORD` (value: API token UUID)
     - `AWS_ACCESS_KEY_ID` (value: S3 backend access key)
     - `AWS_SECRET_ACCESS_KEY` (value: S3 backend secret key)
     - `TALOSCONFIG` (value: base64-encoded ~/.talos/config)
     - `DISCORD_WEBHOOK_URL` (value: webhook URL for notifications channel)
     - `GITHUB_APP_ID` (value: runner registration app ID)
     - `GITHUB_APP_INSTALLATION_ID` (value: installation ID for prox-ops repo)
     - `GITHUB_APP_PRIVATE_KEY` (value: PEM-formatted private key)

2. **Secret Documentation Created**:
   - Create `.claude/.ai-docs/runbooks/GITHUB_ACTIONS_SECRETS.md`
   - Document each secret's purpose, format, and example value (sanitized)
   - Document 90-day rotation schedule with procedures
   - Document secret access audit procedures (GitHub audit log)

3. **Secret Rotation Policy Established**:
   - Calendar reminder created for 90-day rotation
   - Rotation procedures documented in runbook
   - Test rotation by creating new Proxmox API token and updating secret

4. **Access Audit Logging Enabled**:
   - Verify GitHub audit log captures secret access events
   - Document how to review audit log (Settings → Logs → Audit log)
   - Test: Access secret in workflow, verify audit log entry appears

5. **Test Workflow Validates Access**:
   - Create `.github/workflows/test-secrets.yml` with secret echo (masked values)
   - Workflow successfully reads all 9 secrets
   - Workflow output shows masked values (e.g., `***` instead of actual secret)
   - Delete test workflow after validation

#### Integration Verification

**IV1: Secrets Never Exposed in Git History**

- Run `git log --all --full-history -- '*secret*' '*credential*'` → No results
- Run `git log --all --full-history -S 'PROXMOX_PASSWORD'` → No results
- Verify `.gitignore` contains patterns preventing accidental commit

**IV2: Existing Secret Management Unaffected**

- 1Password Connect continues syncing ExternalSecrets (5 active)
- SOPS-encrypted files remain unchanged (`.sops.yaml` files encrypted)
- No conflicts between GitHub Actions Secrets and Kubernetes Secrets (separate domains)

**IV3: Security Best Practices Validated**

- GitHub secret scanning enabled (repository Settings → Security → Secret scanning)
- No alert for exposed secrets in repository
- Dependabot alerts enabled for workflow dependencies

---

### Story 1.3: Self-Hosted GitHub Actions Runner Deployment

**Story**:

As a **cluster operator**,
I want **GitHub Actions runners deployed in the Kubernetes cluster via Flux GitOps**,
so that **workflows can execute infrastructure operations with direct cluster access and avoid egress costs**.

#### Acceptance Criteria

1. **Namespace and RBAC Created**:
   - Create `kubernetes/apps/github-actions/namespace.yaml`
   - Create `kubernetes/apps/github-actions/app/rbac.yaml`:
     - ServiceAccount: `github-actions-runner`
     - ClusterRole: `github-actions-runner-admin` (permissions: nodes/*, pods/*, deployments/*, statefulsets/*)
     - ClusterRoleBinding: Binds ServiceAccount to ClusterRole

2. **1Password ExternalSecret Created**:
   - Create `kubernetes/apps/github-actions/app/externalsecret.yaml`
   - ExternalSecret syncs GitHub App credentials from 1Password vault
   - Target Kubernetes Secret: `github-runner-registration`
   - Fields: `github_app_id`, `github_app_installation_id`, `github_app_private_key`
   - RefreshInterval: 5 minutes

3. **HelmRelease Deployed**:
   - Create `kubernetes/apps/github-actions/app/helmrelease.yaml`
   - Chart: `actions-runner-controller` (version 0.9.x)
   - Repository: `https://actions-runner-controller.github.io/actions-runner-controller`
   - Configuration:
     - `githubConfigUrl: https://github.com/jlengelbrecht/prox-ops`
     - `githubConfigSecret: github-runner-registration`
     - `runnerScaleSetName: cattle-upgrade`
     - `minRunners: 2`, `maxRunners: 5`
   - ServiceAccount: `github-actions-runner`

4. **Flux Kustomization Created**:
   - Create `kubernetes/apps/github-actions/ks.yaml`
   - Kustomization references `./app/kustomization.yaml`
   - DependsOn: `external-secrets` (ensures ExternalSecret operator available)
   - Health checks enabled for HelmRelease

5. **Runners Successfully Register**:
   - Flux reconciles Kustomization: `flux reconcile kustomization github-actions -n flux-system`
   - Verify runner pods: `kubectl get pods -n github-actions` → 2/2 Running
   - Verify GitHub registration: Repository Settings → Actions → Runners → Shows 2 idle runners

6. **Test Workflow Executes**:
   - Create `.github/workflows/test-runner.yml` with `runs-on: [self-hosted, kubernetes, cattle-upgrade]`
   - Workflow prints node name, kubectl version, terraform version
   - Workflow successfully executes on self-hosted runner (not GitHub-hosted)

#### Integration Verification

**IV1: Existing Flux Reconciliation Unaffected**

- All existing Kustomizations remain Applied: `flux get kustomizations -A`
- All existing HelmReleases remain Ready: `flux get helmreleases -A`
- GitHub actions namespace added to Flux monitoring, no reconciliation conflicts

**IV2: Runner Pod Resource Limits Respected**

- Runners have resource requests: CPU 500m, Memory 1Gi
- Runners have resource limits: CPU 2000m, Memory 4Gi
- Verify no resource exhaustion on worker nodes: `kubectl top nodes`

**IV3: ServiceAccount Permissions Validated**

- Test: Runner executes `kubectl get nodes` → Success (lists all 15 nodes)
- Test: Runner executes `kubectl cordon k8s-work-1` → Success (node cordoned)
- Test: Runner executes `kubectl uncordon k8s-work-1` → Success (node uncordoned)
- Verify: ServiceAccount cannot access secrets in other namespaces (RBAC isolation)

---

### Story 1.4: Renovate Bot Custom Managers

**Story**:

As a **cluster operator**,
I want **Renovate Bot to automatically detect Talos and Kubernetes version updates in Terraform files**,
so that **version update PRs are created automatically with appropriate upgrade strategy labels**.

#### Acceptance Criteria

1. **Custom Managers Added to Renovate Config**:
   - Update `.github/renovate.json5` with two custom regex managers:
     - **Manager 1**: Detects `talos_version = "1.11.3"` in `terraform/variables.tf`
       - `datasourceTemplate: "github-releases"`
       - `depNameTemplate: "siderolabs/talos"`
     - **Manager 2**: Detects `kubernetes_version = "1.34.1"` in `terraform/variables.tf`
       - `datasourceTemplate: "github-releases"`
       - `depNameTemplate: "kubernetes/kubernetes"`

2. **Package Rules Configured**:
   - Separate PRs for major vs minor vs patch updates (`separateMinorPatch: true`)
   - Label `upgrade-pets` applied to patch updates
   - Label `upgrade-cattle` applied to minor/major updates
   - Group name: "Talos Linux" and "Kubernetes" (clear PR titles)
   - Automerge disabled (`automerge: false`) - require manual approval

3. **Test PR Created**:
   - Manually trigger Renovate: `renovate:rebuildAll` in PR comment (or wait for scheduled run)
   - Renovate creates test PR with current version → next version
   - PR includes changelog link, release notes, and upgrade strategy label

4. **PR Description Includes Version Delta**:
   - PR body shows: "Talos: 1.11.3 → 1.11.4" (or current → latest)
   - PR body includes link to Talos release notes
   - PR body includes recommended upgrade strategy (pets or cattle)

5. **Renovate Dashboard Updated**:
   - Check Renovate Dependency Dashboard issue in repository
   - Talos and Kubernetes updates listed under "Detected dependencies"
   - No errors in Renovate logs

#### Integration Verification

**IV1: Existing Renovate Updates Continue**

- Existing Helm chart updates still create PRs (5+ merged in past week)
- Existing container image updates still detected
- No conflicts between new custom managers and existing package rules

**IV2: Version Detection Accuracy**

- Test regex against mock `terraform/variables.tf` with different versions
- Verify Renovate correctly parses semantic versioning (1.11.3, 1.12.0, 2.0.0)
- Verify Renovate ignores pre-release versions (1.12.0-alpha1) unless configured

**IV3: Label Application Correct**

- Patch update PR (1.11.3 → 1.11.4) has label: `upgrade-pets`
- Minor update PR (1.11.x → 1.12.x) has label: `upgrade-cattle`
- Labels visible in GitHub UI: Pull Requests → Filter by label

---

### Story 1.5: Cattle Upgrade Workflow Implementation

**Story**:

As a **cluster operator**,
I want **automated cattle upgrade workflow that destroys and recreates nodes with new Talos version**,
so that **major version upgrades complete with zero configuration drift and minimal human intervention**.

#### Acceptance Criteria

1. **Workflow File Created**:
   - Create `.github/workflows/upgrade-cattle.yml`
   - Trigger: `on.pull_request.types: [closed]` + `if: merged == true` + label `upgrade-cattle`
   - Runs on: `[self-hosted, kubernetes, cattle-upgrade]`
   - Strategy: Matrix with node list, `max-parallel: 1` (sequential upgrades)

2. **Workflow Stages Implemented**:
   - **Stage 1: Setup**: Checkout code, setup Terraform, init remote state
   - **Stage 2: Cordon**: `kubectl cordon k8s-${{ matrix.node }}`
   - **Stage 3: Drain**: `kubectl drain --ignore-daemonsets --delete-emptydir-data --timeout=300s`
   - **Stage 4: Destroy**: `terraform destroy -target='module.nodes["k8s-${{ matrix.node }}"]' -auto-approve`
   - **Stage 5: Create**: `terraform apply -target='module.nodes["k8s-${{ matrix.node }}"]' -auto-approve`
   - **Stage 6: Wait**: `kubectl wait --for=condition=Ready node/k8s-${{ matrix.node }} --timeout=600s`
   - **Stage 7: Uncordon**: `kubectl uncordon k8s-${{ matrix.node }}`
   - **Stage 8: Validate**: Call reusable workflow `health-check.yml`
   - **Stage 9: Notify**: Discord webhook with success/failure status

3. **Environment Variables from Secrets**:
   - `TF_VAR_proxmox_username: ${{ secrets.PROXMOX_USERNAME }}`
   - `TF_VAR_proxmox_password: ${{ secrets.PROXMOX_PASSWORD }}`
   - `AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}`
   - `AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}`

4. **Node Upgrade Order Defined**:
   - Control plane first: `[ctrl-1, ctrl-2, ctrl-3]`
   - Workers second: `[work-1, work-2, ..., work-16]` (excluding non-existent work-6 through work-10)
   - GPU nodes treated as standard workers (no special sequence)

5. **Error Handling Implemented**:
   - Each stage has `continue-on-error: false` (halt on failure)
   - Timeout for drain: 5 minutes
   - Timeout for node Ready: 10 minutes
   - If any stage fails, workflow halts and sends Discord alert

6. **Workflow Tested on Dev Node**:
   - Create test PR with label `upgrade-cattle` targeting single worker (k8s-work-1)
   - Merge PR, verify workflow executes
   - Verify node destroyed, recreated, and rejoins cluster
   - Verify pods reschedule to new node

#### Integration Verification

**IV1: Existing Workload Availability Maintained**

- Before upgrade: Note pod distribution across nodes
- During upgrade: Verify pods evicted from upgrading node reschedule to other nodes
- After upgrade: Verify all 27 HelmReleases remain Ready: True
- Zero unplanned pod restarts (only expected rescheduling)

**IV2: PodDisruptionBudget Compliance**

- Workflow respects PDBs for critical workloads (Ceph, Prometheus, Flux controllers)
- If PDB prevents drain, workflow waits (up to 5-minute timeout)
- Test with intentional PDB violation (single-replica deployment with minAvailable=1) → Drain fails gracefully

**IV3: Terraform State Consistency**

- After node upgrade: `terraform plan` shows no changes (infrastructure matches state)
- Destroyed node removed from state: `terraform state list | grep k8s-work-1` → Empty after destroy
- Recreated node added to state: `terraform state list | grep k8s-work-1` → Present after apply

---

### Story 1.6: Pets Upgrade Workflow Implementation

**Story**:

As a **cluster operator**,
I want **automated pets upgrade workflow that performs in-place Talos upgrades**,
so that **patch version updates complete quickly without node recreation**.

#### Acceptance Criteria

1. **Workflow File Created**:
   - Create `.github/workflows/upgrade-pets.yml`
   - Trigger: `on.pull_request.types: [closed]` + `if: merged == true` + label `upgrade-pets`
   - Runs on: `[self-hosted, kubernetes, cattle-upgrade]`
   - Strategy: Matrix with node IP list, `max-parallel: 1`

2. **Workflow Stages Implemented**:
   - **Stage 1: Setup**: Decode `TALOSCONFIG` from GitHub Actions Secret
   - **Stage 2: Upgrade**: `talosctl upgrade --nodes ${{ matrix.ip }} --image factory.talos.dev/installer/${{ env.NEW_VERSION }}`
   - **Stage 3: Wait**: `kubectl wait --for=condition=Ready node/${{ matrix.node }} --timeout=600s`
   - **Stage 4: Validate**: Call reusable workflow `health-check.yml`
   - **Stage 5: Notify**: Discord webhook per node completion

3. **Talosconfig Handling**:
   - Environment variable: `TALOSCONFIG: ${{ secrets.TALOSCONFIG }}`
   - Decode base64: `echo "$TALOSCONFIG" | base64 -d > /tmp/talosconfig`
   - Export path: `export TALOSCONFIG=/tmp/talosconfig`

4. **Node Upgrade Order**:
   - Control plane first (IPs: 10.20.67.1, 10.20.67.2, 10.20.67.3)
   - Workers second (IPs: 10.20.67.4 through 10.20.67.15)

5. **Version Extraction**:
   - Parse new version from PR diff: `git diff HEAD~1 terraform/variables.tf | grep talos_version`
   - Store in environment variable: `NEW_VERSION`

6. **Workflow Tested on Dev Node**:
   - Create test PR with label `upgrade-pets` for patch version (1.11.3 → 1.11.4)
   - Merge PR, verify workflow executes
   - Verify talosctl upgrade command succeeds
   - Verify node reboots and rejoins cluster with new version

#### Integration Verification

**IV1: In-Place Upgrade Preserves Configuration**

- Before upgrade: Note node labels, taints, annotations
- After upgrade: Verify labels/taints/annotations preserved
- Verify multi-NIC configuration preserved (3 network interfaces)

**IV2: Minimal Disruption**

- Upgrade duration per node: ≤ 3 minutes (faster than cattle's 6 minutes)
- Pod rescheduling: Minimal (only DaemonSets restart, deployments remain on node)
- API availability: Maintained (2/3 control plane nodes operational during each upgrade)

**IV3: Version Verification**

- After upgrade: `talosctl version --nodes 10.20.67.1` → Shows new version
- After upgrade: `kubectl get node k8s-ctrl-1 -o yaml | grep talos` → Shows new version in node annotations

---

### Story 1.7: Health Validation Framework

**Story**:

As a **cluster operator**,
I want **comprehensive automated health checks after each node upgrade**,
so that **workflow automatically detects failures and halts before cascading issues occur**.

#### Acceptance Criteria

1. **Reusable Workflow Created**:
   - Create `.github/workflows/health-check.yml`
   - Callable from other workflows: `workflow_call` trigger
   - Input: `node_name` (string)
   - Output: `health_status` (pass/fail)

2. **Health Check Categories Implemented**:

   **Node Readiness**:
   - `kubectl get node ${{ inputs.node_name }} -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}'` → Must be "True"
   - Node not in SchedulingDisabled state

   **Pod Health**:
   - All namespaces: `kubectl get pods -A --field-selector spec.nodeName=${{ inputs.node_name }}` → No CrashLoopBackOff
   - No pending pods > 5 minutes
   - Critical namespaces (kube-system, flux-system, rook-ceph) have all pods Running

   **Storage Health**:
   - Ceph health: `kubectl exec -n rook-ceph deploy/rook-ceph-tools -- ceph status` → HEALTH_OK
   - CSI pods operational: `kubectl get pods -n kube-system -l app=csi` → All Running

   **Flux Reconciliation**:
   - All Kustomizations: `kubectl get kustomizations -A -o json | jq '.items[] | select(.status.conditions[] | select(.type=="Ready" and .status!="True"))'` → Empty result
   - All HelmReleases: `kubectl get helmreleases -A -o json | jq '.items[] | select(.status.conditions[] | select(.type=="Ready" and .status!="True"))'` → Empty result

   **DNS Resolution**:
   - Test internal DNS: `kubectl run test-dns --rm -i --image=busybox -- nslookup kubernetes.default.svc.cluster.local` → Resolves successfully

   **Network Connectivity**:
   - Pod-to-pod: `kubectl run test-ping --rm -i --image=busybox -- ping -c 3 10.42.0.1` → 0% packet loss
   - Pod-to-service: `kubectl run test-curl --rm -i --image=curlimages/curl -- curl -s kubernetes.default.svc.cluster.local:443` → Connection successful

   **GPU Availability** (if node is GPU node):
   - `kubectl get node ${{ inputs.node_name }} -o json | jq '.status.allocatable["nvidia.com/gpu"]'` → Shows "1" for GPU nodes
   - NVIDIA device plugin pods running: `kubectl get pods -n kube-system -l app=nvidia-device-plugin --field-selector spec.nodeName=${{ inputs.node_name }}` → Running

3. **Health Check Timeout**:
   - Total health check duration: ≤ 5 minutes
   - Individual check timeout: 30 seconds
   - Retry failed checks 3 times before declaring failure

4. **Health Check Output**:
   - Print summary: "✅ Node k8s-work-1: 7/7 health checks passed"
   - Or: "❌ Node k8s-work-1: 5/7 health checks passed (failed: Storage Health, DNS Resolution)"
   - Failed checks include diagnostic output (last 10 lines of error)

5. **Integration with Upgrade Workflows**:
   - Cattle workflow calls health-check after each node upgrade
   - Pets workflow calls health-check after each node upgrade
   - If health check fails, workflow halts and sends alert

#### Integration Verification

**IV1: Health Checks Detect Actual Failures**

- Simulate failure: Scale down CoreDNS to 0 replicas
- Run health check → DNS Resolution check fails
- Restore CoreDNS → Health check passes

**IV2: Health Checks Complete Within Timeout**

- Run health check on healthy node → Completes in < 2 minutes
- Run health check on unhealthy node (simulated failure) → Completes in < 5 minutes (includes retries)

**IV3: No False Positives**

- Run health check 10 times on stable cluster → 10/10 pass
- No intermittent failures due to timing issues or network hiccups

---

### Story 1.8: Rollback Automation

**Story**:

As a **cluster operator**,
I want **automatic rollback if node upgrade fails health checks**,
so that **cluster stability is preserved and manual intervention is minimized**.

#### Acceptance Criteria

1. **Rollback Logic in Cattle Workflow**:
   - If health check fails 3 times: Trigger rollback
   - Rollback steps:
     1. Destroy failed node: `terraform destroy -target='module.nodes["k8s-${{ matrix.node }}"]'`
     2. Revert Terraform variables to previous version (via Git)
     3. Recreate node with old version: `terraform apply -target='module.nodes["k8s-${{ matrix.node }}"]'`
     4. Wait for Ready: `kubectl wait --for=condition=Ready`
     5. Run health check again (verify rollback success)

2. **Rollback Logic in Pets Workflow**:
   - If health check fails 3 times: Trigger rollback
   - Rollback steps:
     1. Downgrade via talosctl: `talosctl upgrade --nodes <IP> --image factory.talos.dev/installer/<old-version>`
     2. Wait for Ready
     3. Run health check (verify rollback success)

3. **Rollback Notification**:
   - Discord alert: "🔴 ROLLBACK: k8s-work-1 failed health checks, reverting to v1.11.3"
   - Include failure reason (which health check failed)
   - Include rollback status (in progress / completed / failed)

4. **Workflow Halt on Rollback Failure**:
   - If rollback fails: Halt workflow entirely (do not proceed to next node)
   - Send critical Discord alert: "🚨 CRITICAL: Rollback failed for k8s-work-1, manual intervention required"
   - Workflow exit code: Non-zero

5. **Rollback Testing**:
   - Simulate failure: Intentionally break health check (e.g., stop CoreDNS)
   - Trigger cattle upgrade workflow
   - Verify rollback executes automatically
   - Verify node restored to previous version

#### Integration Verification

**IV1: Rollback Preserves Cluster Stability**

- Simulate failure on k8s-work-1
- Verify rollback completes
- Verify all 27 HelmReleases remain Ready
- Verify no cascading failures to other nodes

**IV2: Rollback State Consistency**

- After rollback: `terraform plan` shows no changes
- After rollback: Node version matches pre-upgrade version
- After rollback: Terraform state matches actual infrastructure

**IV3: Rollback Documentation**

- Create runbook: `.claude/.ai-docs/runbooks/ROLLBACK_PROCEDURES.md`
- Document manual rollback steps (for when automation fails)
- Document how to identify failed node in GitHub Actions logs

---

### Story 1.9: Integration Testing and Documentation

**Story**:

As a **cluster operator**,
I want **comprehensive end-to-end testing and documentation**,
so that **automated upgrades are validated and future operators can understand the system**.

#### Acceptance Criteria

1. **End-to-End Testing - Cattle Strategy**:
   - Test on dev worker (k8s-work-1):
     - Create PR with Talos version update (1.11.3 → 1.11.4)
     - Apply label: `upgrade-cattle`
     - Merge PR, verify workflow executes
     - Verify node destroyed, recreated, rejoins cluster
     - Verify health checks pass
     - Verify workloads reschedule correctly
   - Measure duration: Target ≤ 6 minutes per node

2. **End-to-End Testing - Pets Strategy**:
   - Test on dev worker (k8s-work-2):
     - Create PR with Talos patch version (simulate 1.11.4 → 1.11.5)
     - Apply label: `upgrade-pets`
     - Merge PR, verify workflow executes
     - Verify in-place upgrade completes
     - Verify node rejoins with new version
   - Measure duration: Target ≤ 3 minutes per node

3. **Failure Scenario Testing**:
   - Test health check failure detection
   - Test automatic rollback execution
   - Test workflow halt on critical failure
   - Test PodDisruptionBudget compliance

4. **Secret Exposure Testing**:
   - Review all workflow logs for exposed secrets
   - Verify GitHub masks all secret values (shows `***`)
   - Run `git log --all -S 'PROXMOX_PASSWORD'` → No results

5. **Documentation Completion**:

   **CLAUDE.md Updates**:
   - Section: "Automated Cattle Upgrade Workflow"
   - Subsections:
     - Overview (how it works)
     - GitHub Actions Secrets setup
     - Workflow trigger process (Renovate PR → Merge → Execution)
     - Manual intervention procedures
     - Emergency stop (cancel workflow)

   **Runbooks Created**:
   - `.claude/.ai-docs/runbooks/CATTLE_UPGRADE_RUNBOOK.md`:
     - Pre-upgrade checklist
     - Workflow monitoring guide
     - Post-upgrade validation
   - `.claude/.ai-docs/runbooks/ROLLBACK_PROCEDURES.md`:
     - Automatic rollback process
     - Manual rollback steps
     - Rollback verification
   - `.claude/.ai-docs/runbooks/TROUBLESHOOTING_GITHUB_ACTIONS.md`:
     - Runner debugging
     - Workflow failure diagnosis
     - Common errors and solutions

   **PROJECT_STATE.md Updates**:
   - Add GitHub Actions runner to service inventory (namespace: github-actions)
   - Update automation coverage: "CI/CD: GitHub Actions workflows deployed"
   - Add Epic-019 to completed epics section
   - Update infrastructure maturity: 90% → 95%

6. **Test Results Documented**:
   - Create `.claude/.ai-docs/stories/EPIC-019-TEST-RESULTS.md`
   - Include:
     - Test execution logs
     - Performance metrics (upgrade duration per node)
     - Success/failure rates
     - Screenshots of Discord notifications
     - Lessons learned

#### Integration Verification

**IV1: No Regressions Introduced**

- All 27 HelmReleases remain Ready after testing
- All 15 nodes remain Ready after testing
- Ceph health remains HEALTH_OK
- Flux reconciliation continues normally

**IV2: GitHub Actions Secrets Never Exposed**

- Review all workflow run logs → No plaintext secrets visible
- Review Git history → No secrets committed
- GitHub secret scanning → No alerts

**IV3: Documentation Completeness**

- New cluster operator can follow documentation to understand system
- Runbooks tested by following steps verbatim (catch any gaps)
- All screenshots and examples use sanitized values (no real credentials)

---

## Summary

This PRD defines a comprehensive automated cattle upgrade strategy for the prox-ops Talos Kubernetes cluster. The enhancement delivers:

- **90% reduction in manual effort** (hours → minutes)
- **Zero configuration drift** (cattle strategy with fresh nodes)
- **Production-grade safety** (health checks, rollback, validation)
- **Complete audit trail** (GitOps workflow with PR approvals)

The implementation follows a phased approach across 9 sequential stories totaling 40-55 hours of effort, leveraging existing infrastructure (Renovate Bot, 1Password ExternalSecrets, Flux GitOps) while adding new components (GitHub Actions runners, remote Terraform state, automated workflows).

**Total Estimated Effort**: 40-55 hours (10-15 days at 4-6 hours/day)

**Prerequisites**: Terraform state migration to S3/Terraform Cloud (Story 1.1) is P0 blocker

**Risk Level**: MEDIUM-LOW (comprehensive mitigation strategies, proven cattle replacement time 5-6 min/node)

---

**Document Status**: FINAL
**Ready for**: Scrum Master breakdown into work items
**Next Step**: Create stories in `.claude/.ai-docs/stories/` and begin Story 1.1 implementation
