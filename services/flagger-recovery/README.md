# flagger-recovery

Stdlib-only Python that resolves an immutable identity for a Flagger canary
candidate and persists a durable record of it. No third-party dependencies,
no `subprocess`, no `eval`/`exec` — an AST gate in the story's test suite
enforces that. The identity resolver and API reader never write to the
cluster; the record store's only write is a ConfigMap `create`.

## Why identity, not phase

FRP-004's canary matrix (`_bmad-output/epics/flagger-recovery-pilot/outputs/canary-matrix.md`,
finding F8) showed that a `git revert` to the last promoted spec runs no
Flagger analysis and leaves `Canary.status.phase` at `Failed` forever —
Flagger's controller explicitly refuses to start an analysis once the new
pod-template hash equals `status.lastPromotedSpec` (`pkg/canary/spec.go`,
v1.45.0). Recovery automation that waits for `phase == Succeeded` after a
revert will wait forever, even though the cluster is healthy again.

`flagger_recovery.identity` therefore builds a `CandidateIdentity` once, when
a candidate is first seen — from the Canary's own tracked hash, the candidate
pods' image digests, the HelmRelease chart version, the OCIRepository digest,
and the Git revision Flux applied — and never re-derives it from
`status.phase`. `is_manual_rollback(canary_status, new_template_hash)` is the
one helper that reads the phase-is-not-state finding directly: it is `True`
exactly when the new hash equals `lastPromotedSpec`, which is the signal that
a revert needs a distinguishing follow-up change before a real analysis will
run again.

**Two hashers that must never be compared.** `CandidateIdentity.template_hash`
is `Canary.status.lastAppliedSpec` — Flagger's own hash of the pod template.
`CandidateIdentity.replicaset_hash` is the selected ReplicaSet's
`pod-template-hash` label — the Deployment controller's own hash of the same
template. The two hashers are different algorithms and their outputs never
coincide, even for the identical template, so the candidate ReplicaSet cannot
be found by comparing `template_hash` to `pod-template-hash`. `resolve()`
instead selects the ReplicaSet owned by the Deployment whose
`deployment.kubernetes.io/revision` annotation equals the Deployment's own —
refusing if the Deployment lacks that annotation or if zero or more than one
ReplicaSet matches. `template_hash` remains the identity Flagger tracks (and
what `is_manual_rollback` compares); `replicaset_hash` is recorded only for
observability and must never be compared to `template_hash`.

