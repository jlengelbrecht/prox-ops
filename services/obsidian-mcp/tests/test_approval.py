"""Isolated contract tests for the app approval coordinator."""

import sys
import threading
import unittest
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from approval import (  # noqa: E402
    AdapterConflict, AgentContext, ApprovalCoordinator, ApprovalError, Operation, OwnerContext,
    DiagnosticCount, DiagnosticResult, PackageAttestation, PackagePreview, Preview, SettingRule, Snapshot,
    StateEntry, VaultEnrollment,
)


class FakeAdapter:
    def __init__(self):
        self.entries = (StateEntry("note", "note.md", "rev-1"),)
        self.writes = []
        self.observations = 0
        self.preview_error = False
        self.execute_error = False
        self.incomplete = False
        self.files = {"note.md": "rev-1"}
        self.reads = []
        self.destinations = set()
        self.package_bytes = b"package-v1"
        self.execute_package = None
        self.executed_package_bytes = None
        self.pins = {}
        self.on_observe = None

    def preview(self, action):
        if self.preview_error:
            raise RuntimeError("secret note body")
        entries = self.entries
        return Preview(Snapshot(entries, not self.incomplete, len(entries)), "Bounded diff")

    def observe(self, action, expected):
        self.observations += 1
        if self.on_observe:
            self.on_observe()
        entries = self.entries
        return Snapshot(entries, True, len(entries))

    def read(self, action):
        self.reads.append(action.verb)
        if action.verb == "diagnostics":
            return DiagnosticResult(action.arguments["kind"], "ok", (DiagnosticCount("app_error", 1),))
        return {"ok": True}

    def attest_package(self, action):
        pin = f"pin-{len(self.pins) + 1}"
        self.pins[pin] = self.package_bytes
        return PackageAttestation(action.arguments["source"], action.arguments["version"],
                                  hashlib.sha256(self.package_bytes).hexdigest(), pin)

    def preview_package(self, action):
        return PackagePreview(action.arguments["source"], action.arguments["version"],
                              hashlib.sha256(self.package_bytes).hexdigest())

    def release_package(self, pin):
        self.pins.pop(pin, None)

    def execute(self, action, *, package=None):
        if action.verb in {"plugin_install", "plugin_update"}:
            pinned = self.pins.get(package.pin) if package else None
            if pinned is None or hashlib.sha256(pinned).hexdigest() != package.digest:
                raise AdapterConflict()
            self.execute_package = package
            self.executed_package_bytes = pinned
        if action.verb in {"rename_note", "move_note", "rename_folder", "move_folder", "restore_note"}:
            if action.arguments["destination"] in self.destinations:
                raise AdapterConflict()
            self.destinations.add(action.arguments["destination"])
        if action.verb == "create_note":
            if action.arguments["path"] in self.files:
                raise AdapterConflict()
            self.files[action.arguments["path"]] = "rev-1"
        elif action.verb == "append_note":
            if self.files.get(action.arguments["path"]) != action.arguments["expected_revision"]:
                raise AdapterConflict()
            self.files[action.arguments["path"]] = "rev-2"
        self.writes.append((action.vault, action.verb))
        if self.execute_error:
            raise RuntimeError("secret note body")
        return {"ok": True}


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        self.now = 100.0
        self.registry = {
            "iam": VaultEnrollment(frozenset({"owner"}), {"sync.mode": SettingRule(str, ("on", "off"), "sync")}),
            "homelab": VaultEnrollment(frozenset({"owner"}), {}),
        }
        self.coordinator = ApprovalCoordinator(
            self.adapter, self.registry, clock=lambda: self.now, ttl=60,
        )
        self.agent = AgentContext("codex", {"iam": frozenset({"replace_note", "create_note", "append_note", "read_note", "plugin_install", "plugin_enable", "plugin_update", "plugin_uninstall", "plugin_disable", "setting_update"})})
        self.owner = OwnerContext("owner", frozenset({"iam", "homelab"}))
        self.action = Operation("iam", "replace_note", {"path": "note.md", "content": "new"})

    def assert_code(self, code, fn):
        with self.assertRaises(ApprovalError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("secret", str(raised.exception))

    def approved(self, action=None):
        action = action or self.action
        pending = self.coordinator.prepare(self.agent, action)
        self.coordinator.approve(self.owner, pending.id, pending.digest)
        return pending

    def test_harmless_call_executes_once(self):
        action = Operation("iam", "create_note", {"path": "new.md", "content": "text", "expected_absent": True})
        self.assertEqual(self.coordinator.execute(self.agent, action), {"ok": True})
        self.assertEqual(self.adapter.writes, [("iam", "create_note")])

    def test_create_collision_and_append_revision_are_preconditions(self):
        create = Operation("iam", "create_note", {"path": "note.md", "content": "text", "expected_absent": True})
        append = Operation("iam", "append_note", {"path": "note.md", "content": "more", "expected_revision": "old"})
        self.assert_code("stale_state", lambda: self.coordinator.execute(self.agent, create))
        self.assert_code("stale_state", lambda: self.coordinator.execute(self.agent, append))
        self.assertEqual(self.adapter.writes, [])
        append = Operation("iam", "append_note", {"path": "note.md", "content": "more", "expected_revision": "rev-1"})
        self.coordinator.execute(self.agent, append)
        self.assertEqual(self.adapter.files["note.md"], "rev-2")

    def test_prepare_requires_distinct_owner_and_has_no_mutation(self):
        pending = self.coordinator.prepare(self.agent, self.action)
        self.assertEqual(self.adapter.writes, [])
        self.assertTrue(pending.id)
        self.assertEqual(len(pending.digest), 64)
        self.assertEqual(pending.expires_at, 160)
        self.assert_code("approval_required", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        self.assert_code("invalid_context", lambda: self.coordinator.approve(self.agent, pending.id, pending.digest))
        owner_view = self.coordinator.owner_view(self.owner, pending.id)
        self.assertEqual(owner_view.action, self.action)
        self.assert_code("not_allowed", lambda: self.coordinator.owner_view(OwnerContext("other", frozenset({"iam"})), pending.id))
        self.coordinator.approve(self.owner, pending.id, pending.digest)
        self.coordinator.commit(self.agent, pending.id, self.action)
        self.assertEqual(self.adapter.writes, [("iam", "replace_note")])

    def test_agent_spoof_and_payload_approval_are_rejected(self):
        self.assert_code("invalid_action", lambda: self.coordinator.prepare(self.agent, Operation("iam", "replace_note", {"path": "note.md", "content": "new", "approved": True})))
        self.assert_code("invalid_context", lambda: self.coordinator.approve(self.agent, "pending", "digest"))
        self.assertEqual(self.adapter.writes, [])

    def test_owner_reject_and_wrong_owner(self):
        pending = self.coordinator.prepare(self.agent, self.action)
        self.assert_code("not_allowed", lambda: self.coordinator.approve(OwnerContext("other", frozenset({"iam"})), pending.id, pending.digest))
        self.coordinator.reject(self.owner, pending.id, pending.digest)
        self.assert_code("unknown_pending", lambda: self.coordinator.commit(self.agent, pending.id, self.action))

    def test_wrong_action_identity_vault_and_scope(self):
        pending = self.approved()
        self.assert_code("mismatch", lambda: self.coordinator.commit(self.agent, pending.id, Operation("iam", "replace_note", {"path": "other.md", "content": "new"})))
        self.assert_code("mismatch", lambda: self.coordinator.commit(AgentContext("other", self.agent.grants), pending.id, self.action))
        other_vault = Operation("homelab", "replace_note", self.action.arguments)
        self.assert_code("not_allowed", lambda: self.coordinator.commit(self.agent, pending.id, other_vault))
        self.assert_code("not_allowed", lambda: self.coordinator.commit(AgentContext("codex", {}), pending.id, self.action))
        self.coordinator.commit(self.agent, pending.id, self.action)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_stale_state_consumes_grant(self):
        pending = self.approved()
        self.adapter.entries = (StateEntry("note", "note.md", "rev-2"),)
        self.assert_code("stale_state", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        self.assert_code("unknown_pending", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        self.assertEqual(self.adapter.writes, [])

    def test_expiry_restart_and_replay(self):
        pending = self.approved()
        restarted = ApprovalCoordinator(self.adapter, self.registry, clock=lambda: self.now)
        self.assert_code("unknown_pending", lambda: restarted.commit(self.agent, pending.id, self.action))
        self.now = 161
        self.assert_code("expired", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        fresh = self.approved()
        self.coordinator.commit(self.agent, fresh.id, self.action)
        self.assert_code("unknown_pending", lambda: self.coordinator.commit(self.agent, fresh.id, self.action))

    def test_concurrent_commit_and_uncertain_failure(self):
        pending = self.approved()
        barrier = threading.Barrier(3)
        outcomes = []

        def run():
            barrier.wait()
            try:
                self.coordinator.commit(self.agent, pending.id, self.action)
                outcomes.append("ok")
            except ApprovalError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["ok", "unknown_pending"])
        self.assertEqual(len(self.adapter.writes), 1)
        pending = self.approved()
        self.adapter.execute_error = True
        self.assert_code("uncertain_result", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        self.assert_code("unknown_pending", lambda: self.coordinator.commit(self.agent, pending.id, self.action))

    def test_incomplete_preview_and_adapter_errors(self):
        self.adapter.incomplete = True
        self.assert_code("incomplete_preview", lambda: self.coordinator.prepare(self.agent, self.action))
        self.adapter.incomplete = False
        self.adapter.preview_error = True
        self.assert_code("adapter_failure", lambda: self.coordinator.prepare(self.agent, self.action))
        self.assertEqual(self.adapter.writes, [])

    def test_preview_must_bind_primary_destination_and_package(self):
        self.adapter.entries = (StateEntry("note", "other.md", "rev-1"),)
        self.assert_code("incomplete_preview", lambda: self.coordinator.prepare(self.agent, self.action))
        move = Operation("iam", "move_note", {"source": "note.md", "destination": "new.md"})
        mover = AgentContext("codex", {"iam": frozenset({"move_note"})})
        self.adapter.entries = (StateEntry("note", "note.md", "rev-1"),)
        self.assert_code("incomplete_preview", lambda: self.coordinator.prepare(mover, move))
        self.adapter.entries = (StateEntry("note", "note.md", "rev-1"), StateEntry("destination", "new.md", "absent"))
        pending = self.coordinator.prepare(mover, move)
        self.coordinator.reject(self.owner, pending.id, pending.digest)
        plugin = Operation("iam", "plugin_install", {"plugin_id": "p", "version": "2", "source": "catalog", "digest": "b" * 64})
        self.adapter.entries = (StateEntry("plugin", "p", "absent", "catalog", "1", "a" * 64),)
        self.assert_code("incomplete_preview", lambda: self.coordinator.prepare(self.agent, plugin))

    def test_request_bounds_and_scope_before_observation(self):
        self.assert_code("not_allowed", lambda: self.coordinator.prepare(AgentContext("other", {}), self.action))
        self.assertEqual(self.adapter.observations, 0)
        self.assert_code("invalid_action", lambda: self.coordinator.prepare(self.agent, Operation("iam", "replace_note", {"path": "note.md", "content": "x" * 4097})))
        self.assert_code("invalid_action", lambda: self.coordinator.execute(self.agent, Operation("iam", "read_note", {"path": "../escape.md"})))
        self.assert_code("invalid_action", lambda: self.coordinator.execute(self.agent, Operation("iam", "append_note", {"path": "note.md", "content": "x", "expected_revision": "rev-1", "approved": True})))
        self.assert_code("invalid_action", lambda: self.coordinator.execute(self.agent, Operation("iam", "create_note", {"path": "new.md", "content": "x"})))
        self.assertEqual(self.adapter.writes, [])

    def test_reentrant_adapter_call_fails_closed(self):
        original = self.adapter.read

        def nested(action):
            self.assert_code("reentrant_call", lambda: self.coordinator.execute(self.agent, action))
            return original(action)

        self.adapter.read = nested
        action = Operation("iam", "read_note", {"path": "note.md"})
        self.coordinator.execute(self.agent, action)
        self.assertEqual(self.adapter.writes, [])

    def test_all_read_verbs_have_zero_mutations(self):
        actions = {
            "read_note": {"path": "note.md"}, "list_notes": {}, "list_folders": {},
            "search": {"query": "text"}, "read_media": {"path": "image.png"},
            "plugin_catalog": {}, "diagnostics": {"kind": "errors"}, "sync_status": {},
        }
        agent = AgentContext("codex", {"iam": frozenset(actions)})
        for verb, args in actions.items():
            with self.subTest(verb=verb):
                self.coordinator.execute(agent, Operation("iam", verb, args))
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.reads, list(actions))

    def test_diagnostics_rejects_unstructured_or_secret_content(self):
        agent = AgentContext("codex", {"iam": frozenset({"diagnostics"})})
        action = Operation("iam", "diagnostics", {"kind": "errors"})
        self.adapter.read = lambda _: {"raw": "secret note body"}
        self.assert_code("invalid_diagnostics", lambda: self.coordinator.execute(agent, action))
        for result in (
            DiagnosticResult("errors", "ok", (DiagnosticCount("secret note body", 1),)),
            DiagnosticResult("errors", "ok", (DiagnosticCount("api_key=credential", 1),)),
            DiagnosticResult("errors", "ok", (DiagnosticCount("app_error", 100001),)),
            DiagnosticResult("console", "ok", (DiagnosticCount("app_error", 1),)),
            DiagnosticResult("errors", "ok", tuple(DiagnosticCount("warning", 1) for _ in range(33))),
        ):
            self.adapter.read = lambda _, result=result: result
            self.assert_code("invalid_diagnostics", lambda: self.coordinator.execute(agent, action))

    def test_adapter_exceptions_never_escape_as_public_error_codes(self):
        def bad_adapter(_):
            raise ApprovalError("secret note body")

        self.adapter.read = bad_adapter
        self.assert_code("adapter_failure", lambda: self.coordinator.execute(
            self.agent, Operation("iam", "read_note", {"path": "note.md"})))
        self.adapter.execute = bad_adapter
        self.assert_code("uncertain_result", lambda: self.coordinator.execute(
            self.agent, Operation("iam", "create_note",
                                  {"path": "new.md", "content": "text", "expected_absent": True})))

    def test_expiry_during_observe_prevents_mutation(self):
        pending = self.approved()
        self.adapter.on_observe = lambda: setattr(self, "now", 161)
        self.assert_code("expired", lambda: self.coordinator.commit(self.agent, pending.id, self.action))
        self.assertEqual(self.adapter.writes, [])
        self.assert_code("unknown_pending", lambda: self.coordinator.commit(self.agent, pending.id, self.action))

    def test_expiry_after_package_attestation_prevents_mutation(self):
        digest = hashlib.sha256(self.adapter.package_bytes).hexdigest()
        action = Operation("iam", "plugin_install",
                           {"plugin_id": "p", "version": "1", "source": "catalog", "digest": digest})
        self.adapter.entries = (StateEntry("plugin", "p", "absent"),
                                StateEntry("package", "p", "attested", "catalog", "1", digest))
        pending = self.approved(action)
        attest = self.adapter.attest_package

        def slow_attest(operation):
            result = attest(operation)
            if self.adapter.observations:
                self.now = 161
            return result

        self.adapter.attest_package = slow_attest
        self.assert_code("expired", lambda: self.coordinator.commit(self.agent, pending.id, action))
        self.assertEqual(self.adapter.writes, [])
        self.assertEqual(self.adapter.pins, {})

    def test_owner_view_shows_client_and_affected_snapshot(self):
        a = self.coordinator.prepare(self.agent, self.action)
        b_agent = AgentContext("other", self.agent.grants)
        self.adapter.entries = (StateEntry("note", "note.md", "rev-2"),)
        b = self.coordinator.prepare(b_agent, self.action)
        a_view = self.coordinator.owner_view(self.owner, a.id)
        b_view = self.coordinator.owner_view(self.owner, b.id)
        self.assertEqual(a_view.client_id, "codex")
        self.assertEqual(b_view.client_id, "other")
        self.assertEqual(a_view.state, (StateEntry("note", "note.md", "rev-1"),))
        self.assertEqual(b_view.state, (StateEntry("note", "note.md", "rev-2"),))
        self.assertNotEqual(a_view.digest, b_view.digest)

    def test_guarded_moves_never_clobber_destination(self):
        mover = AgentContext("codex", {"iam": frozenset({"move_note"})})
        action = Operation("iam", "move_note", {"source": "note.md", "destination": "new.md"})
        self.adapter.entries = (StateEntry("note", "note.md", "rev-1"),
                                StateEntry("destination", "new.md", "occupied-rev"))
        self.assert_code("stale_state", lambda: self.coordinator.prepare(mover, action))
        self.assertEqual(self.adapter.writes, [])
        self.adapter.entries = (StateEntry("note", "note.md", "rev-1"),
                                StateEntry("destination", "new.md", "absent"))
        pending = self.coordinator.prepare(mover, action)
        self.coordinator.approve(self.owner, pending.id, pending.digest)
        self.adapter.destinations.add("new.md")
        self.assert_code("stale_state", lambda: self.coordinator.commit(mover, pending.id, action))
        self.assertEqual(self.adapter.writes, [])

    def test_vault_isolation_and_capacity(self):
        self.assert_code("unknown_vault", lambda: self.coordinator.prepare(self.agent, Operation("missing", "replace_note", self.action.arguments)))
        self.assert_code("not_allowed", lambda: self.coordinator.prepare(self.agent, Operation("homelab", "replace_note", self.action.arguments)))
        both = AgentContext("codex", {"iam": frozenset({"replace_note"}), "homelab": frozenset({"replace_note"})})
        other_action = Operation("homelab", "replace_note", self.action.arguments)
        other = self.coordinator.prepare(both, other_action)
        self.assertNotEqual(other.digest, self.coordinator.prepare(both, self.action).digest)
        self.assert_code("not_allowed", lambda: self.coordinator.approve(OwnerContext("owner", frozenset({"iam"})), other.id, other.digest))
        self.coordinator.approve(self.owner, other.id, other.digest)
        self.assert_code("not_allowed", lambda: self.coordinator.commit(self.agent, other.id, other_action))
        self.coordinator.commit(both, other.id, other_action)
        self.assertEqual(self.adapter.writes, [("homelab", "replace_note")])
        limited = ApprovalCoordinator(self.adapter, self.registry, clock=lambda: self.now, max_pending=1)
        first = limited.prepare(self.agent, self.action)
        self.assert_code("capacity", lambda: limited.prepare(self.agent, self.action))
        limited.reject(self.owner, first.id, first.digest)
        limited.prepare(self.agent, self.action)

    def test_plugin_and_setting_operations_need_owner(self):
        package_digest = hashlib.sha256(self.adapter.package_bytes).hexdigest()
        cases = {
            "plugin_install": {"plugin_id": "p", "version": "1", "source": "catalog", "digest": package_digest},
            "plugin_enable": {"plugin_id": "p"},
            "plugin_disable": {"plugin_id": "p"},
            "plugin_update": {"plugin_id": "p", "version": "2", "source": "catalog", "digest": package_digest},
            "plugin_uninstall": {"plugin_id": "p"},
            "setting_update": {"setting_id": "sync.mode", "value": "off"},
        }
        self.adapter.entries = (StateEntry("plugin", "p", "installed-1", "catalog", "1", "a" * 64),)
        for verb, arguments in cases.items():
            with self.subTest(verb=verb):
                if verb == "setting_update":
                    self.adapter.entries = (StateEntry("setting", "sync.mode", "value-hash"),)
                elif verb in {"plugin_install", "plugin_update"}:
                    installed = (StateEntry("plugin", "p", "absent") if verb == "plugin_install"
                                 else StateEntry("plugin", "p", "installed-1", "catalog", "1", "a" * 64))
                    self.adapter.entries = (installed, StateEntry("package", "p", "attested",
                                             "catalog", arguments["version"], package_digest))
                else:
                    self.adapter.entries = (StateEntry("plugin", "p", "installed-1", "catalog", "1", "a" * 64),)
                action = Operation("iam", verb, arguments)
                self.assert_code("approval_required", lambda: self.coordinator.execute(self.agent, action))
                pending = self.coordinator.prepare(self.agent, action)
                self.assertEqual(self.adapter.writes, [])
                self.coordinator.reject(self.owner, pending.id, pending.digest)
        self.assert_code("not_allowed", lambda: self.coordinator.prepare(
            self.agent, Operation("iam", "setting_update", {"setting_id": "sync.mode", "value": "invalid"})))
        self.assert_code("not_allowed", lambda: self.coordinator.prepare(
            self.agent, Operation("iam", "setting_update", {"setting_id": "sync.mode", "value": True})))

    def test_plugin_artifact_bytes_are_attested_and_version_drift_rejected(self):
        digest = hashlib.sha256(self.adapter.package_bytes).hexdigest()
        action = Operation("iam", "plugin_update",
                           {"plugin_id": "p", "version": "2", "source": "catalog", "digest": digest})
        installed = StateEntry("plugin", "p", "installed-1", "catalog", "1", "a" * 64)
        artifact = StateEntry("package", "p", "attested", "catalog", "2", digest)
        self.adapter.entries = (installed, artifact)
        pending = self.approved(action)
        self.assertEqual(self.coordinator.owner_view(self.owner, pending.id).state, tuple(sorted((installed, artifact), key=lambda e: (e.kind, e.key))))
        self.adapter.entries = (StateEntry("plugin", "p", "installed-2", "catalog", "1", "a" * 64), artifact)
        self.assert_code("stale_state", lambda: self.coordinator.commit(self.agent, pending.id, action))
        self.assertEqual(self.adapter.writes, [])
        self.adapter.entries = (installed, artifact)
        pending = self.approved(action)
        self.adapter.package_bytes = b"changed-at-same-source"
        self.assert_code("stale_state", lambda: self.coordinator.commit(self.agent, pending.id, action))
        self.assertEqual(self.adapter.writes, [])
        self.assert_code("incomplete_preview", lambda: self.coordinator.prepare(self.agent, action))
        self.adapter.package_bytes = b"package-v1"
        pending = self.approved(action)
        self.coordinator.commit(self.agent, pending.id, action)
        self.assertEqual(self.adapter.execute_package.digest, digest)
        self.assertEqual(self.adapter.executed_package_bytes, b"package-v1")
        self.assertEqual(self.adapter.pins, {})

    def test_package_pins_are_short_lived_across_reject_expiry_and_failures(self):
        digest = hashlib.sha256(self.adapter.package_bytes).hexdigest()
        action = Operation("iam", "plugin_install",
                           {"plugin_id": "p", "version": "1", "source": "catalog", "digest": digest})
        self.adapter.entries = (StateEntry("plugin", "p", "absent"),
                                StateEntry("package", "p", "attested", "catalog", "1", digest))
        for _ in range(30):
            pending = self.coordinator.prepare(self.agent, action)
            self.assertEqual(self.adapter.pins, {})
            self.coordinator.reject(self.owner, pending.id, pending.digest)
            self.assertEqual(self.adapter.pins, {})
        pending = self.approved(action)
        self.now = 161
        self.assert_code("expired", lambda: self.coordinator.commit(self.agent, pending.id, action))
        self.assertEqual(self.adapter.pins, {})
        self.now = 100
        pending = self.approved(action)
        self.adapter.execute_error = True
        self.assert_code("uncertain_result", lambda: self.coordinator.commit(self.agent, pending.id, action))
        self.assertEqual(self.adapter.pins, {})

    def test_unknown_unsafe_and_noncanonical_operations(self):
        for verb in ("shell", "eval", "command", "dev:cdp", "hard_delete", "write_config", "plugin_command"):
            self.assert_code("invalid_action", lambda verb=verb: self.coordinator.prepare(self.agent, Operation("iam", verb, {})))
        self.assert_code("invalid_action", lambda: self.coordinator.execute(self.agent, Operation("iam", "append_note", {"path": "note.md", "content": "x"})))
        self.assert_code("invalid_action", lambda: self.coordinator.execute(self.agent, Operation("iam", "create_note", {"path": "new.md", "content": "x", "expected_absent": False})))
        self.assertEqual(self.adapter.writes, [])


if __name__ == "__main__":
    unittest.main()
