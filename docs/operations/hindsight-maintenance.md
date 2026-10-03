# Hindsight retained volumes and local checkpoints

This procedure prepares local Ceph recovery points for Hindsight. It does not
run automatically. The existing `ceph-block` and `ceph-block-snapshot` classes
remain Delete defaults; the new `*-retain` classes are opt-in. Existing
`ai/hindsight-codex-auth` and `database/postgres-1` claims remain in place and
keep their current storage class. Retention prevents deletion of the backing
RBD image when a claim is removed; it does not protect against Ceph pool loss,
operator error on the retained image, or site loss. The server database checkpoint covers both Hindsight banks. Local agent configuration is host-owned state outside these Kubernetes snapshots; verify its existing host backup separately without reading or copying it in this procedure.

## Before an application or cluster upgrade

Use a reviewed kube context and an authorized maintenance window. The
database is shared by Hindsight and other tenants. The offline CNPG snapshot
stops PostgreSQL for **all** tenants while it is taken. Coordinate that pause
with every tenant owner before running the checkpoint command.

```sh
python3 scripts/hindsight-maintenance.py --context YOUR_CONTEXT inspect
python3 scripts/hindsight-maintenance.py --context YOUR_CONTEXT retain-volumes
python3 scripts/hindsight-maintenance.py --context YOUR_CONTEXT inspect
python3 scripts/hindsight-maintenance.py --context YOUR_CONTEXT checkpoint --record ./hindsight-maintenance-record.json --ack-shared-db-pause --auth-timeout-seconds 900 --db-timeout-seconds 900
```

`inspect` reads the two fixed PVCs and their bound PVs, then lists actual CNPG instance PVCs and their reclaim policies. It exits nonzero if any bound instance PV still has Delete policy or a binding is invalid. Investigate any new Delete-policy instance volume before relying on retention; the tool does not patch discovered instance PVs. `retain-volumes`
validates both bindings, their claim UIDs and CSI driver, then JSON-patches only
`persistentVolumeReclaimPolicy`, guarded by resource versions and claim
identity. A failed guard requires a fresh inspection; do not force a patch or
replace a claim. Repeating the operation after both PVs are Retain is safe.

`checkpoint` requires both PVs retained, a Retain snapshot class, the expected
CNPG Cluster configuration, Retain on every currently labelled instance PV, and the explicit pause acknowledgement. It first
creates a retained VolumeSnapshot of `ai/hindsight-codex-auth` and waits for
`readyToUse`; it then creates an offline CNPG `Backup` for the shared
`database/postgres` Cluster and waits for `completed`. Names are written to the required local JSON record and printed before creation. The record contains resource names, snapshot/content linkage and metadata only. Keep it with the recovery procedure; a retained VolumeSnapshotContent without its VolumeSnapshot definition needs manual metadata reconstruction. A timeout exits nonzero but never cancels an already running CNPG Backup and never deletes a recovery point. Inspect the exact names in the record and resume the same pair, with a new shared pause acknowledgement, after resolving the cause:

```sh
kubectl --context YOUR_CONTEXT -n ai get volumesnapshot AUTH_NAME -o yaml
kubectl --context YOUR_CONTEXT -n database get backups.postgresql.cnpg.io BACKUP_NAME -o yaml
python3 scripts/hindsight-maintenance.py --context YOUR_CONTEXT checkpoint --resume-record ./hindsight-maintenance-record.json --ack-shared-db-pause
```

Do not treat an auth snapshot alone as a complete checkpoint. CNPG's offline
snapshot is used because this cluster has no WAL archive; an online base
snapshot alone may need unavailable WAL to recover. There is no scheduled
database pause. The retained VolumeSnapshotContent and RBD snapshots consume
Ceph capacity; review capacity and prune only through a separately approved
recovery-point decision.

## Upgrade verification

Before changing the application image or cluster, confirm the auth snapshot is
`readyToUse`, CNPG Backup phase is `completed`, method is `volumeSnapshot`,
and `online` is `false`; inspect every associated VolumeSnapshot and bound VolumeSnapshotContent for matching names, UIDs and `deletionPolicy: Retain`. Record the existing image tag and database schema
version separately: reverting an image does not revert a schema migration.
After the upgrade, inspect both PVs again, check both banks and configured
agents through ordinary application reads, and verify a new Codex login can
authenticate. A snapshot of mutable OAuth state can become stale and may need
a fresh login after restore. Do not display or copy auth or Secret contents
into logs.

## Recovery plan

