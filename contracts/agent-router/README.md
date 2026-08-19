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
  schemas/
    execution-profile.schema.json   POST /v1/route response
    heartbeat.schema.json           POST /v1/capacity/heartbeat request
    place-result.schema.json        POST /v1/place response
    error.schema.json               the error envelope, every endpoint
    status.schema.json              GET /v1/status response
  examples/
    execution-profile/  5 files     incl. the metered refusal
    heartbeat/          6 files     one per state, plus unmeasured hardware
    place/              4 files     placed and unavailable
    errors/            14 files     one per error code, plus both metered refusals
    status/             2 files     today, and once an edge node exists
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
- **This contract does not modify `agent-flow-kit`.** The kit is updated separately,
  afterwards, by someone else, against what is written here.

## Three axes, and the vocabulary that names them

Harness, model/capability profile and physical placement are three independent axes and no
field in this API collapses two of them. `/v1/route` decides the first two, `/v1/place`
decides the third. Resolving the same profile onto a different compatible GPU at dispatch
time is legitimate; substituting a different model is not.

Four vocabularies are read verbatim from ConfigMap `ai/agent-router-catalog`, catalog
document version **1.0.0**, `schema_version: 1`:

| Vocabulary | Values | Where it is pinned |
| --- | --- | --- |
| harness | `claude`, `codex`, `devin` | `execution-profile.schema.json#/$defs/harness` |
| model_profile | `local-code-fast`, `local-code-standard`, `local-general`, `local-unrestricted`, `claude/strong`, `openai/strong`, `devin/free` | `execution-profile.schema.json#/$defs/model_profile` |
| placement_policy | `prefer-warm-local`, `cluster-only`, `edge-only`, `any-24gb` | `execution-profile.schema.json#/$defs/placement_policy` |
| placement | `kserve-a5000`, `cachyos-7900xtx`, `bazzite-5090`, `laptop-rtx5000` | `place-result.schema.json#/$defs/placement` |

**`local-agent` is not in the harness enum.** It exists in the catalog as a reserved,
`supported: false` / `selectable: false` placeholder so the axis is future-proof, and the
catalog records `router_behaviour: refuse-to-emit`. llama.cpp, vLLM and llama-swap are
inference runtime, not autonomous coding harnesses. Making it selectable takes its own story
evaluating a specific harness, and no story in EPIC-035 does that. The schema encodes the
refusal twice - the enum excludes it, and a redundant `not` clause keeps the refusal true if
somebody later widens the enum without reading the description.

### Vocabulary coupling, and what has to move together

Enumerating catalog names in this contract buys mechanical checking and costs coupling. The
split is deliberate:

- **Enumerated**: harness, profile, policy and placement names. All four are compiled into
  something outside the catalog - a placement name appears verbatim as an agentgateway
  provider `name` and as the value of `x-placement`, so it cannot move without a coordinated
  change anyway.
- **Not enumerated**: `model_id`. The whole reason the profile indirection exists is that
  changing which physical model backs a profile is a pull request against the catalog and
  touches nothing else. Enumerating model ids here would break that guarantee.

So: **adding or renaming a profile, policy or placement is a catalog PR and a matching PR
against this directory.** Swapping the model behind a profile is a catalog PR alone. A
contract change that adds an optional field is a minor bump of `info.version`; removing a
field, re-typing one, or changing what a field means is a major bump.

`catalog_version` is a content digest - sha256 over the UTF-8 bytes of the rendered catalog
document (ConfigMap key `catalog.yaml`) exactly as mounted, before any parsing. The router
computes it at load time. It is not the catalog's semver `version` field, and it is never
written by hand.

**Two digests appear in these examples, and only one of them is real.**

| Digest | What it is |
| --- | --- |
| `sha256:280e5ccd…48da53` | The **real** digest of catalog 1.0.0 exactly as committed - `sha256` over `data["catalog.yaml"]` of `kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml`. Reproducible from the repository today. |
| `sha256:decafbad…decafbad` | A **placeholder**, obviously fabricated, carried by the examples that depict a catalog which does not exist yet. No router will ever emit it. |

