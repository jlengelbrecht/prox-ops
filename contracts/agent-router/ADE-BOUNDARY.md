# The Orca/ADE ownership boundary

Frozen by STORY-035-24 against the EPIC-035 §10ao re-baseline. This file is authoritative for
who owns what between **Orca**, the Agent Development Environment, and **`agent-router`**.
`openapi.yaml`, `README.md` and the schemas describe the wire; this describes the seam.

`verify-ade-boundary.sh` reads the allowlists and the denylist below and enforces the
`/v1/route` half of them mechanically. Editing those blocks changes what the check enforces,
which is the point: growth is allowed, silence is not.

---

## 1. Who owns what

### Orca (the ADE) owns coding-session mechanics

- The **worktree**: creating it, its branch, its lifetime, its cleanup.
- The **terminal and process lifecycle**: starting, supervising, signalling and reaping the
  agent process; restarts; concurrency across sessions.
- The **execution environment**: environment variables, provider base URLs, credential
  material, isolation between sessions, and which secrets a session can see at all.
- The **agent-process host**: which machine the coding agent itself runs on.
- **Session state and identity**: whatever handle a session has, it is Orca's, and it exists
  nowhere in this contract.

### `agent-router` owns recommendation and policy, and nothing else

Its entire output vocabulary is the `ExecutionProfile` property set in §4. Exhaustively, it
recommends:

- a **harness name** (`claude` | `codex` | `devin`);
- a **model/intelligence profile** and an **effort** tier;
- an **entitlement/funding candidate** (`entitlement_pool`) and the attempt's `cost_class`,
  `metered` and `metered_denied` economics;
- ordered **fallbacks**, obeying the same rules as the primary;
- when `placement_required` is true, the **placement policy** that `/v1/place` later resolves
  the *model inference* under;
- provenance for a human reading a stamped story later: `rationale`, `catalog_version`,
  `catalog_document_version`, `expires_at`.

That list is the whole job. The router never manages a session, never chooses where an agent
process runs, never holds session state, and never touches a credential.

---

## 2. The seam is one-way

**Router recommends → the ADE consumes and executes.** Nothing flows back.

There is no router-to-session control channel, no execution callback, no session
registration, and no router-side knowledge that a recommendation was ever acted on. A
recommendation the ADE ignores leaves no trace in the router, by design: a router that knew
would be holding session state.

The one inbound operational signal the router accepts is `POST /v1/capacity/heartbeat` from
an edge worker. That is **about GPUs, not sessions** - VRAM, warmth, loaded models - and it
is a capacity report, not a session lifecycle event. It is the exception that proves the
rule, and it is the only one.

### What a future story may NOT add without an explicit owner reversal

Not "should avoid": may not. Each of these has an obvious-looking justification, which is why
each is named:

1. **A session registry.** No endpoint, field or store that records that a session exists,
   its identity, its status, or its outcome. "The router should know if the attempt
   succeeded" is how a recommendation service becomes a scheduler.
2. **Execution callbacks or webhooks.** No callback URL, no webhook target, no
   router-initiated call to a session. The seam has no return leg.
3. **Agent-process host selection.** The router may name a *model inference* placement
   policy. It may never name, prefer, constrain or hint at the machine the agent process runs
   on. `/v1/place` is structurally incapable of expressing agent-execution placement and must
   stay that way.
4. **Credential transport.** No request or response may carry provider credentials, API keys,
   OAuth material, session tokens or anything from which one could be derived.
   `entitlement_pool` carries a *name from a closed enum*, and that is the whole mechanism.

Reversing any of these is an owner decision, recorded here, in the same change that
implements it. There is no path where the code moves first.

---

## 3. Native harnesses own their own authentication

Claude Code (Anthropic Max) and Codex (ChatGPT subscription) run as **native CLIs inside
ADE-owned sessions, carrying their own account/session auth**. The ADE places that auth in the
session's own process environment; the router is not involved and does not know what is there.

This is the statement 35.16′ (ChatGPT subscription → Codex) and 35.17′ (Anthropic Max → Claude
Code) validate against. Both validate a subscription path *through* this execution model, so
if either finds it does not hold, that is live evidence against a frozen assumption and the
boundary gets re-opened rather than worked around.

Two consequences:

- **No gateway-side centralization of personal OAuth or session material.** Invariant 10
  stands: a subscription is not an API key, and the Anthropic Max path introduces no
  `ANTHROPIC_API_KEY`.
- **No router payload transports credentials, and no shape in the contract can express one.**
  `RouteRequest` and `ExecutionProfile` are closed (`additionalProperties: false`) with the
  property sets pinned in §4, and the denylist in §5 rejects the field names such a leak would
  arrive under.

### `entitlement_pool` names a decision, not an action

It identifies the approved funding/entitlement decision for the attempt. It does not instruct
anything to inject a credential or manage a provider session. A consumer reads it to know
*which entitlement was approved*; provisioning and isolating an environment that honours that
decision is the ADE's work.