Keep production `database/postgres` and its other tenant databases untouched.
For a database recovery, create an **isolated, differently named** CNPG Cluster in the `database` namespace, where the Backup and snapshots exist, following the
[CNPG 1.28 snapshot recovery procedure](https://cloudnative-pg.io/docs/1.28/recovery/).
Use distinct PVCs and credentials, a restricted service/network policy, and prevent clients from connecting to the isolated cluster until data has been checked. Reference the same-namespace snapshot names from the record in `spec.bootstrap.recovery.volumeSnapshots.storage` (and WAL/tablespace snapshots if present). If only retained VolumeSnapshotContent remains, first manually reconstruct matching VolumeSnapshot metadata and binding from the record and CSI metadata; test that reconstruction away from production before recovery. Recover only the `hindsight`
database logically from the isolated cluster into a separately prepared target,
then cut over Hindsight with a tenant-specific plan. Never point a recovery
Cluster at the production PVC or rewind the shared production PostgreSQL
cluster; that would roll back other tenants. Verify the isolated copy and its
schema against the intended application image before any cutover.

Before a logical restore rehearsal, inventory the source database's required
roles, ownership, grants, extensions and versions in the isolated Cluster.
Restore into a separate target with restricted credentials and network access.
Verify object ownership, grants, extension versions, and both banks with the
intended application image pointed at that target through restricted connection
settings. Confirm the application's actual connection target and representative
read and write operations before any cutover. For a separately approved cutover,
verify the endpoint, both banks, permissions and login after switching traffic.
Keep the prior application endpoint and original shared PostgreSQL untouched so
rollback can restore the prior connection target; verify its endpoint and bank
reads after rollback. Never rewind production or another tenant.

For auth recovery, create a new claim from the selected retained
VolumeSnapshot in isolation, inspect it without revealing credential values,
and plan a controlled Hindsight mount change only after confirming the bank
paths. Local agent configuration remains the responsibility of the existing host backup; verify that backup and restore independently. Avoid deleting the original claim or snapshot.
OAuth tokens may have expired since the checkpoint; reauthenticate through the
normal login flow if needed.

CNPG fields used here are defined in the [1.28 API reference](https://cloudnative-pg.io/docs/1.28/cloudnative-pg.v1/)
and [snapshot appendix](https://cloudnative-pg.io/docs/1.28/appendixes/backup_volumesnapshot/).
The pinned [cluster chart 0.5.0 values](https://github.com/cloudnative-pg/charts/blob/cluster-v0.5.0/charts/cluster/values.yaml)
only gate backup rendering through object-store settings, so the Flux
post-render patch adds exactly `spec.backup.volumeSnapshot` while leaving
`backups.enabled: false`.

To run the offline chart render test from a clean checkout, fetch the pinned
chart with the already required Helm CLI into a local directory you choose:

```sh
mkdir -p /path/to/chart-cache
helm pull cluster --version 0.5.0 --repo https://cloudnative-pg.github.io/charts --destination /path/to/chart-cache
HINDSIGHT_CNPG_CHART=/path/to/chart-cache/cluster-0.5.0.tgz python3 -m unittest discover -s tests/hindsight-maintenance -v
```

The standard local cache path is
`_bmad-output/implementation-artifacts/cluster-0.5.0.tgz` from the repository
root; placing the archive there lets the same test command run without the
environment variable. The chart archive is a test input and is not committed.
The test checks that Helm reports chart name `cluster` and version `0.5.0`;
a missing archive or different chart fails the test.

## Periodic isolated restore rehearsal

In a scheduled operator-run exercise, select a completed record and check each referenced snapshot and retained content. Recover a new CNPG Cluster in `database` from the named snapshots with distinct PVCs, credentials and blocked client access; keep `database/postgres` and its PVCs untouched. Restore only a copy of the `hindsight` database into a test target. Apply the role, ownership, grant, extension, connection-target and rollback checks above. Through application checks, verify both banks, representative memories, configured agents, operations and schema compatibility with the intended application image. Restore the auth snapshot to a separate test claim and verify login or perform normal reauthentication if the OAuth state is stale. Record the rehearsal outcome and clean up test resources only under a separate approved procedure. Repeat after material schema or image changes and periodically as part of recovery readiness. A server checkpoint does not cover host-owned local agent configuration; verify its existing host backup during the same exercise.

The desired CNPG storage class is `ceph-block-retain` for future instance claims. CNPG 1.28's PVC reconciler skips claims already present by name and its existing-claim reconciliation does not change storage class; the current bound `postgres-1` claim keeps `ceph-block`. The new class does not retain the current PV retroactively: run the guarded `retain-volumes` command during the approved window, and inspect every instance PVC after reconciliation. This repository change makes no live storage change.
