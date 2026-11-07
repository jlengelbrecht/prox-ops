"""The sync job against the dev server, with op replaced by a double that keeps items in a JSON
file. It mimics op's interface as documented, so it doesn't prove the real CLI behaves the same."""
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import lab

SYNC = str(lab.ROOT / "scripts/pam/pam_sync.py")
CANARY = "S3cretCanaryValue"

OP_DOUBLE = r'''#!/usr/bin/env python3
"""A stand-in for op: item list, get, create and edit, stored in $OP_STORE."""
import json, os, sys
store_path = os.environ["OP_STORE"]
store = json.load(open(store_path))
args = sys.argv[1:]
with open(os.environ["OP_CALLS"], "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("OP_FAIL") and args[1] in os.environ["OP_FAIL"].split(","):
    sys.exit(1)
def flag(name):
    return args[args.index(name) + 1] if name in args else None
def write():
    store["clock"] += 1
    json.dump(store, open(store_path, "w"))
    return f"2026-11-07T00:00:{store['clock']:02d}Z"
def summary(item):
    return {k: item[k] for k in ("id", "title", "tags", "updated_at")}
action = args[1]
if action == "list":
    print(json.dumps([summary(i) for i in store["items"].values() if flag("--tags") in i["tags"]]))
elif action == "get":
    print(json.dumps(store["items"][args[2]]))
elif action == "create":
    assert args[-3:] == ["--format", "json"] or "-" in args
    doc = json.load(sys.stdin)
    item_id = f"id{len(store['items']) + 1}"
    item = {"id": item_id, "title": flag("--title"), "tags": flag("--tags").split(","), "fields": doc["fields"]}
    store["items"][item_id] = item
    item["updated_at"] = write()
    json.dump(store, open(store_path, "w"))
    print(json.dumps(item))
elif action == "edit":
    item = store["items"][args[2]]
    if flag("--tags"):
        item["tags"] = flag("--tags").split(",")
    else:
        doc = json.load(sys.stdin)
        item["fields"], item["tags"] = doc["fields"], doc["tags"]
    item["updated_at"] = write()
    json.dump(store, open(store_path, "w"))
    print(json.dumps(item))
'''


