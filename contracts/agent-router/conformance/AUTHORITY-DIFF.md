# Authority diff: what counts as expanding what the router may do

`authority-diff.sh` takes two agent-router catalog documents and answers one
question about the change between them:

> Does this change hand out authority that was not there before?

```console
$ bash authority-diff.sh old-catalog.yaml new-catalog.yaml
authority-diff: old-catalog.yaml -> new-catalog.yaml
  narrowing profiles.local-code-standard.forbidden_for   + "security"   [restriction list: ...]
summary: 0 expanding, 1 narrowing, 0 neutral (1 atom(s))
verdict: narrowing
```

Either document may be a bare catalog (the YAML under `data."catalog.yaml"`) or
the whole ConfigMap that ships one; the envelope is unwrapped and never
appears in a verdict. The tool reads two files and nothing else - no network,
no cluster, no credential, and no dependency on the live catalog.

`verify-authority-diff.sh` beside it asserts every rule below against seeded
fixture pairs in `fixtures/authority-diff/`, in both directions.

Both scripts are invoked as `bash <script>`; if you add them to a workflow that
runs `./<script>`, set the executable bit first.

## The question, stated precisely

Take `Authority(C)` to be the set of execution attempts catalog `C` admits - a
harness, a profile, a physical candidate, a funding source, a placement policy,
and the task context the profile may be selected for. This is the Git-managed
half of the rule the contract freezes as

    effective capability = Git/catalog authority INTERSECT heartbeat observation

Then:

| Verdict | Means |
| --- | --- |
| `expanding` | `Authority(new)` admits an attempt `Authority(old)` did not |
| `narrowing` | `Authority(new)` withdraws an attempt and admits none |
| `neutral` | the two admit the same attempts |

A change containing both is **`expanding`**. This is a frozen rule and it is
also the only defensible one: the verdict exists to make a reviewer look at
authority arriving, and a withdrawal elsewhere in the same PR does not pay for
one. Netting them out would let "we removed a placement, so the new
`selectable: true` is fine" pass as neutral.

## Why atoms rather than set inclusion

Computing `Authority(C)` and comparing the two sets directly is not tractable -
it means resolving every profile against every policy against every placement
against every entitlement, which is the router's whole job and would make this
tool a second implementation of it, free to disagree with the first.

So the change is decomposed into **atoms**: one per changed leaf value, one per
changed collection membership, one per added or removed table entry. Each atom
is classified on its own by the rule family its field belongs to, and the atoms
are combined by **dominance** - any `expanding` atom wins, else any `narrowing`
atom wins, else `neutral`.

The cost of this is real and worth naming: an atom is classified from the field
alone, with no knowledge of what else the document says. Adding a model nothing
references reads as expanding even though no profile can reach it yet. That is
the conservative direction, and a false "look at this" is a cheaper failure than
a false "nothing to see".

Rules are keyed on the **leaf field name**, never on a full path. `cost_class`
means one thing whether it sits on a pool, a profile or an entitlement
candidate, and one rule covers all three; splitting them per table is how two
copies of one concept quietly drift apart. It also means the classifier works
on a fragment - a two-line document carrying a single `selectable:` is
classified by the same rule as the full catalog.

## The taxonomy

