#!/usr/bin/env python3
"""Explicit, local-only Hindsight volume retention and checkpoint operations."""

import argparse
import datetime
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import time
import uuid


CLAIMS = (("ai", "hindsight-codex-auth"), ("database", "postgres-1"))
DRIVER = "rook-ceph.rbd.csi.ceph.com"
SNAPSHOT_CLASS = "ceph-block-snapshot-retain"


class MaintenanceError(Exception):
    pass


class Kubectl:
    def __init__(self, context):
        self.prefix = ["kubectl", "--context", context]

    def run(self, *args, document=None):
        command = self.prefix + list(args)
        try:
            result = subprocess.run(command, input=None if document is None else json.dumps(document),
                                    text=True, capture_output=True, check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise MaintenanceError("kubectl invocation failed or timed out") from exc
        if result.returncode:
            # kubectl diagnostics may contain server data. Never echo them.
            raise MaintenanceError("kubectl operation failed: " + " ".join(args[:3]))
        if not result.stdout.strip():
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise MaintenanceError("kubectl returned invalid JSON") from exc

    def get(self, kind, name, namespace=None):
        args = ["get", kind, name]
        if namespace:
            args += ["-n", namespace]
        obj = self.run(*args, "-o", "json")
        if not isinstance(obj, dict):
            raise MaintenanceError("kubectl returned an invalid resource")
        return obj

    def get_optional(self, kind, name, namespace=None):
        args = ["get", kind, name]
        if namespace:
            args += ["-n", namespace]
        obj = self.run(*args, "--ignore-not-found", "-o", "json")
        if obj is not None and not isinstance(obj, dict):
            raise MaintenanceError("kubectl returned an invalid resource")
        return obj

    def list(self, kind, namespace, selector):
        obj = self.run("get", kind, "-n", namespace, "-l", selector, "-o", "json")
        if not isinstance(obj, dict) or not isinstance(obj.get("items"), list):
            raise MaintenanceError("kubectl returned an invalid resource list")
        return obj["items"]

    def create(self, document):
        self.run("create", "-f", "-", "-o", "json", document=document)


def bindings(kube):
    result = []
    for namespace, claim_name in CLAIMS:
        pvc = kube.get("pvc", claim_name, namespace)
        pmeta = pvc.get("metadata", {})
        pspec = pvc.get("spec", {})
        if (pmeta.get("name") != claim_name or pmeta.get("namespace") != namespace
                or not pmeta.get("uid") or pvc.get("status", {}).get("phase") != "Bound"
                or pspec.get("storageClassName") != "ceph-block" or not pspec.get("volumeName")):
            raise MaintenanceError("claim missing, unbound, or unexpected: %s/%s" % (namespace, claim_name))
        pv = kube.get("pv", pspec["volumeName"])
        meta, spec = pv.get("metadata", {}), pv.get("spec", {})
        ref = spec.get("claimRef", {})
        csi = spec.get("csi", {})
        if (meta.get("name") != pspec["volumeName"] or not meta.get("resourceVersion")
                or pv.get("status", {}).get("phase") != "Bound"
                or spec.get("storageClassName") != "ceph-block"
                or csi.get("driver") != DRIVER or not csi.get("volumeHandle")
                or ref.get("namespace") != namespace or ref.get("name") != claim_name
                or ref.get("uid") != pmeta["uid"] or not ref.get("resourceVersion")
                or spec.get("persistentVolumeReclaimPolicy") not in ("Delete", "Retain")):
            raise MaintenanceError("PV binding or driver mismatch for %s/%s" % (namespace, claim_name))
        result.append((namespace, claim_name, pv))
    return result


def claim_binding(kube, pvc, allowed_classes=("ceph-block", "ceph-block-retain")):
    meta, spec = pvc.get("metadata", {}), pvc.get("spec", {})
    name, namespace, pv_name = meta.get("name"), meta.get("namespace"), spec.get("volumeName")
    if (not name or not namespace or not meta.get("uid") or not pv_name
            or pvc.get("status", {}).get("phase") != "Bound"
            or spec.get("storageClassName") not in allowed_classes):
        raise MaintenanceError("invalid or unbound instance claim")
    pv = kube.get("pv", pv_name)
    pmeta, pspec = pv.get("metadata", {}), pv.get("spec", {})
    ref, csi = pspec.get("claimRef", {}), pspec.get("csi", {})
    if (pmeta.get("name") != pv_name or not pmeta.get("uid")
            or pv.get("status", {}).get("phase") != "Bound"
            or pspec.get("storageClassName") != spec["storageClassName"]
            or ref.get("namespace") != namespace or ref.get("name") != name
            or ref.get("uid") != meta["uid"] or csi.get("driver") != DRIVER
            or not csi.get("volumeHandle")
            or pspec.get("persistentVolumeReclaimPolicy") not in ("Delete", "Retain")):
        raise MaintenanceError("CNPG instance binding mismatch: " + name)
    return {"claim": name, "claim_uid": meta["uid"], "pv": pv_name,
            "pv_uid": pmeta["uid"], "volume_handle": csi["volumeHandle"],
            "reclaim": pspec["persistentVolumeReclaimPolicy"], "storage_class": spec["storageClassName"],
            "labels": meta.get("labels", {})}


def instance_bindings(kube, require_retain=False):
    result = {}
    for pvc in kube.list("pvc", "database", "cnpg.io/cluster=postgres"):
        meta = pvc.get("metadata", {})
        if (meta.get("namespace") != "database"
                or meta.get("labels", {}).get("cnpg.io/cluster") != "postgres"):
            raise MaintenanceError("unexpected CNPG instance PVC")
        binding = claim_binding(kube, pvc)
        if binding["claim"] in result:
            raise MaintenanceError("duplicate CNPG instance PVC")
        result[binding["claim"]] = binding
    if require_retain and any(b["reclaim"] != "Retain" for b in result.values()):
        raise MaintenanceError("unretained bound PV")
    return result


def fixed_identity(kube, namespace, name):
    pvc = kube.get("pvc", name, namespace)
    return claim_binding(kube, pvc, ("ceph-block",))


def inspect(kube):
    fixed = bindings(kube)
    for namespace, claim, pv in fixed:
        print("%s/%s -> %s: %s" % (namespace, claim, pv["metadata"]["name"],
                                     pv["spec"]["persistentVolumeReclaimPolicy"]))
    # CNPG may create additional instance PVCs after the original binding.
    instances = instance_bindings(kube)
    for name, binding in instances.items():
        print("database/%s -> %s: class=%s reclaim=%s" % (
            name, binding["pv"], binding["storage_class"],
            binding["reclaim"]))
    if (any(binding["reclaim"] != "Retain" for binding in instances.values())
            or any(pv["spec"]["persistentVolumeReclaimPolicy"] != "Retain" for _, _, pv in fixed)):
        raise MaintenanceError("unretained CNPG instance PV")


def retain(kube):
    # Validate both bindings before the first mutation.
    records = bindings(kube)
    for namespace, claim, pv in records:
        spec, meta = pv["spec"], pv["metadata"]
        if spec["persistentVolumeReclaimPolicy"] == "Retain":
            print("already retained: %s/%s" % (namespace, claim))
            continue
        ref = spec["claimRef"]
        patch = [
            {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
            {"op": "test", "path": "/spec/claimRef/namespace", "value": namespace},
            {"op": "test", "path": "/spec/claimRef/name", "value": claim},
            {"op": "test", "path": "/spec/claimRef/uid", "value": ref["uid"]},
            {"op": "test", "path": "/spec/claimRef/resourceVersion", "value": ref["resourceVersion"]},
            {"op": "test", "path": "/spec/storageClassName", "value": "ceph-block"},
            {"op": "test", "path": "/spec/csi/driver", "value": DRIVER},
            {"op": "test", "path": "/spec/csi/volumeHandle", "value": spec["csi"]["volumeHandle"]},
            {"op": "test", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Delete"},
            {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Retain"},
        ]
        kube.run("patch", "pv", meta["name"], "--type=json", "-p", json.dumps(patch), "-o", "json")
        current = fixed_identity(kube, namespace, claim)
        if (current["claim_uid"] != ref["uid"] or current["pv"] != meta["name"]
                or current["pv_uid"] != meta.get("uid")
                or current["volume_handle"] != spec["csi"]["volumeHandle"]
                or current["reclaim"] != "Retain"):
            raise MaintenanceError("retention postcheck failed: %s/%s" % (namespace, claim))
        print("retained: %s/%s" % (namespace, claim))


def save_record(path, record, exclusive=False):
    path = Path(path)
    data = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            raise
    else:
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_record(path, context):
    try:
        record = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise MaintenanceError("invalid maintenance record") from exc
    if (not isinstance(record, dict) or record.get("schema") != "hindsight-maintenance/1"
            or record.get("context") != context or not isinstance(record.get("auth"), dict)
            or not isinstance(record.get("backup"), dict)
            or record["auth"].get("namespace") != "ai"
            or record["backup"].get("namespace") != "database"
            or not re.fullmatch(r"hindsight-auth-\d{14}-[a-f0-9]{8}", str(record["auth"].get("name", "")))
            or record["backup"].get("name") != record["auth"]["name"].replace("hindsight-auth-", "hindsight-db-")):
        raise MaintenanceError("maintenance record/context mismatch")
    return record


def validate_snapshot(kube, snapshot, namespace, expected_name, expected_claim, backup_name=None,
                      source_handle=None):
    meta, spec, status = (snapshot.get(k, {}) for k in ("metadata", "spec", "status"))
    labels = meta.get("labels", {})
    if (meta.get("name") != expected_name or meta.get("namespace") != namespace
            or not meta.get("uid") or spec.get("volumeSnapshotClassName") != SNAPSHOT_CLASS
            or spec.get("source", {}).get("persistentVolumeClaimName") != expected_claim
            or (backup_name and (labels.get("cnpg.io/backupName") != backup_name
                                 or labels.get("cnpg.io/cluster") != "postgres"))):
        raise MaintenanceError("snapshot identity/linkage mismatch: " + expected_name)
    if status.get("error") or status.get("readyToUse") is not True:
        raise MaintenanceError("snapshot failed or incomplete: " + expected_name)
    content_name = status.get("boundVolumeSnapshotContentName")
    if not content_name:
        raise MaintenanceError("snapshot has no bound content: " + expected_name)
    content = kube.get("volumesnapshotcontent", content_name)
    cref = content.get("spec", {}).get("volumeSnapshotRef", {})
    content_status = content.get("status", {})
    if (content.get("metadata", {}).get("name") != content_name
            or not content.get("metadata", {}).get("uid")
            or content.get("spec", {}).get("deletionPolicy") != "Retain"
            or content.get("spec", {}).get("driver") != DRIVER
            or content.get("spec", {}).get("volumeSnapshotClassName") != SNAPSHOT_CLASS
            or cref.get("name") != expected_name or cref.get("namespace") != namespace
            or cref.get("uid") != meta["uid"] or content_status.get("readyToUse") is not True
            or content_status.get("error") or not content_status.get("snapshotHandle")
            or (source_handle is not None
                and content.get("spec", {}).get("source", {}).get("volumeHandle") != source_handle)):
        raise MaintenanceError("snapshot content mismatch: " + expected_name)
    return {"name": expected_name, "namespace": namespace, "uid": meta["uid"],
            "content": content_name, "content_uid": content.get("metadata", {}).get("uid"),
            "snapshot_handle": content_status["snapshotHandle"],
            "creation_time": status.get("creationTime"), "ready": True}


def checkpoint(kube, acknowledged, auth_timeout, interval, record_path, context,
               db_timeout=None, resume=False, clock=time.monotonic, sleep=time.sleep):
    if not acknowledged:
        raise MaintenanceError("offline checkpoint pauses ALL tenants on shared PostgreSQL; pass --ack-shared-db-pause")
    if db_timeout is None:
        db_timeout = auth_timeout
    if auth_timeout <= 0 or db_timeout <= 0 or interval <= 0:
        raise MaintenanceError("timeouts and polling intervals must be positive")
    record = load_record(record_path, context) if resume else None
    # Read-only preflight must finish before a new record reserves its path.
    records = bindings(kube)
    if any(pv["spec"]["persistentVolumeReclaimPolicy"] != "Retain" for _, _, pv in records):
        raise MaintenanceError("retain both bound PVs before checkpoint")
    instances = instance_bindings(kube, require_retain=True)
    if "postgres-1" not in instances:
        raise MaintenanceError("fixed postgres instance missing from cluster PVC inventory")
    auth_source = fixed_identity(kube, "ai", "hindsight-codex-auth")
    if auth_source["reclaim"] != "Retain":
        raise MaintenanceError("auth source is not retained")
    snapshot_class = kube.get("volumesnapshotclass", SNAPSHOT_CLASS)
    if (snapshot_class.get("driver") != DRIVER or snapshot_class.get("deletionPolicy") != "Retain"):
        raise MaintenanceError("retained snapshot class is missing or mismatched")
    cluster = kube.get("cluster.postgresql.cnpg.io", "postgres", "database")
    config = cluster.get("spec", {}).get("backup", {}).get("volumeSnapshot", {})
    if (config.get("className") != SNAPSHOT_CLASS or config.get("snapshotOwnerReference") != "none"
            or config.get("online") is not False):
        raise MaintenanceError("postgres Cluster lacks retained offline snapshot configuration")
    source_fields = {k: auth_source[k] for k in ("claim_uid", "pv", "pv_uid", "volume_handle")}
    if resume:
        if record["auth"].get("source") != source_fields:
            raise MaintenanceError("auth source changed since checkpoint was planned")
    else:
        suffix = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        record = {"schema": "hindsight-maintenance/1", "context": context,
                  "state": "planned", "auth": {"name": "hindsight-auth-" + suffix, "namespace": "ai",
                                               "source": source_fields},
                  "backup": {"name": "hindsight-db-" + suffix, "namespace": "database"},
                  "snapshots": []}
        try:
            save_record(record_path, record, exclusive=True)
        except FileExistsError as exc:
            raise MaintenanceError("record already exists; use --resume-record") from exc
    auth_name, backup_name = record["auth"]["name"], record["backup"]["name"]
    print("planned auth snapshot ai/%s and database Backup database/%s; record %s" %
          (auth_name, backup_name, record_path), flush=True)
    auth = {"apiVersion": "snapshot.storage.k8s.io/v1", "kind": "VolumeSnapshot",
            "metadata": {"name": auth_name, "namespace": "ai",
                         "labels": {"hindsight-maintenance/backup": backup_name}},
            "spec": {"volumeSnapshotClassName": SNAPSHOT_CLASS,
                     "source": {"persistentVolumeClaimName": "hindsight-codex-auth"}}}
    backup = {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup",
              "metadata": {"name": backup_name, "namespace": "database",
                           "labels": {"hindsight-maintenance/auth": auth_name}},
              "spec": {"cluster": {"name": "postgres"}, "method": "volumeSnapshot", "online": False}}
    auth_deadline = clock() + auth_timeout
    existing_auth = kube.get_optional("volumesnapshot", auth_name, "ai")
    if existing_auth is None:
        if resume and record.get("state") not in ("planned", "auth_creating"):
            raise MaintenanceError("record says auth created but resource is missing")
        record["state"] = "auth_creating"
        save_record(record_path, record)
        print("creating ai/" + auth_name, flush=True)
        try:
            kube.create(auth)
        except MaintenanceError as exc:
            existing_auth = kube.get_optional("volumesnapshot", auth_name, "ai")
            if existing_auth is None:
                raise MaintenanceError("auth create uncertain; inspect exact name and resume record") from exc
    def observe(obj, kind, expected, entry):
        meta = obj.get("metadata", {})
        if (meta.get("name") != expected["metadata"]["name"]
                or meta.get("namespace") != expected["metadata"]["namespace"]
                or not meta.get("uid") or meta.get("labels", {}).get(next(iter(expected["metadata"]["labels"])))
                != next(iter(expected["metadata"]["labels"].values()))
                or obj.get("spec") != expected["spec"]):
            raise MaintenanceError("unrelated %s uses planned name" % kind)
        if entry.get("uid") and entry["uid"] != meta["uid"]:
            raise MaintenanceError("%s UID changed during checkpoint" % kind)
        if not entry.get("uid"):
            entry["uid"] = meta["uid"]
            save_record(record_path, record)

    if existing_auth is not None:
        observe(existing_auth, "auth snapshot", auth, record["auth"])
    auth_obj = wait_for(kube, "volumesnapshot", auth_name, "ai", auth_deadline, interval, clock, sleep,
                        observer=lambda obj: observe(obj, "auth snapshot", auth, record["auth"]))
    if fixed_identity(kube, "ai", "hindsight-codex-auth") != auth_source:
        raise MaintenanceError("auth source changed during snapshot")
    record["snapshots"] = [validate_snapshot(kube, auth_obj, "ai", auth_name, "hindsight-codex-auth",
                                              source_handle=auth_source["volume_handle"])]
    if clock() >= auth_deadline:
        raise MaintenanceError("auth phase deadline exceeded; database Backup not started")
    record["state"] = "auth_ready"
    save_record(record_path, record)
    if clock() >= auth_deadline:
        raise MaintenanceError("auth phase deadline exceeded; database Backup not started")
    db_deadline = clock() + db_timeout
    existing_backup = kube.get_optional("backup", backup_name, "database")
    if existing_backup is None:
        if resume and record.get("backup_created"):
            raise MaintenanceError("record says Backup created but resource is missing")
        if clock() >= db_deadline:
            raise MaintenanceError("database phase deadline exceeded before create")
        record["state"] = "backup_creating"
        save_record(record_path, record)
        print("creating database/" + backup_name + "; all tenants may pause", flush=True)
        try:
            kube.create(backup)
        except MaintenanceError as exc:
            existing_backup = kube.get_optional("backup", backup_name, "database")
            if existing_backup is None:
                raise MaintenanceError("Backup create uncertain; inspect exact name and resume record") from exc
    if existing_backup is not None:
        observe(existing_backup, "Backup", backup, record["backup"])
    record["backup_created"] = True
    record["state"] = "backup_waiting"
    save_record(record_path, record)
    backup_obj = wait_for(kube, "backup", backup_name, "database", db_deadline, interval, clock, sleep,
                          observer=lambda obj: observe(obj, "Backup", backup, record["backup"]))
    record["snapshots"] = record["snapshots"][:1]
    status = backup_obj.get("status", {})
    instance = status.get("instanceID", {}).get("podName")
    if not isinstance(instance, str) or not re.fullmatch(r"postgres-[0-9]+", instance):
        raise MaintenanceError("Backup has no selected instance")
    elements = status.get("snapshotBackupStatus", {}).get("elements", [])
    if not isinstance(elements, list) or not elements:
        raise MaintenanceError("Backup missing snapshot linkage")
    expected_roles = {("PG_DATA", None)}
    if cluster.get("spec", {}).get("walStorage"):
        expected_roles.add(("PG_WAL", None))
    for tablespace in cluster.get("spec", {}).get("tablespaces", []):
        if not isinstance(tablespace, dict) or not tablespace.get("name"):
            raise MaintenanceError("invalid Cluster tablespace configuration")
        expected_roles.add(("PG_TABLESPACE", tablespace["name"]))
    seen_names, seen_roles = set(), set()
    for element in elements:
        if clock() >= db_deadline:
            raise MaintenanceError("database phase deadline exceeded during snapshot verification")
        if not isinstance(element, dict) or not isinstance(element.get("name"), str) or not element["name"]:
            raise MaintenanceError("Backup missing snapshot linkage")
        name = element["name"]
        role = (element.get("type"), element.get("tablespaceName") if element.get("type") == "PG_TABLESPACE" else None)
        if (name in seen_names or role in seen_roles or role not in expected_roles
                or (role[0] != "PG_TABLESPACE" and element.get("tablespaceName"))):
            raise MaintenanceError("Backup snapshot roles or names mismatch")
        seen_names.add(name)
        seen_roles.add(role)
        snap = kube.get("volumesnapshot", name, "database")
        claim = snap.get("spec", {}).get("source", {}).get("persistentVolumeClaimName")
        binding = instances.get(claim)
        labels = binding["labels"] if binding else {}
        if (not binding or labels.get("cnpg.io/instanceName") != instance
                or labels.get("cnpg.io/pvcRole") != role[0]
                or (role[0] == "PG_TABLESPACE" and labels.get("cnpg.io/tablespaceName") != role[1])
                or (role[0] != "PG_TABLESPACE" and labels.get("cnpg.io/tablespaceName"))):
            raise MaintenanceError("Backup snapshot has wrong selected-instance claim or role")
        current = claim_binding(kube, kube.get("pvc", claim, "database"))
        if current != binding or current["reclaim"] != "Retain":
            raise MaintenanceError("Backup source claim changed")
        captured = validate_snapshot(kube, snap, "database", name, claim, backup_name,
                                     source_handle=binding["volume_handle"])
        captured.update({"role": role[0], "tablespace": role[1], "claim": claim,
                         "claim_uid": binding["claim_uid"], "pv": binding["pv"],
                         "pv_uid": binding["pv_uid"], "volume_handle": binding["volume_handle"]})
        record["snapshots"].append(captured)
        save_record(record_path, record)
    if seen_roles != expected_roles:
        raise MaintenanceError("Backup snapshot role inventory incomplete")
    if clock() >= db_deadline:
        raise MaintenanceError("database phase deadline exceeded during snapshot verification")
    record["state"] = "complete"
    save_record(record_path, record)
    print("checkpoint complete: %s %s" % (auth_name, backup_name))


def wait_for(kube, kind, name, namespace, deadline, interval, clock, sleep, observer=None):
    while True:
        if clock() >= deadline:
            raise MaintenanceError("checkpoint timed out waiting for %s %s" % (kind, name))
        obj = kube.get(kind, name, namespace)
        if clock() >= deadline:
            raise MaintenanceError("checkpoint timed out waiting for %s %s" % (kind, name))
        if observer:
            observer(obj)
        status = obj.get("status", {})
        if kind == "volumesnapshot":
            if status.get("error"):
                raise MaintenanceError("auth snapshot failed: " + name)
            if status.get("readyToUse") is True:
                return obj
        else:
            phase = status.get("phase", "")
            if phase == "completed":
                if status.get("online") is not False or status.get("method") != "volumeSnapshot":
                    raise MaintenanceError("database Backup completed with unexpected method or online mode")
                return obj
            if phase in ("failed", "walArchivingFailing") or status.get("error"):
                raise MaintenanceError("database Backup failed: " + name)
        remaining = deadline - clock()
        if remaining <= 0:
            raise MaintenanceError("checkpoint timed out waiting for %s %s" % (kind, name))
        sleep(min(interval, remaining))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True, help="explicit kube context")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect", help="read only claim and PV inspection")
    sub.add_parser("retain-volumes", help="change only bound PV reclaim policies")
    cp = sub.add_parser("checkpoint", help="create retained auth and offline CNPG checkpoints")
    cp.add_argument("--ack-shared-db-pause", action="store_true")
    paths = cp.add_mutually_exclusive_group(required=True)
    paths.add_argument("--record", help="new local JSON maintenance record path")
    paths.add_argument("--resume-record", help="resume exact names in existing record")
    cp.add_argument("--auth-timeout-seconds", type=int, default=900)
    cp.add_argument("--db-timeout-seconds", type=int, default=900)
    cp.add_argument("--poll-seconds", type=int, default=5)
    args = parser.parse_args(argv)
    if not args.context.strip():
        parser.error("--context cannot be empty")
    if args.command == "checkpoint" and (args.auth_timeout_seconds <= 0 or args.db_timeout_seconds <= 0 or args.poll_seconds <= 0):
        parser.error("timeouts and polling intervals must be positive")
    kube = Kubectl(args.context)
    try:
        if args.command == "inspect":
            inspect(kube)
        elif args.command == "retain-volumes":
            retain(kube)
        else:
            checkpoint(kube, args.ack_shared_db_pause, args.auth_timeout_seconds, args.poll_seconds,
                       args.resume_record or args.record, args.context,
                       db_timeout=args.db_timeout_seconds, resume=bool(args.resume_record))
    except (MaintenanceError, OSError) as exc:
        if isinstance(exc, OSError):
            exc = MaintenanceError("local maintenance record I/O failed")
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