class Sync(unittest.TestCase):
    def setUp(self):
        self.lab = lab.Lab()
        self.addCleanup(self.lab.close)
        self.dir = tempfile.TemporaryDirectory(prefix="pam-sync-")
        self.addCleanup(self.dir.cleanup)
        base = pathlib.Path(self.dir.name)
        (base / "op").write_text(OP_DOUBLE)
        (base / "op").chmod(stat.S_IRWXU)
        self.store = base / "store.json"
        self.store.write_text(json.dumps({"items": {}, "clock": 0}))
        self.calls = base / "calls"
        self.calls.touch()
        self.env = {"PATH": f"{base}:{os.environ['PATH']}", "BAO_ADDR": self.lab.addr,
                    "BAO_TOKEN": self.lab.token("pam-sync"), "OP_STORE": str(self.store), "OP_CALLS": str(self.calls)}

    def sync(self, **env):
        return subprocess.run([sys.executable, SYNC], env={**self.env, **env}, capture_output=True, text=True, timeout=30)

    def items(self):
        return json.loads(self.store.read_text())["items"]

    def item(self, title):
        return next(i for i in self.items().values() if i["title"] == title)

    def edit_item(self, title, **changes):
        state = json.loads(self.store.read_text())
        item = next(i for i in state["items"].values() if i["title"] == title)
        item.update(changes)
        self.store.write_text(json.dumps(state))

    def fields(self, title):
        return {f["label"]: f for f in self.item(title)["fields"]}

    def writes(self):
        return [c for c in self.calls.read_text().splitlines() if '"create"' in c or '"edit"' in c]

    def custom(self, path):
        return self.lab.ok("GET", f"kv/metadata/pam/{path}")["data"]["custom_metadata"]

    def assertClean(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn(CANARY, result.stdout + result.stderr)

    def test_creates_one_item_per_path(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/inbox/agent-claude/two", value=CANARY, port=5432)
        self.assertClean(self.sync())
        fields = self.fields("pam/live/one")
        self.assertEqual(fields["password"]["type"], "CONCEALED")
        self.assertEqual(fields["password"]["value"], CANARY)
        self.assertEqual(fields["bao_path"]["value"], "live/one")
        self.assertEqual(fields["bao_version"]["value"], "1")
        self.assertEqual(self.item("pam/live/one")["tags"], ["openbao-sync"])
        self.assertEqual(self.fields("pam/inbox/agent-claude/two")["port"]["value"], "5432")
        custom = self.custom("live/one")
        self.assertEqual(custom["pam_synced_version"], "1")
        self.assertEqual(custom["pam_item_updated"], self.item("pam/live/one")["updated_at"])

    def test_values_never_reach_the_command_line(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.seed("pam/live/one", password=CANARY + "2")
        self.sync()
        self.assertNotIn(CANARY, self.calls.read_text())

    def test_only_changed_paths_write(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/two", password=CANARY)
        self.sync()
        first = len(self.writes())
        self.assertClean(self.sync())
        self.assertEqual(len(self.writes()), first)
        self.assertEqual(len(self.calls.read_text().splitlines()), first + 2)  # The first and second list.

    def test_rotation_updates_the_same_item(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        item_id = self.item("pam/live/one")["id"]
        self.lab.seed("pam/live/one", password="rotated")
        self.assertClean(self.sync())
        self.assertEqual(self.item("pam/live/one")["id"], item_id)
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], "rotated")
        self.assertEqual(self.fields("pam/live/one")["bao_version"]["value"], "2")
        self.assertEqual(self.custom("live/one")["pam_synced_version"], "2")

    def test_a_hand_edit_holds_the_item_and_fails(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.edit_item("pam/live/one", updated_at="2026-11-07T01:00:00Z")
        self.lab.seed("pam/live/one", password="rotated")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("pam/live/one", result.stdout)
        self.assertIn("edited", result.stdout)
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)

    def test_a_hand_edit_holds_a_soft_delete_and_survives_a_new_version(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.edit_item("pam/live/one", updated_at="2026-11-07T01:00:00Z")
        self.lab.ok("DELETE", "kv/data/pam/live/one")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("edited", result.stdout)
        self.assertNotIn("deleted-in-openbao", self.item("pam/live/one")["tags"])
        self.lab.seed("pam/live/one", password="rotated")
        self.assertEqual(self.sync().returncode, 1)
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)

    def test_values_come_from_the_version_asked_for(self):
        import importlib.util
        self.lab.seed("pam/live/one", password="first")
        self.lab.seed("pam/live/one", password="second")
        env = {"BAO_ADDR": self.lab.addr, "BAO_TOKEN": self.env["BAO_TOKEN"]}
        with unittest.mock.patch.dict(os.environ, env):
            spec = importlib.util.spec_from_file_location("pam_sync_under_test", SYNC)
            sync = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sync)
            bao = sync.Bao()
        self.assertEqual(bao.values("live/one", 1), {"password": "first"})
        self.assertEqual(bao.values("live/one", 2), {"password": "second"})

    def test_an_item_ahead_of_openbao_holds_and_fails(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        fields = self.item("pam/live/one")["fields"]
        next(f for f in fields if f["label"] == "bao_version")["value"] = "9"
        self.edit_item("pam/live/one", fields=fields, updated_at="2026-11-07T01:00:00Z")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("ahead of OpenBao", result.stdout)
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)

    def test_one_held_item_doesnt_stop_the_others(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.edit_item("pam/live/one", updated_at="2026-11-07T01:00:00Z")
        self.lab.seed("pam/live/two", password=CANARY)
        self.assertEqual(self.sync().returncode, 1)
        self.assertEqual(self.fields("pam/live/two")["bao_version"]["value"], "1")

    def test_a_crash_after_writing_the_item_recovers_without_a_false_alarm(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.seed("pam/live/one", password="rotated")
        fields = self.item("pam/live/one")["fields"]
        next(f for f in fields if f["label"] == "bao_version")["value"] = "2"
        content = hashlib.sha256(json.dumps({"password": "rotated"}, sort_keys=True).encode()).hexdigest()
        next(f for f in fields if f["label"] == "bao_hash")["value"] = content
        self.edit_item("pam/live/one", fields=fields, updated_at="2026-11-07T01:00:00Z")
        self.assertClean(self.sync())
        self.assertEqual(self.custom("live/one")["pam_synced_version"], "2")

    def test_soft_delete_tags_the_item_and_keeps_it(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/data/pam/live/one")
        self.assertClean(self.sync())
        item = self.item("pam/live/one")
        self.assertIn("deleted-in-openbao", item["tags"])
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)
        before = len(self.writes())
        self.assertClean(self.sync())
        self.assertEqual(len(self.writes()), before)

    def test_undelete_clears_the_tag(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/data/pam/live/one")
        self.sync()
        self.lab.ok("POST", "kv/undelete/pam/live/one", {"versions": [1]})
        self.assertClean(self.sync())
        self.assertEqual(self.item("pam/live/one")["tags"], ["openbao-sync"])

    def test_a_new_version_after_delete_clears_the_tag(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/data/pam/live/one")
        self.sync()
        self.lab.seed("pam/live/one", password="back")
        self.assertClean(self.sync())
        self.assertEqual(self.item("pam/live/one")["tags"], ["openbao-sync"])
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], "back")

    def test_an_empty_listing_with_items_holds_the_run(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/two", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/metadata/pam/live/one")
        self.lab.ok("DELETE", "kv/metadata/pam/live/two")
        before = len(self.writes())
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold run: OpenBao lists nothing", result.stdout)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        self.assertEqual(len(self.writes()), before)
        self.assertEqual(self.item("pam/live/one")["tags"], ["openbao-sync"])

    def test_full_delete_tags_the_item_and_keeps_it(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/kept", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/metadata/pam/live/one")
        self.assertClean(self.sync())
        self.assertIn("deleted-in-openbao", self.item("pam/live/one")["tags"])
        self.assertClean(self.sync())

    def test_never_calls_delete(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/kept", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/metadata/pam/live/one")
        self.sync()
        self.assertNotIn('"delete"', self.calls.read_text())

    def test_an_op_failure_fails_the_job_and_retries(self):
        self.lab.seed("pam/live/one", password=CANARY)
        result = self.sync(OP_FAIL="create")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        self.assertClean(self.sync())
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)

    def test_openbao_down_fails_the_job(self):
        result = self.sync(BAO_ADDR=f"http://127.0.0.1:{lab.free_port()}")
        self.assertNotEqual(result.returncode, 0)

    def test_same_version_with_other_content_holds(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.ok("PATCH", "kv/metadata/pam/live/one", {"custom_metadata": {"pam_content_hash": "older"}},
                    headers={"Content-Type": "application/merge-patch+json"})
        fields = self.item("pam/live/one")["fields"]
        next(f for f in fields if f["label"] == "bao_hash")["value"] = "older"
        self.edit_item("pam/live/one", fields=fields)
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("different content", result.stdout)
        self.assertEqual(self.fields("pam/live/one")["password"]["value"], CANARY)

    def test_a_content_change_with_a_matching_version_is_noticed_without_a_write(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        self.lab.ok("PATCH", "kv/metadata/pam/live/one", {"custom_metadata": {"pam_content_hash": "stale"}},
                    headers={"Content-Type": "application/merge-patch+json"})
        before = len(self.writes())
        self.assertClean(self.sync())
        self.assertEqual(len(self.writes()), before)
        self.assertNotEqual(self.custom("live/one")["pam_content_hash"], "stale")

    def test_hand_added_tags_survive(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/kept", password=CANARY)
        self.sync()
        self.edit_item("pam/live/one", tags=["openbao-sync", "keep-me"])
        self.lab.ok("PATCH", "kv/metadata/pam/live/one", {"custom_metadata": {"pam_item_updated": self.item("pam/live/one")["updated_at"]}},
                    headers={"Content-Type": "application/merge-patch+json"})
        self.lab.seed("pam/live/one", password="rotated")
        self.assertClean(self.sync())
        self.assertIn("keep-me", self.item("pam/live/one")["tags"])
        self.lab.ok("DELETE", "kv/data/pam/live/one")
        self.assertClean(self.sync())
        self.assertEqual(set(self.item("pam/live/one")["tags"]), {"openbao-sync", "deleted-in-openbao", "keep-me"})
        self.lab.seed("pam/live/one", password="back")
        self.assertClean(self.sync())
        self.assertEqual(set(self.item("pam/live/one")["tags"]), {"openbao-sync", "keep-me"})
        self.lab.ok("DELETE", "kv/metadata/pam/live/one")
        self.assertClean(self.sync())
        self.assertEqual(set(self.item("pam/live/one")["tags"]), {"openbao-sync", "deleted-in-openbao", "keep-me"})

    def test_a_removed_key_leaves_the_item_and_built_in_fields_stay(self):
        self.lab.seed("pam/live/one", password=CANARY, old=CANARY)
        self.sync()
        fields = self.item("pam/live/one")["fields"] + [{"id": "notesPlain", "label": "notesPlain", "purpose": "NOTES", "value": "note"}]
        self.edit_item("pam/live/one", fields=fields)
        self.lab.seed("pam/live/one", password=CANARY)
        self.assertClean(self.sync())
        self.assertEqual(set(self.fields("pam/live/one")), {"bao_path", "bao_version", "bao_hash", "password", "notesPlain"})

    def test_a_duplicate_title_stops_that_path_only(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/two", password=CANARY)
        self.sync()
        first = self.item("pam/live/one")
        state = json.loads(self.store.read_text())
        state["items"]["twin"] = {**first, "id": "twin"}
        self.store.write_text(json.dumps(state))
        self.lab.seed("pam/live/one", password="rotated")
        self.lab.seed("pam/live/two", password="rotated")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("more than one item"), 1)
        self.assertEqual(self.custom("live/one")["pam_synced_version"], "1")
        self.assertEqual(self.custom("live/two")["pam_synced_version"], "2")
        self.assertEqual(next(f for f in first["fields"] if f["label"] == "password")["value"], CANARY)
        for item_id in (first["id"], "twin"):
            stored = json.loads(self.store.read_text())["items"][item_id]
            self.assertEqual(next(f for f in stored["fields"] if f["label"] == "password")["value"], CANARY)

    def test_bad_version_metadata_holds_one_path(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed("pam/live/two", password=CANARY)
        self.sync()
        self.lab.ok("PATCH", "kv/metadata/pam/live/one", {"custom_metadata": {"pam_synced_version": "not-a-number"}},
                    headers={"Content-Type": "application/merge-patch+json"})
        self.lab.seed("pam/live/two", password="rotated")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("pam_synced_version", result.stdout)
        self.assertEqual(self.custom("live/two")["pam_synced_version"], "2")

    def test_a_bad_version_on_the_item_holds_one_path(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        fields = self.item("pam/live/one")["fields"]
        next(f for f in fields if f["label"] == "bao_version")["value"] = "nine"
        self.edit_item("pam/live/one", fields=fields, updated_at="2026-11-07T01:00:00Z")
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("bao_version", result.stdout)

    def test_names_outside_the_bounds_hold_and_the_rest_carry_on(self):
        self.lab.seed("pam/inbox/agent-claude/" + "n" * 210, value=CANARY)
        self.lab.seed("pam/inbox/agent-claude/" + "/".join("d" * 8), value=CANARY)
        self.lab.seed("pam/inbox/agent-claude/bad%20name", value=CANARY)
        self.lab.seed("pam/live/fine", value=CANARY)
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.count("outside the allowed bounds"), 3)
        self.assertEqual([i["title"] for i in self.items().values()], ["pam/live/fine"])

    def test_log_lines_cant_be_forged_by_a_name(self):
        self.lab.ok("POST", "kv/data/pam/inbox/agent-claude/forged", {"data": {"x\nfailed nothing": CANARY}})
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)

    def test_keys_outside_the_bounds_hold(self):
        self.lab.seed("pam/live/long", **{"k" * 65: "x"})
        self.lab.seed("pam/live/many", **{f"k{i}": "x" for i in range(51)})
        self.lab.ok("POST", "kv/data/pam/live/odd", {"data": {"bad key": "x"}})
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.items(), {})

    def test_a_listing_error_holds_the_run_and_marks_nothing_deleted(self):
        self.lab.seed("pam/live/one", password=CANARY)
        self.sync()
        result = self.sync(BAO_TOKEN=self.lab.token("default"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold run: could not list", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.item("pam/live/one")["tags"], ["openbao-sync"])

    def test_a_failure_part_way_down_marks_nothing_deleted(self):
        name = "n" * 150
        self.lab.seed("pam/live/one", password=CANARY)
        self.lab.seed(f"pam/{name}/x", password=CANARY)
        self.lab.seed("pam/zz/gone", password=CANARY)
        self.sync()
        self.lab.ok("DELETE", "kv/metadata/pam/zz/gone")
        self.lab.policy("partial", f'''
path "kv/metadata/pam/*" {{
  capabilities = ["read", "list"]
}}
path "kv/metadata/pam/{name}/*" {{
  capabilities = ["deny"]
}}
''')
        result = self.sync(BAO_TOKEN=self.lab.token("partial"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("hold run: could not list", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn(name, result.stdout + result.stderr)
        self.assertEqual(self.item("pam/zz/gone")["tags"], ["openbao-sync"])

    def test_reserved_key_is_held(self):
        self.lab.seed("pam/live/one", bao_version="x", password=CANARY)
        result = self.sync()
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved", result.stdout)

    def test_scope_is_kv_pam_only(self):
        self.lab.seed("warpgate/k8s-read", token=CANARY)
        self.assertClean(self.sync())
        self.assertEqual(self.items(), {})

    def test_empty_store_is_fine(self):
        self.assertClean(self.sync())


if __name__ == "__main__":
    unittest.main()
