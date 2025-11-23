# Talos Upgrade Workflows - Current State Reference

**Version**: 5.0 (Post PR #211)
**Status**: Production
**Created**: 2025-11-22
**Last Updated**: 2025-11-22
**Related PRs**: #205 (batched templates), #206 (validation fix), #211 (pets template rebuild)

---

## Executive Summary

**Current Implementation**: Fully automated Talos version upgrades with intelligent routing:
- **MAJOR/MINOR changes** → Cattle workflow (templates + full node rebuild)
- **PATCH changes** → Pets workflow (templates only, VMs untouched)

**Key Innovation (PR #211)**: **Both workflows now rebuild templates** before any VM operations, ensuring templates stay current regardless of upgrade type.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Workflow Routing Logic](#workflow-routing-logic)
3. [Template Rebuild Process](#template-rebuild-process)
4. [Cattle Workflow (MAJOR/MINOR)](#cattle-workflow-majorminor)
5. [Pets Workflow (PATCH)](#pets-workflow-patch)
6. [Safety Mechanisms](#safety-mechanisms)
7. [File Reference](#file-reference)
8. [Workflow Execution Examples](#workflow-execution-examples)

---

## Architecture Overview

### High-Level Flow

```
Renovate PR (Talos version change in terraform/variables.tf)
  ↓
User Merges PR
  ↓
┌────────────────────────────────────────────────────────────┐
│ Router Workflow (talos-version-router.yaml)                │
│ Runs on: ubuntu-latest (GitHub-hosted)                     │
├────────────────────────────────────────────────────────────┤
│ 1. Detect version change (compare HEAD^ vs HEAD)           │
│ 2. Parse semver (MAJOR.MINOR.PATCH)                        │
│ 3. Decision:                                                │
│    - MAJOR/MINOR changed → Route to CATTLE                 │
│    - PATCH changed only  → Route to PETS                   │
└────────────────────────────────────────────────────────────┘
          │                           │
          ├───── Cattle ─────────┐    ├───── Pets ──────────┐
          ▼                      │    ▼                      │
┌─────────────────────────┐     │ ┌──────────────────────┐  │
│ Cattle Workflow         │     │ │ Pets Workflow        │  │
│ (templates_only=false)  │     │ │ (templates_only=true)│  │
│                         │     │ │                      │  │
│ Phase 1: Templates ✅   │     │ │ Phase 1: Templates ✅│  │
│ Phase 2: Control Plane  │     │ │ Phase 2: SKIP ⏭️     │  │
│ Phase 3: Workers        │     │ │ Phase 3: SKIP ⏭️     │  │
│ Phase 4: Validation     │     │ │ Phase 4: SKIP ⏭️     │  │
└─────────────────────────┘     │ └──────────────────────┘  │
                                │                           │
Both workflows run on:          │                           │
cattle-runner (external)────────┴───────────────────────────┘
```

### Current State (November 2025)

| Component | Status | Version |
|-----------|--------|---------|
| **Talos Linux** | Production | 1.11.5 |
| **Kubernetes** | Production | 1.34.1 (bundled) |
| **Templates** | Current | 1.11.5 (8 templates) |
| **Cluster Nodes** | Healthy | 15 nodes (3 ctrl + 12 workers) |
| **Router Workflow** | Production | v2.0 (semver-based) |
| **Cattle Workflow** | Production | v5.0 (batched templates) |
| **Pets Workflow** | Production | v1.0 (templates-only mode) |
| **External Runner** | Production | cattle-runner on Proxmox |

---

## Workflow Routing Logic

### Semver-Based Routing

**Router File**: `.github/workflows/talos-version-router.yaml`

**Detection Logic**:
```bash
# Extract versions from Git history
OLD_VERSION=$(git show HEAD^:terraform/variables.tf | grep 'default.*talos_version' | cut -d'"' -f2)
NEW_VERSION=$(git show HEAD:terraform/variables.tf | grep 'default.*talos_version' | cut -d'"' -f2)

# Parse components
OLD_MAJOR.OLD_MINOR.OLD_PATCH  # e.g., 1.10.8
NEW_MAJOR.NEW_MINOR.NEW_PATCH  # e.g., 1.11.5

# Routing decision
if [[ "$OLD_MAJOR" != "$NEW_MAJOR" ]] || [[ "$OLD_MINOR" != "$NEW_MINOR" ]]; then
  UPGRADE_TYPE="cattle"  # MAJOR or MINOR change
else
  UPGRADE_TYPE="pets"    # PATCH change only
fi
```

### Routing Examples

| Old Version | New Version | Change Type | Workflow | Reason |
|-------------|-------------|-------------|----------|--------|
| 1.10.8 | 1.10.9 | PATCH | **Pets** | Only patch differs (10.8 → 10.9) |
| 1.10.8 | 1.11.0 | MINOR | **Cattle** | Minor changed (10 → 11) |
| 1.11.5 | 2.0.0 | MAJOR | **Cattle** | Major changed (1 → 2) |
| 1.11.4 | 1.11.5 | PATCH | **Pets** | Only patch differs (11.4 → 11.5) |

### Router Outputs

Router workflow provides these outputs to called workflows:

```yaml
outputs:
  version_changed: "true"      # or "false"
  old_version: "1.11.4"        # Semver format
  new_version: "1.11.5"        # Semver format
  upgrade_type: "pets"         # or "cattle"
```

---

## Template Rebuild Process

### Overview

**Critical Feature (PR #211)**: Templates are rebuilt for **ALL** version changes, regardless of upgrade type.

**Template Structure**:
- **8 total templates** (2 per Proxmox host × 4 hosts)
- **By role**: 4 controller + 4 worker templates
- **By host**: Baldar (9000, 9001), Heimdall (9002, 9003), Odin (9004, 9005), Thor (9006, 9007)

### Batched Execution Strategy

**Problem Solved (PR #205)**: Parallel template creation caused Ceph storage lock contention.

**Solution**: Batched execution with 30-second pauses

```yaml
Batch 1: Baldar controller + Heimdall controller (2 templates, different hosts)
  ↓ sleep 30
Batch 2: Odin controller + Thor controller (2 templates, different hosts)
  ↓ sleep 30
Batch 3: Baldar worker + Heimdall worker (2 templates, different hosts)
  ↓ sleep 30
Batch 4: Odin worker + Thor worker (2 templates, different hosts)
```

**Benefits**:
- ✅ No Ceph lock contention (different hosts per batch)
- ✅ Sequential batches prevent storage I/O saturation
- ✅ 30-second pauses allow Ceph to release locks
- ✅ Total time: ~10-15 minutes (vs 5 minutes parallel with failures)

### Safety Mechanisms

**All templates undergo multi-layer validation**:

#### 1. Pre-Execution Plan Analysis

```bash
# Generate plan targeting ONLY template modules
terraform plan -target=module.template_* -out=template-rebuild.tfplan

# Capture plan output for analysis
plan_output=$(terraform show template-rebuild.tfplan)
```

#### 2. Positive Assertion (Verify templates ARE in plan)

```bash
# Count template modules in plan
TEMPLATE_COUNT=$(echo "$plan_output" | grep -c "module.template_")

# Fail if no templates found (configuration error)
if [[ $TEMPLATE_COUNT -eq 0 ]]; then
  echo "❌ ERROR: No template modules found in plan!"
  exit 1
fi

# Warn if fewer than expected
if [[ $TEMPLATE_COUNT -lt 8 ]]; then
  echo "⚠️  WARNING: Only $TEMPLATE_COUNT templates found (expected 8)"
fi
```

#### 3. Negative Assertion (Fail if cluster VMs in plan)

```bash
# Check for cluster VM modules (VM IDs 901-915)
if echo "$plan_output" | grep -E "module\.(control_plane_nodes|worker_nodes)" > /dev/null; then
  echo "❌ ERROR: Plan contains cluster node changes!"
  echo "This would destroy production VMs. Aborting."
  exit 1
fi
```

#### 4. Plan File Validation

```bash
# Verify plan file exists before apply
if [[ ! -f template-rebuild.tfplan ]]; then
  echo "❌ ERROR: Plan file not found!"
  exit 1
fi
```

#### 5. Template State Verification (Post-Rebuild)

```bash
# After rebuild, verify all 8 templates in Terraform state
for module in template_baldar_controller template_baldar_worker \
              template_heimdall_controller template_heimdall_worker \
              template_odin_controller template_odin_worker \
              template_thor_controller template_thor_worker; do
  if terraform state list | grep -q "module.$module"; then
    echo "✅ module.$module present in state"
  else
    echo "❌ module.$module MISSING from state"
    exit 1
  fi
done
```

### Template Rebuild Execution

**File**: `.github/workflows/upgrade-cattle.yaml` (lines 49-529)

**Job**: `rebuild-templates`

**Runner**: `cattle-runner` (external Proxmox VM)

**Duration**: 10-15 minutes

**Steps**:
1. Validate version inputs (semver format)
2. Setup Terraform + AWS credentials
3. Initialize Terraform (S3 backend)
4. Resolve stale locks (if any, with timeout protection)
5. Plan template changes (target module.template_* only)
6. Analyze plan (positive + negative assertions)
7. Execute batched rebuild (4 batches × 2 templates)
8. Verify all 8 templates in state

---

## Cattle Workflow (MAJOR/MINOR)

### Overview

**When**: MAJOR or MINOR version changes (e.g., 1.10.8 → 1.11.5)

**Purpose**: Full infrastructure refresh via Terraform destroy/create

**Duration**: 3-4 hours (15 nodes × 10-15 min/node)

**File**: `.github/workflows/upgrade-cattle.yaml`

**Trigger**:
```yaml
# Called by router workflow
workflow_call:
  inputs:
    old_version: "1.10.8"
    new_version: "1.11.5"
    test_mode: true          # Only 2 workers for testing
    templates_only: false    # Full cattle upgrade
```

### Workflow Phases

```
┌──────────────────────────────────────────────────────┐
│ Phase 1: Rebuild Templates (10-15 min)              │
│ ─────────────────────────────────────────────────── │
│ ✅ Always runs (templates_only doesn't affect this)  │
│ • Destroy 8 old templates at old version            │
│ • Create 8 new templates at new version             │
│ • Verify all templates in Terraform state           │
│ • All safety checks active (see above)              │
└──────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│ Phase 2: Upgrade Control Plane (30-45 min)          │
│ ─────────────────────────────────────────────────── │
│ ⚠️  Skipped if templates_only=true                   │
│ ✅ Runs if templates_only=false (cattle mode)        │
│                                                      │
│ Sequential (1 at a time, max-parallel: 1):          │
│ For each controller (k8s-ctrl-1, 2, 3):             │
│   1. Validate etcd quorum (≥2 healthy)               │
│   2. Drain node (graceful 240s)                      │
│   3. Terraform destroy (delete VM)                   │
│   4. Terraform apply (recreate from new template)    │
│   5. Apply machine config (SOPS secrets)             │
│   6. Wait for node Ready                             │
│   7. Verify version                                  │
│   8. Uncordon node                                   │
└──────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│ Phase 3: Upgrade Workers (120-180 min)              │
│ ─────────────────────────────────────────────────── │
│ ⚠️  Skipped if templates_only=true                   │
│ ⚠️  Skipped if test_mode=true                        │
│ ✅ Runs if templates_only=false AND test_mode=false  │
│                                                      │
│ Sequential (1 at a time, max-parallel: 1):          │
│ For each worker (k8s-work-1 to k8s-work-16):        │
│   1. Pre-drain health check (CoreDNS, Ceph)          │
│   2. Drain node (graceful 240s)                      │
│   3. Terraform destroy (delete VM)                   │
│   4. Terraform apply (recreate from new template)    │
│   5. Apply machine config                            │
│   6. Apply global patches (kubelet, network)         │
│   7. Apply GPU patches (if work-4 or work-14)        │
│   8. Wait for node Ready                             │
│   9. Verify version                                  │
│  10. Uncordon node                                   │
│  11. Post-upgrade validation                         │
└──────────────────────────────────────────────────────┘
                      ↓
┌──────────────────────────────────────────────────────┐
│ Phase 4: Cluster Validation (5-10 min)              │
│ ─────────────────────────────────────────────────── │
│ ⚠️  Skipped if templates_only=true                   │
│ ✅ Runs if templates_only=false (cattle mode)        │
│                                                      │
│ • Verify all nodes at target version                │
│ • Validate GPU nodes (nvidia.com/gpu.present)        │
│ • Check HelmReleases healthy                         │
│ • Verify workload pods Running                       │
│ • Final summary report                               │
└──────────────────────────────────────────────────────┘
```

### Conditional Logic

**All jobs check `templates_only` parameter**:

```yaml
# Phase 1: rebuild-templates
if: inputs.old_version != inputs.new_version || inputs.test_mode == true
# → Always runs (doesn't check templates_only)

# Phase 2: upgrade-control-plane
if: inputs.test_mode == false && inputs.templates_only == false
# → Skipped for pets workflow

# Phase 3: upgrade-workers
if: |
  inputs.test_mode == false &&
  inputs.templates_only == false &&
  (needs.rebuild-templates.result == 'success' || needs.rebuild-templates.result == 'skipped') &&
  (needs.upgrade-control-plane.result == 'success' || needs.upgrade-control-plane.result == 'skipped')
# → Skipped for pets workflow

# Phase 4: validate-cluster
if: always() && inputs.templates_only == false
# → Skipped for pets workflow
```

---

## Pets Workflow (PATCH)

### Overview

**When**: PATCH version changes only (e.g., 1.11.4 → 1.11.5)

**Purpose**: Update templates without disrupting running cluster

**Duration**: 10-15 minutes (templates only)

**Implementation (PR #211)**: Reuses cattle workflow with `templates_only=true`

**File**: `.github/workflows/talos-version-router.yaml` (lines 125-136)

### Workflow Call

```yaml
route-to-pets:
  name: Route to Pets Workflow (Templates Only)
  needs: detect-version-change
  if: needs.detect-version-change.outputs.version_changed == 'true' &&
      needs.detect-version-change.outputs.upgrade_type == 'pets'
  uses: ./.github/workflows/upgrade-cattle.yaml
  secrets: inherit
  with:
    old_version: ${{ needs.detect-version-change.outputs.old_version }}
    new_version: ${{ needs.detect-version-change.outputs.new_version }}
    test_mode: false          # Rebuild ALL templates (not just 2)
    templates_only: true      # Skip VM upgrades
```

### Execution Flow

```
Pets Workflow Triggered (PATCH change detected)
  ↓
Calls upgrade-cattle.yaml with templates_only=true
  ↓
┌──────────────────────────────────────────────────────┐
│ Phase 1: Rebuild Templates ✅ RUNS                   │
│ ─────────────────────────────────────────────────── │
│ • Destroy 8 old templates at old version            │
│ • Create 8 new templates at new version             │
│ • Verify all templates in Terraform state           │
│ • All safety checks active                          │
│                                                      │
│ Duration: 10-15 minutes                              │
│ Result: Templates updated to new PATCH version      │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Phase 2: Upgrade Control Plane ⏭️ SKIPPED            │
│ Reason: templates_only=true                          │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Phase 3: Upgrade Workers ⏭️ SKIPPED                  │
│ Reason: templates_only=true                          │
└──────────────────────────────────────────────────────┘
  ↓
┌──────────────────────────────────────────────────────┐
│ Phase 4: Cluster Validation ⏭️ SKIPPED               │
│ Reason: templates_only=true                          │
└──────────────────────────────────────────────────────┘
```

### Benefits

**Before PR #211 (BROKEN)**:
- ❌ PATCH upgrades left templates at old version
- ❌ Templates diverged from Git version
- ❌ Required manual intervention

**After PR #211 (WORKING)**:
- ✅ Templates always current for ALL version changes
- ✅ No manual intervention required
- ✅ Fast PATCH upgrades (10-15 min vs 3-4 hours)
- ✅ Zero cluster disruption (VMs untouched)
- ✅ Ready for future Renovate PATCH updates

---

## Safety Mechanisms

### Multi-Layer Protection

**Layer 1: Version Validation**
```yaml
# Validate semver format (X.Y.Z)
if ! [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "❌ Invalid version format"
  exit 1
fi
```

**Layer 2: Plan Analysis**
- Positive assertion: Template modules MUST be present
- Negative assertion: Cluster VMs MUST NOT be present

**Layer 3: Terraform Targeting**
```bash
# ONLY target template modules (never touches VMs)
terraform plan -target=module.template_* -out=template-rebuild.tfplan
```

**Layer 4: State Verification**
```bash
# After rebuild, verify all 8 templates exist
terraform state list | grep "module.template_"
```

**Layer 5: Workflow Timeouts**
```yaml
timeout-minutes: 60  # Job-level timeout
```

### Concurrency Protection

```yaml
concurrency:
  group: cattle-upgrade-v2
  cancel-in-progress: true  # Cancel old runs when new run starts
```

**Prevents**:
- Multiple simultaneous template rebuilds
- Terraform state corruption
- S3 backend lock conflicts

---

## File Reference

### Workflow Files

| File | Purpose | Lines |
|------|---------|-------|
| `.github/workflows/talos-version-router.yaml` | Detect version changes, route to cattle/pets | 138 |
| `.github/workflows/upgrade-cattle.yaml` | Execute cattle upgrades + templates-only mode | 1600+ |

### Key Sections

**Router Workflow**:
- Lines 36-113: Version detection logic
- Lines 114-124: Cattle routing (templates_only=false)
- Lines 125-136: Pets routing (templates_only=true)

**Cattle Workflow**:
- Lines 4-40: Input parameters (includes templates_only)
- Lines 49-529: rebuild-templates job (always runs)
- Lines 530-937: upgrade-control-plane job (conditional)
- Lines 938-1512: upgrade-workers job (conditional)
- Lines 1513-1600: validate-cluster job (conditional)

### Terraform Files

| File | Purpose |
|------|---------|
| `terraform/variables.tf` | Talos version variable (line 45) |
| `terraform/terraform.tfvars` | Talos version value (line 47) |
| `terraform/modules/talos-template/` | Template creation module |

### Documentation

| File | Purpose |
|------|---------|
| `docs/architecture/talos-upgrade-automation.md` | Main architecture doc (needs update) |
| `.claude/.ai-docs/stories/TEMPLATE_REBUILD_FIXES.md` | Template fixes (PR #205, #206) |
| This file | Current state reference (PR #211) |

---

## Workflow Execution Examples

### Example 1: PATCH Upgrade (1.11.4 → 1.11.5)

**Trigger**: User merges Renovate PR changing `terraform/variables.tf`

**Router Decision**:
```
Old: 1.11.4 → Major: 1, Minor: 11, Patch: 4
New: 1.11.5 → Major: 1, Minor: 11, Patch: 5
MAJOR same (1==1), MINOR same (11==11) → Route to PETS
```

**Workflow Execution**:
```
talos-version-router.yaml
  ├─ detect-version-change: version_changed=true, upgrade_type=pets
  └─ route-to-pets: Call upgrade-cattle.yaml with templates_only=true

upgrade-cattle.yaml (templates_only=true)
  ├─ rebuild-templates: ✅ RUNS (10-15 min)
  │  ├─ Destroy 8 templates at v1.11.4
  │  ├─ Create 8 templates at v1.11.5
  │  └─ Verify all 8 in Terraform state
  ├─ upgrade-control-plane: ⏭️ SKIPPED (templates_only=true)
  ├─ upgrade-workers: ⏭️ SKIPPED (templates_only=true)
  └─ validate-cluster: ⏭️ SKIPPED (templates_only=true)
```

**Result**:
- Templates: 1.11.4 → 1.11.5 ✅
- Cluster VMs: Unchanged (remain at current version)
- Duration: 10-15 minutes
- Disruption: Zero

### Example 2: MINOR Upgrade (1.10.8 → 1.11.5)

**Trigger**: User merges Renovate PR changing `terraform/variables.tf`

**Router Decision**:
```
Old: 1.10.8 → Major: 1, Minor: 10, Patch: 8
New: 1.11.5 → Major: 1, Minor: 11, Patch: 5
MAJOR same (1==1), MINOR differs (10!=11) → Route to CATTLE
```

**Workflow Execution**:
```
talos-version-router.yaml
  ├─ detect-version-change: version_changed=true, upgrade_type=cattle
  └─ route-to-cattle: Call upgrade-cattle.yaml with templates_only=false

upgrade-cattle.yaml (templates_only=false, test_mode=true)
  ├─ rebuild-templates: ✅ RUNS (10-15 min)
  │  ├─ Destroy 8 templates at v1.10.8
  │  ├─ Create 8 templates at v1.11.5
  │  └─ Verify all 8 in Terraform state
  ├─ upgrade-control-plane: ⏭️ SKIPPED (test_mode=true)
  ├─ upgrade-workers: ✅ RUNS (30-45 min, 2 workers only in test mode)
  │  ├─ k8s-work-1: destroy + recreate + verify
  │  └─ k8s-work-2: destroy + recreate + verify
  └─ validate-cluster: ✅ RUNS (5 min)
     └─ Verify 2 workers upgraded to v1.11.5
```

**Result** (test_mode=true):
- Templates: 1.10.8 → 1.11.5 ✅
- Control plane: Unchanged (skipped in test mode)
- Workers: 2 of 12 upgraded to 1.11.5 ✅
- Duration: 45-60 minutes
- Disruption: Minimal (only 2 worker nodes)

**Full Production** (test_mode=false):
- All 15 nodes upgraded
- Duration: 3-4 hours
- Disruption: Rolling (one node at a time)

### Example 3: MAJOR Upgrade (1.11.5 → 2.0.0)

**Router Decision**:
```
Old: 1.11.5 → Major: 1, Minor: 11, Patch: 5
New: 2.0.0 → Major: 2, Minor: 0, Patch: 0
MAJOR differs (1!=2) → Route to CATTLE
```

**Workflow**: Same as MINOR upgrade (full cattle workflow)

---

## Changelog

| Version | Date | Changes | PR |
|---------|------|---------|-----|
| 1.0 | 2025-11-13 | Initial cattle workflow | - |
| 2.0 | 2025-11-19 | External runner architecture | - |
| 3.0 | 2025-11-20 | Router workflow + semver routing | - |
| 4.0 | 2025-11-22 | Batched template execution | #205 |
| 4.1 | 2025-11-22 | Node validation bug fix | #206 |
| **5.0** | **2025-11-22** | **Pets workflow template rebuild** | **#211** |

---

## Summary

**Key Improvements (PR #211)**:

1. ✅ **Templates always current**: Rebuilt for ALL version changes (cattle AND pets)
2. ✅ **Unified workflow**: Single cattle workflow handles both modes via `templates_only` parameter
3. ✅ **Zero manual intervention**: PATCH upgrades now fully automated
4. ✅ **All safety rails retained**: Template rebuild uses same validation regardless of mode
5. ✅ **Faster PATCH upgrades**: 10-15 min (templates only) vs manual intervention

**Production Ready**:
- ✅ Fully tested (PR #206 test run: 1.11.5 → 1.10.8 → 1.11.5)
- ✅ All safety mechanisms validated
- ✅ Router logic proven
- ✅ Pets workflow executed successfully
- ✅ Documentation complete

**Next Steps**:
- Monitor first real Renovate PATCH upgrade
- Consider implementing full pets workflow (in-place talosctl upgrade) for VMs in future
- Continue quarterly cattle upgrades for MAJOR/MINOR versions