The placeholder exists because a digest is a content commitment: two documents showing
different catalog contents cannot honestly share one. An example depicting a post-35.6 world
- an edge placement that is `selectable`, a profile with a second physical candidate, a
policy that now resolves - is describing a **later catalog**, and says so with a different
digest and a bumped `catalog_document_version`. Reusing the real 1.0.0 digest there taught
the opposite of the rule the field exists to enforce.

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
is a weaker model than `local-code-standard` in every respect except refusal behaviour, so
"it refuses less" is never a reason to prefer it. It is never auto-selected; an operator can
still choose it explicitly and own that, which is what
`examples/execution-profile/unrestricted-operator-choice.json` shows.

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

Either way the obligation is the same: authorizing the spend is a human decision, made by
re-submitting with `allow_metered: true` **from a principal that is allowed to make it**.
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

**The normal BMAD/dispatcher machine principal must not hold that permission.** It plans and
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

Worth recording: catalog 1.0.0 contains no `cost_class: metered` profile at all - Devin's
paid on-demand tier deliberately has no profile name to hide behind. So this path cannot fire
today. The shape is specified now so that it exists before the money does.

### `placement_required` - does this attempt need a placement at all

Every `ExecutionProfile` carries `placement_required`, and it is required rather than
optional because a client that does not know the answer cannot dispatch correctly.

- `true` - a **local** profile. The model is served through agentgateway on a specific GPU,
  so the client calls `/v1/place` with the stamped profile and policy and sends the returned
  `x-placement`.
- `false` - a **vendor** profile, resolved by the harness itself (the catalog records these
  as `resolved_by: harness`). The traffic never touches agentgateway. Launch the harness
  directly; calling `/v1/place` would be a round trip with nothing on the other end.

The client flow is therefore: `/v1/route` → if `placement_required`, `/v1/place` → dispatch;
otherwise launch the harness.

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
selection rather than an error. Enforcing "you must ask the router" belongs at the
client/dispatcher layer; it is a policy question, not a gateway capability.

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
started, a broken stream fails the execution attempt; starting a new attempt is the
client/dispatcher's job, never the gateway's.

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

The heartbeat examples in this directory therefore say `qwen36-27b`. The illustrative JSON
blob in `edge/EDGE-WORKER-CONTRACT.md` §1 still shows `qwen3.6-27b` there; its field table
types the field as an unqualified string and says nothing about which namespace, so this is
a namespace the edge contract left open rather than a rule it set. Correcting that example
belongs to 35.6, and is listed in the open items below.

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

> `edge/EDGE-WORKER-CONTRACT.md` §1.2 currently says a missed heartbeat and a 503 are the only
> two withdrawal signals the cluster acts on. That sentence is superseded by owner ruling R8
> and is being corrected in PR #1257 before it merges. The three-mechanism table above is the
> frozen version.

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
| `metered_denied` | 409 | route | `abort` | Every option is billable and none was authorized. Escalate to a human. **Never resend with `allow_metered: true` automatically.** |
| `metered_denied` | 200 (in-band note) | route | `re-plan` | A billable option was withheld and a non-metered substitute was returned. Proceed with it, or seek authorization. |
| `no_eligible_profile` | 409 | route | `re-plan` | Nothing satisfies the constraints. Relax one, wait for a pool, or authorize spend. Resending unchanged is a loop. |
| `no_eligible_placement` | **200**, `status: "unavailable"` | place | do not dispatch | Not an error envelope. See "An empty result is HTTP 200" above. Reason codes: `no_eligible_placement`, `policy_resolves_to_nothing`, `all_candidates_withdrawn`, `constraint_unsatisfiable`. |
| `rate_limited` | 429 | all | `retry` | Back off; honour `retry_after_seconds`. If this fires routinely, the caller is calling per inference request instead of caching for `ttl_seconds`. |
| `catalog_unavailable` | 503 | all | `retry` | **Transient only**: no catalog is loaded yet. The same request succeeds unchanged once the load completes. Retry with backoff. |
| `catalog_schema_unsupported` | 503 | all | `abort` | **Permanent**: the router refused a catalog whose `schema_version` it does not implement, rather than best-effort parsing an unknown shape. Retrying cannot clear it - an operator deploys a router that understands the catalog, or reverts the catalog. Alert. |
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

