# agent-router model & capability catalog

Schema document for `catalog-configmap.yaml` (ConfigMap `ai/agent-router-catalog`,
key `catalog.yaml`). EPIC-035 section 6, story 35.1.

This file is the contract. If the data and this document disagree, that is a bug in one
of them, not a judgement call for the reader.

## Inert

Nothing reads this ConfigMap. There is no Deployment, no volume mount, no `envFrom`, no
workload of any kind in this Kustomization, and no other manifest in `kubernetes/`
references the name `agent-router-catalog`. `consumers: []` in the data asserts this
explicitly so a future reader can tell "nothing uses it yet" from "somebody forgot to
write it down".

The consumer arrives in story 35.9 (`agent-router`). Until then the value of this file is
that the schema can be argued about while it is still free to change.

## Why the profile indirection exists

Three axes, kept apart on purpose (invariant 1):

| axis | what it answers | table |
| --- | --- | --- |
| harness | which agent runs the work | `harnesses` |
| model / capability profile | what class of model it needs | `profiles` |
| placement | which physical GPU serves it | `placements` |

BMAD stamps a **profile name**. The profile name is stable; the model behind it is data.
Swapping `local-code-standard` from one model to another is a pull request against
`catalog-configmap.yaml` and touches nothing else - no routing policy, no story rewrite,
no BMAD edit. Any change that makes a profile name mean "one specific model" defeats the
whole structure.

## Tables

`harnesses`, `profiles`, `placements` and `policies` are the tables EPIC-035 section 6
specifies. `models` is a fifth, added here for one reason: section 6 defines a physical
candidate as exactly `{model_id, placement}`, so `model_id` needs a referent, and a model
that appears in two profiles must not be able to describe itself differently in each.
`physical[]` entries stay exactly two keys wide, as specified.

`entitlement_pools` is a sixth, added in 1.1.0 (owner ruling R17). A profile said what it
cost but never said **where the money came from**, which is not the same question and
cannot be derived from the first one. Without it, a consumer works out economics by
inference - from the provider, from the harness, or worst of all from what the credential
looks like - and every one of those inferences is wrong for at least one row in this
catalog.

Presentation order in the file is referents-before-referrers (`placements`, `models`,
`entitlement_pools`, then `profiles`), which is not the order section 6 lists them in.
YAML mappings are unordered; nothing reads meaning from it.

### Absent keys

An absent key and an explicit `null` mean the same thing to a consumer. A field marked
`required` in the tables below is always present; a field marked `required when X` is
present exactly when X holds; everything else is optional and reads as `null` when
missing.

Fields that only make sense for one kind of row are omitted on the rows where they do not
apply rather than carried as `null` padding - `upstream_id_form`, `runtime`,
`vram_gb_estimate`, `vram_estimate_source` and `idle_retention_min` on a vendor model,
`resolved_by` on a local one. The `notes` column says which rows each field applies to.

### `harnesses`

Keyed by harness id. Deliberately thin - a harness entry says whether the axis value
exists and whether a router may emit it, and nothing else, so that no harness field can
quietly carry a model or a placement.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `supported` | bool | required. Is this a harness this project runs at all? |
| `selectable` | bool | required. May routing policy emit it? |
| `reason` | string | required when `supported: false` |
| `router_behaviour` | string | required when `selectable: false`. Currently only `refuse-to-emit` |

`claude`, `codex` and `devin` are supported and selectable. `local-agent` is
`supported: false` / `selectable: false` and must stay that way: invariant 16 reserves the
value so the axis is future-proof, and a router that emits it is wrong and must refuse
loudly rather than degrade. llama.cpp, vLLM and llama-swap are inference runtime, not
harnesses. Making `local-agent` selectable requires its own story evaluating a specific
harness. No story in EPIC-035 does that.

### `placements`

Keyed by placement name. **Placement names are public identifiers**: 35.5 froze that a
placement name appears verbatim as an agentgateway provider `name` and as the value of
the `x-placement` request header. Renaming one is a breaking change across this catalog,
the gateway configuration and every caller. Adding a placement is a Git change by design;
choosing among declared placements at runtime is free.

