# agent-router API contract v1

Specification for the `agent-router` control plane: OpenAPI 3.1 plus JSON Schemas for every
payload, with a worked example for each endpoint and each error class.

**There is no implementation.** No service, no Deployment, no container, no handler code -
this directory is documents only. That is the point: the contract is frozen before an
implementation exists to bias it. The service arrives in stories 35.9 (`/v1/status` +
heartbeat), 35.10 (`/v1/route`) and 35.11 (`/v1/place`); story 35.12 freezes the final
wording against what those three learn.

Story 35.2 of EPIC-035. Sources at the bottom.

## Layout

```text
contracts/agent-router/
  openapi.yaml                      OpenAPI 3.1 - the four endpoints, request shapes, errors
  ADE-BOUNDARY.md                   who owns what: Orca (the ADE) vs the router
  schemas/
    execution-profile.schema.json   POST /v1/route response
    heartbeat.schema.json           POST /v1/capacity/heartbeat request
    place-result.schema.json        POST /v1/place response
    error.schema.json               the error envelope, every endpoint
    status.schema.json              GET /v1/status response
  examples/
    execution-profile/  4 files     incl. the metered refusal
    heartbeat/          6 files     one per state, plus unmeasured hardware
    place/              4 files     placed and unavailable
    errors/            14 files     one per error code, plus both metered refusals
    status/             3 files     today, before the edge existed, and after a restart
  verify-digests.sh                 fails if an example's catalog fingerprint has rotted
  verify-placement-cases.sh         fails if the catalog stops yielding the frozen table
  verify-ade-boundary.sh            fails if the /v1/route contract grows session mechanics
  README.md                         this file
```

`error.schema.json` and `status.schema.json` are additions to the layout the story asked
for. Both exist for the same reason: without them the error examples and the `/v1/status`
example would be illustrations that nothing checks, and an error taxonomy that only lives in
prose drifts from the code that has to implement it.

The schema files are the single source of truth for the payload shapes. `openapi.yaml`
references them rather than restating them, so the two cannot disagree.

## Non-goals

Stated explicitly, because each of these is a thing a reasonable implementer might otherwise
add:

- **`agent-router` never proxies inference.** No request body containing a prompt ever
  reaches it.
- **It never sits in the request path.** It is called at planning time and at dispatch time.
  A client calling it per inference request is using it wrongly and should cache the
  placement for its `ttl_seconds`.
- **It never mutates Flux-managed gateway configuration.** Gateway topology - backends,
  routes, provider hostnames - is Git-managed and stays that way.
- **`/v1/place` reconfigures nothing.** It returns a name. The caller sets a header. No CR is
  written, no route is rewritten, no backend is created.
- **No CRD.** Catalog data lives in a Git-managed ConfigMap until a concrete requirement
  forces otherwise, and that requirement gets written down when it appears.
- **No endpoint is ever returned to a caller.** See R9 below - this one is a security
  boundary, not a design preference.
- **No persistence.** Capacity is in-memory; a restart re-learns it within one heartbeat
  interval. The only durable input is the Git catalog.
- **It never manages a coding session.** No session registry, no execution callback, no
  agent-process host selection, no credential transport. Those belong to the ADE; see the
  next section and `ADE-BOUNDARY.md`.
- **This contract does not modify `agent-flow-kit`.** The kit is updated separately,
  afterwards, by someone else, against what is written here.

## The ownership boundary: the router recommends, the ADE executes

**Orca is the Agent Development Environment (ADE).** It owns coding-session mechanics:
worktree, terminal and process lifecycle, execution environment, and the agent-process host.

**`agent-router` is recommendation and policy only.** It names a harness, a
model/intelligence profile, an entitlement decision, and - when `placement_required` - the
placement policy `/v1/place` later resolves under. It never manages a session, never chooses
where an agent process runs, and holds no session state.

**The seam is one-way.** Router recommends → the ADE consumes and executes. No
router-to-session control channel, no callback, no session registration, no router-side
knowledge that a recommendation was acted on. The router's only inbound operational signal is
the edge capacity heartbeat, which is about GPUs rather than sessions.

**Native harnesses own their own authentication.** Claude Code (Anthropic Max) and Codex
(ChatGPT subscription) run as native CLIs inside ADE-owned sessions carrying their own
account/session auth. No request or response in this API transports provider credentials or
personal OAuth/session material, and no shape here can express one.

`ADE-BOUNDARY.md` is authoritative: both ownership lists, what a later change may not add
without an explicit owner reversal, and how a recommendation reaches the ADE today.
`verify-ade-boundary.sh` enforces the `/v1/route` input/output half of it mechanically.

## Three axes, and the vocabulary that names them

Harness, model/capability profile and physical placement are three independent axes and no
field in this API collapses two of them. `/v1/route` decides the first two, `/v1/place`
decides the third. Resolving the same profile onto a different compatible GPU at dispatch
time is legitimate; substituting a different model is not.

Funding is a fourth thing these three do not answer - *who is paying for this attempt* - and
it gets its own field rather than being inferred from any of them. See "Economics" below.

Five vocabularies are read verbatim from ConfigMap `ai/agent-router-catalog`, catalog
document version **1.3.0**, `schema_version: 1`:

| Vocabulary | Values | Where it is pinned |
| --- | --- | --- |
| harness | `claude`, `codex`, `devin` | `execution-profile.schema.json#/$defs/harness` |
| model_profile | `local-code-fast`, `local-code-standard`, `local-general`, `local-unrestricted`, `claude/strong`, `openai/strong`, `devin/free`, `minimax/strong` | `execution-profile.schema.json#/$defs/model_profile` |
| entitlement_pool | `anthropic-max`, `openai-plus`, `devin-free`, `minimax-max` (or `null`) | `execution-profile.schema.json#/$defs/entitlement_pool` |
| placement_policy | `prefer-warm-local`, `cluster-only`, `edge-only`, `any-24gb` | `execution-profile.schema.json#/$defs/placement_policy` |
| placement | `kserve-a5000`, `cachyos-7900xtx`, `bazzite-5090`, `laptop-rtx5000` | `place-result.schema.json#/$defs/placement` |

**`selectable: true` in the catalog means a profile MAY TAKE PART IN AN APPROVED EXECUTION
DECISION.** It does not mean the router will auto-select it, and it is not a recommendation.
`local-unrestricted` used to be the load-bearing case for this distinction - `selectable: true`
and never auto-selected by `/v1/route` - but STORY-035-8c retired its only candidate model
(`dolphin-chat`) with no substitute chosen, so it is now `selectable: false` too, alongside
`local-code-fast`. See "`/v1/route` is recommendation-only" below for where a deliberate
choice happens and what it still may not bypass; the distinction stays true in principle even
without a currently-selectable `alignment: unrestricted` profile to demonstrate it live.

**`minimax/strong` is in the enum but is not selectable.** Catalog 1.3.0 declares it with
`selectable: false` and a `blocked_by` list, so the router must not emit it until a later
story has validated it physically. It is in the vocabulary anyway, exactly as the placement
enum carries `status: planned` placement names: the name is frozen now so the catalog and
this contract land together, and flipping it to selectable is then a catalog change alone.
Excluding it would force a contract PR to accompany that flip - which is precisely the
drift window the 404 taxonomy describes.

**`local-agent` is not in the harness enum.** It exists in the catalog as a reserved,
`supported: false` / `selectable: false` placeholder so the axis is future-proof, and the
catalog records `router_behaviour: refuse-to-emit`. llama.cpp, vLLM and llama-swap are
inference runtime, not autonomous coding harnesses. Making it selectable takes its own story
evaluating a specific harness, and no story in EPIC-035 does that. The schema encodes the
refusal twice - the enum excludes it, and a redundant `not` clause keeps the refusal true if
somebody later widens the enum without reading the description.

**MiniMax is not in the harness enum either, and for a different reason.** `local-agent` is
refused permanently; MiniMax simply is not a harness. The first supported MiniMax path is
`harness: claude` + `model_profile: minimax/strong`, because Claude Code reaches MiniMax
through its Anthropic-compatible endpoint. A new provider is a new model, and possibly a new
funding pool - never automatically a new harness. This is the separation of axes working
rather than an exception to it. MiniMax Code is a different harness, is not approved, and
would be evaluated on its own.