`flagger_recovery.record` persists a `DeploymentRecord` per (canary,
template-hash, phase) as a ConfigMap in `flagger-system`. The idempotency key
is `sha256(namespace/canary/template-hash/phase)[:32]`; a duplicate write is
detected via the Kubernetes API's own `409 AlreadyExists` on `create`, then
confirmed with a follow-up GET that checks the existing ConfigMap's labels
actually belong to this record before reporting it as a duplicate — never a
read-modify-write, never a PATCH or PUT. A 409 whose GET shows a different
record (or that can't be resolved after one retried create) raises
`ForeignConfigMap` instead, so the caller can log and refuse rather than
assume its record was ever stored.

## Authentication and event intake

Flagger's webhook definition cannot reference a Secret, so the receiver
authenticates callers with a shared token that travels in the Canary webhook
entry's `metadata` map (`flagger_recovery.auth`). `load_token()` reads it from
a mounted Secret file or the environment and raises `TokenUnavailable` rather
than returning an empty string — an unconfigured token must never degrade into
an open endpoint. `presented_token()` prefers an `X-Flagger-Recovery-Token`
header where the installed Flagger can send one, and `token_matches()` compares
in constant time.

`flagger_recovery.inbox` turns an authenticated webhook into exactly one
durable event. The idempotency key is
`sha256(namespace/name + checksum + phase + hook)[:32]`, so a redelivery of the
same hook lands on the same ConfigMap name and the store's `create` reports it
as a duplicate without writing anything. `checksum` is the payload's own field
and **not** `status.lastAppliedSpec` — see "The payload checksum" below for what
it actually is and how an event is tied to a `DeploymentRecord`. The shared token
is stripped from `metadata` before the event is stored, and `metadata` values
are bounded and coerced to strings: nothing in a payload is trusted for
anything beyond being recorded, and nothing in one is ever executed.

Records and non-record documents share one storage path. `record.Document` is
a ConfigMap with a free-form JSON payload, named
`flagger-recovery-<kind>-<key>` and labelled `flagger-recovery/kind=<kind>`
(`event` here; `proposal` in the follow-up). Both go through the same
create-only `ConfigMapStore._create`, which is the one write in this package.

Label values for an event, all set by `inbox`:

| label | value |
|---|---|
| `app.kubernetes.io/part-of` | `flagger-recovery` |
| `flagger-recovery/kind` | `event` |
| `flagger-recovery/canary` | `<namespace>.<canary>`, clamped by `label_value()` |
| `flagger-recovery/hook` | `pre-rollout`, `post-rollout` or `event` |
| `flagger-recovery/phase` | the payload's `phase`, or `none` |
| `flagger-recovery/status` | `received` or `attribution-pending` |
| `flagger-recovery/template-hash` | the payload's `checksum`, or `none` |

`phase` and `checksum` come from an untrusted payload, so `label_value()`
clamps anything that is not a valid Kubernetes label value to a short, stable
hash form instead of rejecting the event.

**Records and documents are labelled differently, and one label name lies.**
Records carry `flagger-recovery/phase`, `flagger-recovery/template-hash`,
`flagger-recovery/checksum` and no `flagger-recovery/kind` at all; documents
(events, proposals) carry `flagger-recovery/kind` and no checksum. So
`-l flagger-recovery/kind` lists only documents. And on a *document*,
`flagger-recovery/template-hash` holds the payload's `checksum`, while on a
*record* the same label name holds the real `status.lastAppliedSpec` — never
equal, so do not join on that label across the two kinds.

```sh
# every object this service owns
kubectl get cm -n flagger-system -l app.kubernetes.io/part-of=flagger-recovery
# records only, and the rollout each one belongs to. Select on the *absence* of
# a kind: an event's flagger-recovery/phase is the payload's phase, so a
# phase=candidate selector would also match a hook that reported that phase.
kubectl get cm -n flagger-system -l 'app.kubernetes.io/part-of=flagger-recovery,!flagger-recovery/kind' \
  -L flagger-recovery/template-hash,flagger-recovery/checksum
# events and proposals only
kubectl get cm -n flagger-system -l flagger-recovery/kind=event
```

## The payload checksum, and why it is not the template hash

Flagger fills every payload's `Checksum` from `canaryChecksum(canary)` in
`pkg/controller/webhook.go` (v1.45.0), which is `canary.ComputeHash` over a
struct *containing* `status.lastAppliedSpec` and the tracked ConfigMap/Secret
checksums; `status.lastAppliedSpec` is that same `ComputeHash`
(`pkg/canary/spec.go`, FNV-1a + `rand.SafeEncodeString`) applied to the target's
pod spec. The payload checksum is therefore a hash *of* `lastAppliedSpec`: the two
look alike and never compare equal — the pilot's first live rollout sent
`checksum: 5f5697644f` while its `lastAppliedSpec` was `6d4d7b659`. That is the
third hash here that looks joinable and is not (see `pod-template-hash` above).

Records stay keyed by `template_hash`, because that is what `is_manual_rollback`,
the promoted-record lookup and FRP-007's freshness check compare against. The
bridge is an **index, not the key**: `pre-rollout` is the one moment both values
are in hand, so it writes the payload checksum onto the candidate record as the
`flagger-recovery/checksum` label and a `checksum` field in `record.json`. Every
later hook of that rollout carries the same checksum, so
`store.find_candidate(canary, checksum)` finds the record by label selector.

Deliberately *not* done: re-resolving the identity live at post-rollout time. A
late hook for a superseded revision would then attribute to whatever
`lastAppliedSpec` says now — the wrong candidate, which NFR4 forbids. An
unresolvable checksum answers `202 AttributionPending` instead, and two records
sharing a checksum (reachable only if `label_value()` clamps two hostile values
together) refuse rather than pick one. Records written before this index existed
have no checksum label and, the store being create-only, can never gain one; the
reconcile pass counts their events `events_unattributable` with a logged reason
rather than pending forever, since a finished rollout never re-sends its hooks.

## The receiver server

`flagger_recovery.server` is a stdlib `http.server` on port 8080, no
framework. `Receiver.handle(hook, headers, body)` is the whole decision
surface — it takes bytes, not a socket, so every response code is
unit-testable without a listener; `_Handler` is a thin HTTP shim over it.

Routes: `POST /hooks/pre-rollout`, `POST /hooks/post-rollout`,
`POST /hooks/event`, `GET /healthz`, `GET /readyz`. Every other path is 404,
and a `GET` on a hook path or a `POST` on a health path is 405. Bodies are
capped at 64 KiB (`413` before the body is even read, from `Content-Length`)
and must be a JSON object (`400` otherwise); a missing or wrong token is
`401` with no state change; every rejection is checked against the store's
write counter in tests, never just the response code.

`pre-rollout` resolves the candidate identity live through `CandidateSource`
(`kube.CandidateReader` in production) and stores a `candidate`
`DeploymentRecord`, per FRP-005's contract; a resolution refusal (no
candidate pods yet) is `202 AttributionPending` with a retry hint, never a
5xx — Flagger halts the rollout on a failed `pre-rollout` webhook, and "no
pods visible yet" is not a reason to fail somebody's release. A `Succeeded`
`post-rollout` re-states the identity already recorded at `pre-rollout` as a
`promoted` record — found through the checksum index, never a live re-resolve,
because by the time this hook fires Flagger has already scaled the candidate
pods away.