`x-placement` is a scheduling signal only (owner ruling R5). It is never an authorization
input, and nothing in this catalog expresses authorization.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `status` | enum | `available` \| `planned` |
| `selectable` | bool | must be `false` while `status: planned` |
| `kind` | enum | `kserve` \| `edge` |
| `node` | string \| null | `null` when the host is not yet named |
| `gpu.vendor` | enum \| null | `nvidia` \| `amd` |
| `gpu.model` | string \| null | `null` when the SKU is not established |
| `gpu.arch` | string \| null | optional |
| `capacity.vram_gb` | number \| null | see below |
| `capacity.vram_gb_nameplate` | number \| null | see below |
| `capacity.source` | enum | `measured` \| `nameplate` \| `unmeasured` |
| `runtime` | string \| null | |
| `preemptible` | bool | can a human take the GPU away mid-request? |
| `scale_to_zero` | bool \| null | |
| `concurrent_models` | int \| null | how many models fit at once |
| `cold_start_s_estimate` | number \| null | |
| `cold_start_source` | enum | `measured` \| `unmeasured` \| `n-a` |
| `cold_start_evidence` | string | required when `cold_start_source: measured` |
| `blocked_by` | list of story ids | required when `status: planned` |
| `notes` | string | optional |

**Capacity numbers are sourced, not assumed.** `capacity.vram_gb` only ever holds a figure
this repository can stand behind; `null` means no measurement exists.
`capacity.vram_gb_nameplate` holds the vendor SKU figure and is informational.

> A consumer MUST NOT satisfy `min_vram_gb` or `min_free_vram_gb` from a null
> `capacity.vram_gb`, and MUST NOT substitute `vram_gb_nameplate` for it.

That rule is what makes it safe to declare planned hardware at all. `kserve-a5000` is the
only placement with a measured figure today, so it is the only one a size constraint can
select. `laptop-rtx5000` carries a null nameplate as well: "RTX 5000" is ambiguous across
generations, no SKU has been established, and hardware detection is task 1 of story 35.14.

### `models`

Keyed by `model_id`. For local models the key is the **gateway-facing alias** - the string
a client puts in the request body and in `x-model`, and the literal the per-model CEL
authorization policy compares against. `upstream_model_id` is the different string
agentgateway sends to the upstream server.

Both are copied verbatim from
`kubernetes/apps/ai/agentgateway-pilot/app/local-model-backends.yaml`
(`metadata.name` and `spec.ai.provider.openai.model` respectively). Neither is invented.
Swapping them breaks authorization in one direction and upstream resolution in the other:

- a client that sends `/models/Qwen/Qwen2.5-Coder-7B-Instruct` as its body model is
  rejected by the CEL policy, which expects `qwen-coder`;
- vLLM identifies these models by the filesystem path it was started with (no
  `--served-model-name`), so the leading `/models/...` has to reach it verbatim.

LiteLLM's legacy catalogue writes the same upstream identifiers with an `openai/` provider
prefix. That prefix is LiteLLM's, not the model's, and is not used here.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `hosting` | enum | required. `local` \| `vendor` |
| `upstream_model_id` | string \| null | required. `null` for vendor models |
| `upstream_id_form` | string | local models only. How the upstream derives `upstream_model_id`; absent on vendor models, which have no upstream id |
| `resolved_by` | enum | vendor models only. Always `harness` today; absent on local models |
| `runtime` | string \| null | local models only |
| `placements` | list | required. Placement names this model is deployed on; `[]` for vendor |
| `capabilities` | list | required. `chat` \| `tools` \| `vision` \| `audio` |
| `max_context` | int \| null | required. Tokens; `null` means unknown |
| `max_context_source` | enum | required when `max_context` is `null` |
| `alignment` | enum | required. `standard` \| `unrestricted` |
| `vram_gb_estimate` | number \| null | local models only. Working-set estimate |
| `vram_estimate_source` | string | required when `vram_gb_estimate` is set |
| `cold_start_s_estimate` | number \| null | required |
| `cold_start_source` | enum | required. `measured` \| `unmeasured` \| `n-a` |
| `idle_retention_min` | int \| null | local models only. Scale-to-zero retention |
| `source` | string | required. Where the identifiers came from |

