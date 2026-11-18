# Hindsight worker autoscaling qualification and recovery

This branch is not a Flux source and remains an unmerged draft. The source
manifests include the worker switch and HPA, so merging or activating them
would start workers before qualification. Keep the PR draft and do not merge,
deploy, or activate this change until every live check below passes and the
activation receives separate review. This manifest change is **not production
qualification**. Use a disposable,
separately named deployment of the pinned 1.2.1 image/chart and the rendered
values from `kubernetes/apps/ai/hermes-hindsight/app/hindsight.yaml`. Keep the
same service account, Cilium policy, API pod affinity, auth directory mount,
model aliases, pool limits, three worker slots and 150 second termination grace.
Do not use historical memory rows, production credentials or Kubernetes Secret
objects as test fixtures. Use synthetic operations in two disposable banks.
Record the tested revision, chart digest, image digest, values digest, start/end
times, redacted counts, timings and controller conditions. Remove the temporary
deployment after qualification. Do not merge the final Flux HPA change until
every check below passes in a separate review.

## Repeatable sequence

1. Render the pinned chart with the committed HelmRelease values and apply its
   post-render patches with native Kustomize. Check that the API deployment has
   `HINDSIGHT_API_WORKER_ENABLED=false`, the worker StatefulSet has no
   `/spec/replicas`, the worker ID comes from `metadata.name`, and the HPA has
   exactly one external metric. Record the revision and digests.
2. Bring up the temporary stack at one worker and wait for a valid, fresh
   collector sample, ready adapter HelmRelease, Available external APIService,
   and a numeric zero external metric. Confirm adapter TLS and Cilium traffic
   without broadening network policy. Inject collector scrape loss, invalid
   sample, overflow, stale and future timestamp, then absence. For each, record
   no metric and unchanged replicas. Restore a valid zero sample.
3. Enqueue eligible refresh, consolidation, retain and graph work in two banks.
   Observe demand 2 and transition 1 to 2, with no more than one new pod per
   minute and no third pod. Confirm both worker ordinals claim work. Drain the
   queue; include a busy worker with no pending rows. Observe no downscale while
   processing demand requires two, then 2 to 1 after the 300 second window,
   at most one pod per minute. In the isolated qualification environment,
   reconcile the temporary Flux source at two workers and verify its StatefulSet
   manifest still omits replicas and `/scale` remains at two.
4. During a long synthetic task on ordinal 1, request one normal termination.
   Observe claims stop, bounded drain, task completion or hand-back to pending,
   and exactly one eventual completion on another worker. Repeat with refresh
   and consolidation. Force-kill an isolated worker with a task, restore the
   same ordinal and verify startup recovery. A normal SIGTERM that leaves a
   processing row blocks activation.
5. With both workers busy, replay the same fixed synchronous API workload at
   one and two workers. Record provider concurrent request peak (at most 3),
   database role connection peak including jobs and API restart (at most 25),
   zero pool acquire timeouts, p95 latency at most 1.2 times baseline, and a
   5xx increase no greater than 0.5 percentage point. Confirm both workers
   can authenticate through the existing provider route using counts only;
   confirm auth rotation on each pod by inode/mtime or a harmless request.
6. Sustain mixed eligible work across two banks. Every bank must complete a
   task within ten shared-slot releases after its first runnable sample;
   refresh begins when its reservation frees. Record completions per minute
   per operation and bank, refresh latency, and oldest runnable consolidation
   age. Agree a maximum consolidation age before activation; persistent
   starvation fails. Two-worker non-refresh throughput must exceed one-worker
   throughput under the same non-provider-limited load.

The evaluator signs each row with observed values and a pass/fail result.
Missing observation is **pending**, never pass. Record the actual controller
conditions for metric loss and downscale; source-unit fixtures do not establish
controller behavior. Warpgate errors or unavailable cluster access leave the
runtime rows pending. Do not bypass access controls to collect them.

## Recovery

If scale-down strands processing rows, stop activation and restore the removed
ordinal with the same StatefulSet name. Hindsight 1.2.1 startup recovery only
reclaims rows owned by that worker ID; verify the row returns to pending or
completes once. For an unrecoverable ordinal, pause automatic scaling and
escalate to the Hindsight operator for a reviewed, version-specific repair.
Do not edit historical memory rows or run a migration as an ad hoc recovery.
Keep the API available and preserve the existing network and auth boundaries.