---

## 4. The frozen property sets

These are the router's entire vocabulary for a `/v1/route` request and response, verified
against the contract as committed. `verify-ade-boundary.sh` parses the two blocks below and
fails if either schema's top-level property set differs from them in either direction, or if
`additionalProperties: false` is removed from either shape.

**Adding a property is not forbidden - it is made deliberate.** The pin fails until this
allowlist and the schema change in the same pull request.

`RouteRequest` (14 properties, `openapi.yaml` → `components.schemas.RouteRequest`):

<!-- allowlist:RouteRequest -->
```text
allow_metered
ambiguity
blast_radius
catalog_version
context_size
placement_policy
repo
requester
story_id
summary
tags
title
touched_paths
volume_hint
```

`ExecutionProfile` (14 properties, `schemas/execution-profile.schema.json`):

<!-- allowlist:ExecutionProfile -->
```text
catalog_document_version
catalog_version
cost_class
effort
entitlement_pool
expires_at
fallbacks
harness
metered
metered_denied
model_profile
placement_policy
placement_required
rationale
```

### Capabilities and tools

Required capabilities and tools are within the router's **recommendation semantics** - the
kind of thing it is legitimately in the business of recommending. But **no `tools` or
`capabilities` wire field exists on `ExecutionProfile` today**, and nothing in this contract
implies one does. Today the model profile implies its capability set via the catalog.

If 35.10 finds it needs one, that change amends this file's allowlist **and** the schema in
the same pull request. That is the sanctioned path for contract growth. The check exists so
the path cannot be taken quietly.

---

## 5. The semantic denylist

Property names and fixture keys in the route-scoped shapes are normalized to semantic tokens -
split on `_`, on `-`, and at camelCase boundaries, then lowercased - and matched against the
sets below. Tokenizing rather than matching words means `sessionId`, `session_id` and
`SESSION-ID` are the same finding, at any nesting depth.

Single forbidden tokens:

<!-- denylist:tokens -->
```text
session
worktree
pid
process
callback
webhook
credential
token
oauth
secret
password
apikey
```

The second group enforces §3 mechanically: the contract prohibits credential transport, so a
property name that *is* credential-shaped — `provider_token`, `oauth_session`, `apiKey` — fails
regardless of intent. (Values are not scanned; the tag value `secrets` in task metadata is data,
not a field name.)

Forbidden adjacent token pairs (each token is innocent alone; the pair is not):

<!-- denylist:pairs -->
```text
exec host
container id
api key
```

This applies to **names, never to prose**. A description may say "no session field exists" -
that sentence is the boundary being documented, not a violation of it.

### Scope: `/v1/route` only

The denylist applies to `RouteRequest`, `ExecutionProfile`, the nested route-specific
definitions they reference, the `/v1/route` inline examples in `openapi.yaml`, and
`examples/execution-profile/*.json`.

It deliberately does **not** apply to the heartbeat, status or place contracts. Operational
telemetry legitimately speaks of processes and hosts - that is a different concern, and a
check that cried wolf there would teach people to ignore it.

---

## 6. How a recommendation reaches the ADE today

Descriptive, not normative. `agent-flow-kit` is updated separately per the 35.12 rule, and
nothing here requires a particular frontmatter shape.

Today the planning flow calls `/v1/route`, a human accepts or deliberately overrides the
recommendation, and the result is stamped into the story's `route:` frontmatter. The ADE's own
flow reads that stamp and runs the session: it creates the worktree, starts the harness
process with the right environment, and supervises it. The router is not called again and is
not told what happened.

If the profile carries `placement_required: true`, `/v1/place` is called for the **model
inference** placement and the returned `x-placement` header travels on the inference requests.
That is a scheduling header consumed by agentgateway - which GPU serves the model - and never
an agent-execution instruction. Reading it as one is the boundary error this document exists
to forbid.

## 7. Policy validation stays policy-side

Any pre-execution validation of a stamped or overridden recommendation - does it still satisfy
`forbidden_for`, entitlement restrictions, metered-spend authorization, placement constraints -
is **routing-policy validation**. It sits outside Orca's session mechanics, and it is not the
router's either: the router is not in that path and cannot be.

**The freeze cuts both ways.** The router must not grow session mechanics, and Orca must not
become the routing-policy authority. Collapsing either direction loses the separation this
document exists to hold.

---

## Sources

- `.claude/.ai-docs/epics/EPIC-035-unified-agent-control-plane.md` §10ao - the frozen MVP
  re-baseline: Orca is the ADE, the router stays recommendation-only, `/v1/place` is
  model-inference placement only, and EPIC-035 builds no dispatcher.
- `README.md`, `openapi.yaml`, `schemas/execution-profile.schema.json` - the contract this
  boundary is frozen against.