A vendor model is resolved by its harness against a subscription, so there is no upstream
server, no local runtime and no GPU footprint to describe: `upstream_id_form`, `runtime`,
`vram_gb_estimate`, `vram_estimate_source` and `idle_retention_min` are absent on all four
rather than set to `null`.

`vision` and `audio` on `qwen-omni` are **input** modalities. Audio output is a separate
service (`qwen-tts`) and is out of scope here.

`tools` is set only where the deployment actually enables tool calling: `hermes-jarvis`
(`--enable-auto-tool-choice --tool-call-parser=hermes`) and `qwen36-27b` (`--jinja` with a
tool-calling chat template). `qwen-coder`, `dolphin-chat` and `qwen-omni` do not have it,
so they do not claim it.

### `entitlement_pools`

Keyed by pool name. A pool is a **funding source**: an entitlement the operator holds, and
the credential used to draw on it.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `provider` | string | required. Who is on the other end |
| `cost_class` | enum | required. `free` \| `subscription` \| `metered`. The economics |
| `credential_class` | string | required. The **shape and provisioning** of the secret. Never an input to a cost decision |
| `credential_notes` | string | optional. Isolation and provisioning rules that a consumer must honour |
| `spillover` | enum | required. `none` on every seeded pool. What happens when the entitlement runs out |
| `spillover_notes` | string | optional |

#### Billing class and credential type are independent axes

This is the rule the table exists to enforce, and it is worth stating in the negative
because the opposite heuristic is already embedded in tooling elsewhere in this estate:

> **An API-key-shaped credential does not imply `cost_class: metered`.** Read economics
> from the entitlement, never from the shape of the secret, the name of the provider, or
> the harness that happens to be running.

The seeded pools make the point on their own. `anthropic-max` and `minimax-max` are both
`cost_class: subscription`, and their credentials could not look less alike - one is a
signed-in subscription session, the other a provider-issued API key. MiniMax's Max Token
Plan is a **fixed monthly subscription** that happens to be reached with a key. A consumer
that pattern-matches on the key would bill a flat subscription as per-token spend, and
would refuse work the operator has already paid for.

The same independence runs the other way: `devin-free` is `cost_class: free` and is not a
subscription at all, which is why this abstraction is called an **entitlement** pool
rather than a subscription pool. A future explicitly-authorized pay-as-you-go source is a
third shape again, and has to fit the same table without special-casing.

#### Spillover, and invariant 4

`spillover: none` on every seeded pool is not a default that happened to be picked. It is
invariant 4 written as data: **exhausting an entitlement never promotes to billable
spend.** When a pool is exhausted or unavailable it simply stops being a candidate, and
routing falls back to another approved pool, to free, or to local compute.

Specifically for MiniMax: Token Plan exhaustion **must never** spill into MiniMax
pay-as-you-go. Those are two different credential classes for two different entitlements,
and holding one grants nothing about the other. A pay-as-you-go path would need all three
of a separately provisioned metered credential, `allow_metered` intent on the request, and
independent metered-spend authorization held by the calling principal - which the ordinary
automation principal does not have. Two of the three is a refusal.

### `profiles`

Keyed by profile name. This is the only name BMAD stamps.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `cost_class` | enum | `free` \| `subscription` \| `metered` |
| `hosting` | enum | `local` \| `vendor` |
| `selectable` | bool | |
| `capabilities` | list | the floor this profile **guarantees** |
| `min_context` | int \| null | the context floor this profile **guarantees** |
| `alignment` | enum | `standard` \| `unrestricted` |
| `forbidden_for` | list | task tags this profile is never auto-selected for |
| `entitlements` | ordered list of `{pool, cost_class}` | required. Funding candidates, first is the default |
| `physical` | ordered list of `{model_id, placement}` | first entry is preferred |
| `blocked_by` | list | required when `selectable: false`; what has to be true before it flips |
| `notes` | string | optional |

Semantics that are easy to get wrong:

- **`capabilities` is a guarantee, not a union.** Every candidate in `physical[]` provides
  at least these. Individual candidates may provide more - `local-general` guarantees
  `chat` because `qwen-omni` has no tool calling, even though two of its three candidates
  do. A consumer that needs an optional capability filters `physical[]` against
  `models[].capabilities` instead of assuming the whole list has it.
