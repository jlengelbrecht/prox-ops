import copy
import importlib.util
import json
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE = pathlib.Path(__file__).resolve().parents[2] / "scripts/hindsight-maintenance.py"
spec = importlib.util.spec_from_file_location("hindsight_maintenance", MODULE)
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


def fixture():
    objects = {}
    for namespace, name in maintenance.CLAIMS:
        pv_name = "pv-" + name
        objects[("pvc", name, namespace)] = {
            "metadata": {"name": name, "namespace": namespace, "uid": "uid-" + name,
                         "labels": ({"cnpg.io/cluster": "postgres", "cnpg.io/instanceName": "postgres-1",
                                     "cnpg.io/pvcRole": "PG_DATA"} if namespace == "database" else {})},
            "spec": {"volumeName": pv_name, "storageClassName": "ceph-block"},
            "status": {"phase": "Bound"},
        }
        objects[("pv", pv_name, None)] = {
            "metadata": {"name": pv_name, "uid": "uid-" + pv_name, "resourceVersion": "52"},
            "spec": {"storageClassName": "ceph-block", "persistentVolumeReclaimPolicy": "Delete",
                     "claimRef": {"namespace": namespace, "name": name, "uid": "uid-" + name,
                                  "resourceVersion": "7"},
                     "csi": {"driver": maintenance.DRIVER, "volumeHandle": "handle-" + name}},
            "status": {"phase": "Bound"},
        }
    objects[("volumesnapshotclass", maintenance.SNAPSHOT_CLASS, None)] = {
        "driver": maintenance.DRIVER, "deletionPolicy": "Retain"}
    objects[("cluster.postgresql.cnpg.io", "postgres", "database")] = {
        "spec": {"backup": {"volumeSnapshot": {
            "className": maintenance.SNAPSHOT_CLASS, "snapshotOwnerReference": "none", "online": False}}}}
    return objects


