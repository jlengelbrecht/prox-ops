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

Presentation order in the file is referents-before-referrers (`placements`, `models`,
then `profiles`), which is not the order section 6 lists them in. YAML mappings are
unordered; nothing reads meaning from it.

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
| `hosting` | enum | `local` \| `vendor` |
| `upstream_model_id` | string \| null | `null` for vendor models |
| `upstream_id_form` | string | how the upstream derives it |
| `resolved_by` | enum | `harness`, for vendor models |
| `runtime` | string \| null | |
| `placements` | list | placement names this model is deployed on; `[]` for vendor |
| `capabilities` | list | `chat` \| `tools` \| `vision` \| `audio` |
| `max_context` | int \| null | tokens; `null` means unknown |
| `max_context_source` | enum | required when `max_context: null` |
| `alignment` | enum | `standard` \| `unrestricted` |
| `vram_gb_estimate` | number \| null | working-set estimate |
| `vram_estimate_source` | string | required when `vram_gb_estimate` is set |
| `cold_start_s_estimate` | number \| null | |
| `cold_start_source` | enum | `measured` \| `unmeasured` \| `n-a` |
| `idle_retention_min` | int \| null | scale-to-zero retention |
| `source` | string | where the identifiers came from |

`vision` and `audio` on `qwen-omni` are **input** modalities. Audio output is a separate
service (`qwen-tts`) and is out of scope here.

`tools` is set only where the deployment actually enables tool calling: `hermes-jarvis`
(`--enable-auto-tool-choice --tool-call-parser=hermes`) and `qwen36-27b` (`--jinja` with a
tool-calling chat template). `qwen-coder`, `dolphin-chat` and `qwen-omni` do not have it,
so they do not claim it.

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
| `physical` | ordered list of `{model_id, placement}` | first entry is preferred |
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

`claude/strong`, `openai/strong` and `devin/free` describe work the harness does through
its own subscription. The router does not proxy, place, or hold credentials for any of it.
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

## Versioning

`version` is the catalog document version (semver) and `schema_version` is the structural
version. They move independently:

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
  from Git.
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

## Sources

- `.claude/.ai-docs/epics/EPIC-035-unified-agent-control-plane.md`, sections 4, 5, 6, 10a,
  10b, and the invariants in section 3
- `kubernetes/apps/ai/agentgateway-pilot/app/local-model-backends.yaml` - model
  identifiers, both forms
- `kubernetes/apps/ai/kserve/app/*-inferenceservice.yaml` - context windows, tool-calling
  flags, VRAM and cold-start records, scale-to-zero retention