### Vocabulary coupling, and what has to move together

Enumerating catalog names in this contract buys mechanical checking and costs coupling. The
split is deliberate:

- **Enumerated**: harness, profile, policy, placement and entitlement pool names. Each is
  compiled into something outside the catalog - a placement name appears verbatim as an
  agentgateway provider `name` and as the value of `x-placement`, and a pool name identifies
  which entitlement the ADE has to have provisioned for a session before it runs - so none of
  them can move without a coordinated change anyway.
- **Not enumerated**: `model_id`. The whole reason the profile indirection exists is that
  changing which physical model backs a profile is a pull request against the catalog and
  touches nothing else. Enumerating model ids here would break that guarantee.

- **Not enumerated either**: a pool's economics. `entitlement_pool` names the pool; what that
  pool costs, which credential class reaches it and what it does when exhausted are declared
  in the catalog and read from there. Encoding a pool-to-cost mapping in this contract would
  be the exact mistake ruling R16 forbids, in a different file: deriving economics from a
  name rather than from the entitlement.

So: **adding or renaming a profile, policy, placement or entitlement pool is a catalog PR and
a matching PR against this directory.** Swapping the model behind a profile is a catalog PR
alone, and so is flipping a declared-but-not-selectable profile to selectable.

Catalog 1.1.0 and this contract revision land in the same change for that reason. Adding
`minimax/strong` here alone would have opened the drift window the 404 taxonomy describes -
a name in the contract vocabulary that the loaded catalog does not have - so the two move
together and the window never exists. A
contract change that adds an optional field is a minor bump of `info.version`; removing a
field, re-typing one, or changing what a field means is a major bump.

`catalog_version` is a content digest - sha256 over the UTF-8 bytes of the rendered catalog
document (ConfigMap key `catalog.yaml`) exactly as mounted, before any parsing. The router
computes it at load time. It is not the catalog's semver `version` field, and it is never
written by hand.

**Five digests appear in these examples, and only one of them is real.**