## Examples

31 files, every one validated in CI-shaped commands below. Every example is also referenced
from `openapi.yaml`, so an example that stops being reachable from the spec is visible.

Each one states which catalog it is drawn against. Examples on the real 1.0.0 digest describe
the cluster as it is today; those carrying the `decafbad…` placeholder describe a later
catalog, and are marked below.

**`examples/execution-profile/`** - `/v1/route` responses.

| File | Shows |
| --- | --- |
| `local-code-standard.json` | The ordinary case: free local profile, two fallbacks. **`placement_required: true`** - a local result, so the client places it - with both vendor fallbacks at `false`. |
| `security-tagged-excludes-unrestricted.json` | `forbidden_for` firing as a hard exclusion on a critical-blast-radius task. **`placement_required: false`** - the vendor-hosted result: no placement call, the harness resolves it. |
| `metered-denied-substituted.json` | **The refusal case.** Billable option withheld, non-metered substitute returned, `metered_denied` note attached. |
| `docs-low-risk-cluster-only.json` | Cheapest profile, empty `fallbacks` on purpose. |
| `unrestricted-operator-choice.json` | The only way `local-unrestricted` is ever reached: named explicitly, no forbidden tag present. |

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
| `placed-kserve-only-candidate.json` | Today's real answer: one candidate, readiness `unknown` because KServe is not probed. |
| `placed-warm-edge.json` | **Later catalog** (`decafbad…`): a strict warm preference beats `prefer_order` once an edge placement is selectable. |
| `unavailable-policy-edge-only.json` | `edge-only` on catalog **1.0.0** - every placement it names is `status: planned`. Explicit empty result, not a fall-through. |
| `unavailable-all-withdrawn.json` | **Later catalog** (`decafbad…`): interactive, draining and silent candidates; note the warm-but-ineligible one. Withdrawal is only meaningful for a placement that was selectable in the first place. |

**`examples/errors/`** - one file per envelope code, 14 in total. Three of them carry the
`decafbad…` placeholder because they are about a catalog that has moved on:
`unknown-profile.json` and `unknown-placement-policy.json` show a stamped name the loaded
catalog has dropped, and `catalog-version-stale.json` shows the caller's 1.0.0 digest against
the newer one the router is serving.

**`examples/status/`** - `status-today.json` is catalog 1.0.0 exactly as it stands, on the
real digest, with one usable placement. `status-with-edge.json` is the same view after 35.6
has landed: `cachyos-7900xtx` selectable and heartbeating, `local-code-standard` with a second
physical candidate, `edge-only` resolving. Those are catalog-static facts, so it carries the
`decafbad…` placeholder digest and `catalog_document_version: 1.1.0` rather than pretending to
be 1.0.0 with different contents. It is also the example that shows a heartbeat re-exposed
verbatim inside a placement.

The examples that describe a world which does not exist yet are the ones naming
`cachyos-7900xtx` as usable: `placed-warm-edge.json`, `unavailable-all-withdrawn.json` and
`status-with-edge.json`. In catalog 1.0.0 that placement is `status: planned` /
`selectable: false` and cannot be returned by `/v1/place` at all, which is why they sit on a
later catalog digest instead of contradicting the one they claim.

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

Plus a check that the four endpoints exist, and a grep proving no tracked file here carries a
literal LAN address - placeholders such as `<edge-host>` and `<node-id>` are used throughout,
matching the edge worker contract.