- **`min_context` is a guarantee too**, equal to the minimum `max_context` across
  `physical[]`. `null` means no guarantee, and a consumer must not use a null-guarantee
  profile to satisfy an explicit context requirement.
- **`physical[]` order is the only preference expression.** There are no weights.
- **`entitlements[]` is the funding axis, and it is a LIST for a reason.** A profile is not
  assumed to have exactly one funding source. The first entry is the default; later entries
  are alternatives that are reachable only under the conditions they declare. That is what
  lets `minimax/strong` normally draw on `minimax-max` while a future `minimax-payg` entry
  sits behind `requires: [allow_metered, metered-spend-authorization]` - one profile, one
  model, one harness, two funding candidates, and no axis collapsed to express it. The
  shape is shown commented out on `minimax/strong` rather than declared as data, because
  no pay-as-you-go pool exists and declaring one would make it look approved.
- **`profiles[].cost_class` is the cost class of the FIRST entitlement**, kept as its own
  field because it is what a consumer reads when it only wants the default economics. It is
  derived, not independent: the two disagreeing is a catalog bug (validation rule 19).
- **`pool: null` is correct for local execution**, not a gap. Local compute draws on no
  entitlement: the GPU is already paid for.
- **`placement: null`** is correct for vendor-hosted candidates, not a gap. Placement names
  are agentgateway provider names; vendor traffic never touches agentgateway, so it has no
  placement. A local model with a null placement is an error.
- **`forbidden_for` is a hard exclusion**, not a score or a tie-break. A request carrying
  any listed tag must never be auto-selected onto that profile, however well it otherwise
  fits. An operator can still choose it explicitly and own the choice.

`alignment: unrestricted` is an **alignment property, not a quality tier** (invariant 12).
`local-unrestricted` is a weaker model than `local-code-standard` in every respect except
refusal behaviour. Its `forbidden_for` list - `security`, `iam`, `secrets`, `prod-iac`,
`destructive-tools` - is a minimum, extensible by catalog PR.

#### Vendor models

`claude/strong`, `openai/strong`, `devin/free` and `minimax/strong` describe work the
harness does through its own entitlement. The router does not proxy, place, or hold
credentials for any of it.
Their `min_context` is `null` because this repository has measured nothing about vendor
context windows and will not carry a guessed number. A real value gets in here from the
agent-flow-kit harness registry or from the 35.16-35.18 verdicts, as a catalog PR that
also records where the number came from.

Two invariants to keep in view when editing these:

- Invariant 10 - subscriptions are not API keys. Nothing here authorises introducing
  `ANTHROPIC_API_KEY` or centralising a personal OAuth token as a provider credential. Gap
  G9 and stories 35.16-35.18 concern proxying subscription traffic through cluster ingress,
  which these profiles do not do.
- Invariant 4 - metered API is default-deny. No seeded profile has
  `cost_class: metered`; Devin's paid on-demand tier deliberately has no profile name to
  hide behind. Billable use stays explicit, per-attempt and recorded.

#### `minimax/strong`, and why MiniMax is not a harness

`minimax/strong` is declared in 1.1.0 and is `selectable: false`. The name is fixed now so
that this catalog and `contracts/agent-router/` can land together; making it selectable is
a later story's catalog PR, once it has demonstrated everything in `blocked_by` - Token
Plan billing rather than pay-as-you-go, Claude Code driving MiniMax M3 end to end,
verifiable provider and model identity, tool calling, MCP compatibility, isolation from the
normal Claude Max environment, observable quota behaviour, no automatic pay-as-you-go
spillover, and correct fallback when MiniMax capacity is unavailable.

**MiniMax gets no `harnesses` entry.** The execution path is `harness: claude` +
`model_profile: minimax/strong`, because Claude Code reaches MiniMax through its
Anthropic-compatible endpoint. That is the harness/model separation this catalog exists to
preserve: a new provider is a new model and possibly a new funding pool, not automatically
a new harness. MiniMax Code is a different harness, is not approved, and would need its own
evaluation story.

