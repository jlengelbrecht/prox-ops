# GitHub Copilot Instructions for prox-ops

## Review Behavior (IMPORTANT)

**Tone**: Direct, technical, assertive. No fluff or unnecessary praise.

**Action**: When issues are found, **REQUEST CHANGES** - do not just leave comments. This ensures PRs cannot be merged until issues are addressed.

**Summary Format**: Every review MUST begin with a structured summary:

```markdown
## Summary
1. **Services/Namespaces Affected**: [list affected services and namespaces]
2. **Breaking Changes**: [any breaking changes or "None"]
3. **Security Implications**: [security concerns or "No issues found"]
4. **Flux Dependency Impacts**: [HelmRelease/Kustomization dependencies affected]
5. **Resource Changes**: [CPU/memory/storage changes]

**Key Items**: [Flag Talos constraints, SOPS encryption, ExternalSecrets prominently]
```

**Review Profile**: Be assertive. Flag all issues clearly. Prioritize security, reliability, and GitOps compliance. Do not approve PRs with unresolved critical or high-severity issues.

---

## Repository Context

This is a **GitOps-managed Kubernetes homelab** running on Talos Linux v1.11.3 with Flux CD. All infrastructure is declarative and version-controlled.

### Key Technologies
- **Platform**: Talos Linux (immutable Kubernetes OS, kernel 6.12.52-talos)
- **GitOps**: Flux CD v2.x (HelmRelease, Kustomization)
- **Container Runtime**: containerd with eBPF support
- **Infrastructure**: Proxmox VE 8.x (managed via Terraform)
- **Networking**: Cilium CNI with VLAN support
- **Storage**: Rook-Ceph for persistent volumes
- **Security**: Tetragon eBPF-based runtime monitoring
- **Observability**: Prometheus, Loki, Grafana, Promtail

### Cluster Architecture
- **15-node cluster**: 3 control plane, 12 workers
- **Workload segregation**: DMZ (media), IoT, general apps
- **GPU nodes**: RTX A2000, RTX A5000 for transcoding
- **Network isolation**: VLANs, CiliumNetworkPolicy

---

## Pull Request Review Guidelines

When reviewing PRs, focus on these critical aspects:

### 1. Security (HIGHEST PRIORITY)

**✅ MUST CHECK:**
- No plaintext secrets, passwords, API tokens, or credentials
- Secrets encrypted with SOPS (files ending in `.sops.yaml`)
- No exposure of internal IPs, private infrastructure details
- CiliumNetworkPolicy uses least-privilege networking
- Pod Security Standards appropriate for workload
- Service accounts follow principle of least privilege
- No `.claude/` or `.ai-docs/` files committed (should be gitignored)

**❌ NEVER SUGGEST:**
- Embedding credentials in YAML files
- Relaxing security policies without justification
- Exposing services to internet without explicit DMZ approval

### 2. Kubernetes and Container Context

**Remember this is containerized infrastructure:**
- Containers run isolated workloads (Plex, Home Assistant, etc.)
- Traditional filesystem paths like `/home/user/.ssh/` don't exist in containers
- Focus on container-relevant paths: `/etc/shadow`, `/etc/passwd`, `/var/run/secrets/`, `/etc/kubernetes/`
- Service account tokens are at `/var/run/secrets/kubernetes.io/serviceaccount/`

**✅ VALID concerns:**
- Application-specific config paths (e.g., `/config/` for Home Assistant)
- Kubernetes secret mounts
- Container privilege escalation
- Network policies and pod-to-pod communication