| Family | Fields | Direction | Why |
| --- | --- | --- | --- |
| **documentary** | `description`, `notes`, `reason`, `runtime`, `source`, `version`, `updated`, `epic`, `story`, `schema_version`, `consumers`, `upstream_id_form`, anything ending `_source` / `_notes` / `_evidence` / `_estimate`, the `gpu` subtree, `vram_gb_nameplate`, `idle_retention_min`, `resolves_today`, `preemptible`, `scale_to_zero`, `warm_preference`, `warm_bonus_rank_shift` | always neutral | Prose, provenance and scheduling hints. None of them decides whether an attempt is admitted - the last few decide which admitted candidate wins, which is the same reason ordering is neutral. |
| **gate** | `selectable`, `supported`, `allow_cold_start`; `status` | to admitting = expanding, away = narrowing | Only `true` admits (only `available`, for `status`). `false`, `null` and absent are one answer. `selectable: true` means the entry MAY TAKE PART IN AN APPROVED EXECUTION DECISION - the flip is exactly where authority moves. |
| **closed value** | `spillover` (closed: `none`), `router_behaviour` (closed: `refuse-to-emit`) | opening = expanding, closing = narrowing | Invariant 4 as data: `spillover: none` is not a default that happened to be chosen, it IS the rule that exhaustion never promotes to billable spend. Opening it creates a path from an exhausted entitlement to money. |
| **restriction list** | `forbidden_for`, `blocked_by`, `requires` | member added = narrowing, removed = expanding | A `forbidden_for` tag is a hard exclusion that fires however well the profile otherwise fits, and `blocked_by` / `requires` are declared preconditions. Members subtract. |
| **grant list** | `capabilities`, `placements`, `prefer_order`; `physical` and `entitlements` (records, identified by `{model_id, placement}` and `{pool}`) | member added = expanding, removed = narrowing | Each member is a way for an attempt to be admitted: a capability guaranteed, a GPU the profile may resolve onto, a policy that may reach a placement, a source that may pay. |
| **capability claim** | `min_context`, `max_context`, `vram_gb`, `concurrent_models` | higher = expanding | A claim is authority. `min_context` is the floor a profile GUARANTEES, so raising it makes the profile eligible for work it could not previously satisfy - the catalog side of the rule that a heartbeat may never report itself into a capability the catalog has not granted. |
| **hard constraint** | `min_vram_gb`, `min_free_vram_gb` | higher = narrowing | A floor admits fewer candidates the higher it goes. |
| **lattice** | `cost_class` (`metered` < `subscription` < `free`), `alignment` (`standard` < `unrestricted`) | higher rank = expanding | Ranked by reachability. A candidate that moves from `metered` to `free` needs fewer things to be true before it can run; `unrestricted` admits work a refusal-trained model would decline. An unrecognized value on either lattice falls through to the conservative default. |
| **order** | any list | neutral | Order is preference, not permission. Reordering `prefer_order` inverts rows of the frozen placement decision table, and it is still not authority: the same candidates are admitted. Reported as a `reordered` atom so the change is acknowledged rather than passed over in silence. |
| **conservative default** | everything else | expanding, in both directions | See below. |

### The two absence rules

An absent key and an explicit `null` mean the same thing in this catalog, so
every rule has to say what that thing is. There are two answers, and which one
applies is what separates the two numeric families:

- **Absence of a claim grants nothing.** `min_context: null` guarantees no
  context at all; `vram_gb: null` means no measurement exists and cannot
  satisfy a floor. Null is the bottom of the scale.
- **Absence of a restriction restricts nothing.** `min_vram_gb: null` is no
  floor; an undeclared `spillover` or `alignment` is the open value. Null is
  the permissive end.

The same literal `null`, opposite meanings, which is why
`capability-raise-new.yaml` and `constraint-raise-new.yaml` are separate
fixtures asserting opposite verdicts for the same numeric shape.

### The conservative default

A field no rule table names is classified **expanding, in either direction**.
Not neutral, and not "narrowing when it is removed":

- This document bounds everything the router may do. A field that arrives in it
  arrived for a reason, and the reason a reviewer needs to hear about is the one
  where it grants something.
- Direction is unknowable for an unknown field. If a later story adds
  `denied_for:`, then removing an entry from it is an expansion and adding one
  is a narrowing - the exact inverse of a grant list. Guessing either way is
  worse than refusing to guess.
- The refusal is cheap to clear: teach the field to the rule table, add a
  fixture, add a case row. That is one small PR, in the change that introduces
  the field, by the person who knows what it means.

`unknown-key-new.yaml` pins this, and pins it in both directions on purpose.

### Declaring an entry is not granting authority

The one asymmetry worth arguing about. Adding a table entry that is
`selectable: false` (or `supported: false`, or `status: planned`) is
**neutral**, and so is removing one. Adding an entry that admits is expanding;
removing one that admits is narrowing.

The contract already turns on this distinction. `minimax/strong` is in the
contract's profile vocabulary while the catalog declares it `selectable: false`
with a `blocked_by` list, and planned placements are named years before they
work, precisely so a name can be frozen before it does anything - otherwise
every flip would need a contract PR alongside it, which is the drift window the
404 taxonomy describes. Authority moves at the flip, and the flip is a gate
atom, which is caught.

A rename is an addition plus a removal, so renaming an admitting entry comes
back `expanding`. That is correct rather than pedantic: a placement name appears
verbatim as an agentgateway provider name and as the `x-placement` header value,
so a rename is a breaking change across the catalog, the gateway config and
every caller.