class FakeKube:
    def __init__(self, objects=None):
        self.objects = copy.deepcopy(objects if objects is not None else fixture())
        self.calls = []

    def get(self, kind, name, namespace=None):
        self.calls.append(("get", kind, name, namespace))
        key = (kind, name, namespace)
        if key not in self.objects:
            raise maintenance.MaintenanceError("missing resource")
        return copy.deepcopy(self.objects[key])

    def get_optional(self, kind, name, namespace=None):
        self.calls.append(("get_optional", kind, name, namespace))
        return copy.deepcopy(self.objects.get((kind, name, namespace)))

    def list(self, kind, namespace, selector):
        self.calls.append(("list", kind, namespace, selector))
        return [copy.deepcopy(v) for (k, _, ns), v in self.objects.items()
                if k == kind and ns == namespace and v.get("metadata", {}).get("labels", {}).get("cnpg.io/cluster") == "postgres"]

    def run(self, *args, document=None):
        self.calls.append(("run", args, document))
        if args[:2] == ("patch", "pv"):
            self.objects[("pv", args[2], None)]["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
        return {}

    def create(self, document):
        self.calls.append(("create", copy.deepcopy(document)))
        if document["kind"] == "VolumeSnapshot":
            self.objects[("volumesnapshot", document["metadata"]["name"], "ai")] = {
                "metadata": {**document["metadata"], "uid": "auth-uid"}, "spec": document["spec"],
                "status": {"readyToUse": True, "boundVolumeSnapshotContentName": "content-auth"}}
            self.content("content-auth", document["metadata"]["name"], "ai", "auth-uid")
        else:
            self.objects[("backup", document["metadata"]["name"], "database")] = {
                "metadata": {**document["metadata"], "uid": "backup-uid"}, "spec": document["spec"],
                "status": {"phase": "completed", "online": False, "method": "volumeSnapshot",
                           "instanceID": {"podName": "postgres-1"},
                           "snapshotBackupStatus": {"elements": [{"name": "db-snapshot", "type": "PG_DATA"}]}}}
            self.objects[("volumesnapshot", "db-snapshot", "database")] = {
                "metadata": {"name": "db-snapshot", "namespace": "database", "uid": "db-uid",
                             "labels": {"cnpg.io/backupName": document["metadata"]["name"], "cnpg.io/cluster": "postgres"}},
                "spec": {"volumeSnapshotClassName": maintenance.SNAPSHOT_CLASS,
                         "source": {"persistentVolumeClaimName": "postgres-1"}},
                "status": {"readyToUse": True, "boundVolumeSnapshotContentName": "content-db"}}
            self.content("content-db", "db-snapshot", "database", "db-uid")

    def content(self, name, snapshot, namespace, uid):
        self.objects[("volumesnapshotcontent", name, None)] = {
            "metadata": {"name": name, "uid": "uid-" + name},
             "spec": {"deletionPolicy": "Retain", "driver": maintenance.DRIVER,
                     "volumeSnapshotClassName": maintenance.SNAPSHOT_CLASS,
                     "volumeSnapshotRef": {"name": snapshot, "namespace": namespace, "uid": uid},
                     "source": {"volumeHandle": "handle-hindsight-codex-auth" if namespace == "ai" else "handle-postgres-1"}},
            "status": {"snapshotHandle": "handle-" + name, "readyToUse": True}}


class RetentionTests(unittest.TestCase):
    def test_exact_json_patch_guards_and_only_reclaim_change(self):
        kube = FakeKube()
        maintenance.retain(kube)
        mutations = [call for call in kube.calls if call[0] == "run"]
        self.assertEqual(len(mutations), 2)
        for call, (_, name) in zip(mutations, maintenance.CLAIMS):
            args = call[1]
            self.assertEqual(args[:3], ("patch", "pv", "pv-" + name))
            self.assertEqual(args[3], "--type=json")
            patch = json.loads(args[5])
            self.assertEqual([op["op"] for op in patch], ["test"] * 9 + ["replace"])
            self.assertEqual(patch[0], {"op": "test", "path": "/metadata/resourceVersion", "value": "52"})
            self.assertEqual(patch[-1], {"op": "replace", "path": "/spec/persistentVolumeReclaimPolicy", "value": "Retain"})
            self.assertEqual(patch[3]["value"], "uid-" + name)

    def test_missing_unbound_driver_mismatch_and_race_are_denied(self):
        for mutation in (lambda o: o.pop(("pvc", "postgres-1", "database")),
                         lambda o: o[("pvc", "postgres-1", "database")]["status"].update(phase="Pending"),
                         lambda o: o[("pv", "pv-postgres-1", None)]["spec"]["csi"].update(driver="other"),
                         lambda o: o[("pv", "pv-postgres-1", None)]["spec"]["claimRef"].update(uid="other")):
            with self.subTest(mutation=mutation):
                objects = fixture()
                mutation(objects)
                kube = FakeKube(objects)
                with self.assertRaises(maintenance.MaintenanceError):
                    maintenance.retain(kube)
                self.assertFalse(any(c[0] == "run" for c in kube.calls))

    def test_already_retained_is_noop_but_still_inspects_both(self):
        objects = fixture()
        for key, obj in objects.items():
            if key[0] == "pv":
                obj["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
        kube = FakeKube(objects)
        maintenance.retain(kube)
        self.assertFalse(any(c[0] == "run" for c in kube.calls))
        self.assertEqual(len([c for c in kube.calls if c[0] == "get"]), 4)

    def test_patch_conflict_is_failure_and_later_pv_is_not_patched(self):
        kube = FakeKube()
        def conflict(*args, document=None):
            kube.calls.append(("run", args, document))
            raise maintenance.MaintenanceError("kubectl operation failed: patch pv")
        kube.run = conflict
        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.retain(kube)
        self.assertEqual(len([c for c in kube.calls if c[0] == "run"]), 1)

    def test_patch_return_without_retention_fails_postcheck(self):
        kube = FakeKube()
        kube.run = lambda *args, **kwargs: {}
        with self.assertRaisesRegex(maintenance.MaintenanceError, "postcheck"):
            maintenance.retain(kube)


def add_second_instance(kube, reclaim="Delete"):
    pvc = copy.deepcopy(kube.objects[("pvc", "postgres-1", "database")])
    pvc["metadata"].update(name="postgres-2", uid="uid-postgres-2")
    pvc["metadata"]["labels"]["cnpg.io/instanceName"] = "postgres-2"
    pvc["spec"].update(volumeName="pv-postgres-2", storageClassName="ceph-block-retain")
    pv = copy.deepcopy(kube.objects[("pv", "pv-postgres-1", None)])
    pv["metadata"].update(name="pv-postgres-2", uid="uid-pv-postgres-2")
    pv["spec"].update(storageClassName="ceph-block-retain", persistentVolumeReclaimPolicy=reclaim)
    pv["spec"]["claimRef"].update(name="postgres-2", uid="uid-postgres-2")
    pv["spec"]["csi"]["volumeHandle"] = "handle-postgres-2"
    kube.objects[("pvc", "postgres-2", "database")] = pvc
    kube.objects[("pv", "pv-postgres-2", None)] = pv


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.record = str(pathlib.Path(self.temp.name) / "record.json")

    def run_checkpoint(self, kube, ack=True, timeout=10, interval=1, **kwargs):
        return maintenance.checkpoint(kube, ack, timeout, interval, self.record, "named-context", **kwargs)

    def retained(self):
        objects = fixture()
        for key, obj in objects.items():
            if key[0] == "pv":
                obj["spec"]["persistentVolumeReclaimPolicy"] = "Retain"
        return FakeKube(objects)

    def test_pause_ack_denies_before_any_cluster_access_or_create(self):
        kube = self.retained()
        with self.assertRaisesRegex(maintenance.MaintenanceError, "ALL tenants"):
            self.run_checkpoint(kube, False)
        self.assertEqual(kube.calls, [])

    def test_exact_create_documents_and_completion(self):
        kube = self.retained()
        self.run_checkpoint(kube)
        record = json.loads(pathlib.Path(self.record).read_text())
        self.assertEqual(record["state"], "complete")
        self.assertEqual({s["content"] for s in record["snapshots"]}, {"content-auth", "content-db"})
        creates = [c[1] for c in kube.calls if c[0] == "create"]
        self.assertEqual([c["kind"] for c in creates], ["VolumeSnapshot", "Backup"])
        self.assertEqual(creates[0]["spec"], {
            "volumeSnapshotClassName": maintenance.SNAPSHOT_CLASS,
            "source": {"persistentVolumeClaimName": "hindsight-codex-auth"}})
        self.assertEqual(creates[1]["spec"], {
            "cluster": {"name": "postgres"}, "method": "volumeSnapshot", "online": False})
        self.assertEqual(creates[0]["metadata"]["namespace"], "ai")
        self.assertEqual(creates[1]["metadata"]["namespace"], "database")

    def test_wrong_configuration_denies_all_creates(self):
        kube = self.retained()
        kube.objects[("cluster.postgresql.cnpg.io", "postgres", "database")]["spec"]["backup"]["volumeSnapshot"]["online"] = True
        with self.assertRaises(maintenance.MaintenanceError):
            self.run_checkpoint(kube)
        self.assertFalse(any(c[0] == "create" for c in kube.calls))

    def test_new_delete_instance_denies_checkpoint_without_record(self):
        kube = self.retained()
        add_second_instance(kube)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "unretained"):
            self.run_checkpoint(kube)
        self.assertFalse(pathlib.Path(self.record).exists())
        self.assertFalse(any(c[0] == "create" for c in kube.calls))

    def test_bad_preflight_does_not_reserve_record(self):
        kube = self.retained()
        kube.objects[("volumesnapshotclass", maintenance.SNAPSHOT_CLASS, None)]["deletionPolicy"] = "Delete"
        with self.assertRaises(maintenance.MaintenanceError):
            self.run_checkpoint(kube)
        self.assertFalse(pathlib.Path(self.record).exists())

    def test_auth_source_race_and_content_handle_fail(self):
        for change in ("claim", "handle"):
            with self.subTest(change=change):
                kube = self.retained()
                original = kube.create
                def create(document):
                    original(document)
                    if document["kind"] == "VolumeSnapshot":
                        if change == "claim":
                            kube.objects[("pvc", "hindsight-codex-auth", "ai")]["metadata"]["uid"] = "changed"
                        else:
                            kube.objects[("volumesnapshotcontent", "content-auth", None)]["spec"]["source"]["volumeHandle"] = "wrong"
                kube.create = create
                with self.assertRaises(maintenance.MaintenanceError):
                    self.run_checkpoint(kube)
                self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 1)
                pathlib.Path(self.record).unlink()

    def test_content_must_be_ready_and_have_handle(self):
        for change in ("ready", "handle", "error"):
            with self.subTest(change=change):
                kube = self.retained()
                original = kube.create
                def create(document):
                    original(document)
                    if document["kind"] == "VolumeSnapshot":
                        status = kube.objects[("volumesnapshotcontent", "content-auth", None)]["status"]
                        if change == "ready": status["readyToUse"] = False
                        if change == "handle": status.pop("snapshotHandle")
                        if change == "error": status["error"] = {"message": "secret data"}
                kube.create = create
                with self.assertRaises(maintenance.MaintenanceError) as error:
                    self.run_checkpoint(kube)
                self.assertNotIn("secret data", str(error.exception))
                pathlib.Path(self.record).unlink()

    def test_pinned_uids_reject_recreated_auth_or_backup_on_resume(self):
        for kind, key in (("auth", "volumesnapshot"), ("backup", "backup")):
            with self.subTest(kind=kind):
                kube = self.retained()
                self.run_checkpoint(kube)
                record = json.loads(pathlib.Path(self.record).read_text())
                self.assertTrue(record[kind]["uid"])
                namespace = "ai" if kind == "auth" else "database"
                kube.objects[(key, record[kind]["name"], namespace)]["metadata"]["uid"] = "replacement"
                with self.assertRaisesRegex(maintenance.MaintenanceError, "UID changed"):
                    self.run_checkpoint(kube, resume=True)
                pathlib.Path(self.record).unlink()

    def test_selected_instance_and_role_inventory(self):
        for change in ("wrong_instance", "missing_data", "duplicate", "wrong_role"):
            with self.subTest(change=change):
                kube = self.retained()
                add_second_instance(kube, "Retain")
                original = kube.create
                def create(document):
                    original(document)
                    if document["kind"] == "Backup":
                        backup = kube.objects[("backup", document["metadata"]["name"], "database")]
                        elements = backup["status"]["snapshotBackupStatus"]["elements"]
                        if change == "wrong_instance":
                            kube.objects[("volumesnapshot", "db-snapshot", "database")]["spec"]["source"]["persistentVolumeClaimName"] = "postgres-2"
                        elif change == "missing_data": elements.clear()
                        elif change == "duplicate": elements.append(copy.deepcopy(elements[0]))
                        else: elements[0]["type"] = "PG_WAL"
                kube.create = create
                with self.assertRaises(maintenance.MaintenanceError):
                    self.run_checkpoint(kube)
                self.assertNotEqual(json.loads(pathlib.Path(self.record).read_text())["state"], "complete")
                pathlib.Path(self.record).unlink()

    def test_configured_wal_or_tablespace_requires_matching_snapshot_role(self):
        for field, value in (("walStorage", {"size": "1Gi"}),
                             ("tablespaces", [{"name": "archive", "storage": {"size": "1Gi"}}])):
            with self.subTest(field=field):
                kube = self.retained()
                kube.objects[("cluster.postgresql.cnpg.io", "postgres", "database")]["spec"][field] = value
                with self.assertRaisesRegex(maintenance.MaintenanceError, "inventory incomplete"):
                    self.run_checkpoint(kube)
                pathlib.Path(self.record).unlink()

    def test_polled_auth_uid_change_is_rejected(self):
        kube = self.retained()
        original = kube.get
        observations = 0
        def get(kind, name, namespace=None):
            nonlocal observations
            obj = original(kind, name, namespace)
            if kind == "volumesnapshot" and namespace == "ai":
                observations += 1
                if observations == 1:
                    obj["status"]["readyToUse"] = False
                else:
                    obj["metadata"]["uid"] = "replacement"
            return obj
        kube.get = get
        with self.assertRaisesRegex(maintenance.MaintenanceError, "UID changed"):
            self.run_checkpoint(kube, sleep=lambda _: None)
        record = json.loads(pathlib.Path(self.record).read_text())
        self.assertEqual(record["auth"]["uid"], "auth-uid")
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 1)

    def test_unretained_pv_denies_all_creates(self):
        kube = FakeKube()
        with self.assertRaisesRegex(maintenance.MaintenanceError, "retain both"):
            self.run_checkpoint(kube)
        self.assertFalse(any(c[0] == "create" for c in kube.calls))

    def test_snapshot_failure_prevents_backup_and_never_deletes(self):
        kube = self.retained()
        old_create = kube.create
        def fail_snapshot(document):
            old_create(document)
            kube.objects[("volumesnapshot", document["metadata"]["name"], "ai")]["status"] = {"error": {"message": "sensitive"}}
        kube.create = fail_snapshot
        with self.assertRaisesRegex(maintenance.MaintenanceError, "auth snapshot failed") as ctx:
            self.run_checkpoint(kube)
        self.assertNotIn("sensitive", str(ctx.exception))
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 1)
        self.assertFalse(any(c[0] == "run" for c in kube.calls))

    def test_backup_failure_and_timeout_keep_existing_points(self):
        for state in ("failed", "pending"):
            with self.subTest(state=state):
                kube = self.retained()
                old_create = kube.create
                def set_state(document):
                    old_create(document)
                    if document["kind"] == "Backup":
                        kube.objects[("backup", document["metadata"]["name"], "database")]["status"]["phase"] = state
                kube.create = set_state
                ticks = iter((0, 0, 2))
                with self.assertRaises(maintenance.MaintenanceError):
                    self.run_checkpoint(kube, timeout=1, clock=lambda: next(ticks), sleep=lambda _: None)
                self.assertFalse(any(c[0] == "run" for c in kube.calls))

    def test_retained_content_and_backup_linkage_required(self):
        for mutation in (lambda k: k.objects[("volumesnapshotcontent", "content-db", None)]["spec"].update(deletionPolicy="Delete"),
                         lambda k: k.objects[("volumesnapshot", "db-snapshot", "database")]["metadata"]["labels"].update({"cnpg.io/backupName": "other"})):
            kube = self.retained()
            original = kube.create
            def create(document):
                original(document)
                if document["kind"] == "Backup":
                    mutation(kube)
            kube.create = create
            with self.assertRaises(maintenance.MaintenanceError):
                self.run_checkpoint(kube)
            self.assertNotEqual(json.loads(pathlib.Path(self.record).read_text())["state"], "complete")
            pathlib.Path(self.record).unlink()

    def test_uncertain_auth_create_reconciles_exact_name_on_resume(self):
        kube = self.retained()
        original = kube.create
        def uncertain(document):
            original(document)
            if document["kind"] == "VolumeSnapshot":
                raise maintenance.MaintenanceError("client timeout")
        kube.create = uncertain
        self.run_checkpoint(kube)
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 2)
        self.run_checkpoint(kube, resume=True)
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 2)

    def test_uncertain_backup_create_resumes_same_pair(self):
        kube = self.retained()
        original = kube.create
        def uncertain(document):
            original(document)
            if document["kind"] == "Backup":
                raise maintenance.MaintenanceError("client timeout")
        kube.create = uncertain
        self.run_checkpoint(kube)
        self.assertEqual(json.loads(pathlib.Path(self.record).read_text())["state"], "complete")
        self.run_checkpoint(kube, resume=True)
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 2)

    def test_database_phase_has_own_deadline(self):
        kube = self.retained()
        ticks = iter((0, 0, 0, 9, 9, 9, 10, 10, 11, 11, 15))
        self.run_checkpoint(kube, timeout=10, db_timeout=20,
                            clock=lambda: next(ticks), sleep=lambda _: None)
        self.assertEqual(json.loads(pathlib.Path(self.record).read_text())["state"], "complete")

    def test_record_context_and_unrelated_existing_resource_denied(self):
        kube = self.retained()
        self.run_checkpoint(kube)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "context mismatch"):
            maintenance.checkpoint(kube, True, 10, 1, self.record, "other", resume=True)
        with self.assertRaisesRegex(maintenance.MaintenanceError, "already exists"):
            self.run_checkpoint(kube)
        record = json.loads(pathlib.Path(self.record).read_text())
        kube.objects[("volumesnapshot", record["auth"]["name"], "ai")]["metadata"]["labels"].clear()
        with self.assertRaisesRegex(maintenance.MaintenanceError, "unrelated"):
            self.run_checkpoint(kube, resume=True)

    def test_deadline_checked_before_accepting_late_success(self):
        kube = self.retained()
        ticks = iter((0, 0, 11))
        with self.assertRaisesRegex(maintenance.MaintenanceError, "timed out"):
            self.run_checkpoint(kube, timeout=10, clock=lambda: next(ticks), sleep=lambda _: None)
        self.assertEqual(len([c for c in kube.calls if c[0] == "create"]), 1)

    def test_instance_inspection_surfaces_new_unretained_pv(self):
        kube = self.retained()
        pvc = copy.deepcopy(kube.objects[("pvc", "postgres-1", "database")])
        pvc["metadata"].update(name="postgres-2", uid="uid-postgres-2", labels={"cnpg.io/cluster": "postgres"})
        pvc["spec"].update(volumeName="pv-postgres-2", storageClassName="ceph-block")
        pv = copy.deepcopy(kube.objects[("pv", "pv-postgres-1", None)])
        pv["metadata"]["name"] = "pv-postgres-2"
        pv["metadata"]["uid"] = "uid-pv-postgres-2"
        pv["spec"]["persistentVolumeReclaimPolicy"] = "Delete"
        pv["spec"]["claimRef"].update(name="postgres-2", uid="uid-postgres-2")
        kube.objects[("pvc", "postgres-2", "database")] = pvc
        kube.objects[("pv", "pv-postgres-2", None)] = pv
        with mock.patch("builtins.print") as printed:
            with self.assertRaisesRegex(maintenance.MaintenanceError, "unretained"):
                maintenance.inspect(kube)
        self.assertTrue(any("postgres-2" in str(call) and "Delete" in str(call) for call in printed.call_args_list))