**❌ INVALID concerns:**
- Traditional Linux user home directories in containers
- SSH keys in `/home/*/.ssh/` (containers don't have SSH servers)
- System services like systemd (Talos is immutable)

### 3. GitOps Workflow Compliance

**✅ VERIFY:**
- Changes are declarative YAML in `kubernetes/apps/` directory
- HelmRelease resources reference existing HelmRepository
- Kustomization dependencies use `dependsOn` when needed
- No imperative `kubectl apply` commands in documentation
- Flux reconciliation tested after merge

**❌ ANTI-PATTERNS:**
- Direct `kubectl apply` (violates GitOps)
- Manual cluster modifications
- Missing `dependsOn` for CRD/resource ordering

### 4. Talos Linux Compatibility

**✅ TALOS-AWARE:**
- Talos is **immutable** - no package managers, no shell access
- eBPF requires BTF support (available at `/sys/kernel/btf/vmlinux`)
- Use `modern_ebpf` driver for security tools (not kernel modules)
- Talos has no traditional init system (systemd, etc.)
- Host filesystem is read-only

**❌ INCOMPATIBLE:**
- Suggestions requiring host filesystem writes
- Kernel module compilation (use eBPF instead)
- DaemonSets requiring privileged mode without justification
- Assumptions about shell access to nodes

### 5. YAML and Flux Syntax

**✅ CHECK:**
- Valid Kubernetes API versions for installed CRDs
- HelmRelease `values:` match chart schema
- TracingPolicy/TracingPolicyNamespaced syntax correct for Tetragon v1.2.0
- Kustomization `resources:` paths exist
- No typos in field names (common: `matchBinaries` not `matchArgs` for binaries)

**❌ WATCH FOR:**
- Outdated API versions (e.g., `v1beta1` when `v2` required)
- Invalid CRD fields (check operator documentation)
- Incorrect operator syntax (e.g., `In` vs `Prefix` in TracingPolicy)
- Missing required fields in HelmRelease

### 6. Observability Integration

**✅ VERIFY:**
- `ServiceMonitor` resources for Prometheus scraping
- Logs exported to stdout (not files) for Promtail collection
- JSON structured logging enabled where available
- Grafana dashboards reference correct data sources

**❌ DON'T SUGGEST:**
- File-based logging with rotation (use stdout → Promtail → Loki)
- Direct writes to Loki (use log collectors)
- Hardcoded Prometheus endpoints (use ServiceMonitor)

### 7. Resource Management

**✅ CHECK:**
- CPU/memory limits set appropriately
- GPU resources requested correctly (`nvidia.com/gpu: 1`)
- Node affinity/taints for specialized hardware
- Storage claims reference existing StorageClass (Rook-Ceph)

**❌ CONCERNS:**
- Excessive resource requests (cluster has finite capacity)
- Missing limits on high-cardinality workloads
- GPU requests without node affinity

### 8. Documentation Quality

**✅ GOOD PRACTICES:**
- Clear summary of what changed and why
- Testing plan with specific validation steps
- Risk assessment for production impact
- Related work items referenced (STORY-XXX, EPIC-XXX)

**❌ AVOID:**
- Excessive emojis or AI-generated boilerplate
- "Amazing", "Incredible" superlatives
- Missing test plans for complex changes
- AI attribution (this looks human-written)

---

## Common Mistakes to Avoid

### 1. Container vs Host Confusion
**Wrong**: "Monitor `/home/user/.ssh/` for SSH key access"
**Right**: "Monitor `/var/run/secrets/` for service account token access"

### 2. Talos Assumptions
**Wrong**: "Install package via apt/yum"
**Right**: "Use eBPF for kernel-level monitoring (no kernel modules)"

### 3. GitOps Violations
**Wrong**: "Run `kubectl apply -f deployment.yaml`"
**Right**: "Commit YAML to Git, let Flux reconcile"

### 4. Security Oversights
**Wrong**: Ignoring plaintext secrets in ConfigMap
**Right**: Flag any credentials not using SOPS or ExternalSecret

### 5. Incomplete Testing Plans
**Wrong**: "Deploy and verify it works"
**Right**: "1. Check pod status, 2. Test functionality, 3. Review logs, 4. Monitor metrics"

---

## Review Priorities

Rate issues by severity:

### CRITICAL (Block merge)
- Secrets/credentials exposed
- Security policies weakened without justification
- YAML syntax errors causing deployment failures
- GitOps workflow violations

### HIGH (Request changes)
- Missing resource limits
- Incomplete testing plan
- Talos/eBPF compatibility issues
- Missing dependencies in Kustomization

### MEDIUM (Suggest improvements)
- Suboptimal logging configuration
- Documentation clarity
- Resource optimization opportunities

### LOW (Informational)
- Code style preferences
- Documentation typos
- Optional best practices

---

## Example Good Review

```markdown
## Summary
1. **Services/Namespaces Affected**: media/plex, media/tautulli
2. **Breaking Changes**: None
3. **Security Implications**: SOPS encryption verified for DB credentials ✅
4. **Flux Dependency Impacts**: HelmRelease depends on rook-ceph StorageClass
5. **Resource Changes**: +500m CPU request, +1Gi memory limit

**Key Items**:
- ✅ ExternalSecret references valid 1Password item
- ✅ Talos compatible (no host filesystem writes)
- ✅ SOPS encrypted secrets only

---

## Detailed Review

**Security**: ✅ SOPS encryption verified for DB credentials
**Kubernetes**: ✅ PodSecurityStandard set to 'restricted'
**GitOps**: ✅ HelmRelease properly references cilium-charts repo
**Talos**: ✅ Uses modern_ebpf driver (compatible with Talos 1.11.3)

### Suggestions (Medium Priority)
- Consider adding resource limits to prevent resource exhaustion
- Testing plan could include Prometheus metrics verification
```

---

## Repository-Specific Knowledge

### Secrets Management (FLAG PROMINENTLY)

**This repo uses a dual-layer secrets approach:**

1. **ExternalSecrets Operator** (v1 API - `external-secrets.io/v1`)
   - Syncs secrets from 1Password to Kubernetes
   - All ExternalSecret resources MUST use `apiVersion: external-secrets.io/v1` (NOT v1beta1)
   - SecretStore references 1Password Connect in `external-secrets` namespace

2. **SOPS Encryption**
   - For secrets that must be in Git (bootstrap, etc.)
   - Files end in `.sops.yaml`
   - Encrypted with age key

**Review Checklist for Secrets:**
- [ ] No plaintext secrets in HelmRelease values
- [ ] ExternalSecret uses v1 API (not v1beta1)
- [ ] SOPS files have `ENC[AES256_GCM,...]` encrypted values
- [ ] Stakater Reloader annotation present for secret rotation
- [ ] `existingSecret` pattern used (not inline credentials)

### Security Policies
- **Media namespace (DMZ)**: Plex runs with GPU access, enforced shell blocking
- **IoT namespace**: Home Assistant monitored for shell spawns, network access
- **Cluster-wide**: Privilege escalation detection via Tetragon

### Storage
- **Rook-Ceph**: Used for PVCs (config, databases)
- **NFS**: Used for media libraries (read-only mounts)
- **StorageClass**: `ceph-block` for RWO, `ceph-filesystem` for RWX

### Networking
- **DMZ VLAN 81**: External LoadBalancer for Plex (port 32400)
- **Internal**: ClusterIP for all other services
- **CiliumNetworkPolicy**: Deny-by-default, explicit allow rules

### GPU Scheduling
- **RTX A2000** (k8s-work-4): Reserved for Plex transcoding
- **RTX A5000** (k8s-work-14): Available for other GPU workloads
- Use `runtimeClassName: nvidia` for GPU pods

---

## When in Doubt

If you're unsure about Talos compatibility, eBPF support, or GitOps workflows:
1. Flag the concern in review
2. Ask for clarification on Talos-specific behavior
3. Verify against Flux/Tetragon documentation
4. Prioritize security and stability over features

Remember: This is a **production homelab**. Changes affect real services (media streaming, home automation). Err on the side of caution.