## What this deliberately does not do

- **No referential integrity.** It does not check that `physical[].model_id`
  resolves, that a `prefer_order` entry names a declared placement, or that
  `capabilities` really is the intersection over `physical[]`. Those are catalog
  validation rules with their own home; conflating them here would make one
  check answer two questions and fail for two reasons.
- **No cross-object reasoning.** Each atom is classified alone. Adding a model
  no profile references reads as expanding; so does adding a pool nothing draws
  on. Conservative by construction.
- **No semver opinion.** It does not care whether `version` was bumped -
  `verify-digests.sh` and review own that. `version` is documentary here for a
  hard reason: the catalog's own change protocol REQUIRES a bump on every edit,
  so a classifier that counted it would return `expanding` for every change ever
  made and be worth exactly nothing.
- **Lists are sets.** Duplicate members collapse. No catalog list is meaningfully
  a multiset.
- **Leaf names can collide.** A table entry named `description` or `gpu` would be
  read as documentary. No catalog name comes close, and the alternative -
  path-scoped rules - buys nothing but a second place for the same concept to
  drift.
- **It does not read the live catalog**, and it is not wired into CI. Wiring it
  in touches workflow files, which is the repository owner's decision - the same
  position `verify-digests.sh` records.

## Fixture coverage

Every rule family above is asserted by `verify-authority-diff.sh`, in both
directions, against `fixtures/authority-diff/`. `base.yaml` is the old side of
every case that does not name its own; each `*-new.yaml` is `base.yaml` plus the
one edit named in its own header.

| Case | Fixture (vs `base.yaml`) | Forward / reverse | Family |
| --- | --- | --- | --- |
| `identical` | `base.yaml` | neutral / neutral | nothing changed |
| `envelope` | `envelope-configmap.yaml` vs `envelope-plain.yaml` | neutral / neutral | the ConfigMap envelope is not catalog data |
| `documentary` | `documentary-new.yaml` | neutral / neutral | documentary |
| `prefer-order-reorder` | `prefer-order-reorder-new.yaml` | neutral / neutral | order |
| `declaration-only` | `declaration-only-new.yaml` | neutral / neutral | non-admitting entry |
| `selectable-enable` | `selectable-enable-new.yaml` | expanding / narrowing | gate |
| `status-withdraw` | `status-withdraw-new.yaml` | narrowing / expanding | gate (`status`) |
| `forbidden-add` | `forbidden-add-new.yaml` | narrowing / expanding | restriction list |
| `placement-add` | `placement-add-new.yaml` | expanding / narrowing | admitting entry + grant list |
| `physical-add` | `physical-add-new.yaml` | expanding / narrowing | grant record list |
| `entitlement-add` | `entitlement-add-new.yaml` | expanding / narrowing | admitting entry + grant record list |
| `capability-raise` | `capability-raise-new.yaml` | expanding / narrowing | capability claim |
| `constraint-raise` | `constraint-raise-new.yaml` | narrowing / expanding | hard constraint |
| `spillover-open` | `spillover-open-new.yaml` | expanding / narrowing | closed value |
| `mixed` | `mixed-new.yaml` | expanding / expanding | dominance |
| `unknown-key` | `unknown-key-new.yaml` | expanding / expanding | conservative default |

Plus four tool-error paths - no arguments, one argument, a missing file, and
`broken.yaml` - each of which must **exit non-zero and print no verdict at all**.
A classifier that answered `neutral` because it could not read its input would
report "nothing changed" for a document it never parsed, and the one place that
answer gets read is a review of a change to the document that bounds everything
the router may do.

The verifier also fails if a committed fixture is not read by any case, and if
one of the required classes loses its case. A suite that can pass vacuously is
worse than no suite.

## Adding a field to the catalog

1. Put the field in the right table in `authority-diff.sh` - or decide it is
   documentary and say so there.
2. Add a fixture pair (usually `base.yaml` plus a new `*-new.yaml`) and a row in
   `CASES` in `verify-authority-diff.sh`, with both directions.
3. Add a row to the taxonomy table above with the reason, not just the verdict.

Until step 1 happens, the field is unrecognized and every change to it comes
back `expanding`. That is the tool working, not the tool broken.
