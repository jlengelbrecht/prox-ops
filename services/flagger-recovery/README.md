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

## Layout

- `flagger_recovery/identity.py` — `resolve()`, `CandidateIdentity`,
  `ContainerImage`, `AttributionRefused`, `is_manual_rollback()`, and a
  `python3 -m flagger_recovery.identity` CLI for the read-only smoke below.
- `flagger_recovery/record.py` — `DeploymentRecord`, `make_key()`,
  `RecordStore` protocol, `ConfigMapStore` (stdlib `urllib`), `InMemoryStore`.
- `flagger_recovery/kube.py` — `ApiReader`, a GET-only Kubernetes API client.
  No writes live here; `ConfigMapStore` has its own minimal POST/GET.
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