The schemas do real work rather than describing the examples back to themselves. They reject,
among others: `harness: local-agent`; a profile or placement name not in catalog 1.0.0;
`cost_class: metered` with `metered: false` and the reverse; a `metered_denied` note on a
metered profile; `state: INTERACTIVE` with `interactive: false`; a header other than
`x-placement` in a `PlaceResult`; an `unavailable` result that still carries a placement; and
`no_eligible_placement` used as an error code.

Four rules that were prose-only are now checked as well, because a rule this document states
and the schema permits is a rule an implementer reading only the schema will break:

- a result-level reason code (`placed_warm`, `no_eligible_placement`) inside `alternatives[]`,
  where only candidate-level codes belong;
- a `selectable: false` harness in `/v1/status` with no `router_behaviour` saying what the
  router does instead;
- `state_source: "heartbeat"` on a placement with no heartbeat attached, or
  `state_source: "static"` on one that has heartbeated;
- a heartbeat with a null `gpu.model` or `gpu.arch`, which the edge contract types as strings
  whose unmeasured form is the literal `"unmeasured"`.

## Known tensions and open items

Recorded rather than resolved, because each belongs to a story that has not run yet.

1. **`gpu.utilization_pct` is not nullable** while the two VRAM fields are. That is what the
   edge contract's field table says, and where the two could drift the edge contract wins, so
   the schema follows it. A node that genuinely cannot read utilization is out of contract
   today; 35.6 is where that gets found out.
2. **No metered profile exists in catalog 1.0.0**, so `/v1/route` cannot emit `metered: true`
   at all right now. The refusal shape is specified ahead of the money on purpose.
3. **The heartbeat interval is router-side** and deliberately not added to the payload. 35.9
   sets the real value; 30 s / 90 s in these examples is a starting point, not a measurement.
4. **Harness-to-profile reachability is unproven.** A cloud harness reaching a LAN-only
   gateway has not been demonstrated, and nothing in this contract asserts it can. The catalog
   README carries the same open item against 35.10.
5. **`RouteRequest` has no `required_capabilities`.** The router infers capability needs from
   the task metadata. If 35.10 finds that inference too weak, adding the field is an additive
   minor bump.
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
11. **`placement_required` cannot be schema-checked.** It must equal the selected profile's
   catalog `hosting: local`, and nothing here can verify that, because hosting is catalog data
   this contract deliberately does not enumerate. It is a MUST on the router, in the same class
   as `x-placement` having to equal the `placement` field.
12. **The edge contract's example still shows the upstream model id.** Its §1 JSON blob has
   `active_model: "qwen3.6-27b"`; the canonical rule frozen here is that the field carries the
   catalog `model_id` (`qwen36-27b`). The field table types it as an unqualified string and
   never states a namespace, so this is a gap being filled rather than a contradiction — but
   the example should be corrected when 35.6 next touches that document.

## Sources

- `.claude/.ai-docs/epics/EPIC-035-unified-agent-control-plane.md` - §3 invariants, §4 the
  interface sketch, §5 the heartbeat payload, §6 the catalog, §10a the design-gate result,
  §10b rulings R1-R7, §10d rulings R8-R11.
- `.claude/.ai-docs/stories/WI-035-5-SPIKE-EVIDENCE.md` - the `x-placement` mechanics, the
  grouped-backend requirement, the retry/backoff findings and the 64 KiB replay cap, all
  measured against agentgateway v1.3.1.
- `edge/EDGE-WORKER-CONTRACT.md` (PR #1257) - authoritative for heartbeat field semantics,
  transport requirements and per-host preemption rules.
- `kubernetes/apps/ai/agent-router-catalog/app/catalog-configmap.yaml` and its `README.md` -
  the four vocabularies, catalog document version 1.0.0, and the change protocol this
  directory has to stay in step with.
