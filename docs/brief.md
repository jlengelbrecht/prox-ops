# Project Brief: Prox-Ops Homelab Infrastructure with BMAD Workflow Transformation

**Document Version:** 1.0
**Date Created:** 2025-11-15
**Author:** Business Analyst Mary (BMAD Agent)
**Owner:** jlengelbrecht
**Status:** Draft - Pending Approval

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Problem Statement](#problem-statement)
3. [Proposed Solution](#proposed-solution)
4. [Target Users](#target-users)
5. [Goals & Success Metrics](#goals--success-metrics)
6. [MVP Scope](#mvp-scope)
7. [Post-MVP Vision](#post-mvp-vision)
8. [Technical Considerations](#technical-considerations)
9. [Constraints & Assumptions](#constraints--assumptions)
10. [Risks & Open Questions](#risks--open-questions)
11. [Appendices](#appendices)
12. [Next Steps](#next-steps)

---

## Executive Summary

The **Prox-Ops Homelab** is a production-grade Kubernetes infrastructure deployed on Proxmox, featuring GitOps-driven application management, automated zero-downtime cluster upgrades, and sophisticated multi-VLAN networking. The cluster hosts self-managed applications using Flux for continuous reconciliation from a GitHub repository, with Terraform managing infrastructure-as-code for the 15-node K8s cluster.

The platform implements an innovative "cattle" upgrade strategy where Talos OS updates trigger automated node-by-node replacement: Renovate detects new Talos releases, self-hosted GitHub Actions runners create new cloud-init templates, and Terraform orchestrates rolling replacements while maintaining workload availability. This achieves zero-downtime cluster upgrades without manual intervention.

**Current Challenge:** While the infrastructure is robust, the development workflow suffers from "vibe coding" - 377 documentation files created inconsistently, ad-hoc agent delegation, and security gate violations (including a critical Proxmox API key leak). This limits autonomous operation and requires constant supervision.

**Transformation Initiative:** Adopting the BMAD (Brownfield Methodology for Agile Development) framework to formalize workflows with mandatory security gates, pre-approved command whitelists, structured agent delegation, and automated documentation. This preserves 377 files of historical context while establishing consistent patterns for future work.

**Target Outcome:** A homelab infrastructure where Claude Code agents operate autonomously with strategic oversight - users intervene only at PR merge points while agents handle planning, execution, security validation, and documentation automatically.

---

## Problem Statement

### Current State: Infrastructure Excellence, Workflow Chaos

The prox-ops homelab has achieved technical sophistication that rivals enterprise infrastructure: zero-downtime automated upgrades, GitOps-driven deployments, multi-VLAN network isolation, and self-hosted CI/CD. However, the **development workflow that builds and maintains this infrastructure is fundamentally broken**.

### Pain Point 1: "Vibe Coding" - Unstructured Documentation

**Current reality:** 377 documentation files exist in `.claude/.ai-docs/` across 34 subdirectories, created organically without clear organization or purpose. The repository owner doesn't know what files exist, why they were created, or how they're organized.

**Impact:**
- Context loss across conversations - agents can't find relevant documentation
- Duplicate efforts - same patterns documented multiple times (network/ vs networking/ folders)
- No reusable patterns - each deployment is custom instead of following templates
- Root-level clutter - 30+ loose status reports and summaries with unclear purpose

**Quantified cost:** Estimated 2-3 hours per week spent searching for documentation, recreating patterns, and organizing ad-hoc files.

### Pain Point 2: "Hit or Miss" Agent Delegation

**Current reality:** Specialized agents exist (homelab-infra-architect, security-guardian, cachyos-nix-specialist) but are used inconsistently. Claude sometimes delegates correctly, sometimes handles tasks directly, sometimes forgets agents exist.

**Impact:**
- Inconsistent quality - work quality varies based on whether correct agent was used
- Lost expertise - specialized agent knowledge isn't consistently applied
- Manual reminders required - user must remind Claude which agents exist
- No deterministic routing - agent selection based on Claude "remembering" not workflow structure

**Example:** Security reviews should be mandatory before GitHub pushes, but agents skip them ~40% of the time based on recent history.

### Pain Point 3: Security Gate Violations (Critical)

**Current reality:** Security review is documented as "mandatory" in CLAUDE.md but agents violate this requirement regularly. Most critical incident: Proxmox API token leaked to public GitHub when homelab-infra-architect agent removed `.claude/` from `.gitignore` and pushed 157 files without security review.

**Impact:**
- **CRITICAL SECURITY RISK:** Public repository means leaked secrets are permanently accessible
- Trust erosion - can't trust agents to follow documented rules
- Post-incident cleanup - 30 commits rewritten, API token revoked/regenerated, 3+ hours recovery time
- Cannot operate autonomously - user must monitor every push

**Root cause:** Documentation-based rules have no structural enforcement. Agents can skip mandatory gates with no mechanism to prevent violations.

### Pain Point 4: Manual Babysitting - "1000 Yes/No Prompts"

**Current reality:** Every kubectl/flux/terraform command requires explicit user approval, even for read-only operations like `kubectl get pods` or `flux logs`. User estimates receiving ~20-30 approval prompts per task.

**Impact:**
- Cannot "walk away for coffee" - constant supervision required
- Interrupts deep work - every 2-3 minutes during deployments
- Slows velocity - 15-20 minute tasks take 45+ minutes due to approval overhead
- Cognitive load - context switching between coding and approving commands

**User quote:** "I want to just say 'deploy this app' and walk away, come back in 30 minutes and it's done with a PR ready for me to review."

### Pain Point 5: Zero Visibility Into Agent Actions

**Current reality:** User doesn't know what agents are doing, what documents they're creating, or why decisions are being made. Work happens in a "black box" with only final outputs visible.

**Impact:**
- No trust - can't delegate fully without understanding agent logic
- Surprise documentation - 377 files accumulated without awareness
- Can't learn patterns - no transparency into decision-making process
- Hard to debug - when something goes wrong, can't trace agent reasoning

### Why Existing Solutions Fall Short

**Documentation alone doesn't work:** CLAUDE.md contains comprehensive rules (security gates, agent delegation, GitOps workflow) but lacks structural enforcement. Agents violate rules because nothing prevents violations at execution time.

**Manual oversight isn't scalable:** Current approach requires constant supervision, eliminating benefits of AI assistance. The goal is autonomous operation, but trust gap prevents delegation.

**Ad-hoc improvements are insufficient:** Individual fixes (better prompts, more reminders) don't address root cause: lack of structured, enforced workflows.

### Urgency: Why Solve This Now

1. **Security incident** (November 2025): Proxmox API leak demonstrated critical failure mode. Next incident could compromise cluster credentials, encryption keys, or application secrets.

2. **Opportunity cost**: Spending 10-15 hours/week on manual oversight and cleanup instead of building new capabilities.

3. **Momentum exists**: Recent brainstorming session generated 35+ actionable ideas with strong user validation. Strike while motivation is high.

4. **Foundation for growth**: Can't expand homelab capabilities (new apps, advanced networking, storage) while maintaining current operational overhead.

---

## Proposed Solution

### Solution Overview: BMAD Framework with Structural Enforcement

Adopt the **BMAD (Brownfield Methodology for Agile Development)** framework to transform the prox-ops workflow from ad-hoc "vibe coding" to structured, autonomous operation with mandatory safety gates. Unlike documentation-based approaches, BMAD uses **executable task workflows** that enforce rules at runtime - agents physically cannot skip security reviews or violate policies because the workflow structure prevents it.

The solution is **additive, not destructive**: preserve all 377 existing files in `.claude/.ai-docs/` as historical reference while establishing new BMAD structure (`.bmad-core/`, `docs/`, `.ai/`) for future work. No file migration required, zero risk to existing context.

### Core Solution Components

**1. Mandatory Security Gates (Un-Skippable)**

Replace documentation-based "should run security review" with **task-enforced workflow** where security-guardian agent delegation is a required step that cannot be bypassed:

```
Task: Create Pull Request
├─ Step 1: Stage changes (git add)
├─ Step 2: MANDATORY security-guardian review (elicit=true)
│   └─ If fails → Fix issues and retry
│   └─ If passes → Proceed to Step 3
├─ Step 3: Commit and push (only after approval)
├─ Step 4: Create PR (agent stops, user reviews)
└─ Step 5: Post-merge validation
```

**Key differentiator:** Agent cannot proceed to Step 3 without completing Step 2. Security review isn't optional - it's a workflow dependency.

**2. Pre-Approved Command Whitelist (Autonomous Operation)**

Eliminate "1000 yes/no prompts" by defining clear trust boundaries in CLAUDE.md:

**ALWAYS APPROVED (No prompt needed):**
- Read operations: `kubectl get/describe/logs`, `flux get/logs`, `git status/diff/log`
- Planning: `terraform plan` (no apply)
- Network tests: `curl`, `ping`, `nc -zv`
- File operations: Read tool for any file

**ALWAYS ASK (Require approval):**
- Destructive: `kubectl delete`, `terraform destroy`, `git rm`
- Write operations: `kubectl apply` (unless via GitOps), `terraform apply`
- Merge operations: `gh pr merge` (user-only)

**Key differentiator:** Trust model based on impact categorization, not blanket approval. Agents operate autonomously for safe operations, require oversight for risky ones.

**3. Structured Agent Delegation (Deterministic Routing)**

Create BMAD task definitions that specify which agent handles which work type:

- `deploy-new-app.md` → Routes to: homelab-infra-architect
- `plan-feature.md` → Routes to: PM agent → Scrum Master agent
- `troubleshoot-issue.md` → Routes to: homelab-infra-architect
- `install-dependency.md` → Routes to: cachyos-nix-specialist
- `create-pr-with-security.md` → Routes to: security-guardian (mandatory)

**Key differentiator:** Agent selection based on workflow structure, not Claude "remembering" which agents exist. Delegation becomes deterministic and consistent.

**4. Automated Documentation and Visibility**

Agents automatically document outcomes in structured locations:

- **Todo tracking:** Real-time progress visibility (what agent is working on)
- **Debug logs:** `.ai/debug-log.md` captures agent decisions, document references, command executions
- **App documentation:** Automatic generation of `OVERVIEW.md`, `ACCESS_POLICY.md`, `NETWORK_POLICY.md` after deployments
- **Epic/story tracking:** Work items linked to actual files created

**Key differentiator:** No more black-box operation. User sees what's happening and why, with full traceability.

**5. Additive Adoption Strategy (Zero Risk)**

Preserve existing `.claude/.ai-docs/` structure entirely while adding BMAD framework alongside:

```
Current (Preserved)              New (Added)
────────────────────────────────────────────────
.claude/.ai-docs/        →      (Kept as-is, 377 files intact)
.claude/agents/          →      (Enhanced with BMAD tasks)
(none)                   →      .bmad-core/ (tasks, templates, data)
(none)                   →      docs/ (managed deliverables: brief, PRD, architecture)
(none)                   →      .ai/ (debug logs, temp files)
```

**Key differentiator:** No migration pain, no file moves, no risk of breaking existing references. Incremental adoption with full rollback capability.

### Why This Solution Will Succeed Where Others Haven't

**1. Addresses Root Cause, Not Symptoms**

Previous attempts improved documentation (CLAUDE.md rules) but couldn't prevent violations. BMAD enforces rules **structurally** - security gates are workflow dependencies, not suggestions in documentation.

**2. Builds on What Works**

Doesn't replace existing infrastructure or agents - enhances them with structured workflows. Existing homelab-infra-architect agent gains BMAD task library, security-guardian becomes integrated into PR workflow, Flux/GitOps remain unchanged.

**3. User-Validated Approach**

Brainstorming session (2025-11-14) generated 35+ ideas with strong user alignment. Top 3 priorities (mandatory security gates, pre-approved commands, structured delegation) directly informed this solution design.

**4. Incremental, Low-Risk Adoption**

Additive approach means:
- Can test BMAD workflows alongside existing process
- Zero impact to current operations
- Full rollback capability (delete new directories)
- Learn and adjust before committing fully

**5. Proven Framework**

BMAD methodology has established patterns for brownfield project adoption, agent coordination, and workflow enforcement. Not inventing custom solution - applying proven framework to homelab context.

### High-Level Vision

**Near-term (4-8 weeks):** User says "deploy app X" → Claude/agents handle entire workflow (files creation, security review, PR creation, post-merge validation, documentation) → User intervenes only at PR merge point → 30 minutes later, app is deployed and documented.

**Mid-term (2-4 months):** Reusable pattern library established (3-NIC network checklist, ACCESS_POLICY template, deployment runbooks) → New app deployments follow consistent patterns → Historical .ai-docs/ knowledge formalized into BMAD templates.

**Long-term (6+ months):** Agents operate fully autonomously for routine operations (dependency updates, configuration changes, troubleshooting) → User provides strategic direction only → Homelab scales capabilities without scaling operational overhead.

**Ultimate Goal:** Transform homelab from "sophisticated infrastructure requiring constant babysitting" to "autonomous platform with strategic oversight" - preserving all technical capabilities while eliminating workflow friction.

---

## Target Users

### Primary User Segment: Solo Homelab Operator with Production-Grade Ambitions

**Profile:**
- **Role:** Infrastructure enthusiast, self-hosting advocate, privacy-focused technologist
- **Technical level:** Advanced - understands Kubernetes, GitOps, Infrastructure-as-Code, networking concepts (VLANs, network policies)
- **Time availability:** Hobby/side project - 10-15 hours/week available for homelab work (evenings, weekends)
- **Infrastructure scale:** 15-node K8s cluster on Proxmox, 377+ documentation files, multiple self-hosted applications
- **Work style:** Prefers strategic direction over tactical execution - wants to say "what" not "how"

**Current Behaviors and Workflows:**

*Strategic Planning:*
- Envisions new capabilities (GPU transcoding, DMZ isolation, advanced storage)
- Documents requirements in epic/story format via PM/Scrum agents
- Prioritizes work based on impact and urgency

*Tactical Execution:*
- Relies on Claude Code agents for implementation (homelab-infra-architect, security-guardian)
- Reviews/approves every command due to trust gap (~30 approvals per task)
- Manually monitors agent behavior to catch rule violations
- Intervenes when agents delegate incorrectly or skip security reviews

*Documentation and Maintenance:*
- 377 files exist but organization/awareness is low ("honestly don't know what's there")
- Searches through documentation reactively when needed
- No consistent pattern capture or template reuse

**Specific Needs and Pain Points:**

1. **Autonomous operation with safety:** "I want to walk away for coffee and come back to a PR ready for review" - needs agents to operate independently but within safety guardrails

2. **Security confidence:** Post-Proxmox API leak, cannot trust agents to follow security rules consistently - needs structural enforcement not documentation promises

3. **Visibility without micromanagement:** Wants to know what agents are doing and why, but doesn't want to approve every `kubectl get pods` command

4. **Preserve historical context:** 377 files represent significant investment - any solution must preserve this work, not replace it

5. **Consistent quality:** Agent delegation is "hit or miss" - needs deterministic routing to ensure specialized agents are used correctly

6. **Time efficiency:** Currently spending 10-15 hours/week on operational overhead (approval prompts, fixing violations, organizing docs) - wants to redirect this toward building new capabilities

**Goals They're Trying to Achieve:**

*Primary Goal:* **Scale homelab capabilities without scaling operational overhead**
- Add new applications, networking features, storage solutions without proportionally increasing time investment
- Maintain production-grade reliability while operating as solo operator

*Secondary Goals:*
- **Trust delegation:** Confidently delegate work to agents without constant supervision
- **Security assurance:** Zero tolerance for credential leaks or policy violations - needs 100% confidence
- **Knowledge capture:** Document patterns, architectures, and decisions in reusable format
- **Learning and experimentation:** Free up time for trying new technologies, not maintaining existing systems

---

### Secondary User Segment: AI Agents (System Actors)

**Profile:**
- **Agents:** homelab-infra-architect, security-guardian, cachyos-nix-specialist, PM agent, Scrum Master agent
- **Role:** Autonomous executors of homelab operations within structured workflows
- **Current state:** Inconsistent usage, unclear routing, documented rules but no enforcement mechanism
- **Desired state:** Predictable, autonomous operation with mandatory safety checkpoints

**Current Behaviors and Workflows:**

*Planning Phase:*
- PM agent creates epics defining strategic features
- Scrum Master agent breaks epics into stories and work items
- Assign work to appropriate specialist agents (when remembered)

*Execution Phase:*
- homelab-infra-architect: Infrastructure deployment, K8s configs, Terraform operations
- security-guardian: Pre-push security validation (when invoked)
- cachyos-nix-specialist: System dependency installation via Nix flakes

*Current Issues:*
- Delegation is inconsistent - sometimes Claude uses agents, sometimes doesn't
- Security-guardian skipped ~40% of the time despite mandatory requirement
- No deterministic routing - agent selection based on prompt interpretation

**Specific Needs and Pain Points:**

1. **Clear task routing:** Agents need unambiguous criteria for "when am I responsible for this work?"

2. **Structured workflows:** Need step-by-step task definitions with mandatory checkpoints (can't skip security review if it's a workflow dependency)

3. **Context availability:** Need access to relevant documentation (app ACCESS_POLICY, network architecture) to make informed decisions

4. **Feedback loops:** Need to know when work is blocked or requires user input (strategic stop points)

5. **Documentation templates:** Need structured formats for outputs (not ad-hoc markdown creation)

**Goals They're Trying to Achieve:**

*Primary Goal:* **Operate autonomously within well-defined guardrails**
- Execute entire workflows (planning → implementation → security validation → deployment) without human intervention except at strategic decision points

*Secondary Goals:*
- **Consistency:** Apply specialized expertise consistently, not "hit or miss"
- **Safety:** Prevent security violations structurally (can't push without security review)
- **Transparency:** Document decisions, actions, and rationale automatically
- **Collaboration:** Coordinate across multiple agents for complex tasks (infra + security + documentation)

---

## Goals & Success Metrics

### Business Objectives

**Objective 1: Eliminate Security Violations**
- **Goal:** Achieve zero security incidents (credential leaks, .gitignore violations, plaintext secrets in Git) through structural enforcement
- **Metric:** 100% security-guardian review compliance before GitHub pushes (vs. current ~60% compliance)
- **Timeframe:** 100% compliance within 2 weeks of BMAD task deployment
- **Measurement:** Track all GitHub pushes, verify security-guardian agent invocation in git commit history
- **Success criteria:** 8+ consecutive weeks with zero security violations

**Objective 2: Reduce Operational Overhead by 60%**
- **Goal:** Cut manual supervision time from 10-15 hours/week to 4-6 hours/week through pre-approved command automation
- **Metric:** Approval prompts reduced from ~25 per task to <5 per task (80% reduction)
- **Timeframe:** 4 weeks after pre-approved whitelist implementation
- **Measurement:** Track approval prompt counts during standard workflows (app deployment, troubleshooting, dependency updates)
- **Success criteria:** User can "walk away for coffee" during routine deployments without constant supervision

**Objective 3: Achieve Consistent Agent Delegation**
- **Goal:** 100% deterministic agent routing based on task type, eliminating "hit or miss" behavior
- **Metric:** Correct specialist agent used for every task (homelab-infra-architect for infrastructure, security-guardian for reviews, cachyos-nix-specialist for dependencies)
- **Timeframe:** Immediate upon BMAD task workflow implementation
- **Measurement:** Audit task execution logs to verify agent routing matches task definitions
- **Success criteria:** Zero instances of Claude bypassing agent delegation for 4+ consecutive weeks

**Objective 4: Establish Documentation Consistency**
- **Goal:** All new work follows structured BMAD templates, eliminating ad-hoc "vibe coding"
- **Metric:** 100% of new deployments generate required documentation (OVERVIEW.md, ACCESS_POLICY.md, network configs) in correct locations
- **Timeframe:** Within 8 weeks of template library creation
- **Measurement:** Track files created in `docs/` and `.bmad-core/` vs. ad-hoc locations
- **Success criteria:** Zero loose files in repository root, all documentation follows templates

**Objective 5: Enable Autonomous Multi-Step Workflows**
- **Goal:** Agent completes entire workflow (planning → implementation → security → PR creation → post-merge validation) with user intervention only at strategic decision points (PR merge)
- **Metric:** User interactions reduced to 1-2 strategic approvals per workflow (PR merge, architectural decisions) vs. current 25-30 tactical approvals
- **Timeframe:** 6-8 weeks after core BMAD tasks deployed
- **Measurement:** Count user interactions during end-to-end app deployment workflow
- **Success criteria:** Successfully deploy 3 consecutive applications with <3 user interactions each

---

### User Success Metrics

**Time Efficiency:**
- **Deployment velocity:** New app deployment time reduced from 45+ minutes (with constant supervision) to 30 minutes autonomous execution + 5 minutes PR review
- **Documentation discovery:** Time to find relevant documentation reduced from 10-15 minutes searching to <2 minutes (structured locations, debug logs show references)
- **Troubleshooting speed:** Issue resolution 30% faster due to agents checking app documentation automatically before making changes

**Trust and Confidence:**
- **Delegation comfort:** User can confidently initiate task and disengage, returning only for strategic decisions
- **Security assurance:** 100% confidence that secrets won't leak (structurally impossible to push without security review)
- **Quality consistency:** Predictable work quality regardless of which conversation or context window

**Knowledge and Visibility:**
- **Workflow transparency:** Real-time visibility into agent actions via todo tracking and debug logs
- **Decision traceability:** Ability to trace why agents made specific choices (documented rationale in debug logs)
- **Pattern reuse:** New deployments leverage existing templates/checklists, reducing "figure it out each time" overhead

**Infrastructure Growth:**
- **Capability expansion:** Add 2-3 new applications per month (vs. current ~1 per month) due to reduced overhead
- **Experimentation time:** 40% of time freed up redirected toward learning new technologies and testing advanced features
- **Maintenance burden:** Routine operations (updates, config changes) require minimal supervision

---

### Key Performance Indicators (KPIs)

**KPI 1: Security Gate Compliance Rate**
- **Definition:** Percentage of GitHub pushes preceded by security-guardian agent review
- **Current baseline:** ~60% (40% skip rate)
- **Target:** 100% (zero exceptions)
- **Measurement method:** Parse git commit messages and task execution logs for security-guardian invocation
- **Success threshold:** 100% compliance for 8 consecutive weeks

**KPI 2: Approval Prompt Density**
- **Definition:** Number of user approval prompts per standard task (deployment, troubleshooting, update)
- **Current baseline:** 25-30 prompts per deployment task
- **Target:** <5 prompts per deployment task (80% reduction)
- **Measurement method:** Count approval requests during test deployments
- **Success threshold:** <5 prompts average across 5 consecutive deployments

**KPI 3: Agent Delegation Accuracy**
- **Definition:** Percentage of tasks where correct specialist agent was used
- **Current baseline:** ~60% (estimated from "hit or miss" characterization)
- **Target:** 100% (deterministic routing)
- **Measurement method:** Audit task logs to verify agent matches task type
- **Success threshold:** 100% accuracy over 20 consecutive tasks

**KPI 4: Documentation Template Compliance**
- **Definition:** Percentage of new app deployments that generate all required documentation files in correct locations
- **Current baseline:** ~20% (most deployments create ad-hoc docs)
- **Target:** 100% (all deployments follow templates)
- **Measurement method:** Check for presence of OVERVIEW.md, ACCESS_POLICY.md, network configs after each deployment
- **Success threshold:** 10 consecutive deployments with 100% compliance

**KPI 5: Workflow Autonomy Ratio**
- **Definition:** Ratio of strategic approvals (PR merge, architectural decisions) to tactical approvals (individual commands, routine operations)
- **Current baseline:** 1:25 (1 strategic decision requires 25 tactical approvals)
- **Target:** 1:2 (minimal tactical oversight)
- **Measurement method:** Categorize each user interaction as strategic vs. tactical during workflows
- **Success threshold:** Achieve 1:2 ratio for 5 consecutive multi-step workflows

**KPI 6: Time to Deploy (TTD)**
- **Definition:** Total elapsed time from "deploy app X" instruction to merged PR with working deployment
- **Current baseline:** 45-60 minutes (with constant supervision)
- **Target:** 35 minutes (30 min autonomous execution + 5 min user PR review)
- **Measurement method:** Timestamp from initial request to PR merge across test deployments
- **Success threshold:** 3 consecutive deployments under 40 minutes total time

**KPI 7: Documentation Discoverability**
- **Definition:** Time required to locate relevant documentation for a given app or pattern
- **Current baseline:** 10-15 minutes (searching through 377 files)
- **Target:** <2 minutes (structured locations + debug log references)
- **Measurement method:** Simulate "find network policy for app X" searches and time to locate
- **Success threshold:** Average <2 minutes across 10 different documentation searches

---

## MVP Scope

### Core Features (Must Have)

**Feature 1: Mandatory Security Gate Task Workflow** ⭐ TOP PRIORITY

**Description:** Create un-skippable BMAD task (`create-pr-with-security.md`) that enforces security-guardian review before any GitHub push.

**Rationale:** Prevents repeat of Proxmox API leak incident. Security violations are the highest-risk failure mode - must solve first before enabling autonomous operation.

**Implementation:**
- BMAD task file in `.bmad-core/tasks/create-pr-with-security.md`
- Workflow steps:
  1. Stage changes (`git add`)
  2. **MANDATORY** security-guardian delegation (`elicit: true` - cannot skip)
  3. Security checklist validation:
     - No plaintext secrets (must use SOPS/ExternalSecret)
     - .gitignore integrity (NEVER expose `.claude/`, `.ai/`, secrets)
     - No credentials, API tokens, internal IPs
     - Valid YAML syntax
     - Network architecture compliance (3-NIC pattern)
  4. After approval → commit and push
  5. Create PR (agent stops, user reviews)
  6. Post-merge validation

- Update CLAUDE.md to reference this task for ALL PR workflows
- Agent prompt enforcement: "All pushes MUST use create-pr-with-security task"

**Acceptance criteria:**
- Task file exists and executes end-to-end
- Security-guardian agent invoked 100% of time (structural enforcement)
- Successfully blocks push when security issues detected
- User can review/merge PR after security approval
- Post-merge validation confirms deployment success

---

**Feature 2: Pre-Approved Command Whitelist** ⭐ HIGH PRIORITY

**Description:** Define clear trust boundaries in CLAUDE.md eliminating approval prompts for safe operations while maintaining oversight for risky operations.

**Rationale:** Reduces "1000 yes/no prompts" babysitting, enables "walk away for coffee" autonomous operation. Critical for user quality-of-life and efficiency gains.

**Implementation:**

**ALWAYS APPROVED (No prompt):**
```yaml
Read Operations:
  - kubectl get <resource>
  - kubectl describe <resource>
  - kubectl logs <pod>
  - kubectl exec <pod> -- <read-only-command> (curl, ping, nc -zv, env, cat)

GitOps Monitoring:
  - flux get <resource>
  - flux logs <resource>
  - flux reconcile <resource> (safe - just triggers sync)

Git Operations:
  - git status
  - git diff
  - git log
  - git show
  - git fetch

Planning/Validation:
  - terraform plan (read-only)
  - kubectl apply --dry-run=client
  - kubectl kustomize

Network Testing:
  - curl <url>
  - ping <host>
  - nc -zv <host> <port>
  - nslookup <host>

File Operations:
  - Read tool (any file)
  - Glob tool (any pattern)
  - Grep tool (any search)
```

**ALWAYS ASK (Require approval):**
```yaml
Destructive Operations:
  - kubectl delete <resource>
  - kubectl scale --replicas=0
  - terraform destroy
  - git rm <file>

Write Operations:
  - kubectl apply -f (unless via GitOps workflow)
  - terraform apply
  - kubectl create <resource>
  - kubectl patch <resource>

Merge/Release:
  - gh pr merge (USER ONLY - agents NEVER merge)
  - git push --force
  - helm install/uninstall
```

- Document in CLAUDE.md with clear categorization
- Agent awareness: Include whitelist reference in agent prompts
- Validation: Test with standard workflows (deployment, troubleshooting, updates)

**Acceptance criteria:**
- Whitelist documented in CLAUDE.md
- Approval prompt count <5 per standard deployment task (from baseline ~25)
- Zero false negatives (risky operations still require approval)
- User can initiate task and disengage during execution phase

---

**Feature 3: Structured Agent Delegation via BMAD Tasks**

**Description:** Create core BMAD task definitions that deterministically route work to appropriate specialist agents.

**Rationale:** Eliminates "hit or miss" agent usage. Ensures consistent quality and leverages specialized expertise every time.

**Implementation:**

Create 4 core task files in `.bmad-core/tasks/`:

**Task 1: `deploy-new-app.md`**
- Routes to: homelab-infra-architect agent
- Reads: 3-NIC architecture checklist, app deployment template
- Creates: HelmRelease, NetworkPolicy, PersistentVolumeClaim, kustomization
- Validates: Network isolation, resource limits, security context
- Outputs: App documentation (OVERVIEW.md, ACCESS_POLICY.md)
- Security: Triggers create-pr-with-security task

**Task 2: `troubleshoot-app-issue.md`**
- Routes to: homelab-infra-architect agent
- Reads: App documentation FIRST (OVERVIEW.md, ACCESS_POLICY.md, network config)
- Diagnoses: Checks pod status, logs, events, network connectivity
- Fixes: Applies corrections via GitOps workflow
- Security: Triggers create-pr-with-security task for config changes

**Task 3: `install-system-dependency.md`**
- Routes to: cachyos-nix-specialist agent
- Reads: ~/.dotfiles Nix flake configuration
- Installs: Via Nix flake add + rebuild
- Validates: Command availability post-install
- Documents: Update dependency list

**Task 4: `plan-new-feature.md`**
- Routes to: PM agent → Scrum Master agent (sequential)
- PM creates: Epic document in `.claude/.ai-docs/epics/`
- Scrum Master creates: Story documents in `.claude/.ai-docs/stories/`
- Assigns: Work items to specialist agents
- Outputs: Prioritized backlog with agent assignments

**Acceptance criteria:**
- 4 task files exist and execute successfully
- Correct agent invoked 100% of time based on task type
- Tasks reference relevant documentation automatically
- Zero instances of Claude bypassing agent delegation
- Test each task with real-world scenarios (deploy echo server, fix network issue, install tool, plan feature)

---

**Feature 4: BMAD Core Structure Setup**

**Description:** Establish foundational BMAD directory structure alongside existing `.claude/.ai-docs/` (additive, not replacing).

**Rationale:** Provides organizational foundation for tasks, templates, and managed documentation. Zero risk approach preserves existing work.

**Implementation:**

Create directory structure:
```
.bmad-core/
├── core-config.yaml          # Project configuration
├── tasks/                    # Executable workflow definitions
│   ├── create-pr-with-security.md
│   ├── deploy-new-app.md
│   ├── troubleshoot-app-issue.md
│   ├── install-system-dependency.md
│   └── plan-new-feature.md
├── templates/                # Reusable document templates
│   └── app-documentation-tmpl.yaml (OVERVIEW, ACCESS_POLICY structure)
└── data/                     # Reference data
    └── 3-nic-architecture.md (network design checklist)

docs/
├── brief.md                  # This document
├── prd/                      # Product requirements (future)
└── architecture/             # Architecture docs (future)

.ai/
└── debug-log.md             # Agent decision log (auto-generated)

.claude/.ai-docs/            # PRESERVED AS-IS (377 files untouched)
```

- Update `.gitignore` to exclude `.ai/` (temp files only)
- Ensure `.bmad-core/` and `docs/` ARE tracked in Git
- Validate `.gitignore` integrity (NEVER expose secrets)

**Acceptance criteria:**
- Directory structure created
- core-config.yaml configured with correct paths
- .gitignore updated and validated
- All 377 existing files in `.claude/.ai-docs/` remain untouched
- No file moves or migrations required

---

**Feature 5: Progress Visibility (Todo Tracking + Debug Logs)**

**Description:** Real-time visibility into agent actions via todo tracking and `.ai/debug-log.md` capturing decisions and document references.

**Rationale:** Addresses "I don't know what Claude is doing" problem. Builds trust through transparency.

**Implementation:**

**Todo Tracking:**
- Use TodoWrite tool for all multi-step workflows
- Mark tasks in_progress before starting work
- Mark completed immediately after finishing
- Show progress during long-running operations

**Debug Logging (`.ai/debug-log.md`):**
- Auto-generate log entries for:
  - Agent delegation decisions (which agent, why)
  - Document references (which files read/created)
  - Command executions (what ran, output summary)
  - Security review results (pass/fail, issues found)
  - Strategic decisions (architectural choices, trade-offs)
- Append-only format (chronological log)
- User can review anytime to understand agent reasoning

**Acceptance criteria:**
- Todo tracking used in all BMAD task workflows
- Debug log captures agent decisions during task execution
- User can review debug log to understand "what happened and why"
- Todo progress visible in real-time during workflow execution

---

### Out of Scope for MVP

**Deferred to Phase 2:**

1. **Agent Enhancement (Infrastructure-DevOps Merge):** Extracting templates/checklists from BMAD expansion pack to enhance homelab-infra-architect agent - valuable but not blocking core workflow improvements

2. **Comprehensive Template Library:** Formalizing all existing patterns (ACCESS_POLICY, 3-NIC network, GPU configs) into BMAD templates - start with 1-2 essential templates in MVP, expand later

3. **Automated Runbooks:** Converting troubleshooting docs into executable scripts - requires significant development, not needed for workflow transformation

4. **Rollback System Integration:** Backup orchestration and automated rollback triggers - important for reliability but MVP focuses on preventing issues (security gates) not recovering from them

5. **Historical Documentation Cleanup:** Organizing 30+ loose files in `.claude/.ai-docs/` root - preserve as-is in MVP, tackle incrementally post-adoption

6. **Advanced Monitoring/Alerting:** Dashboard for KPI tracking, automated compliance reporting - manual validation sufficient for MVP

**Explicitly NOT Included:**

1. **Migration of .ai-docs/ files:** All 377 files stay exactly where they are - no moves, no reorganization, no risk

2. **Custom agent development:** Use existing agents (homelab-infra-architect, security-guardian, cachyos-nix-specialist) enhanced with BMAD tasks, not creating new agents

3. **Infrastructure changes:** No changes to K8s cluster, Proxmox, Terraform configs - purely workflow/documentation transformation

4. **Application deployments:** MVP focuses on establishing workflow, not deploying new apps (though will test with 1-2 apps to validate)

5. **Moonshot features:** AI training from epic archive, self-documenting deployments, proactive violation prevention - interesting but premature for MVP

---

### MVP Success Criteria

**The MVP is successful when:**

1. **Zero Security Violations:** 3 consecutive PR workflows execute with 100% security-guardian review compliance (no skips possible)

2. **Autonomous Operation Validated:** 1 complete app deployment workflow (deploy echo server or similar) executes from "deploy app X" to merged PR with <5 user approval prompts (vs. current ~25)

3. **Deterministic Agent Routing:** 5 consecutive tasks correctly route to appropriate specialist agents without manual delegation

4. **Documentation Follows Structure:** New work creates files in `docs/` and `.bmad-core/` following templates, not ad-hoc locations

5. **User Confidence:** User can initiate task, disengage during execution, return to review PR - "walk away for coffee" workflow achieved

6. **Sustained Compliance:** Above criteria maintained for 2 weeks to prove durability (not one-time success)

**MVP Timeline:** 4-6 weeks from BMAD installation to sustained compliance validation

**Minimum Viable Outcome:** User trusts agents enough to delegate PR creation workflows autonomously with security confidence, freeing up 40-60% of current supervision time.

---

## Post-MVP Vision

### Phase 2 Features (Weeks 6-12)

Once MVP establishes core workflow (security gates, autonomous operation, deterministic delegation), Phase 2 expands patterns and capabilities.

**Phase 2.1: Comprehensive Template Library (Weeks 6-8)**

Build on MVP's basic app documentation template to formalize all major patterns:

**Network Architecture Templates:**
- **3-NIC Design Checklist** (`templates/3-nic-network-checklist.yaml`)
  - Default network (cluster-internal)
  - IoT VLAN 62 (smart home devices)
  - DMZ VLAN 81 (external-facing services)
  - CiliumNetworkPolicy patterns for each tier
  - Validation criteria

**Application Documentation Templates:**
- **Comprehensive App Template** (`templates/app-full-documentation.yaml`)
  - OVERVIEW.md (purpose, architecture, dependencies)
  - ACCESS_POLICY.md (who can access, from where, authentication)
  - NETWORK_POLICY.md (CiliumNetworkPolicy rationale, allowed traffic)
  - STORAGE_POLICY.md (PV/PVC specs, backup requirements)
  - SECURITY_CONTEXT.md (pod security, capabilities, user/group IDs)

**Infrastructure Templates:**
- **GPU Workload Template** (NODE_REQUIREMENTS.md, runtime configuration)
- **Backup Policy Template** (what to backup, frequency, retention)
- **Monitoring Template** (Prometheus metrics, Grafana dashboards, alerts)

**Rationale:** Transforms one-off deployments into repeatable patterns. New apps benefit from proven architectures instead of reinventing each time.

**Outcome:** 90% of new deployments use templates, reducing deployment time by 40% and improving consistency.

---

**Phase 2.2: Agent Enhancement - Infrastructure-DevOps Merge (Weeks 8-10)**

Extract templates, checklists, and best practices from BMAD infrastructure-devops expansion pack to enhance existing homelab-infra-architect agent:

**Enhancements:**
- **Deployment checklists:** Pre-flight validation (resource availability, namespace readiness, dependencies satisfied)
- **Post-deployment verification:** Automated testing (pod health, service endpoints, network connectivity)
- **Troubleshooting playbooks:** Systematic diagnosis workflows (pod not starting, network unreachable, storage issues)
- **Terraform best practices:** State backup, plan review, apply verification, rollback procedures

**Rationale:** Leverage BMAD's proven DevOps patterns while preserving homelab-specific knowledge already in agent.

**Outcome:** Agent provides higher-quality deployments with systematic validation and troubleshooting capabilities.

---

**Phase 2.3: Historical Documentation Cleanup (Weeks 10-12)**

Organize 377 files in `.claude/.ai-docs/` without breaking existing references:

**Approach:**
- **Categorize:** DELIVERABLES (epics, stories, app docs) vs. WORKING FILES (screenshots, backups, status reports)
- **Consolidate duplicates:** Merge network/ and networking/ folders
- **Move temp files:** Relocate screenshots, backups to `.ai/archive/` (excluded from Git)
- **Create index:** Generate `.claude/.ai-docs/INDEX.md` mapping categories to files
- **Preserve everything:** No deletions, only organization

**Rationale:** Improves discoverability of historical context while maintaining safety (no data loss).

**Outcome:** Time to find historical documentation reduced from 10-15 minutes to <3 minutes via index.

---

### Long-term Vision (Months 3-12)

**Vision 1: Self-Documenting Infrastructure (Months 3-6)**

Agents automatically maintain documentation in sync with cluster state:

**Concept:**
- After every deployment, agent inspects actual cluster state (pod config, network policies, storage mounts)
- Generates/updates documentation based on observed reality (not assumptions)
- Documents behavior, not just intent (actual resource usage, observed traffic patterns)
- Creates living documentation that evolves with infrastructure

**Example:** Deploy Plex Media Server → Agent observes GPU allocation, transcoding behavior, network access patterns, storage mounts → Automatically generates comprehensive OVERVIEW.md with real performance data

**Outcome:** Documentation never goes stale, accurately reflects current state, includes operational insights (not just design specs)

---

**Vision 2: Automated Runbooks for Common Issues (Months 4-8)**

Convert troubleshooting documentation into executable automation:

**Concept:**
- Extract procedures from boot-issues docs, incident reports, troubleshooting guides
- Convert to executable scripts with decision points (if condition X, try solution Y)
- Agent executes runbook when issue pattern detected
- Escalates to user only when all automated remediation fails

**Example:** "Pod stuck in CrashLoopBackOff" → Agent runs diagnostic runbook (check logs, verify secrets, test network, validate mounts) → Identifies root cause → Applies fix via GitOps → Creates PR with explanation

**Outcome:** 70% of routine issues resolved automatically, user intervenes only for novel problems

---

**Vision 3: Proactive Violation Prevention (Months 6-12)**

Shift from detecting violations to preventing them structurally:

**Concept:**
- **Pre-flight validation:** Before writing files, validate against security policies
- **.gitignore integrity checks:** Verify .gitignore patterns before git add (prevents accidental secret exposure)
- **Real-time secret scanning:** Scan file content before staging (catch secrets before commit)
- **YAML validation gates:** Syntax and schema validation before creating manifests
- **Dependency vulnerability scanning:** Check new packages for CVEs before installation

**Example:** Agent attempts to create file with hardcoded API token → Pre-flight validation detects secret → Blocks file write → Suggests ExternalSecret pattern instead → Prevents violation before it happens

**Outcome:** Zero security incidents - violations become structurally impossible, not just unlikely

---

**Vision 4: Pattern Learning from Historical Archive (Months 9-12)**

Use 377-file epic/story archive to train agents on project-specific patterns:

**Concept:**
- Extract successful patterns from completed work (network designs, storage configs, security contexts)
- Identify anti-patterns from troubleshooting docs (what failed and why)
- Build knowledge base of "how we work" (architectural decisions, tool selections, trade-offs)
- Agents reference historical context when making similar decisions

**Example:** Agent planning new DMZ app deployment → Searches epic archive for similar deployments (Plex, other external services) → Applies proven VLAN 81 + CiliumNetworkPolicy pattern → Avoids pitfalls documented in past troubleshooting

**Outcome:** Agents learn from project history, improving quality over time. "Institutional memory" becomes automated.

---

### Expansion Opportunities

**Opportunity 1: Multi-Cluster Management**

Extend BMAD workflows to manage multiple K8s clusters (prod, dev, test):

- Cluster-specific task variants (deploy-to-prod.md, deploy-to-dev.md)
- Environment-aware security gates (stricter for prod)
- Promotion workflows (dev → staging → prod)
- Cluster health monitoring and alerting

**Enabler:** Template library makes it easy to replicate patterns across clusters

---

**Opportunity 2: Community Template Sharing**

Contribute homelab patterns to broader community:

- Open-source template library (3-NIC architecture, GPU workloads, DMZ isolation)
- Share BMAD tasks for common homelab scenarios
- Collaborate with home-ops, k8s-at-home communities
- Learn from other operators' patterns

**Enabler:** Formalized templates make knowledge shareable (not locked in one person's head)

---

**Opportunity 3: Advanced Automation**

Build on autonomous operation foundation:

- **Scheduled maintenance:** Automated dependency updates with testing
- **Capacity planning:** Predict resource needs based on growth trends
- **Cost optimization:** Identify underutilized resources, suggest consolidation
- **Disaster recovery:** Automated backup testing, failover validation
- **Security hardening:** Continuous compliance checking, remediation suggestions

**Enabler:** Pre-approved whitelist and autonomous workflows provide foundation for advanced automation

---

**Opportunity 4: Infrastructure as Product**

Treat homelab as a product with releases, changelogs, and roadmaps:

- **Release versioning:** Tag major infrastructure milestones (v1.0 = MVP complete, v2.0 = Phase 2 complete)
- **Changelog generation:** Automatic from PR history and epic completion
- **Roadmap visualization:** Public view of planned features, in-progress work
- **Metrics dashboard:** Infrastructure health, deployment velocity, security compliance

**Enabler:** BMAD's epic/story structure provides product management foundation

---

### Ultimate Vision: Autonomous Homelab Operations

**12-Month Horizon:**

User provides strategic direction only:
- "I want to run a media server with GPU transcoding in DMZ"
- "Add monitoring for GPU workloads"
- "Prepare for storage expansion"

Agents handle everything else:
- Planning (epics, stories, work items)
- Implementation (manifests, configs, documentation)
- Security validation (mandatory gates, compliance checking)
- Deployment (GitOps PR workflows)
- Verification (testing, monitoring, alerting)
- Documentation (living docs that reflect reality)
- Maintenance (updates, troubleshooting, optimization)

User interventions:
- Strategic decisions (architectural choices, tool selections)
- PR merge approvals (final deployment gate)
- Novel problem-solving (issues outside agent capabilities)

**Time allocation shift:**
- Current: 70% tactical execution, 20% firefighting, 10% strategic thinking
- 12-month target: 10% tactical oversight, 10% firefighting, 80% strategic experimentation

**Capabilities unlocked:**
- Deploy 5-10 new apps per month (vs. current 1)
- Experiment with advanced features (service mesh, advanced storage, ML workloads)
- Contribute to homelab community (share patterns, document learnings)
- Scale infrastructure without scaling operational burden

**Philosophy:** The homelab infrastructure becomes a force multiplier - one operator can achieve what previously required a small team, while maintaining production-grade reliability and security.

---

## Technical Considerations

### Platform Requirements

**Development Workstation:**
- **OS:** CachyOS Linux (Arch-based, kernel 6.17.7-5-cachyos)
- **Package Manager:** Nix (flake-based) via `~/.dotfiles` repository
- **Terminal:** Claude Code CLI environment
- **Browser Support:** Not applicable (CLI-based workflows)
- **Performance Requirements:**
  - Sufficient RAM for Claude Code operation (~2-4GB)
  - Local Git repository access
  - SSH access to Proxmox hosts

**Infrastructure Platform:**
- **Hypervisor:** Proxmox VE 8.x
- **Kubernetes Distribution:** Talos Linux 1.11.x (immutable, API-managed)
- **Cluster Scale:** 15 nodes (controllers + workers)
- **Node Architecture:** x86_64
- **Special Hardware:** GPU nodes (NVIDIA RTX A2000, RTX A5000 for transcoding/compute)
- **Network:** Multi-VLAN (default, IoT VLAN 62, DMZ VLAN 81)

**Cloud Services:**
- **Git Hosting:** GitHub (public repository)
- **CI/CD:** Self-hosted GitHub Actions runners in-cluster
- **Secrets Management:** 1Password (via ExternalSecrets Operator)
- **Container Registry:** GitHub Container Registry (ghcr.io), Docker Hub

---

### Technology Preferences

**Frontend (N/A):**
- No frontend - purely infrastructure and CLI-based workflows
- Documentation rendered as markdown in Git repository viewers

**Backend/Orchestration:**
- **GitOps:** Flux v2.x (source of truth for cluster state)
- **Infrastructure-as-Code:** Terraform (Proxmox provider for VM/template management)
- **Configuration Management:** Talos machine configs (declarative node configuration)
- **Secret Encryption:** SOPS with age encryption (age key: age1metxlry78...)
- **Networking:** Cilium CNI with CiliumNetworkPolicy for microsegmentation
- **Storage:** Rook-Ceph for persistent volumes, NFS for media storage

**AI/Agent Framework:**
- **Core:** Claude Code (Sonnet 4.5 model)
- **Workflow Framework:** BMAD (Brownfield Methodology for Agile Development)
- **Specialized Agents:**
  - homelab-infra-architect (infrastructure operations)
  - security-guardian (pre-push security validation)
  - cachyos-nix-specialist (dependency installation)
  - PM agent (epic planning)
  - Scrum Master agent (story/work item creation)

**Development Tools:**
- **CLI Tools:** kubectl, flux, terraform, talosctl, git, gh (GitHub CLI), sops
- **Package Installation:** Nix flakes (declarative dependency management)
- **Editor:** Claude Code integrated editing (Read/Write/Edit tools)

**Database (Application-Specific):**
- PostgreSQL (for apps requiring relational DB)
- SQLite (for lightweight apps)
- Not managed by BMAD directly - apps deploy their own databases

**Hosting/Infrastructure:**
- **On-Premises:** Proxmox cluster (physical hardware)
- **No Cloud Hosting:** Fully self-hosted homelab
- **External Access:** Cloudflare Tunnel (for DMZ apps), Tailscale (VPN access)

---

### Architecture Considerations

**Repository Structure:**

Current state (preserves historical work):
```
prox-ops/
├── .claude/
│   ├── agents/              # Agent definitions (existing)
│   └── .ai-docs/            # 377 historical files (PRESERVED)
├── kubernetes/
│   └── apps/                # Flux Kustomizations per app
├── terraform/               # Infrastructure-as-Code
│   ├── main.tf
│   ├── modules/
│   │   ├── talos-template/  # Phase 1: Template creation
│   │   └── talos-node/      # Phase 2: VM creation from templates
│   └── variables.tf
├── talos/
│   ├── patches/             # Node-specific patches
│   └── talconfig.yaml       # Talos cluster config
├── CLAUDE.md                # Agent instructions
└── README.md
```

BMAD additions (new structure):
```
prox-ops/
├── .bmad-core/              # NEW: BMAD framework
│   ├── core-config.yaml
│   ├── tasks/               # Executable workflows
│   ├── templates/           # Document/config templates
│   └── data/                # Reference data (checklists, patterns)
├── docs/                    # NEW: Managed deliverables
│   ├── brief.md             # This document
│   ├── prd/                 # Product requirements (future)
│   └── architecture/        # Architecture docs (future)
├── .ai/                     # NEW: Debug/temp files (excluded from Git)
│   └── debug-log.md
└── .gitignore               # UPDATED: Exclude .ai/, protect .claude/
```

**Rationale:** Additive structure preserves existing work while establishing BMAD organization. No file moves required.

---

**Service Architecture:**

```
User (Claude Code CLI)
    ↓
┌─────────────────────────────────────────┐
│ BMAD Task Workflows                     │
│ (.bmad-core/tasks/)                     │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ create-pr-with-security.md      │   │
│  │   ├─ Stage changes              │   │
│  │   ├─ security-guardian (MANDATORY) │
│  │   ├─ Commit & push              │   │
│  │   ├─ Create PR                  │   │
│  │   └─ Post-merge validation      │   │
│  └─────────────────────────────────┘   │
│                                         │
│  ┌─────────────────────────────────┐   │
│  │ deploy-new-app.md               │   │
│  │   ├─ homelab-infra-architect    │   │
│  │   ├─ Create manifests           │   │
│  │   ├─ Validate configs           │   │
│  │   └─ Trigger PR workflow        │   │
│  └─────────────────────────────────┘   │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ Specialized Agents                      │
│ (.claude/agents/)                       │
│                                         │
│  ┌───────────────────────────────────┐ │
│  │ homelab-infra-architect          │ │
│  │  - Infrastructure deployment     │ │
│  │  - Kubernetes config generation  │ │
│  │  - Terraform operations          │ │
│  │  - Troubleshooting               │ │
│  └───────────────────────────────────┘ │
│                                         │
│  ┌───────────────────────────────────┐ │
│  │ security-guardian                │ │
│  │  - Secret detection              │ │
│  │  - .gitignore validation         │ │
│  │  - YAML syntax checking          │ │
│  │  - Security best practices       │ │
│  └───────────────────────────────────┘ │
│                                         │
│  ┌───────────────────────────────────┐ │
│  │ cachyos-nix-specialist           │ │
│  │  - Dependency installation       │ │
│  │  - Nix flake management          │ │
│  └───────────────────────────────────┘ │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ Git Repository (GitHub)                 │
│                                         │
│  Feature Branch → PR → Main Branch     │
│       ↓                     ↓           │
│  Copilot Review         Flux Webhook    │
└─────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────┐
│ Kubernetes Cluster (Talos)              │
│                                         │
│  Flux reconciles Git → Applies manifests│
│  Self-hosted GitHub runners → CI/CD     │
│  Applications deployed via HelmReleases │
└─────────────────────────────────────────┘
```

**Key Architectural Principles:**

1. **Git as Source of Truth:** All infrastructure changes flow through Git (never direct kubectl apply)
2. **Structural Enforcement:** Security gates are workflow dependencies, not optional steps
3. **Agent Specialization:** Tasks route to agents with domain expertise
4. **Declarative Configuration:** Kubernetes manifests, Terraform configs, Talos machine configs all declarative
5. **Immutable Infrastructure:** Talos nodes are cattle (replaced, not modified), templates are versioned

---

### Integration Requirements

**Git Integration:**
- **Required:** Git CLI access with GitHub authentication
- **SSH Key:** `github-deploy.key` for automated commits/pushes
- **Permissions:** Write access to prox-ops repository
- **Workflow:** Feature branch → PR → User merge → Flux reconcile

**GitHub Integration:**
- **Required:** GitHub CLI (`gh`) for PR creation, issue management
- **Authentication:** Personal access token or GitHub App
- **Permissions:** Create PRs, view workflows, read/write repository
- **Webhooks:** Flux webhook for automatic reconciliation on push

**Flux Integration:**
- **Required:** Flux CLI for manual reconciliation, status checking
- **Connection:** Kubeconfig access to Talos cluster
- **Permissions:** Read GitRepository sources, reconcile Kustomizations/HelmReleases
- **Monitoring:** `flux logs` for deployment observability

**Terraform Integration:**
- **Required:** Terraform CLI with Proxmox provider
- **Authentication:** Proxmox API token (via TF_VAR_proxmox_password)
- **State:** Local terraform.tfstate (not in Git due to secrets)
- **Workflow:** Plan → Manual review → Apply (or destroy for template updates)

**SOPS Integration:**
- **Required:** SOPS CLI with age encryption backend
- **Encryption Key:** age private key (`age.key`, NOT in Git)
- **Usage:** Encrypt secrets before commit (`sops -e -i file.sops.yaml`)
- **Decryption:** Flux in-cluster decryption via age key Secret

**1Password Integration:**
- **Required:** External Secrets Operator with 1Password provider
- **Authentication:** 1Password Connect (self-hosted or cloud)
- **Usage:** ExternalSecret manifests reference 1Password items
- **Security:** API credentials stored in-cluster Secret

---

### Security/Compliance

**Secret Management:**
- **SOPS Encryption (Mandatory):** All secrets in Git MUST be SOPS-encrypted
  - Pattern: `*.sops.yaml` files with `encrypted_regex: ^(data|stringData)$`
  - Encryption: age public key (age1metxlry78...)
  - Decryption: Flux in-cluster with age private key Secret

- **1Password ExternalSecrets (Preferred for Apps):**
  - App credentials stored in 1Password
  - ExternalSecret syncs to Kubernetes Secret
  - Application references `existingSecret` pattern

- **Never in Git (Enforced by .gitignore + security-guardian):**
  - Private keys (`age.key`, `*.pem`, `github-deploy.key`)
  - API tokens in plaintext
  - Terraform state files (contain infrastructure details)
  - Debug/temp files (`.ai/` directory)

**Access Control:**
- **GitHub Repository:** Public (CRITICAL: never commit secrets)
- **Kubernetes RBAC:** Configured per-namespace for workloads
- **Network Policies:** CiliumNetworkPolicy for microsegmentation (3-NIC architecture)
- **Pod Security Standards:** Enforced per-namespace (privileged/restricted)

**Compliance Requirements:**
- **Zero Tolerance for Secrets in Git:** Mandatory security-guardian review prevents leaks
- **Immutable Audit Trail:** All changes via Git commits (full history)
- **Security Gates:** Structural enforcement (cannot bypass)
- **Backup/Recovery:** Terraform state backup, SOPS-encrypted secrets recoverable

**Vulnerability Management:**
- **Renovate:** Automated dependency updates via self-hosted GitHub Actions
- **Container Scanning:** (Future) Integrate Trivy or similar for CVE detection
- **Security Monitoring:** (Future) Falco for runtime threat detection

---

### Performance Considerations

**Claude Code Operations:**
- **Context Window:** Manage conversation length to avoid context loss
- **File Operations:** Read/Write/Edit tools for local file manipulation
- **Command Execution:** Bash tool with timeouts for long-running operations
- **Agent Delegation:** Task tool launches specialist agents (separate context windows)

**Git Operations:**
- **Commit Frequency:** Atomic commits per logical change (not bulk commits)
- **Branch Strategy:** Feature branches per story/work item
- **PR Size:** Keep PRs focused (single app deployment, single fix) for easier review

**Flux Reconciliation:**
- **Sync Interval:** Default 1m for GitRepository, 5m for Kustomizations
- **Webhook:** Immediate reconciliation on push to main branch
- **Resource Usage:** Minimal (Flux controller ~100MB RAM)

**Terraform Operations:**
- **State Size:** Grows with node count (currently 15 nodes = ~500KB state)
- **Plan Time:** ~30 seconds for infrastructure review
- **Apply Time:** Template creation ~5 minutes, VM creation ~2 minutes per node

---

### Disaster Recovery

**Backup Strategy:**
- **Git Repository:** Primary source of truth (GitHub backup, local clone)
- **Terraform State:** Manual backup before major changes (copy to safe location)
- **SOPS Keys:** age private key backed up securely (required for secret decryption)
- **Cluster State:** Periodic etcd backups (Talos built-in)

**Recovery Procedures:**
- **Repository Corruption:** Restore from GitHub, re-clone
- **Terraform State Loss:** Rebuild from scratch or import existing resources
- **Secrets Loss:** Regenerate from 1Password, re-encrypt with SOPS
- **Cluster Failure:** Rebuild from Git (infrastructure-as-code enables full recovery)

**Rollback Mechanisms:**
- **Git Revert:** Rollback bad changes via `git revert` commit
- **Flux Suspend:** Emergency stop reconciliation (`flux suspend kustomization`)
- **Terraform Destroy:** Remove infrastructure and rebuild from previous state
- **Node Replacement:** Cattle strategy - destroy and recreate problematic nodes

---

## Constraints & Assumptions

### Constraints

**Budget:**
- **$0 allocated for BMAD adoption** - This is process/workflow improvement using existing tools (Claude Code, Git, existing infrastructure)
- **Existing infrastructure costs only** - Proxmox hardware, electricity, network (already sunk costs)
- **No new subscriptions required** - GitHub (free public repos), BMAD framework (open methodology), Claude Code (existing subscription)
- **1Password subscription** - Already in use for personal password management (not additional cost)

**Impact:** Solution must use existing tools and infrastructure. Cannot purchase commercial workflow automation platforms, paid agent frameworks, or cloud services.

---

**Timeline:**
- **MVP target: 4-6 weeks** - Initial BMAD adoption (security gates, pre-approved commands, agent delegation)
- **Phase 2 target: Weeks 6-12** - Template library, agent enhancement, documentation cleanup
- **Long-term target: 3-12 months** - Self-documenting infrastructure, automated runbooks, pattern learning

**Impact:** Timeline driven by solo operator availability (10-15 hours/week), not full-time development. Delays expected due to learning curve, troubleshooting, competing priorities.

**Realistic adjustment:** MVP likely 6-8 weeks (not 4-6), Phase 2 likely 8-16 weeks (not 6-12) accounting for real-world friction.

---

**Resources:**

**Human Resources:**
- **Solo operator** - Single person (jlengelbrecht) managing entire homelab
- **No team** - Cannot delegate work to others, all implementation/testing/validation is solo
- **Time availability: 10-15 hours/week** - Evenings and weekends only (hobby project, not day job)
- **Expertise level:** Advanced Kubernetes/infrastructure knowledge, learning BMAD methodology

**Impact:** Work proceeds sequentially, not in parallel. Cannot have separate people working on different features simultaneously. Single point of failure for knowledge and execution.

**Computational Resources:**
- **Existing Proxmox cluster** - 15-node Talos K8s cluster (already deployed)
- **CachyOS workstation** - Development environment (sufficient for Claude Code operations)
- **Network bandwidth** - Home network (sufficient for Git operations, API calls)
- **No cloud resources** - All operations on-premises (no AWS/GCP/Azure)

**Impact:** Cannot scale compute resources on-demand. Cluster capacity is fixed at current hardware. No cloud-based testing environments.

---

**Technical:**

**Existing Infrastructure (Cannot Change):**
- **Proxmox VE 8.x** - Hypervisor layer (stable, production workloads running)
- **Talos Linux 1.11.x** - K8s distribution (immutable, API-managed)
- **Flux v2.x** - GitOps reconciliation (cannot switch to ArgoCD without major disruption)
- **Terraform + Proxmox provider** - Infrastructure-as-Code (established patterns, state files)
- **15-node cluster** - Cannot easily add/remove nodes without hardware changes
- **Network architecture** - 3-NIC design (default, IoT VLAN 62, DMZ VLAN 81) established

**Impact:** BMAD adoption must work with existing stack. Cannot introduce breaking changes. Must preserve current operational stability.

**Agent Framework (Claude Code):**
- **Context window limitations** - Long conversations may lose context
- **Tool availability** - Limited to Claude Code's built-in tools (Read, Write, Edit, Bash, Task, etc.)
- **Agent definitions** - Existing homelab-infra-architect, security-guardian, cachyos-nix-specialist must remain compatible
- **No custom MCP servers** - Limited to standard Claude Code capabilities

**Impact:** BMAD tasks must work within Claude Code's tool constraints. Cannot require custom integrations or external agent frameworks.

**Git Repository (Public):**
- **Public GitHub repository** - Cannot make private (free tier limitation)
- **Zero tolerance for secrets** - SOPS encryption mandatory for all credentials
- **Commit history preserved** - Cannot rewrite history frequently (breaks Flux reconciliation)

**Impact:** Extreme security diligence required. Security-guardian review is non-negotiable. Cannot commit anything sensitive even temporarily.

**Development Environment (CachyOS + Nix):**
- **CachyOS Linux** - Arch-based rolling release (system updates required)
- **Nix package manager** - Declarative dependency management via flakes
- **Compatibility constraints** - BMAD tools must work on CachyOS, installed via Nix if possible

**Impact:** Dependencies must be Nix-compatible or installed via Nix flakes. Cannot use Debian/Ubuntu-specific packages without adaptation.

---

### Key Assumptions

**Assumption 1: BMAD Framework Compatibility**

**Assumption:** BMAD methodology is compatible with Claude Code agent framework and can be implemented using existing tools (Read, Write, Edit, Task).

**Basis:** BMAD documentation indicates framework is tool-agnostic and focuses on workflow structure, not specific implementation technology.

**Risk if wrong:** May need to adapt BMAD patterns significantly or find alternative workflow framework. Could delay MVP by 2-4 weeks.

**Validation:** Install BMAD core structure, create 1 test task, verify Task tool can execute workflow properly.

---

**Assumption 2: Structural Enforcement is Achievable**

**Assumption:** BMAD tasks with `elicit: true` and mandatory workflow steps can prevent agents from skipping security reviews or other critical gates.

**Basis:** BMAD task documentation shows `elicit: true` creates hard stops requiring user interaction before proceeding.

**Risk if wrong:** If agents can still bypass mandatory steps, security gate effectiveness is compromised. Back to documentation-based enforcement (current problem).

**Validation:** Test create-pr-with-security task with deliberate attempts to skip security-guardian step. Verify workflow blocks progression.

---

**Assumption 3: Pre-Approved Whitelist Reduces Prompts 80%**

**Assumption:** Categorizing commands into "always approved" vs "require approval" tiers will reduce approval prompts from ~25 per task to <5 per task.

**Basis:** Analysis of typical workflows shows majority of prompts are for read operations (kubectl get, flux logs, git status) which can be safely whitelisted.

**Risk if wrong:** If command categorization is too broad, might approve risky operations by mistake. If too narrow, won't achieve efficiency gains.

**Validation:** Track approval prompts during 3 test deployments after whitelist implementation. Measure actual reduction vs target.

---

**Assumption 4: Agent Delegation Can Be Deterministic**

**Assumption:** BMAD tasks that explicitly specify agent routing (via Task tool subagent_type parameter) will eliminate "hit or miss" agent usage.

**Basis:** Task tool currently supports subagent_type parameter for launching specific agents (homelab-infra-architect, security-guardian, etc.).

**Risk if wrong:** If agent routing remains non-deterministic despite task specifications, won't achieve consistent quality improvement.

**Validation:** Audit 10 consecutive tasks to verify correct agent invoked based on task definition. Zero instances of wrong agent or Claude handling directly.

---

**Assumption 5: User Will Sustain 10-15 Hours/Week**

**Assumption:** User has 10-15 hours/week available for homelab work and will maintain this investment for 4-6 week MVP period (total 40-90 hours).

**Basis:** Current operating overhead is 10-15 hours/week, so time availability exists. BMAD adoption aims to redirect this time from tactical to strategic work.

**Risk if wrong:** If time availability drops due to life circumstances, MVP timeline extends proportionally. Could take 8-12 weeks instead of 4-6.

**Validation:** Track actual hours spent weekly during MVP. If consistently below 10 hours, adjust timeline expectations.

---

**Assumption 6: MVP Success Enables Phase 2**

**Assumption:** Achieving MVP success criteria (zero security violations, autonomous operation, deterministic delegation) within 4-6 weeks creates momentum and confidence for Phase 2 adoption.

**Basis:** Brainstorming session showed strong user motivation. Quick wins (MVP) should reinforce value and sustain engagement.

**Risk if wrong:** If MVP takes longer than 6 weeks or doesn't deliver expected benefits, user interest may wane. Phase 2 could be delayed or abandoned.

**Validation:** User feedback after MVP completion. Explicit decision to proceed with Phase 2 based on realized benefits.

---

**Assumption 7: Historical Documentation Remains Useful**

**Assumption:** 377 existing files in `.claude/.ai-docs/` contain valuable context that should be preserved and referenced during BMAD adoption.

**Basis:** User stated "I don't want to lose what we did" - indicates value in historical work even if organization is unclear.

**Risk if wrong:** If historical docs are actually obsolete or misleading, preserving them might cause confusion or perpetuate bad patterns.

**Validation:** During first 2 weeks of BMAD adoption, track how often historical docs are referenced. If rarely used, can deprioritize cleanup effort.

---

**Assumption 8: Security Incident Drives Urgency**

**Assumption:** November 2025 Proxmox API leak incident created genuine urgency for mandatory security gates, making security the #1 priority.

**Basis:** User's clear prioritization of security gate as top concern during brainstorming. Recent incident provides emotional motivation.

**Risk if wrong:** If security concern fades over time, might deprioritize security-focused workflows. Could revert to "convenience over security" patterns.

**Validation:** User consistently enforces security review in first 4 weeks. Zero attempts to skip or bypass security-guardian delegation.

---

**Assumption 9: Templates Will Be Reused**

**Assumption:** Once formalized (Phase 2), templates will be actively used for new deployments rather than continuing ad-hoc "figure it out each time" approach.

**Basis:** Reusable patterns align with engineering efficiency values. Templates reduce cognitive load and speed up deployments.

**Risk if wrong:** If each deployment feels unique enough that templates don't apply, investment in template library provides limited value.

**Validation:** Track template usage during Phase 2. Target: 80%+ of new deployments use templates vs. custom implementations.

---

**Assumption 10: Public Repository is Acceptable Risk**

**Assumption:** Continuing with public GitHub repository is acceptable given SOPS encryption and mandatory security gates.

**Basis:** Free GitHub tier + self-hosting values + structural security enforcement makes public repo viable.

**Risk if wrong:** If another leak occurs despite security gates, reputational damage and potential infrastructure compromise. May need to migrate to private repo or self-hosted Git.

**Validation:** Zero security violations over 8 consecutive weeks after security gate implementation. Proves structural enforcement works.

---

**Assumption 11: Single Operator Can Scale This**

**Assumption:** One person can effectively operate production-grade 15-node K8s cluster with BMAD workflows handling complexity.

**Basis:** Automation and autonomous agent operation designed specifically to enable solo operator at scale.

**Risk if wrong:** If operational burden increases despite BMAD (due to framework complexity), might need to reduce scope or recruit help.

**Validation:** Track weekly hours after BMAD adoption. Target: Reduce from 10-15 hours to 6-10 hours within 8 weeks of MVP completion.

---

**Assumption 12: BMAD Learning Curve is Manageable**

**Assumption:** User can learn BMAD methodology concepts (tasks, templates, workflow structures) within 1-2 weeks without external training.

**Basis:** User has advanced technical skills (K8s, Terraform, GitOps). BMAD framework is well-documented.

**Risk if wrong:** If BMAD concepts are harder to grasp than expected, learning curve could add 2-4 weeks to MVP timeline.

**Validation:** User successfully creates first custom BMAD task within week 2 of adoption. Demonstrates framework comprehension.

---

## Risks & Open Questions

### Key Risks

**RISK 1: BMAD Framework Incompatibility with Claude Code** ⚠️ HIGH SEVERITY

**Description:** BMAD task workflows might not be fully compatible with Claude Code's agent framework, tool constraints, or context management.

**Likelihood:** Medium (30-40%) - BMAD designed for general AI agents, not specifically for Claude Code

**Impact:** If incompatible, cannot achieve structural enforcement (security gates, mandatory workflows). Falls back to documentation-based approach (current problem remains unsolved).

**Mitigation Strategies:**
- **Pre-MVP validation:** Install BMAD core, create 1 test task, execute end-to-end before committing to full adoption
- **Adaptation plan:** If incompatible, extract BMAD principles (structured tasks, templates, deterministic routing) and implement using Claude Code native capabilities
- **Fallback:** Use BMAD methodology conceptually but implement enforcement via CLAUDE.md rules + manual verification (not ideal but better than current state)

**Detection:** First 2 weeks of MVP - if test task cannot enforce mandatory steps, incompatibility is confirmed

---

**RISK 2: Security Gate Circumvention** 🔴 CRITICAL SEVERITY

**Description:** Despite BMAD task structure, agents might find ways to bypass security-guardian review (edge cases, workflow gaps, unintended shortcuts).

**Likelihood:** Low-Medium (20-30%) - Structural enforcement should prevent most bypasses, but edge cases exist

**Impact:** Another security incident (credential leak, secret exposure) undermines entire BMAD value proposition. Loss of trust, potential infrastructure compromise.

**Mitigation Strategies:**
- **Defense in depth:** Multiple security layers
  - BMAD task enforcement (primary)
  - Pre-commit hooks for secret scanning (secondary)
  - GitHub secret scanning alerts (tertiary)
  - Weekly manual audits of pushed commits
- **Red team testing:** Deliberately attempt to bypass security gates during MVP to identify gaps
- **Continuous monitoring:** Track all GitHub pushes, verify 100% have security-guardian review logged
- **Incident response plan:** Pre-defined procedures if leak occurs (revoke credentials, rewrite history, post-mortem)

**Detection:** Real-time via GitHub push logs, weekly audit reports

---

**RISK 3: Timeline Slippage - MVP Takes 8-12 Weeks Instead of 4-6** ⚠️ MEDIUM SEVERITY

**Description:** Learning curve, troubleshooting, competing priorities extend MVP timeline beyond target.

**Likelihood:** High (60-70%) - Most projects slip from initial estimates, especially with new frameworks

**Impact:** Delayed benefits realization. Risk of motivation decline if progress feels slow. Operational overhead continues at current levels longer than expected.

**Mitigation Strategies:**
- **Conservative planning:** Assume 6-8 weeks for MVP (not 4-6) in mental model
- **Milestone tracking:** Weekly progress reviews to detect slippage early
- **Scope reduction:** If behind schedule, cut MVP scope to absolute minimum (security gate + pre-approved whitelist only, defer agent delegation)
- **Time boxing:** Set hard deadline (8 weeks max), ship whatever is ready, iterate in Phase 2

**Detection:** Week 3 checkpoint - if <40% progress, slippage likely

---

**RISK 4: Pre-Approved Whitelist Too Permissive** ⚠️ MEDIUM SEVERITY

**Description:** Categorizing commands incorrectly might whitelist risky operations, creating new security vulnerabilities.

**Likelihood:** Medium (30-40%) - Hard to anticipate all edge cases during initial whitelist definition

**Impact:** Agents execute destructive operations without approval. Potential data loss, service disruption, configuration corruption.

**Mitigation Strategies:**
- **Conservative initial whitelist:** Start narrow (only obviously safe operations), expand iteratively based on real usage
- **Trial period:** Run whitelist for 2 weeks with manual verification of all "auto-approved" commands
- **Audit logging:** Capture all whitelisted commands executed, review weekly for unexpected patterns
- **Quick revert:** Document how to disable whitelist and return to full approval mode if issues detected

**Detection:** Weekly audit logs, user observation of unexpected command executions

---

**RISK 5: User Motivation Decline Post-MVP** ⚠️ MEDIUM SEVERITY

**Description:** After MVP completion, user interest/energy wanes. Phase 2 and long-term vision never materialize.

**Likelihood:** Medium (40-50%) - Common pattern for hobby projects after initial enthusiasm fades

**Impact:** Infrastructure remains at MVP state (functional but not optimized). Template library, automated runbooks, advanced features never realized. Operational overhead reduces but doesn't reach target 60-80% improvement.

**Mitigation Strategies:**
- **Quick wins in MVP:** Ensure MVP delivers tangible benefits (reduced prompts, zero security violations) to build momentum
- **Celebrate milestones:** Explicitly acknowledge MVP completion, Phase 2 start
- **Incremental Phase 2:** Break Phase 2 into small, achievable chunks (1 template per week, not all at once)
- **Community engagement:** Share progress with homelab community for external motivation
- **Opportunistic adoption:** If Phase 2 feels like "work", let it emerge organically when needed rather than forcing schedule

**Detection:** User feedback after MVP. Honest assessment of energy/interest level.

---

**RISK 6: Existing Infrastructure Stability Disruption** ⚠️ MEDIUM SEVERITY

**Description:** BMAD adoption work (testing workflows, creating PRs, deploying test apps) inadvertently destabilizes production workloads.

**Likelihood:** Low-Medium (20-30%) - Most testing can be done safely, but mistakes happen

**Impact:** Service outages for self-hosted applications. Family/household disruption if critical services down (home automation, media server, network services).

**Mitigation Strategies:**
- **Dedicated test namespace:** Create `bmad-testing` namespace for all MVP validation work, isolated from production apps
- **Low-risk test apps:** Use simple, non-critical apps for testing (echo server, nginx hello-world) not production services
- **Off-peak testing:** Schedule disruptive testing during low-usage hours (late night, early morning)
- **Rollback readiness:** Before any test, ensure rollback procedure is documented and validated
- **Backup before major changes:** Talos etcd snapshot, Terraform state backup before testing infrastructure-level changes

**Detection:** Production service monitoring, family complaints about outages

---

**RISK 7: Documentation Explosion - Adding BMAD Creates More Clutter** ⚠️ LOW SEVERITY

**Description:** Instead of improving organization, BMAD adoption creates yet another layer of documentation (`.bmad-core/` + `docs/` + existing `.ai-docs/`) increasing complexity.

**Likelihood:** Medium (30-40%) - Additive approach by definition adds structure, risk is it becomes overwhelming

**Impact:** Cognitive overload trying to remember where documentation lives. Defeats purpose of "better organization". Could slow down operations rather than speed them up.

**Mitigation Strategies:**
- **Clear location rules:** Document strict rules for what goes where (deliverables in docs/, workflows in .bmad-core/, historical in .ai-docs/)
- **Index/map creation:** Create `.claude/.ai-docs/INDEX.md` and `.bmad-core/README.md` explaining structure
- **Regular cleanup:** Monthly review to archive or consolidate redundant documentation
- **User feedback:** If feeling confused about where to find things, signal to simplify structure

**Detection:** User experience - if frequently asking "where is X?" or searching multiple locations, problem exists

---

**RISK 8: Agent Delegation Overhead Exceeds Benefit** ⚠️ LOW SEVERITY

**Description:** Launching specialist agents via Task tool might add latency/complexity that outweighs benefits of specialized expertise.

**Likelihood:** Low (15-20%) - Existing agents already provide value, BMAD just makes usage consistent

**Impact:** Workflows feel slower due to agent delegation overhead. User reverts to "just let Claude handle it" pattern, negating consistency benefits.

**Mitigation Strategies:**
- **Performance monitoring:** Track time-to-completion for tasks with vs without agent delegation
- **Selective delegation:** Not all tasks need agents - reserve for complex operations (infrastructure, security, dependencies)
- **Parallel where possible:** Launch multiple agents concurrently if tasks are independent
- **Delegation threshold:** Define criteria for when delegation is warranted vs overkill

**Detection:** User frustration with slow workflows, tendency to skip agent delegation

---

### Open Questions

**QUESTION 1: How will BMAD task execution interact with Claude Code's context window limits?**

**Context:** Long BMAD workflows (deploy app → security review → PR creation → post-merge validation) might exceed context window, causing agents to lose track of workflow state.

**Resolution approach:**
- **Test during MVP Week 1:** Execute longest anticipated workflow (full app deployment) and monitor context window usage
- **Mitigation options if problem:**
  - Break long workflows into shorter sub-tasks with handoff points
  - Use debug logs to persist state between conversations
  - Leverage TodoWrite for workflow state tracking
- **Decision point:** Week 2 of MVP - if context issues detected, implement state persistence strategy

---

**QUESTION 2: Should .gitignore validation be part of security-guardian review or separate pre-commit hook?**

**Context:** Proxmox API leak caused by .gitignore violation. Need to decide where validation belongs in workflow.

**Options:**
- **Option A:** Security-guardian checks .gitignore integrity during review (all-in-one security gate)
- **Option B:** Separate pre-commit hook validates .gitignore before security review (fail-fast)
- **Option C:** Both (defense in depth)

**Resolution approach:**
- **MVP: Option A** (security-guardian handles all security checks for simplicity)
- **Phase 2: Evaluate Option C** if additional .gitignore issues occur
- **Decision point:** During MVP implementation (Week 1-2)

---

**QUESTION 3: How to handle tasks that span multiple agents sequentially?**

**Context:** Some workflows require agent coordination (e.g., deploy app = infra agent creates manifests → security agent reviews → infra agent creates PR).

**Options:**
- **Option A:** Single task with multiple agent delegation points
- **Option B:** Separate tasks with handoff between agents
- **Option C:** Master task that launches sub-tasks for each agent

**Resolution approach:**
- **Prototype during MVP Week 2:** Create deploy-new-app task and test different coordination patterns
- **Evaluate:** Which pattern is clearest, least error-prone, best user experience
- **Decision point:** End of Week 2 based on testing results

---

**QUESTION 4: What happens when security gate fails? Retry workflow or escalate to user?**

**Context:** If security-guardian finds issues (secrets detected, .gitignore violated), workflow should stop. But then what?

**Options:**
- **Option A:** Agent auto-fixes issues if possible (remove secrets, correct .gitignore), re-runs security review
- **Option B:** Agent escalates to user with detailed explanation, waits for manual fix
- **Option C:** Hybrid - auto-fix obvious issues, escalate complex/ambiguous ones

**Resolution approach:**
- **MVP: Option B** (escalate to user for safety - no auto-fixing that might make wrong assumptions)
- **Phase 2: Consider Option C** once patterns of common failures understood
- **Decision point:** During security-guardian task creation (Week 1)

---

**QUESTION 5: Should historical .ai-docs/ files be gradually migrated to BMAD structure or preserved indefinitely?**

**Context:** Additive approach preserves 377 files, but long-term maintenance of parallel structures might be cumbersome.

**Options:**
- **Option A:** Preserve indefinitely as historical archive (read-only reference)
- **Option B:** Gradual migration as files are referenced/updated (move to BMAD structure)
- **Option C:** Hybrid - frequently used docs migrate, rarely used stay in archive

**Resolution approach:**
- **MVP & Phase 2: Option A** (no migration, focus on new work)
- **Month 6 review:** Track .ai-docs/ reference frequency, decide if migration effort justified
- **Decision point:** 6 months post-MVP based on usage data

---

**QUESTION 6: How to version BMAD tasks as workflows evolve?**

**Context:** Tasks will improve over time based on experience. Need versioning strategy to track changes and enable rollback.

**Options:**
- **Option A:** Git commits provide version history (no explicit task versioning)
- **Option B:** Semantic versioning in task files (v1.0, v1.1, v2.0)
- **Option C:** Date-based snapshots (task-2025-11-15.md)

**Resolution approach:**
- **MVP: Option A** (Git history sufficient for early iterations)
- **Phase 2: Evaluate Option B** if multiple task versions need to coexist
- **Decision point:** When first major task revision needed (likely Month 2-3)

---

**QUESTION 7: Can KPI tracking be automated or requires manual auditing?**

**Context:** 7 KPIs defined in Goals & Success Metrics. Manual tracking is tedious but automation might be complex.

**Options:**
- **Option A:** Manual tracking via spreadsheet (simple but time-consuming)
- **Option B:** Parse git logs + debug logs automatically (complex but scalable)
- **Option C:** Hybrid - automate what's easy (security compliance from git logs), manual for subjective metrics

**Resolution approach:**
- **MVP: Option A** (manual tracking for 4-6 weeks is manageable)
- **Phase 2: Implement Option C** (automate most common KPIs)
- **Decision point:** End of MVP when sustained tracking burden becomes clear

---

**QUESTION 8: Should Copilot PR review feedback be automatically addressed or require user approval?**

**Context:** GitHub Copilot reviews PRs and provides suggestions. Uncertain whether agents should apply suggestions autonomously.

**Options:**
- **Option A:** Agent applies Copilot suggestions automatically (after security review)
- **Option B:** Agent presents Copilot suggestions to user for decision
- **Option C:** Agent evaluates suggestions critically, applies obvious improvements, escalates questionable ones

**Resolution approach:**
- **Current CLAUDE.md: Option C** (critical evaluation, not blind application)
- **MVP validation:** Test with 3-5 PRs to see if approach works
- **Decision point:** Week 4 of MVP based on real PR feedback patterns

---

**QUESTION 9: What's the rollback procedure if BMAD adoption proves problematic?**

**Context:** If MVP fails or creates more problems than it solves, need graceful exit strategy.

**Options:**
- **Option A:** Delete .bmad-core/, docs/, .ai/ directories, revert CLAUDE.md changes
- **Option B:** Keep structure but disable task enforcement, return to documentation-based approach
- **Option C:** Selective rollback (keep whitelist, remove security gates, etc.)

**Resolution approach:**
- **Document rollback procedures before MVP starts**
- **Test rollback in Week 3** (dry run to ensure it works)
- **Decision point:** Week 6 - honest assessment if BMAD is net positive or net negative

---

**QUESTION 10: How to handle tasks requiring multi-step user input during workflow?**

**Context:** Some decisions require user input mid-workflow (e.g., "which VLAN should this app use?" during deployment).

**Options:**
- **Option A:** Task pauses at decision point, waits for user input via AskUserQuestion tool
- **Option B:** Task gathers all requirements up-front before execution
- **Option C:** Hybrid - known requirements up-front, unexpected issues trigger mid-workflow pause

**Resolution approach:**
- **MVP: Option C** (balanced approach)
- **Validate:** Create deploy-new-app task with example decision points, test UX
- **Decision point:** Week 3 during task workflow refinement

---

### Areas Needing Further Research

**Research Area 1: BMAD Infrastructure-DevOps Expansion Pack Contents**

**What we need to know:** Specific templates, checklists, and best practices included in expansion pack that could enhance homelab-infra-architect agent.

**Why it matters:** Phase 2 includes agent enhancement, but unclear what exactly to extract/integrate.

**Research approach:**
- Review BMAD expansion pack documentation during MVP weeks 4-6 (while core workflows stabilize)
- Identify templates/checklists applicable to homelab context (Kubernetes, GitOps, IaC)
- Create integration plan for Phase 2

**Timeline:** Weeks 4-6 of MVP

---

**Research Area 2: Claude Code Context Management Best Practices**

**What we need to know:** Optimal strategies for long-running workflows that might exceed context window (state persistence, workflow chunking, handoff patterns).

**Why it matters:** Risk #1 (BMAD incompatibility) and Question #1 (context limits) both relate to this.

**Research approach:**
- Review Claude Code documentation on context management
- Test long workflows during MVP Week 1-2
- Experiment with state persistence via debug logs, TodoWrite, intermediate file outputs
- Document learned patterns for future task design

**Timeline:** Weeks 1-2 of MVP

---

**Research Area 3: Home-Ops and K8s-at-Home Community Patterns**

**What we need to know:** How other homelab operators structure documentation, handle GitOps workflows, organize patterns (compare to our BMAD approach).

**Why it matters:** Could learn from community best practices, validate our approach, potentially contribute back.

**Research approach:**
- Review home-ops repository structure (onedr0p's cluster)
- Study k8s-at-home community organization approaches
- Identify patterns that align with or diverge from BMAD methodology
- Consider contributing prox-ops templates to community if valuable

**Timeline:** Ongoing (opportunistic research during downtime)

---

**Research Area 4: Alternative Secret Management Approaches**

**What we need to know:** Whether SOPS + ExternalSecrets is optimal pattern, or if better alternatives exist (sealed-secrets, external-secrets with other backends, Vault).

**Why it matters:** Security is critical concern. Want to ensure using best-in-class secret management.

**Research approach:**
- Compare SOPS vs sealed-secrets vs Vault for homelab context
- Evaluate complexity vs security trade-offs
- Test alternative approaches in isolated namespace
- Decide if migration justified or current approach sufficient

**Timeline:** Phase 2 (Weeks 8-10) - not blocking for MVP

---

**Research Area 5: Automated Runbook Frameworks**

**What we need to know:** Existing frameworks for converting troubleshooting documentation into executable automation (Ansible, Rundeck, custom scripts).

**Why it matters:** Long-term vision includes automated runbooks. Need to understand implementation options before committing to approach.

**Research approach:**
- Survey runbook automation tools
- Prototype simple runbook (e.g., "pod stuck in CrashLoopBackOff" diagnostic)
- Evaluate integration with BMAD tasks and Claude Code agents
- Determine if worth investment or if manual troubleshooting sufficient

**Timeline:** Months 4-6 (long-term vision phase)

---

## Appendices

### Appendix A: Research Summary

**Brainstorming Session (2025-11-14)**

Comprehensive 70-minute structured brainstorming session facilitated using BMAD methodology to understand current state and design adoption strategy.

**Session Overview:**
- **Facilitator:** Business Analyst Mary (BMAD agent)
- **Participant:** jlengelbrecht (repository owner)
- **Techniques Used:** First Principles Thinking, Morphological Analysis, Assumption Reversal, SCAMPER Method
- **Total Ideas Generated:** 35+ actionable transformation ideas
- **Document Location:** `docs/brainstorming-session-results.md`

**Key Findings:**

1. **Current State Analysis:**
   - 377 files in `.claude/.ai-docs/` across 34 subdirectories
   - Functional organization already exists (epics, stories, apps, deployment, etc.)
   - Root-level clutter: 30+ loose status reports
   - Duplicate categories (network/ vs networking/)

2. **Pain Points Identified:**
   - "Vibe coding" - 377 files created without clear organization
   - "Hit or miss" agent delegation - inconsistent specialist agent usage
   - Security gate violations - agents skip mandatory reviews ~40% of time
   - Manual babysitting - ~25-30 approval prompts per task
   - Zero visibility - user doesn't know what agents are doing or why

3. **Core Needs Validated:**
   - Mandatory security gates (un-skippable via structural enforcement)
   - Pre-approved command whitelist (eliminate babysitting)
   - Structured agent delegation (deterministic routing)
   - Progress visibility (todo tracking, debug logs)
   - Preserve historical context (no file migrations)

4. **Strategic Insights:**
   - User wants to "walk away for coffee" - autonomous operation with strategic oversight
   - Security incident (Proxmox API leak) is primary motivator
   - Additive adoption strongly preferred over migration
   - User values what's been built, wants to enhance not replace
   - Trust requires transparency + guardrails

**Top 3 Prioritized Ideas:**
1. **Mandatory Security Gate** (TOP PRIORITY #1) - Structural enforcement via BMAD task workflow
2. **Pre-Approved Command Whitelist** (TOP PRIORITY #2) - Categorize by impact, eliminate unnecessary prompts
3. **Structured Agent Delegation** (TOP PRIORITY #3) - Deterministic routing based on task type

These three priorities directly informed the MVP scope defined in this brief.

**Ideal Workflow Scenarios:**

The brainstorming session defined two target workflow scenarios:

**Scenario 1: New App Deployment**
- User says "deploy app X"
- Agent creates manifests, evaluates against 3-NIC architecture
- **Automatically triggers security review** (no manual prompt needed)
- Creates PR and stops
- User reviews and merges
- Agent validates deployment and documents automatically

**Scenario 2: App Fix/Change**
- Agent has story to work on
- **Checks app documentation FIRST** (context-aware)
- Applies fixes via GitOps
- **Automatically runs security gate**
- Creates PR and stops
- User reviews and merges
- Agent validates and updates docs

These scenarios became the blueprint for BMAD task design in MVP.

---

### Appendix B: Current Infrastructure State

**Cluster Configuration (as of 2025-11-15):**

**Nodes:**
- 15-node Talos Linux cluster (Talos v1.11.5)
- Controller nodes: 3 (k8s-ctrl-1, k8s-ctrl-2, k8s-ctrl-3)
- Worker nodes: 12 (k8s-work-1 through k8s-work-16, with some gaps)
- GPU-capable nodes: 2 (k8s-work-4: RTX A2000, k8s-work-14: RTX A5000)

**Network Architecture:**
- **Default network:** Standard cluster networking (pod-to-pod, service discovery)
- **IoT VLAN 62:** Isolated network for smart home devices (Home Assistant, etc.)
- **DMZ VLAN 81:** External-facing services with strict network policies (Plex, etc.)
- **CNI:** Cilium with CiliumNetworkPolicy for microsegmentation

**Storage:**
- **Rook-Ceph:** Distributed storage for persistent volumes (RBD, CephFS)
- **NFS:** External NFS server for media storage (read-only mounts)
- **Local storage:** Node-local volumes for specific workloads

**GitOps:**
- **Flux v2.x:** Continuous reconciliation from GitHub repository
- **Source:** GitHub repository `jlengelbrecht/prox-ops` (public)
- **Sync interval:** 1m for GitRepository, 5m for Kustomizations
- **Webhook:** Enabled for immediate reconciliation on push to main

**Infrastructure-as-Code:**
- **Terraform:** Manages Proxmox VMs and templates
- **Template strategy:** Phase 1 creates Talos OS templates, Phase 2 clones VMs from templates
- **Automated updates:** Renovate detects Talos updates → GitHub Actions creates new templates → Terraform replaces nodes

**Applications Deployed:**
- Observability: Prometheus, Grafana, Loki
- Networking: Cilium, CoreDNS
- Storage: Rook-Ceph operators
- Security: External Secrets Operator, SOPS for secret encryption
- Media: Plex Media Server (DMZ VLAN 81)
- IoT: Home Assistant (IoT VLAN 62)
- CI/CD: Self-hosted GitHub Actions runners
- And more... (see `kubernetes/apps/` directory)

**Secrets Management:**
- **SOPS:** age-encrypted secrets in Git (*.sops.yaml files)
- **1Password:** ExternalSecrets pattern for application credentials
- **Age key:** `age1metxlry78wefrmm5ny2zjavtucsmdvw2r3ctexu6h05ak4x2vc7qa02drd` (public recipient)

---

### Appendix C: Historical Context - Security Incident (2025-11-07)

**Incident Summary:**

On November 7, 2025, the homelab-infra-architect agent accidentally committed 157 files from `.claude/.ai-docs/` directory to the public GitHub repository, including a Proxmox API token.

**Root Cause:**
1. `.gitignore` had negation rule that unintentionally allowed `.claude/.ai-docs/` to be tracked
2. Agent modified `.gitignore` without validating impact
3. **No security review was performed before push** (violation of documented policy)
4. Agent assumed changes were safe and pushed directly

**Impact:**
- Proxmox API token exposed in public repository (permanently accessible in Git history)
- 157 files of internal documentation exposed (infrastructure details, IP addresses, configuration secrets)
- Required immediate response: token revocation, Git history rewrite (30 commits), 3+ hours recovery time

**Remediation:**
- ✅ Fixed `.gitignore` to properly exclude `.claude/` directory
- ✅ Removed all 157 files from Git tracking (`git rm --cached`)
- ✅ Rewrote entire Git history (30 commits) to purge sensitive data
- ✅ Revoked exposed Proxmox API token
- ✅ Generated new API token with appropriate permissions
- ✅ Updated agent workflows and CLAUDE.md with mandatory security review requirements

**Lessons Learned:**
1. **Documentation-based rules are insufficient** - Agents violated "mandatory" security review because nothing prevented violations structurally
2. **.gitignore validation is critical** - Any .gitignore modification must be validated before commit
3. **Public repositories are unforgiving** - Once pushed, data is permanently accessible even if removed from current state
4. **Defense in depth is essential** - Need multiple security layers (pre-commit validation, agent review, post-commit scanning)

**Why This Matters for BMAD Adoption:**

This incident is the primary driver for Priority #1 (Mandatory Security Gate). The BMAD approach addresses root cause: instead of relying on agents to "remember" security reviews, task workflows make security-guardian delegation a **structural dependency** that physically cannot be skipped.

**Incident as Validation:**

The security incident validates the core BMAD principle: **structural enforcement prevents violations that documentation cannot**. This brief's entire approach stems from this hard-won lesson.

---

### Appendix D: References

**Project Documentation:**

- **Repository:** https://github.com/jlengelbrecht/prox-ops
- **Main Instructions:** `CLAUDE.md` (agent operating instructions)
- **Brainstorming Results:** `docs/brainstorming-session-results.md`
- **Agent Definitions:**
  - `.claude/agents/homelab-infra-architect.md`
  - `.claude/agents/security-guardian.md` (implied, referenced in CLAUDE.md)
  - `.claude/agents/cachyos-nix-specialist.md` (implied)
- **Historical Documentation:** `.claude/.ai-docs/` (377 files, various topics)

**BMAD Framework:**

- **BMAD Methodology:** (Reference BMAD documentation - exact URL not provided in context)
- **BMAD Core Installation:** `.bmad-core/` structure (to be created)
- **BMAD Agents:** Business Analyst (Mary), Product Manager, Scrum Master, Dev agents
- **Infrastructure-DevOps Expansion Pack:** (Referenced for Phase 2 agent enhancement)

**Technology Stack:**

- **Talos Linux:** https://www.talos.dev/ (immutable Kubernetes OS)
- **Flux v2:** https://fluxcd.io/ (GitOps continuous delivery)
- **Terraform:** https://www.terraform.io/ (Infrastructure-as-Code)
- **Proxmox Provider:** https://registry.terraform.io/providers/bpg/proxmox/latest/docs
- **Cilium:** https://cilium.io/ (eBPF-based networking and security)
- **Rook-Ceph:** https://rook.io/ (cloud-native storage)
- **SOPS:** https://github.com/getsops/sops (encrypted secrets)
- **External Secrets Operator:** https://external-secrets.io/ (Kubernetes secrets management)

**Community Resources:**

- **home-ops (onedr0p):** https://github.com/onedr0p/home-ops (reference homelab cluster using similar stack)
- **k8s-at-home:** https://k8s-at-home.com/ (homelab Kubernetes community)
- **awesome-home-kubernetes:** https://github.com/k8s-at-home/awesome-home-kubernetes (curated homelab resources)

**Development Tools:**

- **Claude Code:** https://code.claude.com/ (AI-powered development assistant)
- **CachyOS:** https://cachyos.org/ (Arch-based Linux distribution)
- **Nix Package Manager:** https://nixos.org/ (declarative package management)

**Security Resources:**

- **GitHub Secret Scanning:** https://docs.github.com/en/code-security/secret-scanning
- **SOPS Age Encryption:** https://github.com/FiloSottile/age
- **Kubernetes Pod Security Standards:** https://kubernetes.io/docs/concepts/security/pod-security-standards/
- **Cilium Network Policy:** https://docs.cilium.io/en/stable/security/policy/

**Related Work:**

- **GitOps Principles:** https://opengitops.dev/
- **Cattle vs Pets:** https://devops.stackexchange.com/questions/653/what-is-the-definition-of-cattle-not-pets (immutable infrastructure philosophy)
- **12-Factor App:** https://12factor.net/ (application design principles)

---

## Next Steps

### Immediate Actions (This Week)

**Action 1: Review and Approve Project Brief**

**Owner:** User (jlengelbrecht)
**Timeline:** 1-2 days
**Purpose:** Validate that this brief accurately captures project vision, current state, and BMAD adoption strategy

**Tasks:**
- Read complete brief thoroughly
- Verify assumptions and constraints are accurate
- Confirm MVP scope aligns with priorities
- Identify any gaps or corrections needed
- Provide explicit approval to proceed or request revisions

**Decision Gate:** GO/NO-GO for BMAD adoption. If NO-GO, iterate on brief or reconsider approach.

---

**Action 2: Install BMAD Core Structure**

**Owner:** User + Claude Code
**Timeline:** 1 day
**Purpose:** Establish foundational BMAD directory structure (additive, no impact to existing files)

**Tasks:**
1. Create `.bmad-core/` directory structure:
   ```bash
   mkdir -p .bmad-core/{tasks,templates,data}
   mkdir -p docs/{prd,architecture}
   mkdir -p .ai
   ```

2. Create `core-config.yaml`:
   ```yaml
   markdownExploder: true
   devStoryLocation: .claude/.ai-docs/stories
   devDebugLog: .ai/debug-log.md
   slashPrefix: BMad
   ```

3. Update `.gitignore`:
   ```
   # Temporary AI files (excluded from Git)
   .ai/

   # Ensure .claude/ is properly excluded
   .claude/
   !.claude/agents/

   # Protect secrets
   age.key
   *.pem
   github-deploy.key
   terraform.tfstate*
   ```

4. Validate `.gitignore` integrity:
   ```bash
   # Test that .ai/ and .claude/.ai-docs/ are excluded
   touch .ai/test.txt
   touch .claude/.ai-docs/test.txt
   git status  # Should NOT show these files
   rm .ai/test.txt .claude/.ai-docs/test.txt
   ```

5. Commit structure (security review first!):
   ```bash
   git add .bmad-core/ docs/ .gitignore
   # MANDATORY: Run security-guardian review before commit
   git commit -m "feat(bmad): initialize BMAD core structure

   - Add .bmad-core/ directory for tasks, templates, data
   - Add docs/ for managed deliverables (brief, PRD, architecture)
   - Add .ai/ for debug logs (excluded from Git)
   - Update .gitignore to exclude temporary files
   - Preserve all existing .claude/.ai-docs/ files (377 untouched)

   Relates to BMAD adoption - MVP Week 0"
   ```

**Success Criteria:**
- Directory structure created
- .gitignore updated and validated
- No existing files moved or deleted
- Security review completed before push
- Structure committed to feature branch

---

**Action 3: Create Pre-Approved Command Whitelist**

**Owner:** User + Claude Code
**Timeline:** 1 day
**Purpose:** Document trust boundaries to eliminate unnecessary approval prompts

**Tasks:**
1. Review current CLAUDE.md whitelist section
2. Validate categorization (safe vs risky operations)
3. Add any missing common commands
4. Create validation test plan:
   - Test deployment workflow
   - Count approval prompts before whitelist
   - Verify whitelist reduces prompts to <5
   - Ensure no risky operations auto-approved

5. Update CLAUDE.md with finalized whitelist
6. Commit changes (with security review)

**Success Criteria:**
- Whitelist documented in CLAUDE.md
- Test workflow shows <5 prompts (from baseline ~25)
- Zero risky operations in "always approved" category
- User comfortable with trust boundaries

---

**Action 4: Backup Current State**

**Owner:** User
**Timeline:** 1 hour
**Purpose:** Ensure rollback capability if BMAD adoption encounters issues

**Tasks:**
1. Backup Terraform state:
   ```bash
   cp terraform/terraform.tfstate terraform/terraform.tfstate.backup.$(date +%Y%m%d)
   ```

2. Backup age encryption key (if not already):
   ```bash
   # Ensure age.key is backed up to secure location (NOT in Git)
   # Verify backup is accessible and valid
   ```

3. Create Git tag for pre-BMAD state:
   ```bash
   git tag -a pre-bmad-adoption -m "State before BMAD adoption - 2025-11-15"
   git push origin pre-bmad-adoption
   ```

4. Document rollback procedure in `.bmad-core/ROLLBACK.md`:
   ```markdown
   # BMAD Rollback Procedure

   If BMAD adoption proves problematic:

   1. Revert to pre-BMAD state:
      git checkout pre-bmad-adoption

   2. Delete BMAD structure (if desired):
      rm -rf .bmad-core/ docs/ .ai/

   3. Restore CLAUDE.md:
      git checkout pre-bmad-adoption -- CLAUDE.md

   4. Validate cluster stability
   ```

**Success Criteria:**
- Terraform state backed up
- Age key backup verified
- Git tag created
- Rollback procedure documented

---

### MVP Kickoff (Week 1)

**Action 5: Create First BMAD Task - Security Gate**

**Owner:** homelab-infra-architect agent
**Timeline:** Week 1 (2-3 days)
**Purpose:** Implement Priority #1 - Mandatory Security Gate

**Tasks:**
1. Create `.bmad-core/tasks/create-pr-with-security.md`
2. Define workflow steps:
   - Stage changes
   - MANDATORY security-guardian delegation (elicit: true)
   - Security checklist execution
   - Commit after approval
   - Create PR
   - Post-merge validation
3. Create `.bmad-core/data/security-checklist.md` (validation criteria)
4. Test task execution with dummy PR
5. Validate security-guardian cannot be skipped
6. Update CLAUDE.md to reference task for all PR workflows

**Success Criteria:**
- Task file created and executes end-to-end
- Security-guardian invoked 100% of time (structural enforcement)
- Test PR blocks when security issues detected
- User can review/merge PR after security approval

**Validation Test:**
- Attempt to skip security review → Should fail
- Introduce plaintext secret → Security-guardian should detect
- Valid PR with encrypted secrets → Should pass

---

**Action 6: Create Deployment Task - Agent Delegation**

**Owner:** homelab-infra-architect agent
**Timeline:** Week 1 (2-3 days, parallel with Action 5)
**Purpose:** Implement Priority #3 - Structured Agent Delegation

**Tasks:**
1. Create `.bmad-core/tasks/deploy-new-app.md`
2. Define workflow:
   - Routes to homelab-infra-architect (via Task tool subagent_type)
   - Reads 3-NIC architecture checklist
   - Creates manifests (HelmRelease, NetworkPolicy, etc.)
   - Validates configuration
   - Triggers create-pr-with-security task
3. Create `.bmad-core/data/3-nic-architecture.md` (network design checklist)
4. Test with simple app (echo server)

**Success Criteria:**
- Task routes to homelab-infra-architect automatically
- 3-NIC checklist referenced during deployment
- Manifests created following standards
- Security gate triggered at end of workflow
- Test deployment succeeds end-to-end

---

**Action 7: Validate Context Management**

**Owner:** User + Claude Code
**Timeline:** Week 1 (ongoing during Actions 5-6)
**Purpose:** Address Open Question #1 and Risk #1 - ensure BMAD tasks work within Claude Code constraints

**Tasks:**
1. Monitor context window usage during task execution
2. Test longest anticipated workflow (full app deployment)
3. Verify TodoWrite maintains state across steps
4. Check debug log for decision persistence
5. Document any context issues encountered
6. Implement mitigation if needed (workflow chunking, state files)

**Success Criteria:**
- Full deployment workflow completes without context loss
- Todo tracking maintains state throughout
- Debug log captures all decisions
- No workflow steps forgotten or skipped

---

### Week 2-4: MVP Core Implementation

**Action 8: Create Remaining Core Tasks**

**Owner:** homelab-infra-architect agent
**Timeline:** Weeks 2-3

**Tasks to Create:**
1. `.bmad-core/tasks/troubleshoot-app-issue.md` (Week 2)
   - Routes to homelab-infra-architect
   - Reads app docs FIRST (OVERVIEW.md, ACCESS_POLICY.md)
   - Diagnoses and fixes issues
   - Triggers PR workflow

2. `.bmad-core/tasks/install-system-dependency.md` (Week 2)
   - Routes to cachyos-nix-specialist
   - Updates Nix flake
   - Validates installation

3. `.bmad-core/tasks/plan-new-feature.md` (Week 3)
   - Routes to PM agent → Scrum Master agent
   - Creates epic and story documents
   - Assigns work items

**Success Criteria:**
- All 5 core tasks created and tested
- Each task demonstrates deterministic agent routing
- Real-world scenarios validated (not just hello-world tests)

---

**Action 9: Deploy Test Application Using BMAD Workflow**

**Owner:** User
**Timeline:** Week 3-4
**Purpose:** Validate end-to-end MVP functionality with real deployment

**Tasks:**
1. Select test app (echo server or similar low-risk app)
2. Execute deploy-new-app task
3. Track approval prompts (target: <5)
4. Verify security gate executes automatically
5. Review and merge PR
6. Validate post-merge deployment
7. Verify documentation created (OVERVIEW.md, ACCESS_POLICY.md)

**Success Criteria:**
- Complete deployment with <5 user interactions
- Security review occurred automatically (no manual reminder)
- App deployed successfully via Flux
- Documentation generated in correct locations
- User experience matches "walk away for coffee" vision

---

**Action 10: Deploy Second Test Application**

**Owner:** User
**Timeline:** Week 4
**Purpose:** Prove repeatability and sustainability

**Tasks:**
1. Deploy different app type (different namespace, network tier, etc.)
2. Validate workflow improvements from first deployment applied
3. Track KPIs (approval prompts, security compliance, time to deploy)
4. Compare to baseline (pre-BMAD deployment metrics)

**Success Criteria:**
- Second deployment faster than first (learning curve)
- Same workflow quality (proves consistency)
- KPIs trending toward targets

---

### Week 5-6: MVP Validation and Stabilization

**Action 11: Sustained Compliance Tracking**

**Owner:** User
**Timeline:** Weeks 5-6 (continuous)
**Purpose:** Validate MVP success criteria (2 weeks sustained compliance)

**Track Weekly:**
- Security gate compliance: 100% of PRs with security review?
- Approval prompt density: <5 per deployment?
- Agent delegation accuracy: 100% correct routing?
- Documentation compliance: All apps have required docs?
- Time to deploy: Meeting 35-minute target?

**Success Criteria:**
- 2 consecutive weeks meeting all KPI targets
- Zero security violations
- User confidence in autonomous operation

---

**Action 12: MVP Retrospective**

**Owner:** User + Claude Code
**Timeline:** End of Week 6
**Purpose:** Honest assessment of BMAD MVP outcomes

**Questions to Answer:**
1. Did security gate prevent violations? (100% compliance achieved?)
2. Did pre-approved whitelist reduce supervision burden? (40-60% time savings?)
3. Is agent delegation deterministic? (Hit or miss resolved?)
4. Can user "walk away for coffee"? (Autonomous operation validated?)
5. Are benefits worth continued investment? (Proceed to Phase 2?)

**Outputs:**
- Written retrospective document (`.bmad-core/MVP-RETROSPECTIVE.md`)
- Lessons learned and adjustments needed
- Explicit GO/NO-GO decision for Phase 2
- Updated timeline if needed

**Decision Gate:** GO/NO-GO for Phase 2. If GO, proceed to Action 13. If NO-GO, stabilize at MVP or execute rollback.

---

### Phase 2 Handoff (Week 7+)

**Action 13: Initiate Phase 2 Planning**

**Owner:** Product Manager agent (if GO decision from Action 12)
**Timeline:** Week 7
**Purpose:** Plan Phase 2 implementation (template library, agent enhancement, documentation cleanup)

**Tasks:**
1. Review MVP retrospective and lessons learned
2. Re-prioritize Phase 2 features based on MVP experience
3. Create epic document for Phase 2 (`.claude/.ai-docs/epics/EPIC-002-bmad-phase-2.md`)
4. Break into stories with agent assignments
5. Establish Phase 2 timeline (target: 6-12 weeks)

**Handoff Deliverables:**
- Epic document defining Phase 2 scope
- Story breakdown with work items
- Updated project roadmap

---

### PM Handoff (Alternative Path)

**Action 14: Create PRD from Project Brief** _(If user wants formal PRD)_

**Owner:** Product Manager agent
**Timeline:** After brief approval
**Purpose:** Convert this brief into formal Product Requirements Document

**Context:** This Project Brief provides comprehensive foundation. PM agent can use it to generate detailed PRD covering:
- User stories and acceptance criteria
- Detailed feature specifications
- UI/UX considerations (for documentation, CLI outputs)
- API contracts (between agents, tasks, Git)
- Non-functional requirements (performance, security, reliability)

**Handoff Message:**

_"This Project Brief provides the full context for prox-ops BMAD adoption. Please start in 'PRD Generation Mode', review the brief thoroughly to work with the user to create the PRD section by section as the template indicates, asking for any necessary clarification or suggesting improvements."_

**Decision Point:** User decides whether PRD needed before MVP implementation, or proceed directly to Action 2 (BMAD installation).

---

**Success Indicators:**

If these actions complete successfully, the prox-ops homelab will have:
- ✅ Mandatory security gates preventing violations
- ✅ Pre-approved commands enabling autonomous operation
- ✅ Deterministic agent delegation (consistent quality)
- ✅ Structured documentation (no more vibe coding)
- ✅ User confidence to delegate and disengage
- ✅ Foundation for Phase 2 expansion

**Final Note:**

This brief represents a comprehensive plan for transforming prox-ops workflow from ad-hoc "vibe coding" to structured, autonomous operation. The path forward is clear, risks are identified, and success criteria are measurable.

**The next move is yours.** Review this brief, approve (or request changes), and let's begin the BMAD journey.

---

**End of Project Brief**