Flagger keeps the connection open after a hook's response rather than closing
it, so the handler's 15 s socket timeout expires on every single hook and
`http.server` reports it through `log_error`. `_Handler.log_error` logs that one
condition at DEBUG; every other error still goes through the sanitising
`log_message` at INFO. The timeout itself is unchanged.

Everything else — a `Failed` `post-rollout`, and every plain `event` hook —
flows through a `decider` callable injected into `Receiver`. The default
answers `Ignore("decider-not-installed")`, which is recorded as the accepted
event's `detail`; `decide.decider(store, live_for)` is the real one, and
`_main()` installs it.

## How a failure becomes a proposal

`decide(record, event, live)` is pure — every live read arrives through the
four-method `LiveState` protocol, `kube.LiveCanary` in production — and never
reads `status.phase` as the success signal. That is F8 again: a revert to the
last promoted spec runs no analysis and leaves the Canary `Failed` forever, so a
phase-driven receiver would keep proposing corrections for an already-fixed
failure. The rules, in order: a `Succeeded` `post-rollout` is `Register` (first,
because `lastPromotedSpec` has just become the candidate's own hash); anything
that is not a `Failed` `post-rollout` is `Ignore(not-a-terminal-failure)`; a
failure with no candidate record indexed under its checksum is
`Refuse(no-candidate-record)`. From there every comparison is in the
template-hash space, taken from the record the checksum found — the payload
checksum is only ever the lookup. A record whose `template_hash` already equals
`lastPromotedSpec` is `Ignore(manual-rollback-restored)`, via
`identity.is_manual_rollback`; a `template_hash` that is no longer the live
`lastAppliedSpec` or the hash the target Deployment carries is
`Ignore(superseded)`, because correcting it would revert unrelated work; a
primary not serving
`lastPromotedSpec` is `Refuse(primary-not-serving-promoted)`, and a failing
functional check `Refuse(functional-check-failing)`. Only then,
`ProposeCorrection`. `LiveCanary` answers `None` — which refuses — for a hash
whose Deployment has not observed its spec, and for a primary mid-rollout or
degraded; it never replica-checks the target, which Flagger scales to zero.

A proposal is bounded by branch `flagger-pilot` and path prefix
`kubernetes/pilot/flagger-pilot/`. It carries both identities, both source shas,
and one `correction`: `revert-commit <sha>` when the failed revision is exactly
one commit ahead of the promoted one, otherwise `restore-file
kubernetes/pilot/flagger-pilot/helmrelease.yaml to <sha>`; when neither can be
established (no promoted revision, a revision off the pilot branch, an unusable
sha) `correction` is null and `requires_decision` is true. Its key is the
candidate identity, so repeated `Failed` hooks land on the same
`flagger-recovery/kind=proposal` document and the store's `409` deduplicates.
`decide.reconcile(store, live)` runs at startup and every
`RECONCILE_INTERVAL_SECONDS` (default 300, floor 10, `0` disables the timer),
deciding `attribution-pending` events whose record has since landed and counting
proposals open or superseded — derived from live state, never stored. A pending
event with no record under its checksum splits two ways: a `pre-rollout` counts
as `events_pending`, because Flagger redelivers it and the in-band retry re-runs
resolution; anything else counts as `events_unattributable` and is logged with
its reason, because that rollout is over and no record will ever appear.

## The Git correction writer

`flagger_recovery.policy` holds the bounds as literals — `ALLOWED_REPOSITORY`, `ALLOWED_REF`,
`ALLOWED_PATH_PREFIX`, `ALLOWED_TARGETS` — and `evaluate(proposal, live, tree)` is the pure ladder that
applies them. `flagger_recovery.gitwriter` is the only thing that can write, through stdlib `urllib`
against `api.github.com` and no other host, with no `git` binary, no shell, and a credential that arrives
as a callable rather than from disk or the environment. The transport follows no redirect, so the token is
never re-sent anywhere GitHub names.

**The two correction forms.** The design says "one file, one field"; the shipped proposal says
`restore-file <path> to <sha>` or `revert-commit <sha>`. This implements the proposal. `restore-file`
installs the target file as it was at that revision — the tree entry reuses the blob sha already there, so
no content is uploaded and the commit provably carries the historical bytes. Only a blob does: a symlink,
a submodule or a directory at that path answers `unusable-sha`, which is what makes restating mode
`100644` safe. `revert-commit` is accepted only when the diff it undoes touches exactly one file, under
the prefix, and that file is the target; anything wider is `mixed-scope`. Both reduce to one path in one
tree, parented on the branch head and pushed with `"force": false`.

**The restore revision is bounded to this branch.** Before anything is written, `compare` must place the
branch head on or after the revision being restored; a fork's PR head, a pre-rebase commit, or an object
GitHub will not compare answers `restore-not-on-branch`. Without it the committed bytes are whatever
`last_promoted_source_sha` names, and any GitHub account can put an object into a public repository's
store. The bound is one file where the design said one field, so every change to `helmrelease.yaml`
between the promoted and failed revisions is reversed, not only the image tag; the owner accepted that
width on 2026-09-07 given the revision itself is now bounded to the pilot branch's own history, and a
field-level restore is the tightening FRP-009 may recommend.

**The refusal set** (`policy.REFUSAL_REASONS`, closed): `corrections-disabled` (the module switch is off),
`requires-decision`, `unparsable-correction` (neither form, or a path/sha contradicting the proposal's own
fields), `wrong-repository` (the proposal names another, or the writer was built for one), `wrong-ref`,
`path-outside-prefix`, `not-an-allowed-target`, `unusable-sha` (not 40 hex, or no blob there),
`mixed-scope`, `no-change` (the restore blob already is the head's), `superseded` (the live
`lastAppliedSpec` moved on, or the primary is not serving the promoted spec), `already-restored` (F8 — the
primary serves the failed spec), `target-changed` (the file changed since the failure), `branch-moved` (the
head is neither the failed revision nor a descendant that left the prefix alone; also a `compare` too long
to read whole, and the non-fast-forward `PATCH`), `restore-not-on-branch`, `already-corrected`.

**The correction window, and dry-run between them.** `gitwriter.CORRECTIONS_ENABLED` is `False` in the
source and `_main()` is the one place that turns it on, from `RECOVERY_GIT_WRITE` (`off` | `dry-run` |
`enabled`; unset is `dry-run`, anything else `off` with a warning). The credential is a permanent PAT, so
the cluster holds none between windows: `externalsecret-git.yaml` sits in the app directory *outside* the
kustomization's `resources:`, and a window is one PR to `main` that uncomments it and sets `enabled` — the
reverse PR closes it and Flux prunes the Secret. Nothing breaks in between: the mount is `optional`,
`RECOVERY_GIT_TOKEN_FILE` is read per call, and with no credential the writer omits the `Authorization`
header and evaluates the whole ladder over anonymous reads (60 an hour, far more than a correction needs).
`corrector(..., enabled=)` can only *narrow* the module switch. `lock=` is the design's Lease — a
`coordination.k8s.io` `Lease` named `flagger-recovery-writer`, 60 s, holder the pod name — required rather than
defaulted — a mutual exclusion a caller can forget into a no-op is indistinguishable from one nobody
configured. Corrections run on one worker thread, so a `post-rollout` hook answers `CorrectionQueued`
rather than holding its connection for the nine round trips; one the full queue drops is not lost, because
`reconcile()` re-offers every still-live proposal that has no `correction` marker beside it.

**What a refusal leaves behind.** The `correction` marker is a create-only claim written after every
read-side bound has passed and immediately before the first mutating call, so a transient refusal —
`target-changed`, `branch-moved`, `no-change`, `mixed-scope`, `restore-not-on-branch` — leaves nothing
and the next delivery retries; a duplicate is `already-corrected` with zero GitHub calls. A completed ref
update writes a `correction-result` linking the old head, the restored revision and the new commit, and
`reconcile()` counts a marker with no result beside it `corrections_stale` after ten minutes without ever
retrying it — which covers both a writer that died and a ref update GitHub refused. That refused `PATCH`
also leaves its tree and commit objects unreferenced: no ref points at them, the API cannot remove them,
so a dangling commit here is expected debris rather than a write that half-happened.

## Layout

- `flagger_recovery/identity.py` — `resolve()`, `CandidateIdentity`,
  `ContainerImage`, `AttributionRefused`, `is_manual_rollback()`, and a
  `python3 -m flagger_recovery.identity` CLI for the read-only smoke below.
- `flagger_recovery/record.py` — `DeploymentRecord`, `Document`, `make_key()`,
  `make_key_parts()`, `label_value()`, `checksum_label()`, `RecordStore`
  protocol (including `find_candidate()`), `ConfigMapStore` (stdlib `urllib`),
  `InMemoryStore`.
- `flagger_recovery/auth.py` — `load_token()`, `presented_token()`,
  `token_matches()`, `TokenUnavailable`.
- `flagger_recovery/inbox.py` — `WebhookEvent`, `Inbox`, `MalformedPayload`.
- `flagger_recovery/kube.py` — `ApiReader`, a GET-only Kubernetes API client
  that follows no redirect (its bearer token is a service-account token, and
  `urllib` would re-send it), and `CandidateReader`, which fetches exactly the
  objects `resolve()` takes so a caller can write
  `resolve(**reader.read(namespace, canary))`. No writes live here;
  `ConfigMapStore` has its own minimal POST/GET.
- `flagger_recovery/server.py` — `Receiver`, `build_server()`, the `decider`
  hook (`Decision`, `Ignore()`, `DECIDER_NOT_INSTALLED`), the reconcile timer,
  and the in-cluster `_main()` entry point.
- `flagger_recovery/decide.py` — `decide()`, `LiveState`, `decider()`, `reconcile()`;
  `flagger_recovery/proposal.py` — `Proposal`, `build_proposal()`, branch/path bounds;
  `flagger_recovery/policy.py` — the allowlists, `evaluate()`, `Verdict`, `TreeState`, `is_stale()`;
  `flagger_recovery/gitwriter.py` — `GitWriter`, `Correction`, `corrector()`.
- `tests/fixtures/*.json` — sanitised copies of the live pilot objects
  (domain replaced with `example.invalid`, `managedFields` dropped).
  `replicasets.json` and `pods.json` are the raw `kubectl get ... -o json`
  List shape (`{"items": [...]}`), matching multi-object GETs from the
  cluster API; the single-object fixtures (`canary.json`, `deployment.json`,
  ...) are not wrapped.
- `tests/fixtures/live-{pre-rollout,post-rollout,candidate-records}.json` —
  ConfigMaps this receiver itself wrote in `flagger-system` on 2026-09-07, the
  first two rollouts to reach it with the hooks wired, copied verbatim from
  `kubectl get cm -n flagger-system -l flagger-recovery/hook=pre-rollout -o json`
  and the equivalents for `hook=post-rollout` and `phase=candidate` (items
  sorted by name, `resourceVersion`/`uid` dropped as per-write bookkeeping, and
  nothing else changed; the shared token never reaches a stored document, so
  there is none to strip). `tests/test_checksum_bridge.py` replays both
  rollouts through the receiver from those exact payloads.

## Running the tests

```bash
python3 -m unittest discover -s services/flagger-recovery/tests -t services/flagger-recovery -v
```

No repository CI workflow runs Python tests yet, so this command is the test
evidence for this story — see the PR body for its recorded output.

## Read-only smoke against the live cluster

`resolve()` never touches the network itself; `flagger_recovery.identity`'s
`__main__` uses `kube.ApiReader` to fetch the live objects and calls
`resolve()` over them. It makes only GET requests.

```bash
kubectl proxy --port=8001 &
PROXY_PID=$!
trap 'kill "$PROXY_PID" 2>/dev/null' EXIT
until curl -sf http://127.0.0.1:8001/version >/dev/null; do sleep 0.5; done

cd services/flagger-recovery
python3 -m flagger_recovery.identity --base-url http://127.0.0.1:8001 \
  --namespace flagger-pilot --canary podinfo

kill "$PROXY_PID"
```

This prints the resolved `CandidateIdentity` as JSON. The `cd` (or
`PYTHONPATH=services/flagger-recovery` from the repository root) is required
for `flagger_recovery` to be importable. If `kubectl proxy` cannot be started
under the session's policy, skip this step — the unit tests above are the
gate.