class KubectlTests(unittest.TestCase):
    @mock.patch.object(subprocess, "run")
    def test_context_argv_no_shell_and_redacted_error(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "Secret value")
        kube = maintenance.Kubectl("named-context")
        with self.assertRaises(maintenance.MaintenanceError) as ctx:
            kube.get("pv", "name")
        self.assertNotIn("Secret value", str(ctx.exception))
        self.assertEqual(run.call_args.args[0], ["kubectl", "--context", "named-context", "get", "pv", "name", "-o", "json"])
        self.assertFalse(run.call_args.kwargs.get("shell", False))

    @mock.patch.object(subprocess, "run")
    def test_create_uses_stdin_json_and_context(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "{}", "")
        document = {"apiVersion": "v1", "kind": "Example", "metadata": {"name": "safe"}}
        maintenance.Kubectl("named-context").create(document)
        self.assertEqual(run.call_args.args[0], ["kubectl", "--context", "named-context", "create", "-f", "-", "-o", "json"])
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), document)

    @mock.patch.object(subprocess, "run")
    def test_empty_get_is_inspection_failure(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with self.assertRaisesRegex(maintenance.MaintenanceError, "invalid resource"):
            maintenance.Kubectl("named-context").get("pv", "name")


if __name__ == "__main__":
    unittest.main()