**Credential isolation is part of the contract.** The MiniMax base URL and token reach a
MiniMax-backed worker in that worker's own process or session only. Nothing rewrites the
machine's global Anthropic configuration; the Anthropic Max path introduces no
`ANTHROPIC_API_KEY`; the credential lives in 1Password scoped to MiniMax-backed worker
execution, and never in Git.

### `policies`

`policies.placement_policy` is keyed by policy name. The nesting leaves room for other
policy kinds without renaming anything.

| field | type | notes |
| --- | --- | --- |
| `description` | string | required |
| `prefer_order` | ordered list | placement names, best first |
| `warm_preference` | enum | `strict` \| `weighted` \| `ignored` |
| `warm_bonus_rank_shift` | int \| null | required when `weighted`, else `null` |
| `allow_cold_start` | bool | `false` forbids waking a scaled-to-zero placement |
| `min_vram_gb` | number \| null | hard floor on `capacity.vram_gb` |
| `min_free_vram_gb` | number \| null | hard floor on heartbeat free VRAM |
| `resolves_today` | bool | derived; see below |

- `strict` means any warm candidate beats any cold candidate regardless of `prefer_order`.
  `weighted` moves a warm candidate up `warm_bonus_rank_shift` positions. `ignored` means
  warmth is not a factor.
- `warm_bonus_rank_shift: 1` is **provisional**. Ruling R1 leaves the real cost of a cold
  start to story 35.7's measurements; the value here is a placeholder with defined
  semantics, not a tuned number.
- `allow_cold_start` exists because invariant 7 makes "do not wake the A5000" a real
  requirement. Nothing needs it today; every seeded policy sets `true`, matching current
  behaviour.
- `min_free_vram_gb` is `null` everywhere. Per-model footprints live in
  `models[].vram_gb_estimate`; the headroom a placement actually needs is 35.7/35.11 work
  and is not invented here.

#### Empty candidate sets

`resolves_today` records whether a policy can select anything given today's placement
statuses and constraints. `edge-only` is `false`: every placement it names is
`status: planned`.

> A policy that resolves to nothing MUST produce an explicit empty placement result. It
> must never silently fall through to a placement the caller did not ask for.

This matters more than it looks. 35.5 measured that a route rule pinned at a
single-provider backend returns 503 when that provider is down (3/3), and that the
gateway-side safety net only exists if every rule terminates in a grouped backend. A
router that quietly substituted a different placement would hide exactly the condition the
operator asked it to respect.

## Validation rules

Mechanically checkable. A reviewer is expected to check them; they were verified by an
ad-hoc script when this catalog was seeded.

1. Every `profiles[].physical[].model_id` exists in `models`.
2. Every `physical[]` entry has exactly the keys `model_id` and `placement`.
3. Every non-null `physical[].placement` exists in `placements`, and appears in that
   model's own `placements` list.
4. A null `physical[].placement` occurs only on a model with `hosting: vendor`.
5. Every capability in `profiles[].capabilities` is provided by every candidate, and the
   set equals the intersection of the candidates' capabilities.
6. `profiles[].min_context`, when not null, equals the minimum `max_context` across the
   profile's candidates, and no candidate falls below it.
7. No `alignment: unrestricted` model appears in an `alignment: standard` profile.
8. An `alignment: unrestricted` profile has a non-empty `forbidden_for`, and
   `local-unrestricted` covers at least `security`, `iam`, `secrets`, `prod-iac`,
   `destructive-tools`.
9. Every placement named in a `prefer_order` exists, and `resolves_today` agrees with the
   placement statuses and `min_vram_gb`.
10. `warm_preference: weighted` implies a non-null `warm_bonus_rank_shift`.
11. `defaults.placement_policy` names a defined policy.
12. `harnesses.local-agent` is `supported: false` and `selectable: false`; `claude`,
    `codex` and `devin` are both true.
13. A placement with `capacity.source != measured` has `capacity.vram_gb: null`.
14. A placement with `status: planned` has `selectable: false` and a non-empty
    `blocked_by`.
15. Every model currently routed through the gateway appears in at least one profile.
16. `consumers` is empty for as long as this catalog is inert.
17. Every `profiles[].entitlements[].pool` is either `null` or a key in
    `entitlement_pools`.
