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
as a duplicate without writing anything. `checksum` is the Canary's
`status.lastAppliedSpec` — the candidate's pod-template hash — which is what
ties an event to a `DeploymentRecord` for the same candidate. The shared token
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
`promoted` record — a stored-record lookup keyed by `namespace`/`name`/
`checksum`, never a live re-resolve, because by the time this hook fires
Flagger has already scaled the candidate pods away.

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
because `lastPromotedSpec` has just become the candidate's own hash); a hook
about a hash already equal to `lastPromotedSpec` is
`Ignore(manual-rollback-restored)`, via `identity.is_manual_rollback`; anything
else that is not a `Failed` `post-rollout` is `Ignore(not-a-terminal-failure)`;
a failure with no stored candidate record is `Refuse(no-candidate-record)`; a
failed hash that is not still the stored record's, the live `lastAppliedSpec`
and the hash the target Deployment carries is `Ignore(superseded)`, because
correcting it would revert unrelated work; a primary not serving
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
proposals open or superseded — derived from live state, never stored.

## Layout

- `flagger_recovery/identity.py` — `resolve()`, `CandidateIdentity`,
  `ContainerImage`, `AttributionRefused`, `is_manual_rollback()`, and a
  `python3 -m flagger_recovery.identity` CLI for the read-only smoke below.
- `flagger_recovery/record.py` — `DeploymentRecord`, `Document`, `make_key()`,
  `make_key_parts()`, `label_value()`, `RecordStore` protocol, `ConfigMapStore`
  (stdlib `urllib`), `InMemoryStore`.
- `flagger_recovery/auth.py` — `load_token()`, `presented_token()`,
  `token_matches()`, `TokenUnavailable`.
- `flagger_recovery/inbox.py` — `WebhookEvent`, `Inbox`, `MalformedPayload`.
- `flagger_recovery/kube.py` — `ApiReader`, a GET-only Kubernetes API client,
  and `CandidateReader`, which fetches exactly the objects `resolve()` takes so
  a caller can write `resolve(**reader.read(namespace, canary))`. No writes
  live here; `ConfigMapStore` has its own minimal POST/GET.
- `flagger_recovery/server.py` — `Receiver`, `build_server()`, the `decider`
  hook (`Decision`, `Ignore()`, `DECIDER_NOT_INSTALLED`), the reconcile timer,
  and the in-cluster `_main()` entry point.
- `flagger_recovery/decide.py` — `decide()`, `LiveState`, `decider()`, `reconcile()`;
  `flagger_recovery/proposal.py` — `Proposal`, `build_proposal()`, branch/path bounds.
- `tests/fixtures/*.json` — sanitised copies of the live pilot objects
  (domain replaced with `example.invalid`, `managedFields` dropped).
  `replicasets.json` and `pods.json` are the raw `kubectl get ... -o json`
  List shape (`{"items": [...]}`), matching multi-object GETs from the
  cluster API; the single-object fixtures (`canary.json`, `deployment.json`,
  ...) are not wrapped.

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