| Digest | What it is | Used by |
| --- | --- | --- |
| `sha256:2fd681e0…60e229` | The **real** digest of catalog 1.3.0 exactly as committed - `sha256` over `data["catalog.yaml"]` of `kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml`. Reproducible from the repository today. | every example describing the cluster as it is |
| `sha256:fd8c4c31…50667a` | Not fabricated - the **former real** digest of catalog 1.1.0, frozen by value as a historical placeholder the moment STORY-035-8 brought `cachyos-7900xtx` up and it stopped being real. Depicts the world before the edge placement existed: one usable placement, `edge-only` resolving to nothing. | `placed-kserve-only-candidate.json`, `unavailable-policy-edge-only.json`, `status-pre-edge-1.1.0.json`, and (as the caller's stale claim) `catalog-version-stale.json` |
| `sha256:f00dface…f00dface` | Placeholder, obviously fabricated. A **further** catalog in which all three edge placements are selectable - which is what "every candidate has withdrawn itself" needs, since a placement that was never selectable cannot withdraw. | `unavailable-all-withdrawn.json` |
| `sha256:deadbeef…deadbeef` | Placeholder. A later catalog that retired `local-unrestricted` and folded the `any-24gb` policy into `prefer-warm-local`. | `unknown-profile.json`, `unknown-placement-policy.json`, `catalog-version-stale.json` (as the digest the router now serves) |
| `sha256:cafebabe…cafebabe` | Placeholder. A later catalog that **declares a metered funding source** in its own right - a pay-as-you-go pool with its own credential, added as a second entitlement on `openai/strong` and gated behind explicit intent plus metered-spend authority. | the three metered fixtures: `metered-denied-substituted.json` (`catalog_document_version: 1.3.0`), `metered-denied-no-alternative.json`, and the 403 one |

The placeholders exist because a digest is a content commitment: two documents showing
different catalog contents cannot honestly share one. That rule applies to the placeholders
as much as to the real digest, which is why there are four of them rather than one - "the
world before the edge existed", "all edge placements brought up", "things were retired" and "a
metered funding source exists" are four different documents, and giving them one digest would
have taught the opposite of the rule the field exists to enforce.

`fd8c4c31…50667a` is the odd one out: every other placeholder depicts a catalog that does not
exist yet, but this one depicts one that **did** exist and was superseded. It stays useful for
exactly the same reason the others do - it is a real, specific set of catalog contents, and
the fixtures bound to it are honest about which contents those are.

The metered one earns its own catalog for a reason worth stating plainly. **Catalog 1.3.0
declares no metered funding source at all**, and every pool it does declare says
`spillover: none`. So a fixture showing a billable option, pinned to the real digest, would
be teaching that an exhausted subscription turns into per-token spend - which is precisely
the promotion invariant 4 forbids. A metered candidate can only exist where some catalog
**declares that funding source in its own right**, which is what `cafebabe…` depicts. The
three metered fixtures say so in their own text as well: exhausting `openai-plus` makes that
candidate unavailable and nothing more; the billable candidate is a separate entitlement
that was there all along, gated behind intent plus authorization.

The `execution-profile` and `status` real-digest examples also carry
`catalog_document_version: "1.3.0"`; `internal-error.json`, `no-eligible-profile.json` and
`placed-warm-edge.json` carry the real digest but no `catalog_document_version` at all, because
that field is not part of their response shape. The still-hypothetical fixtures that do have the
field carry a higher version where they have one, and the historical `fd8c4c31…` fixtures carry
`"1.1.0"` - so no example claims a document version its contents do not match.

#### What keeps this honest

`./verify-digests.sh`, next to this file. It checks two things: which catalog a fixture claims,
and whether a fixture's derived fields agree with the rest of that same fixture. Every example carrying the real digest is a
**fixture**, and fixtures rot: the first catalog change that forgets to re-stamp them leaves
fingerprints that are confidently wrong, which is worse than carrying none at all - the field
exists precisely to settle which catalog an answer was computed against, so a lying one
removes the only way a reader could tell.

The script recomputes the digest from the committed ConfigMap and fails if:

- an example carries anything other than **the exact digests that fixture is bound to**.
  Binding is per file, not a global whitelist: each placeholder stands for a different
  hypothetical catalog, so moving `placed-warm-edge.json` onto the retirements digest fails
  even though both digests are documented. A fixture with a plausible but invented
  fingerprint fails for the same reason;
- an example is not listed in the script's expectations at all, or is listed and missing -
  a new fixture has to be given an owner deliberately rather than inheriting permission;
- an example names a `catalog_document_version` other than the one its catalog carries;
- **`openapi.yaml` embeds a digest that is not the real one.** The spec hard-codes the
  fingerprint in three inline examples, and those rot exactly like the JSON fixtures; the
  spec illustrates the current catalog only and never a hypothetical one;
- the abbreviated digest in the table above has drifted from the real one;
- **a `/v1/status` fixture's `resolves_now` or `resolves_today` disagrees with its own
  placements.** Neither is an independent fact: `resolves_now` is "can this policy select
  anything right now", answerable by resolving the policy's `prefer_order` against each
  placement's `eligible` in the same document, and `resolves_today` is the same question
  against `status`/`selectable`. A policy preferring a placement the document does not list
  fails too.

That last one is a **relational join** - `policies[].prefer_order` against
`placements[].name` - and JSON Schema cannot express one. It could only be faked by
enumerating placement names into the schema, which would hard-code catalog data this contract
deliberately does not carry. So the split is: **single-object facts stay in the schema**,
where the state-machine invariants live and are checked well; **cross-object facts are checked
here**, against the fixtures. Neither is a substitute for the other.

It exits non-zero naming the offending file and both digests. **Run it in any change that
touches the catalog**, not only ones that touch this directory - that is the direction the
rot comes from.

It is deliberately **not wired into CI**: that means editing workflow files, which is a
separate decision for the repository owner. Wiring it in is recommended follow-up.

Examples on the real digest go stale the moment the catalog changes, which is exactly the
condition `catalog_version_stale` exists to report.

## `POST /v1/route`

Task metadata in, an `ExecutionProfile` out, stamped immutably into story frontmatter. A
worker may not re-choose its model mid-attempt; re-routing is a new attempt.

Five of the free-form `tags` are load-bearing rather than descriptive, because they appear in
a profile's catalog `forbidden_for` list: `security`, `iam`, `secrets`, `prod-iac`,
`destructive-tools`. A match is a hard exclusion - not a score, not a tie-break - and it
fires however well the profile otherwise fits. Note the trap: `iac` is not `prod-iac`. Tag
production infrastructure work with `prod-iac` if the exclusion is meant to fire.

`alignment: unrestricted` is an alignment property, not a quality tier. `local-unrestricted`
was a weaker model than `local-code-standard` in every respect except refusal behaviour, so
"it refuses less" was never a reason to prefer it. **`/v1/route` never auto-selected it**, and
it never returned it because a caller asked for it either - see the next section for where a
deliberate choice actually happens. STORY-035-8c retired its only candidate (`dolphin-chat`)
with no substitute, so it is `selectable: false` today and cannot be routed to at all, by any
path, until a later catalog PR gives it one.

### `/v1/route` is recommendation-only

**This endpoint recommends. It does not take instructions.** It accepts task, context and
policy inputs and returns the router's recommended `ExecutionProfile`. It **never** accepts a
caller-specified `harness` or `model_profile`, and a caller cannot use it to tell the router
what answer to give back. `RouteRequest` being closed - `additionalProperties: false`, with
nothing in it naming a model, a harness or a placement - is the design, not an omission.

The reason is worth stating, because the alternative looks harmless: an override field on the
request would make the router's recommendation indistinguishable from the caller's instruction
in the router's own logs and in the response. The record of *what was recommended* would be
gone at the moment it started to matter.

**Explicit operator override happens outside the router**, after the recommendation and before
the route is stamped:

```text
/v1/route  ->  recommendation  ->  PM/operator accepts OR deliberately overrides
           ->  final route stamped into the story  ->  immutable execution attempt
```

**What an override may and may not do.** It may bypass **router scoring and preference only**.
It **must not** bypass hard catalog or policy constraints. An operator-selected route still has
to satisfy every one of these:

- the profile and harness exist, and are `supported` / `selectable`;
- `forbidden_for` exclusions;
- entitlement and billing restrictions;
- metered-spend authorization;
- capability and placement constraints.

**`local-unrestricted` illustrated a deliberate, manual profile - and its hard `forbidden_for`
exclusions stayed in force even under an override.** This is the point most likely to be
misread, so it is stated flatly: **human choice is not permission to bypass those controls.**
Even were it still selectable, an operator could not use "I chose it deliberately" to put it
on work tagged `security`, `iam`, `secrets`, `prod-iac` or `destructive-tools` - the exclusion
is not advice the router offers, and overriding the recommendation does not reach it. As of
STORY-035-8c it is `selectable: false` (its only candidate, `dolphin-chat`, was retired with no
substitute), so the first bullet above - "the profile and harness exist, and are `supported` /
`selectable`" - already rules it out before `forbidden_for` is ever reached.

**Where the enforcement lives.** Validating and auditing a manually overridden stamped route is
**routing-policy validation performed before dispatch**, not `/v1/route`'s job - the router is
not in that path and cannot be. It is not the ADE's job either: policy validation is not
session mechanics, and the freeze cuts both ways (`ADE-BOUNDARY.md`). The router must not grow
session mechanics, and Orca must not become the routing-policy authority. That is a requirement
to carry into 35.12 and the later kit work, recorded here so it is not discovered late.

One thing for that work to keep in view, noted rather than designed here: the planning flow
should record enough route provenance to tell **"router recommendation accepted"** from
**"explicit operator override"**, preserving the recommendation itself alongside the override's
identity and reason. The final stamped-frontmatter shape is not this story's to invent, and
nothing here requires it yet.

### Metered is default-deny

Absence of subscription or local capacity never silently promotes to billable API. Billable
use is explicit, per-attempt, and recorded.

`cost_class` is a property of **the attempt**, not a copy of the catalog field. It normally
equals the selected profile's catalog `cost_class`, and differs exactly when the profile's
usual payment path is unavailable and a billable one exists for the same profile - a
subscription pool exhausted for the window, say. That is the case worth naming, because it is
where an unguarded router would quietly start spending money.

One condition, two carriers:

- **HTTP 200 with a `metered_denied` note** when a non-metered option exists. The profile
  returned alongside the note *is* that option. Proceeding with it is the expected path.
- **HTTP 409 `metered_denied` with no profile** when none exists.

**Both carriers say `re-plan`**, and the 409 is not an exception dressed up as one. There is a
way forward; it is simply a *different request* than the one that was sent - one carrying
explicit human metered intent, submitted by a principal that independently holds metered-spend
authority. Needing two things does not make the condition unrecoverable, which is what `abort`
would claim. Either way the obligation is the same: authorizing the spend is a human decision,
made by re-submitting with `allow_metered: true` **from a principal that is allowed to make
it**.
**A client that reacts to a refusal by resending with `allow_metered: true` has removed the
control while leaving the paperwork in place** - and under this contract it also just fails.

### Metered intent is not metered authority

`allow_metered: true` **does not authorize billable spend.** It is intent: the caller is
asking for a billable candidate to be considered. Whether one may be returned is a property
of the **authenticated caller**, checked independently of the flag.

| Request | Caller | Answer |
| --- | --- | --- |
| `allow_metered` absent or false | any | Normal routing. Billable candidates are withheld; a `metered_denied` note or 409 as above. |
| `allow_metered: true` | principal authorized for metered spend | A billable candidate may be returned. |
| `allow_metered: true` | ordinary automation principal | **403 `metered_authorization_required`.** Nothing returned, nothing spent. |

**The normal BMAD planning-automation principal must not hold that permission.** It plans and
stamps hundreds of attempts unattended; giving it the ability to approve its own spend makes
the default-deny decorative, because the one client that would abuse the flag by accident is
exactly the client that runs without a human watching.

Two things this deliberately does **not** do:

- It does not build an approval workflow. There is no request-approval endpoint here, no
  pending state, no ticket. The refusal is terminal for the attempt; a human re-runs it
  under an authorized principal.
- It does not pin the auth mechanism. Which claim, which credential, which identity
  provider - that is settled by the later identity work, and this contract would be
  guessing. What is frozen here is the rule: **the boolean is a request, and the principal
  is the authority.**

The caller's obligation on a 403 is `abort`: escalate to a human, and specifically **do not
retry under a different credential**. A client that goes looking for a token that works has
turned a control into an obstacle course.

Worth recording: catalog 1.3.0 contains no `cost_class: metered` profile and no metered
funding source at all - Devin's paid on-demand tier deliberately has no profile name to hide
behind, and every declared pool says `spillover: none`. **So this path cannot fire today**,
and the three fixtures that show it are pinned to the `cafebabe…` placeholder catalog rather
than to the real digest. Showing a billable option against catalog 1.3.0 would teach that an
exhausted subscription becomes per-token spend, which is the exact promotion invariant 4
exists to forbid. The shape is specified now so that it exists before the money does.

### Economics: what it costs, and who is paying

`cost_class` and `entitlement_pool` answer two different questions, and neither is derivable
from the other:

- **`cost_class` is the economics of the ACTUAL ATTEMPT.** Free, subscription, or billable.
- **`entitlement_pool` is WHICH quota, subscription or funding source backs it.** `null` for
  local execution, which draws on none - the GPU is already paid for.

Both are on the `ExecutionProfile`, and both are on **every `fallbacks[]` entry**, so the
economics of every candidate are auditable *before* anything runs rather than discovered when
a fallback is taken. A fallback routinely crosses funding sources - a local attempt falling
back to a subscription pool is the ordinary shape.

Together they make the funding decision explicit, so policy-side validation can confirm a
stamped route without inferring billing or provider behaviour from the harness or from the
shape of a secret. Acting on the decision - provisioning and isolating the environment the
attempt runs in - is the ADE's job, not the router's: these fields name a decision, carry no
credential, and instruct no injection (`ADE-BOUNDARY.md`). Both of those inferences are wrong
here: `claude` runs against Anthropic Max on one attempt and MiniMax on another, and -

#### Billing class and credential type are independent axes

> **An API-key-shaped credential does not imply `cost_class: metered`.** Read economics from
> the entitlement, never from the shape of the secret, the name of the provider, or the
> harness that happens to be running.

MiniMax's Max Token Plan is a **fixed monthly subscription** reached with a provider-issued,
API-key-shaped credential. "It looks like an API key, therefore it is metered" is **wrong by
construction**: a consumer reasoning that way bills a flat subscription as per-token spend,
and refuses work the operator has already paid for. That heuristic is embedded in tooling
elsewhere in this estate, which is why this is stated rather than left to be re-derived.

The four seeded pools make the point without needing the argument. Two of them are the same
`cost_class` with completely different credential shapes, and one is neither a subscription
nor metered:

| Pool | Provider | cost_class | credential_class | spillover |
| --- | --- | --- | --- | --- |
| `anthropic-max` | anthropic | subscription | subscription-session | none |
| `openai-plus` | openai | subscription | subscription-session | none |
| `devin-free` | cognition | **free** | account-session | none |
| `minimax-max` | minimax | **subscription** | **provider-api-key** | none |

`devin-free` is also why the abstraction is called an **entitlement** pool rather than a
subscription pool: free is not a subscription, and a future explicitly-authorized
pay-as-you-go source is a third shape again. All three have to fit one table without
special-casing.

**A profile is not assumed to have exactly one funding source.** The catalog carries an
ordered `entitlements[]` list per profile: the first entry is the default, later entries are
alternatives reachable only under the conditions they declare. That is how `minimax/strong`
can normally draw on `minimax-max` while a future `minimax-payg` entry sits behind
`requires: [allow_metered, metered-spend-authorization]` - one harness, one model, one
profile, two funding candidates, and no axis collapsed to express it. The `ExecutionProfile`
stamps the ONE pool the attempt actually used, because a stamp is immutable for its attempt;
a different funding source is a different attempt, and appears as a `fallbacks[]` entry.

#### Invariant 4 across pools

Exhausting an entitlement **never** promotes to billable spend. Every seeded pool declares
`spillover: none`, so an exhausted pool simply stops being a candidate and routing falls back
to another approved pool, to free, or to local.

MiniMax specifically: **Token Plan exhaustion must never spill into MiniMax pay-as-you-go.**
Those are two credential classes for two entitlements, and holding one grants nothing about
the other. A pay-as-you-go path would need all three of a separately provisioned metered
credential, `allow_metered` intent on the request, and independent metered-spend
authorization held by the calling principal - which the ordinary automation principal does
not have. Two of the three is a refusal.

#### Credential isolation

The ADE gives a MiniMax-backed Claude Code session its MiniMax base URL and token **in that
session's own process environment only**. It must not rewrite the machine's Anthropic
configuration globally. No `ANTHROPIC_API_KEY` is introduced for the Anthropic Max path -
invariant 10 stands, a subscription is not an API key. The MiniMax credential lives in
1Password, scoped to MiniMax-backed session execution, and never in Git.

**None of it travels through this API.** The router names the pool; it never sees, holds or
forwards the secret, and no shape in this contract can carry one. Isolation is the ADE's to
implement, which is exactly why the router's half is a name.

This is contract text rather than an implementation note because it is the difference
between adding a funding source and quietly repointing every Claude Code session on the host
at a different provider.

### `placement_required` - does this attempt need a placement at all

Every `ExecutionProfile` carries `placement_required`, and it is required rather than
optional because a client that does not know the answer cannot dispatch correctly.

- `true` - a **local** profile. The model is served through agentgateway on a specific GPU,
  so the client calls `/v1/place` with the stamped profile and policy and sends the returned
  `x-placement` on its inference requests.
- `false` - a **vendor** profile, resolved by the harness itself (the catalog records these
  as `resolved_by: harness`). The traffic never touches agentgateway, so there is no
  placement to make; calling `/v1/place` would be a round trip with nothing on the other end.

The flow is therefore: `/v1/route` → if `placement_required`, `/v1/place` → hand the stamped
route to the ADE, which runs the session. It is a **model-inference** placement throughout;
nothing here decides where the agent process itself runs, and the router starts nothing.

Why it exists rather than being left to the caller: without it a client has to work out
which profiles are local, and it has exactly two ways to do that. It can carry its own copy
of catalog knowledge that this contract deliberately does not publish - `hosting` is not in
the vocabulary tables above, on purpose - or it can key off the `local-` name prefix, which
is a naming convention and not a guarantee. Both go stale the moment the catalog moves, and
both fail silently: the round trip that should have happened simply does not, and the
attempt lands wherever the gateway would have put it anyway. The router already knows; it
says so.

Each `fallbacks[]` entry carries its own `placement_required` for the same reason. A
fallback commonly crosses the local/vendor line - a local primary falling back to a vendor
subscription is the ordinary shape - and a client that has just failed over is the last one
that should be guessing.

`placement_required` equals `hosting: local` for the selected profile in the loaded catalog.
That is a MUST on the router that JSON Schema cannot check here, for the same reason
`model_id` is not enumerated: hosting is catalog data, and hard-coding it into this contract
would recreate the coupling the profile indirection exists to avoid.

## `POST /v1/place`

The stamped profile and policy in, a placement name out. Shape (b) of the 35.5 design gate:
`ai.groups` is a passive safety net beneath proactive `agent-router` placement, not the
placement mechanism.

### Authority

**A binding recommendation with a safe default.** The caller should honour it and is not
punished for failing to.

The caller expresses the decision by setting `x-placement: <placement>` on its agentgateway
request - the one per-request selection mechanism measured to work end to end, and the same
shape this repo already ships for `x-model`.

The caller is not bound in the enforcement sense, because it cannot be: the gateway has no
way to reject a request for carrying the wrong placement, and nothing stops a client calling
the gateway without ever asking the router. The design turns that into a property rather than
a hole - **a missing, unknown or stale `x-placement` falls through to a route rule whose
backend is a grouped backend**, so the worst case for an uncooperative caller is gateway-side
selection rather than an error. Enforcing "you must ask the router" belongs at the calling and
routing-policy layer; it is a policy question, not a gateway capability.

Two consequences that have to be designed in from the start:

- **Every route rule must terminate in a grouped backend.** A header-pinned rule aimed at a
  single-provider backend deletes the safety net precisely for the requests that asked for a
  placement - measured 3/3 503s.
- **`ai.groups` and `policies.health` ship in the same change, or neither.** A grouped backend
  without a health policy is decoration that looks like resilience in review and provides
  none - measured 8/8 failures against a dead group-0 provider with a healthy group 1 behind
  it.

### `x-placement` is a scheduling signal only

It carries no authorization meaning whatsoever. Model authorization is, and remains, the
per-model CEL policy binding the request body's `model` to the routed backend model. Nothing
in this contract may let `x-placement` influence an authorization decision, and a design that
does is rejected on sight.

It is also a **model-inference scheduling header consumed by agentgateway**, and never an
agent-execution instruction. It says which GPU should serve a model; it does not say where an
agent process runs, and reading it as such is the boundary error `ADE-BOUNDARY.md` exists to
forbid.

The `headers` object in a `PlaceResult` restricts its key set to `x-placement` alone. That is
mechanical enforcement of the same rule: it is the one channel through which a router could
inject an arbitrary header into a caller's request, so a future router cannot use it to hand
out an `Authorization` header or a model override.

### No endpoint, ever

`PlaceResult` carries no URL. The earlier sketch in EPIC-035 §4 returned a `target` object
with `node` and `endpoint`; that is superseded here, and not for tidiness. Returning an
endpoint would make the heartbeat's `runtime.endpoint` an authoritative routing target, and
**a compromised edge heartbeat credential must not be able to redirect inference traffic by
advertising an arbitrary endpoint.** The caller gets a name and reaches it through
agentgateway, whose backend hostnames stay Git-managed.

### An empty result is HTTP 200

When nothing is eligible, `/v1/place` answers with `status: "unavailable"`, a null placement,
no headers, and a reason - not a 4xx. Three reasons:

1. A policy that resolves to nothing must produce an **explicit empty placement result** and
   must never silently fall through to a placement the caller did not ask for. An empty
   result is the correct answer to a well-formed question, not a failure to answer it.
2. The caller needs the rejected candidates and why each was rejected. An error envelope
   carries neither.
3. Making it an error invites a retry loop, which is the one thing that definitely will not
   help.

The `status` discriminator exists so that a caller who only inspects HTTP status codes still
cannot mistake an empty result for a placement. **On `unavailable`, do not dispatch.** A
caller that shrugs and sends the request anyway gets gateway-side selection, which may be
exactly the placement its policy excluded.

### Long requests have no safety net

Above 64 KiB the gateway cannot replay a request onto a fallback provider: `MAX_BUFFERED_BYTES`
caps the replay buffer and an over-cap body disables retries entirely. Measured: a 100 KiB
body returned 503 with the fallback never contacted, while 2 KiB through identical config
returned 200 via the fallback. Long context is ordinary for this workload, so this is not an
edge case.

`PlaceRequest.estimated_request_bytes` exists so a caller can say so, and the router can place
conservatively up front instead of relying on a safety net that will not be there. Story 35.11
must either do that or state the gap explicitly.

Failover is relied on **only before a response stream has begun.** Once streaming output has
started, a broken stream fails the execution attempt; starting a new attempt belongs to
whatever owns the attempt - for a coding session, the ADE - never to the gateway, and never to
the router.

## `POST /v1/capacity/heartbeat`

The body is the Edge Worker Contract's heartbeat payload, unchanged.
`edge/EDGE-WORKER-CONTRACT.md` §1 is authoritative for field semantics; `heartbeat.schema.json`
is a mechanical restatement of that table, and if the two disagree the schema is the bug.

Every field is required. Unlike the catalog - where an absent key and an explicit `null` mean
the same thing - a heartbeat is generated by a known agent on a fixed schedule, so a missing
key means a broken agent rather than an unknown value. Unmeasured values are sent explicitly
as `null`, or as the literal string `"unmeasured"` where the edge contract says so.

The heartbeat interval is **not** a field in the payload. It is a router-side constant, and a
node discovers it from the `202` acknowledgement (`next_heartbeat_after_seconds`) and from
`/v1/status` (`heartbeat_policy`) rather than hard-coding it.

Authentication is a per-node bearer credential bound to the `node` field. A valid credential
presenting a different node id is `403 node_identity_mismatch`, because a credential that
could heartbeat as any node could withdraw a healthy placement or keep a dead one advertised.
The credential is long-lived and the caller is off-cluster, so this path is TLS - see
"Transport" below.

### A heartbeat may narrow static authority; it may never expand it

This is the rule, and it is frozen:

> **effective capability = Git/catalog authority ∩ runtime heartbeat observation**

A heartbeat is an observation the node makes about itself. Observations **subtract**. A node
may report itself busy, drained, interactive, cold, or short of VRAM, and the router acts on
that immediately: the placement becomes **less** eligible. A node may **never** report
itself into a model, a capability, a context size, or any other authority the Git-managed
catalog does not already grant that placement.

Concretely:

- **`active_model` counts as warm only when that model is catalog-authorized for that
  placement.** A warm claim for a model the catalog never put there does not make the node a
  candidate for it.
- **`capabilities` and `max_context` intersect with the catalog's.** Fewer capabilities than
  the catalog grants is honoured - the runtime came up without tool calling, so the node is
  not a candidate for work needing it. More is capped at the catalog's.
- **An unexpected model or capability claim is an alarm, not an input.** It is an
  observability and reconciliation signal - the node is running something Git did not put
  there - and it is **ignored for eligibility** while somebody looks at it.
- **Dynamic measurements stay self-reported.** Free VRAM, utilization, state: these are
  measurements no Git file can hold, they affect scheduling, and they never affect static
  authorization or capability membership.

Why the rule is stated this strongly. Calling capability self-report "availability-only" was
too weak. It is true that this is **not a model-authorization bypass** - authorization is the
per-model CEL policy, which binds the request body's model to the routed backend model and
never reads a heartbeat. But a compromised node with an unbounded self-report can **bias
placement toward itself**: claim warm, claim capability, claim context, and collect work it
would not otherwise have been given. That is a **placement-integrity** problem, and once the
work lands there it is potentially a **confidentiality** one, inside a trust boundary that
was approved on the understanding that edge hosts serve what the catalog says they serve.
Intersection is what keeps a stolen node credential to the blast radius of withdrawing its
own capacity - which is the blast radius it is supposed to have.

### `active_model` vs `cached_models`

Warm, cached and absent are **three different costs**, and `/v1/place` economics depend on
telling them apart:

| Value | Means | Cost to serve |
| --- | --- | --- |
| `active_model` | loaded and warm in the inference runtime | none; answers now |
| `cached_models` | artifact present on local storage | model/VRAM load time, no download |
| in neither list | absent | a download of tens of gigabytes, then a load |

Collapsing warm into cached loses the ability to prefer a node that answers immediately.
Collapsing cached into absent loses the ability to prefer a node that can load in seconds over
one that must fetch the weights first. `PlaceResult.readiness` carries the resolved value, with
a fourth, honest option - `unknown` - for candidates with no heartbeat-derived signal. **A
router must not treat `unknown` as warm.**

**Which name goes in these fields.** One rule, and it is the catalog's `model_id` - the key
in the catalog `models` table, which for local models is the gateway-facing alias
(`qwen36-27b`). Not the catalog's `upstream_model_id` (`qwen3.6-27b`), which is the different
string agentgateway sends to the upstream server, and which the catalog says outright must
not be swapped with the alias. The reason is mechanical: the router derives readiness by
joining `active_model` and `cached_models` against catalog `physical[].model_id`, so a
heartbeat carrying the upstream id makes a warm node look cold and hands the work to a
placement that has to load from scratch. A node whose runtime only knows its upstream id
translates before it reports; that is the node's job, not the router's guesswork.

`edge/EDGE-WORKER-CONTRACT.md` §1 now carries the same rule, in the document that is
authoritative for heartbeat semantics: its field table types both fields as catalog
`model_id`, its example says `qwen36-27b`, and it assigns the translation explicitly. **The
edge producer translates**, because a runtime knows its own identity and not the catalog's -
llama.cpp reports whatever `--alias` it was started with, vLLM reports the filesystem path it
was launched from. The router keeps no alias table and does no fuzzy matching; a value it
cannot resolve against the catalog is an alarm, ignored for eligibility.

KServe placements normally report `unknown`, and that is correct rather than lazy: the router
does not probe KServe. Waking a scale-to-zero predictor to observe it would be exactly the
mechanism invariant 7 forbids, whatever it was called.

### `runtime.endpoint` is observational only

It exists so an operator can see where a node believes it is listening. It **must not** become
an authoritative routing target, **must not** cause dynamic agentgateway reconfiguration, and
**must not** override Git-managed topology.

Threat, stated plainly: *a compromised edge heartbeat credential must not be able to redirect
inference traffic by advertising an arbitrary endpoint.* This is a security boundary. It pairs
with `x-placement` being a scheduling signal as the two things a compromised edge or a
compromised client cannot do. `GET /v1/status` is the only place the value surfaces, because
reading it is its whole purpose.

## Transport

Every endpoint here is bearer authenticated, so this is a security property of the contract
and not a deployment detail to be settled later: **any path that is not pod-to-pod inside
the cluster is TLS, or it does not ship.**

That covers the edge heartbeat without exception. An edge worker is off-cluster by
definition, and the credential it presents is long-lived and can withdraw or sustain a
placement, so plaintext there puts a durable, high-value secret on the wire on a fixed
schedule.

The requirement is the edge worker contract's rule R4 posture applied in the other
direction - edge to router rather than gateway to edge - and it is deliberately the same
list, because two halves of one link should not have two standards:

- **HTTPS, no plaintext fallback**, even inside the VLAN trust boundary.
- **A real CA.** The trust anchor has `basicConstraints: CA:TRUE`; a self-signed leaf used
  as its own anchor is not one, and is rejected.
- **Hostname SAN.** The certificate matches the name the client connects to. Hostname form,
  never a bare address.
- **No `insecureSkipVerify`.** It was proven to work in the 35.4 spike and must not ship: it
  turns the CA into decoration.
- **A dedicated key and certificate.** Never the cluster wildcard's private key.

`openapi.yaml` carries two `servers` entries to make this legible to a code generator rather
than only to a reader: the plaintext `http://agent-router.ai.svc.cluster.local:{port}` entry
is scoped in its own description to in-cluster callers, and an `https://{edge_host}` entry
covers everything else. The hostname is a placeholder - it is provisioned with the
certificate, and no LAN address appears in this repository.

What is genuinely still open is narrower than "transport is unspecified", and is recorded as
item 10 below: whether mTLS is required on top of bearer-over-TLS, and which story
provisions the certificate and DNS name.

## Withdrawal semantics

**Three mechanisms, not two.** Each has a different actor and a different latency; conflating
them was the error.

| Signal | Actor | Class | Latency |
| --- | --- | --- | --- |
| heartbeat `state=INTERACTIVE\|DRAINING` | **agent-router** marks the placement ineligible **immediately** | proactive control-plane state | detection bounded by the heartbeat interval (30 s), then instant. Callers holding a placement stop using it within its `ttl_seconds` |
| missed heartbeat for **3x interval** | **agent-router** transitions the placement to `OFFLINE` | silence as failure detection | 90 s at a 30 s interval - the slowest of the three |
| inference endpoint **503 / wire failure** | **agentgateway** reacts via passive health, retry and failover | reactive data-plane state | within the request itself, and only before a response stream has begun |

The heartbeat is proactive control-plane state. A 503 is reactive data-plane state. Silence is
failure detection. The router cannot know that the edge daemon wedged 200 ms ago; the gateway
cannot know that the edge is reserved for an interactive session. Neither can do the other's
job, which is why all three exist.

Interactive preemption is **mechanism 1 only**. The router simply stops returning the
placement - no cluster write, no propagation delay, no GitOps interaction, reversible in one
state change. There is also a slow path where the router excludes a provider by creating its
own `AgentgatewayPolicy`, but un-exclusion there is timer-bound and can take many minutes, so
it is for a box being drained for maintenance, never for a human sitting down at a desk.

The heartbeat's `202` response reports `eligible_for_placement`, so a node can see that its
withdrawal was honoured rather than assuming it.

> `edge/EDGE-WORKER-CONTRACT.md` §1.2 carries the same three mechanisms, with the same actors
> and classes. It once listed only two - a missed heartbeat and a 503 - and was corrected
> before it merged. The two documents agree; this table is the version both are frozen at.

## How `/v1/status` reports where a state came from

Every placement in `/v1/status` carries `state_source` alongside `state`, and **`state` cannot be
interpreted without it.** Four values, exhaustive, because there are exactly four ways the router
can stand in relation to a placement:

| `state_source` | Channel | `state` | `heartbeat` / `last_heartbeat` | `eligible` |
| --- | --- | --- | --- | --- |
| `heartbeat` | exists, observed | as the node reported | both present | as reported |
| `silence` | exists, went quiet past `offline_after_seconds` | `OFFLINE` | **retained** from the last report | false |
| `unseen` | exists or is expected, nothing observed this process | `OFFLINE` | both `null` | false |
| `static` | none exists | from the **catalog** | both `null` | from the catalog |

The distinction in one line each:

- **`silence`** - was talking and stopped.
- **`unseen`** - expected to talk, but has not been observed yet.
- **`static`** - was never expected to talk.

Collapsing any two loses a fact somebody acts on. A planned placement reported as `silence`
invents a missed heartbeat window that never existed. A restarted-but-healthy edge node reported
as `silence` says a node failed when nothing did. And **a KServe placement reported as anything
but `static` takes kserve-a5000 out of service on a false signal** - it never checks in by
design, so the absence of a check-in says nothing about its health. That mistake used to zero
out local capacity entirely; as of catalog 1.3.0 cachyos-7900xtx would still be there, but the
`static` rule protects kserve-a5000 for exactly the same reason regardless of how many other
placements exist.

`unseen` exists because capacity is in-memory. On restart the router knows from the catalog which
placements are enrolled, but it has heard from none of them yet: a channel exists, so `static` is
wrong; nothing has arrived, so `heartbeat` is wrong; and nothing ever reported in this process, so
`silence` is wrong too - it has nothing to retain, which is precisely what it differs from
`silence` by. Without a fourth value that state was unrepresentable, and an implementer would have
had to pick one of three wrong answers.

### What `capacity_state` adds

`router.capacity_state` changes nothing about what `unseen` means. It changes how provisional a
caller should treat an answer built from one:

- **`learning`** - the router has been up less than one heartbeat interval. An `unseen` placement
  **may be a healthy edge node that has simply not been relearned yet**, so an unavailable
  placement result is **provisional** and can change without anything having been fixed.
- **`steady`** - a full interval has elapsed. An `unseen` placement **remains unavailable until
  its first heartbeat arrives.** `steady` does not promote it and is not permission to guess: the
  node has now had its interval and has not reported.

`examples/status/status-restarted-unseen.json` is the `learning` case worked through.

## Error taxonomy

Every error path, with its status, machine-readable code, and what the caller must do. The
obligation is a **field in the envelope**, not something to infer from the status code: a
client that retries a `catalog_version_stale` forever, or aborts on a `catalog_unavailable`
that would have cleared in two seconds, read the number instead of the contract.

| Code | HTTP | Endpoints | Obligation | What the caller does |
| --- | --- | --- | --- | --- |
| `invalid_request` | 400 | all | `abort` | Fix the caller. `details.field` names the offending field. |
| `unauthenticated` | 401 | all | `abort` | Do not retry without a new credential; retrying a rejected one is lockout and log noise. Two credentials mean two examples - `caller-unauthenticated.json` for the caller-facing endpoints, `heartbeat-unauthenticated.json` for the heartbeat - because a caller reading a fixture that names `/v1/capacity/heartbeat` learns the wrong thing about its own error path. |
| `node_identity_mismatch` | 403 | heartbeat | `abort` | Stop and alert. The credential is valid but bound to a different node; nothing was recorded. |
| `metered_authorization_required` | 403 | route | `abort` | The request set `allow_metered: true` but **this caller does not hold authority to approve billable spend**. Nothing was returned and nothing was spent. Escalate to a principal that holds it. **Do not retry, and do not retry under a different credential** - hunting for a token that works is the abuse this code exists to stop. |
| `unknown_profile` | 404 | place | `re-plan` | The stamp names a profile that is in this contract's vocabulary but not in the loaded catalog. Call `/v1/route` again - a new attempt, not a repair of this one. A name outside the vocabulary is `400`, not this. |
| `unknown_placement_policy` | 404 | place | `re-plan` | Same, for a policy name. Policies are catalog data, not request parameters. |
| `catalog_version_stale` | 409 | route, place | `re-plan` | The catalog moved between planning and dispatch. Re-run `/v1/route`; a stamped profile is immutable for its attempt. |
| `metered_denied` | 409 | route | `re-plan` | Every option is billable and none was authorized. Escalate to a human. The way forward is a DIFFERENT request: explicit metered intent, from a principal that holds metered-spend authority. Both, or neither works. **Never resend this request with `allow_metered: true` automatically** - that is not re-planning, and it answers 403. |
| `metered_denied` | 200 (in-band note) | route | `re-plan` | A billable option was withheld and a non-metered substitute was returned. Proceed with it, or seek authorization. |
| `no_eligible_profile` | 409 | route | `re-plan` | Nothing satisfies the constraints. Relax one, wait for a pool, or authorize spend. Resending unchanged is a loop. |
| `no_eligible_placement` | **200**, `status: "unavailable"` | place | do not dispatch | Not an error envelope. See "An empty result is HTTP 200" above. Reason codes: `no_eligible_placement`, `policy_resolves_to_nothing`, `all_candidates_withdrawn`, `constraint_unsatisfiable`. |
| `rate_limited` | 429 | all | `retry` | Back off; honour `retry_after_seconds`. If this fires routinely, the caller is calling per inference request instead of caching for `ttl_seconds`. |
| `catalog_unavailable` | 503 | all | `retry` | **Transient only**: no catalog is loaded yet. The same request succeeds unchanged once the load completes. Retry with backoff. |
| `catalog_schema_unsupported` | 503 | route, place, status | `abort` | **Permanent**: the router refused a catalog whose `schema_version` it does not implement, rather than best-effort parsing an unknown shape. Retrying cannot clear it - an operator deploys a router that understands the catalog, or reverts the catalog. Alert. **Never served on the heartbeat path** - see below. |
| `internal_error` | 500 | all | `retry` | Both decision endpoints are side-effect free, so a retry is safe. A persistent failure fails the attempt rather than degrading it onto a different profile. |

Codes are stable: a code is never re-pointed at a different condition, only added, or removed
with a major bump.

**One code, one obligation.** Two conditions that share an HTTP status but not a remedy get
separate codes, because the obligation field is only useful if a client can trust it:

- `catalog_unavailable` (retry) and `catalog_schema_unsupported` (abort) were one code. A
  client that backed off politely against the second would have retried forever against a
  condition only an operator can clear, which is precisely the "read the number instead of
  the contract" failure this envelope exists to prevent.
- `metered_denied` (no authorization claimed) and `metered_authorization_required`
  (authorization claimed, caller does not have it) are different facts about different
  actors, and the second is the one that means "your identity is wrong", not "ask a human".

**A router-side failure never ends the heartbeat loop.** `abort` is written for a caller with
an attempt to fail; an edge node has no attempt, so the only thing it can do with `abort` is
stop reporting - and a node that stopped because the router had a bad catalog would stay
invisible long after an operator had fixed it, with nothing in this contract saying when to
resume. So the heartbeat path never serves `catalog_schema_unsupported`. It serves only the
transient `catalog_unavailable`, and recording a self-reported state needs no catalog data
anyway. The loop ends only on a failure the node itself must fix: `unauthenticated`, or
`node_identity_mismatch`.

## Examples

31 files, every one validated in CI-shaped commands below. Every example is also referenced
from `openapi.yaml`, so an example that stops being reachable from the spec is visible.

Each one states which catalog it is drawn against. Examples on the real 1.3.0 digest describe
the cluster as it is today; those carrying a placeholder digest describe either a later catalog
or, for the frozen `fd8c4c31…` value, an earlier one - and each says which below.

**`examples/execution-profile/`** - `/v1/route` responses.

| File | Shows |
| --- | --- |
| `local-code-standard.json` | The ordinary case: free local profile, two fallbacks. **`placement_required: true`** - a local result, so the client places it - with both vendor fallbacks at `false`. |
| `security-tagged-excludes-unrestricted.json` | `forbidden_for` firing as a hard exclusion on a critical-blast-radius task. **`placement_required: false`** - the vendor-hosted result: no placement call, the harness resolves it. |
| `metered-denied-substituted.json` | **The refusal case**, on the metered placeholder catalog (`cafebabe…`): a billable option withheld, a non-metered substitute returned, `metered_denied` note attached. The billable candidate exists because that catalog declares a pay-as-you-go funding source, **not** because a subscription ran out. |
| `docs-low-risk-cluster-only.json` | Non-code local profile, empty `fallbacks` on purpose. Was `local-code-fast` until STORY-035-8c retired it with no substitute; now `local-general`. |

**`examples/heartbeat/`** - one per state, plus unmeasured hardware.

| File | Shows |
| --- | --- |
| `state-available.json` | Idle, model warm. |
| `state-serving.json` | Handling inference; VRAM nearly gone. |
| `state-draining.json` | Finishing in-flight work with `interactive: true` already - a node on its way to `INTERACTIVE`. |
| `state-interactive.json` | Human has the GPU; model unloaded but still cached. |
| `state-offline.json` | Explicit self-report before shutdown, as opposed to the router inferring it from silence. |
| `unmeasured-laptop-on-battery.json` | `"unmeasured"` GPU strings, null VRAM, `ac_power: false`. |

**`examples/place/`** - `/v1/place` responses.

| File | Shows |
| --- | --- |
| `placed-kserve-only-candidate.json` | **Historical** (`fd8c4c31…`, catalog 1.1.0, before the edge existed): one candidate, readiness `unknown` because KServe is not probed. |
| `placed-warm-edge.json` | Today's real answer: cachyos-7900xtx is warm, first in `prefer_order`, and wins on both counts under a strict warm preference. |
| `unavailable-policy-edge-only.json` | **Historical** (`fd8c4c31…`, catalog 1.1.0): `edge-only` resolved to nothing - every placement it named was `status: planned`. Explicit empty result, not a fall-through. Catalog 1.3.0 resolves this policy; see `status-today.json`. |
| `unavailable-all-withdrawn.json` | **Further catalog** (`f00dface…`, the remaining two edge placements also brought up): interactive, draining and silent candidates; note the warm-but-ineligible one. Withdrawal is only meaningful for a placement that was selectable in the first place. |

**`examples/errors/`** - one file per envelope code, 14 in total. Five sit on a placeholder
catalog. `unknown-profile.json` and `unknown-placement-policy.json` show a stamped name the
loaded catalog has dropped, and `catalog-version-stale.json` shows the caller's stale
`fd8c4c31…` digest (catalog 1.1.0, before the edge existed) against the `deadbeef…` retirements
catalog the router is serving now.
`metered-denied-no-alternative.json` and `metered-authorization-required.json` sit on
`cafebabe…`, the catalog that declares a metered funding source, because catalog 1.3.0
declares none and a billable candidate cannot honestly be shown against it.

The two shared catalog-failure fixtures - `catalog-unavailable.json` and
`catalog-schema-unsupported.json` - name **no** endpoint. Both are served from every
endpoint, so a fixture claiming one would teach a caller the wrong error path for its own
call.

**`examples/status/`** - `status-today.json` is catalog 1.3.0 exactly as it stands, on the
real digest: `cachyos-7900xtx` selectable and heartbeating, `local-code-standard` with a
second physical candidate, `edge-only` resolving, the four entitlement pools, and
`minimax/strong` visible but not selectable - alongside `local-code-fast` and
`local-unrestricted`, retired by STORY-035-8c and visible but not selectable for the same
reason: each lost its only candidate model with no substitute chosen. It is also the example
that shows a heartbeat
re-exposed verbatim inside a placement. `status-pre-edge-1.1.0.json` is the same view as it
stood before 35.6-35.8 landed: one usable placement, `edge-only` resolving to nothing. Its
contents did not change - only its role did, from "today" to "history" - so it keeps the
`fd8c4c31…` digest that was once real and now names exactly that snapshot.
`status-restarted-unseen.json` is today's catalog seconds after the router restarted: the edge
node is enrolled and may well be healthy, but nothing has been observed in this process yet, so
it is `unseen`, `OFFLINE` and not eligible, alongside `capacity_state: learning`.

Two examples describe a world that does not exist yet. `unavailable-all-withdrawn.json` needs
the remaining two edge placements (`bazzite-5090`, `laptop-rtx5000`) to also be selectable - a
node that was never selectable cannot withdraw itself - so it describes a further catalog and
carries its own placeholder digest, `f00dface…`. In catalog 1.3.0 those two placements are
still `status: planned` / `selectable: false` and cannot be returned by `/v1/place` at all,
which is why this fixture cannot sit on the real digest. The `cafebabe…` metered fixtures are
the other still-hypothetical world, described where they are introduced above.

## Validating this contract

The story's gate commands, runnable from the repository root:

```bash
uvx --from openapi-spec-validator openapi-spec-validator \
  contracts/agent-router/openapi.yaml

uvx --from check-jsonschema check-jsonschema --schemafile \
  contracts/agent-router/schemas/execution-profile.schema.json \
  contracts/agent-router/examples/execution-profile/*.json

uvx --from check-jsonschema check-jsonschema --schemafile \
  contracts/agent-router/schemas/heartbeat.schema.json \
  contracts/agent-router/examples/heartbeat/*.json
```

The three example sets the story's gates do not cover are checked the same way:

```bash
uvx --from check-jsonschema check-jsonschema --schemafile \
  contracts/agent-router/schemas/place-result.schema.json \
  contracts/agent-router/examples/place/*.json

uvx --from check-jsonschema check-jsonschema --schemafile \
  contracts/agent-router/schemas/error.schema.json \
  contracts/agent-router/examples/errors/*.json

uvx --from check-jsonschema check-jsonschema --schemafile \
  contracts/agent-router/schemas/status.schema.json \
  contracts/agent-router/examples/status/*.json
```

And the digest check, which is the one that needs running when the CATALOG changes rather
than when this directory does:

```bash
./contracts/agent-router/verify-digests.sh
```

Two more executable checks live here, both standalone and argument-free:

```bash
./contracts/agent-router/verify-placement-cases.sh
./contracts/agent-router/verify-ade-boundary.sh
```

`verify-ade-boundary.sh` is the ownership boundary made mechanical. It pins the exact
top-level property sets of `RouteRequest` and `ExecutionProfile` against the allowlist
declared in `ADE-BOUNDARY.md`, requires `additionalProperties: false` on both, and refuses
session/process semantics - `session`, `worktree`, `pid`, `process`, `exec`+`host`,
`container`+`id`, `callback`, `webhook` - in any property name or fixture key at any nesting
depth of the route-scoped shapes. It matches on semantic TOKENS rather than word boundaries,
so `sessionId` and `exec_host` are caught alongside `session_id`.

Its scope is deliberately narrow: the `/v1/route` input/output contract and its fixtures
only. The heartbeat, status and place contracts are exempt, because operational telemetry
legitimately speaks of processes and hosts, and a check that cried wolf there would train
people to ignore it.

Adding a property to either shape is not forbidden - it is made *deliberate*. The pin fails
until `ADE-BOUNDARY.md`'s allowlist and the schema change in the same pull request.

Plus a check that the four endpoints exist, and a grep proving no tracked file here carries a
literal LAN address - placeholders such as `<edge-host>` and `<node-id>` are used throughout,
matching the edge worker contract.

The schemas do real work rather than describing the examples back to themselves. They reject,
among others: `harness: local-agent`; a profile or placement name not in catalog 1.3.0;
`cost_class: metered` with `metered: false` and the reverse; a `metered_denied` note on a
metered profile; `state: INTERACTIVE` with `interactive: false`; a header other than
`x-placement` in a `PlaceResult`; an `unavailable` result that still carries a placement; and
`no_eligible_placement` used as an error code.

Rules that were prose-only are checked as well, because a rule this document states and the
schema permits is a rule an implementer reading only the schema will break:

- a result-level reason code (`placed_warm`, `no_eligible_placement`) inside `alternatives[]`,
  where only candidate-level codes belong;
- a `selectable: false` harness in `/v1/status` with no `router_behaviour` saying what the
  router does instead;
- `state_source: "heartbeat"` on a placement with no heartbeat attached, `"static"` on one that
  has heartbeated or on an enrolled edge placement that has a channel, `"unseen"` on anything
  that is not OFFLINE-with-nothing-retained, or `"silence"` with no prior report to retain;
- a heartbeat with a null `gpu.model` or `gpu.arch`, which the edge contract types as strings
  whose unmeasured form is the literal `"unmeasured"`;
- an entitlement pool in `/v1/status` that omits any of its four declared attributes, which
  would force a consumer to infer one of them - the failure the pool table exists to prevent;
- a `cost_class: metered` pool that does not say `requires_authorization: true`;
- a profile with an empty `entitlements[]`, or an `ExecutionProfile` or fallback entry with
  no `entitlement_pool` at all.

## Known tensions and open items

Recorded rather than resolved, because each belongs to a story that has not run yet.

1. **`gpu.utilization_pct` is not nullable** while the two VRAM fields are. That is what the
   edge contract's field table says, and where the two could drift the edge contract wins, so
   the schema follows it. A node that genuinely cannot read utilization is out of contract
   today; 35.6 is where that gets found out.
2. **No metered profile exists in catalog 1.3.0**, so `/v1/route` cannot emit `metered: true`
   at all right now. The refusal shape is specified ahead of the money on purpose.
3. **The heartbeat interval is router-side** and deliberately not added to the payload. 35.9
   sets the real value; 30 s / 90 s in these examples is a starting point, not a measurement.
4. **Harness-to-profile reachability is unproven.** A cloud harness reaching a LAN-only
   gateway has not been demonstrated, and nothing in this contract asserts it can. The catalog
   README carries the same open item against 35.10.
5. **Neither shape carries a capabilities or tools field.** Required capabilities and tools
   are within the router's *recommendation semantics* - but there is no `tools` or
   `capabilities` wire field on `RouteRequest` or `ExecutionProfile` today, and nothing in
   this contract implies one exists. The model profile implies its capability set via the
   catalog, and the router infers capability needs from the task metadata. If 35.10 finds
   that inference too weak, adding the field is an additive minor bump - and it amends
   `ADE-BOUNDARY.md`'s allowlist in the same change, because `verify-ade-boundary.sh` pins
   the property set. That is the sanctioned path for contract growth: deliberate, never
   silent.
6. **`x-placement` must equal the `placement` field.** JSON Schema cannot express equality
   between two fields, so this is a MUST on the router that the schema cannot enforce.
7. **`estimated_cold_start_s` is provisional** wherever it comes from the catalog. 35.7
   measures repeatable cold and warm figures on both the A5000 and the 7900 XTX, and 35.8 sets
   final timeout values from those numbers. The 150 s in the examples is the current estimate,
   not a measurement of the distribution.
8. **The 64 KiB retry cliff** is a stated limit, not a solved problem. 35.11 either places
   large-context requests authoritatively up front or documents the gap.
9. **Enforcing the intersection rule is 35.6's work, not this contract's.** The rule itself is
   settled and frozen above — a heartbeat narrows, never expands, and an unexpected claim is an
   alarm that is ignored for eligibility. What is open is the mechanism: which component
   computes the intersection, where the alarm goes, and whether a node that repeatedly claims
   authority it does not have is quarantined rather than merely ignored. JSON Schema cannot
   check any of it, because the catalog side of the intersection is not in this document.
10. **mTLS, and who provisions the certificate.** TLS itself is no longer open: it is required
   for every non-cluster-internal path, edge heartbeat included, on the terms in "Transport"
   above. What remains is whether mutual TLS is required on top of bearer-over-TLS — the edge
   contract's own view is that it is a viable upgrade rather than the v1 requirement, since
   llama.cpp will not verify a client cert today — and which story provisions the certificate
   and the DNS name for the router's own TLS endpoint. 35.9 owns both answers.
11. **A pool's economics cannot be schema-checked here.** `entitlement_pool` names the pool;
   its `cost_class`, `credential_class` and `spillover` are declared in the catalog and read
   from there. This contract deliberately encodes no pool-to-cost mapping, because that would
   be ruling R16's own mistake in a different file - deriving economics from a name rather
   than from the entitlement. A router serving a `cost_class` that disagrees with the pool's
   catalog entry is wrong, and only a check against the catalog can say so.
12. **`placement_required` cannot be schema-checked.** It must equal the selected profile's
   catalog `hosting: local`, and nothing here can verify that, because hosting is catalog data
   this contract deliberately does not enumerate. It is a MUST on the router, in the same class
   as `x-placement` having to equal the `placement` field.
13. **`minimax/strong` is declared but unvalidated.** It is in the vocabulary and
   `selectable: false`, and stays that way until a later story demonstrates everything in its
   catalog `blocked_by` list: Token Plan billing rather than pay-as-you-go, Claude Code
   driving MiniMax M3 end to end, verifiable provider and model identity, tool calling, MCP
   compatibility, isolation from the normal Claude Max environment, observable quota
   behaviour, no automatic pay-as-you-go spillover, and correct fallback when MiniMax
   capacity is unavailable. Its catalog model claims `chat` only; tool calling gets claimed
   when it is demonstrated, not before.
## Sources

- `ADE-BOUNDARY.md` - authoritative for who owns what between Orca (the ADE) and the router,
  and for what a later change may not add without an explicit owner reversal.
- `.claude/.ai-docs/epics/EPIC-035-unified-agent-control-plane.md` - §3 invariants, §4 the
  interface sketch, §5 the heartbeat payload, §6 the catalog, §10a the design-gate result,
  §10b rulings R1-R7, §10d rulings R8-R11, §10ao the frozen MVP re-baseline that makes Orca
  the ADE.
- `.claude/.ai-docs/stories/WI-035-5-SPIKE-EVIDENCE.md` - the `x-placement` mechanics, the
  grouped-backend requirement, the retry/backoff findings and the 64 KiB replay cap, all
  measured against agentgateway v1.3.1.
- `edge/EDGE-WORKER-CONTRACT.md` - authoritative for heartbeat field semantics, transport
  requirements and per-host preemption rules. Merged; read the file, not this summary of it.
- `kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml` and its `README.md` -
  the five vocabularies, catalog document version 1.3.0, the entitlement pools, and the
  change protocol this
  directory has to stay in step with.