18. Every `entitlement_pools[]` entry declares all four of `provider`, `cost_class`,
    `credential_class` and `spillover`. A pool that omits one forces a consumer to infer
    it, which is the failure the table exists to prevent.
19. `profiles[].cost_class` equals the `cost_class` of the profile's FIRST
    `entitlements[]` entry.
20. A `hosting: local` profile has `pool: null` on every entitlement; a `hosting: vendor`
    profile has a non-null pool on every entitlement.
21. Any entitlement with `cost_class: metered` declares `requires` containing both
    `allow_metered` and `metered-spend-authorization`. No seeded entitlement is metered.
22. A profile with `selectable: false` has a non-empty `blocked_by`.
23. Every seeded pool has `spillover: none`. Anything else is invariant 4 leaking.

## Versioning

`version` is the catalog document version (semver) and `schema_version` is the structural
version. They move independently:

1.1.0 was exactly this shape: a new table, a new per-profile field, a new model and a new
non-selectable profile. Additive, nothing removed or re-typed, so `schema_version` stayed
1 - a consumer written against 1.0.0 still parses this document, it just cannot see the
funding axis.

- data-only change (a model swapped behind a profile, a measurement filled in): patch or
  minor `version` bump, `schema_version` unchanged;
- new optional field: minor `version` bump, `schema_version` unchanged;
- removed or re-typed field, or changed field semantics: major `version` bump **and**
  `schema_version` bump.

A consumer must refuse a `schema_version` major it does not implement rather than
best-effort parsing an unknown shape.

`catalog_version` in the `/v1/route` response (EPIC-035 section 4) is a content hash the
router computes over the rendered document at load time. It is not this field and is not
written by hand.

## Change protocol

Changing which physical model backs a profile is a pull request against
`catalog-configmap.yaml`. Never a BMAD policy edit, never a story rewrite, never a change
to a routing rule.

Every change must bump `version`, update `updated`, keep all validation rules above true,
and say in the PR body what a consumer would do differently afterwards.

Adding a placement is a Git change by design (section 10a). Renaming one is breaking.

## Deliberately absent

- `timeouts.request` and eviction durations. The 240 s in use today is provisional
  (ruling R1); 35.7 measures cold and warm for real and 35.8 sets final values. Timeouts
  are gateway configuration and are not duplicated here.
- Endpoints, hostnames, ports and credentials. agentgateway is the only model ingress
  (invariant 5), and endpoint state comes from the heartbeat contract at runtime, never
  from Git. That is also why no placement carries an `ingress` field: the value would be
  `agentgateway` on every row, so it would record a decision already made once here rather
  than anything a consumer could act on.
- Anything that collapses harness, profile and placement into one field (invariant 1).
- Any authorization semantics (ruling R5).

## Open items for later stories

| item | owner |
| --- | --- |
| Vendor `max_context` values, with provenance | 35.16-35.18, then a catalog PR |
| Real cold/warm distributions; `warm_bonus_rank_shift` and any `min_free_vram_gb` | 35.7, 35.11 |
| Measured `capacity.vram_gb` for the edge placements | 35.6, 35.13, 35.14 |
| Whether harness-to-profile reachability needs a field (a cloud harness reaching a LAN-only gateway is unproven) | 35.10 |
| Whether the validation rules above become an executable check in CI | 35.9 |
| Physical validation of `minimax/strong` against everything in its `blocked_by`, then a catalog PR flipping `selectable` and adding any capability it demonstrates | its own story |
| Whether a pay-as-you-go entitlement pool is ever wanted, and under whose authorization | not scheduled; needs an owner decision, not a catalog PR |

## Sources

- `.claude/.ai-docs/epics/EPIC-035-unified-agent-control-plane.md`, sections 4, 5, 6, 10a,
  10b, and the invariants in section 3
- `kubernetes/apps/ai/agentgateway-pilot/app/local-model-backends.yaml` - model
  identifiers, both forms
- `kubernetes/apps/ai/kserve/app/*-inferenceservice.yaml` - context windows, tool-calling
  flags, VRAM and cold-start records, scale-to-zero retention
