"""Bounded runner, allowlist, gate, budget, runtime pins and coherence."""

import os
import selectors
import subprocess
import sys
import time
import unittest
from unittest import mock

import _support as S
from orca_adapter import errors
from orca_adapter.config import Config
from orca_adapter.runner import (
    MAX_COMMANDS_PER_OPERATION,
    MAX_CURSOR_CHARS,
    MAX_LIMIT,
    OPERATION_DEADLINE_SECONDS,
    OrcaRunner,
    _parse_json_object,
    execute_bounded,
)


class _RunnerCase(unittest.TestCase):
    """Shared fixture: a harness with the baseline scenario and the fakes' environment."""

    def setUp(self):
        self.h = S.Harness()
        self.addCleanup(self.h.close)
        self.h.write_scenario(S.build_scenario())
        self.patcher = self.h.patch_env()
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def runner(self, **kw):
        return OrcaRunner(self.h.config(**kw))


class RunnerTests(_RunnerCase):
    def test_gate_runs_before_each_command_with_configured_argv(self):
        # Both fakes append to one log here, so the assertion pins the interleaving
        # (gate, command, gate, command, ...) and not only the two call counts.
        os.environ["FAKE_PREFLIGHT_LOG"] = self.h.orca_log
        r = self.runner(gate=(sys.executable, S.FAKE_PREFLIGHT, "--marker", "x"))
        r.run(("project", "list"))
        r.run(("host", "list"))
        r.run(("worktree", "list"), limit=5)
        self.assertEqual((r.gate_count, r.command_count), (3, 3))
        self.assertEqual(self.h.orca_calls(), [
            ["--marker", "x"], ["project", "list", "--json"],  # gate argv passed through verbatim
            ["--marker", "x"], ["host", "list", "--json"],
            ["--marker", "x"], ["worktree", "list", "--limit", "5", "--json"]])

    def test_without_gate_commands_run_and_nothing_else_is_spawned(self):
        r = self.runner(gate=())
        r.run(("project", "list"))
        r.run(("host", "list"))
        self.assertEqual(self.h.preflight_calls(), [])
        self.assertEqual((r.gate_count, r.command_count), (0, 2))

    def test_rejected_gate_blocks_before_any_metadata_read(self):
        for exit_code, detail in ((1, "findings"), (2, "blocked"), (3, "exit")):  # 3+: any other refusal
            os.environ["FAKE_PREFLIGHT_EXIT"] = str(exit_code)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().run(("project", "list"))
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.PREFLIGHT_REJECTED, detail))
            self.assertEqual(cm.exception.exit_code, errors.EXIT_CONFIG)
        self.assertEqual(self.h.orca_calls(), [])

    def test_missing_gate_script_fails_closed(self):
        cfg = self.h.config(gate=(sys.executable, os.path.join(self.h.dir, "absent.py")))
        with self.assertRaises(errors.AdapterError) as cm:
            OrcaRunner(cfg).run(("status",))
        self.assertEqual(cm.exception.code, errors.PREFLIGHT_REJECTED)  # python exits 2 on a missing file
        self.assertEqual(self.h.orca_calls(), [])

    def test_gate_execution_failures_stop_before_any_metadata_read(self):
        # The gate itself cannot be spawned, hangs, or floods: each is the bounded
        # ``preflight_failed`` with the child's code as detail, exit 5, and no Orca spawn.
        self.h.write_scenario({"status": {"mode": "hang"}, "flood": {"mode": "flood", "stream": "stdout", "bytes": 2 * 1024 * 1024}})
        for gate, detail in (
            ((os.path.join(self.h.dir, "no-such-gate"),), "orca_exec_failed"),  # detail is the bounded child code
            ((sys.executable, S.FAKE_ORCA, "status"), "orca_timeout"),  # the peer's hang mode, used as a gate
            ((sys.executable, S.FAKE_ORCA, "flood"), "orca_output_too_large"),
        ):
            cfg = self.h.config(timeout=1.0, max_bytes=256 * 1024, gate=gate)
            with self.assertRaises(errors.AdapterError, msg=detail) as cm:
                OrcaRunner(cfg).run(("project", "list"))
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.PREFLIGHT_FAILED, "project list", detail))
            self.assertEqual(cm.exception.exit_code, errors.EXIT_CONFIG)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        # The gate spawns were the peer itself here, so the log shows only gate-role calls: never ``project list``.
        self.assertEqual([c[0] for c in self.h.orca_calls()], ["status", "flood"])
        self._assert_children_gone()

    def test_config_rejects_bad_executable_and_gate_shapes(self):
        for kw, detail in (
            ({"executable": ()}, "bad_executable"), ({"executable": ("",)}, "bad_executable"),
            ({"executable": ("-x",)}, "bad_executable"), ({"executable": ("a", "")}, "bad_executable"),
            ({"executable": (sys.executable,), "preflight_argv": ("relative.py",)}, "bad_gate"),
            ({"executable": (sys.executable,), "preflight_argv": (sys.executable, "")}, "bad_gate"),
        ):
            with self.assertRaises(errors.AdapterError, msg=kw) as cm:
                Config(key_file=self.h.key_file, **kw)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, detail), kw)
        self.assertEqual(self.h.orca_calls(), [])

    def test_config_bounds_are_finite_positive_and_capped_on_the_api_path(self):
        base = {"executable": (sys.executable,), "key_file": self.h.key_file}
        for kw in (
            {"timeout_seconds": 0}, {"timeout_seconds": -1}, {"timeout_seconds": float("inf")},
            {"timeout_seconds": float("nan")}, {"timeout_seconds": 20.001}, {"timeout_seconds": True},
            {"timeout_seconds": "20"}, {"max_output_bytes": 0}, {"max_output_bytes": -1},
            {"max_output_bytes": 4 * 1024 * 1024 + 1}, {"max_output_bytes": 10 ** 12},
            {"max_output_bytes": float("inf")}, {"max_output_bytes": 1024.0}, {"max_output_bytes": True},
        ):
            with self.assertRaises(errors.AdapterError, msg=kw) as cm:
                Config(**base, **kw)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, "bad_bounds"))
        cfg = Config(**base, timeout_seconds=1, max_output_bytes=1)  # tightening is allowed
        self.assertEqual((cfg.timeout_seconds, cfg.max_output_bytes), (1, 1))
        cfg = Config(**base)  # the documented defaults are the caps
        self.assertEqual((cfg.timeout_seconds, cfg.max_output_bytes), (20.0, 4 * 1024 * 1024))

    def test_allowlist_refuses_other_commands_and_flags(self):
        r = self.runner()
        # A refused command carries the fixed stage ``command`` and no detail; a refused
        # flag names its command as the stage and the flag (or ``flag`` when unknown) as detail.
        for command, flags, detail in (
            (("orchestration", "check"), {}, ""), (("terminal", "read"), {}, ""), (("orchestration", "send"), {}, ""),
            (("orchestration", "gate-show"), {}, ""), (("project", "list"), {"limit": 1}, "flag"),
            (("repo", "list"), {}, ""), (("worktree", "ps"), {}, ""), (("orchestration", "run-current"), {}, ""),
            (("host", "list"), {"page": 2}, "flag"),
            (("worktree", "list"), {"limit": 0}, "limit"), (("worktree", "list"), {"limit": MAX_LIMIT + 1}, "limit"),
            (("worktree", "list"), {"limit": "5"}, "limit"), (("orchestration", "run-list"), {"cursor": "-x"}, "cursor"),
            (("orchestration", "run-list"), {"cursor": "a b"}, "cursor"), (("orchestration", "task-list"), {"run": "run_a;x"}, "run"),
            (("orchestration", "task-list"), {"run": "--from"}, "run"), (("orchestration", "task-list"), {"brief": False}, "brief"),
        ):
            with self.assertRaises(errors.AdapterError, msg=(command, flags)) as cm:
                r.run(command, **flags)
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.COMMAND_NOT_ALLOWED, " ".join(command) if detail else "command", detail), (command, flags))
        self.assertEqual(self.h.orca_calls(), [])
        self.assertEqual(self.h.preflight_calls(), [])

    def test_nonzero_exit_is_bounded(self):
        sc = S.build_scenario()
        sc["project list"] = {"mode": "garbage", "text": "fatal: CANARY_PATH\n", "exit": 7}
        self.h.write_scenario(sc)
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().run(("project", "list"))
        self.assertEqual(cm.exception.code, errors.ORCA_EXEC_FAILED)
        self.assertEqual(cm.exception.exit_code, errors.EXIT_FAILURE)
        S.assert_no_canaries(self, str(cm.exception) + repr(cm.exception.to_json()))

    def test_ok_false_envelope_maps_to_bounded_orca_error(self):
        sc = S.build_scenario()
        sc["orchestration gate-list"] = {"response": {"ok": False, "error": {"code": "run_not_found", "message": "CANARY_PATH"}}, "exit": 1}
        sc["project list"] = {"response": {"ok": False, "error": {"code": "a" * 40}}, "exit": 1}
        sc["host list"] = {"response": {"ok": False, "error": {"code": "unknown_command", "message": "x"}}, "exit": 1}
        self.h.write_scenario(sc)
        r = self.runner()
        # Only codes seeded from live QA are echoed (``run_not_found``); every other
        # code, well-formed or not, collapses to ``unclassified`` so the vocabulary stays closed.
        for command, flags, detail in ((("orchestration", "gate-list"), {"run": "run_aaa"}, "run_not_found"),
                                       (("project", "list"), {}, "unclassified"),
                                       (("host", "list"), {}, "unclassified")):
            with self.assertRaises(errors.AdapterError) as cm:
                r.run(command, **flags)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_ERROR, detail))
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
            self.assertNotIn("a" * 40, repr(cm.exception.to_json()))
            self.assertNotIn("unknown_command", repr(cm.exception.to_json()))

    def test_malformed_shapes_fail_closed(self):
        for entry in (
            {"mode": "garbage", "text": "not json CANARY_PATH"},
            {"response": [1, 2]}, {"response": {"ok": "yes", "result": {}}},
            {"response": {"ok": True, "result": "CANARY_PATH"}}, {"response": {"ok": True}},
            {"response": {"ok": True, "result": {}}},  # no _meta.runtimeId
            {"response": {"_meta": {"runtimeId": ""}, "ok": True, "result": {}}},
            {"response": {"_meta": {"runtimeId": 7}, "ok": True, "result": {}}},
        ):
            sc = S.build_scenario()
            sc["project list"] = entry
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().run(("project", "list"))
            self.assertEqual(cm.exception.code, errors.MALFORMED_RESULT, entry)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))

    def test_duplicate_keys_and_non_json_constants_are_malformed_not_resolved(self):
        # The qualified runtime never emits these; a peer that does is malformed rather than read
        # by last-key-wins (the duplicate here would otherwise pass the runtime check with "rt_2").
        for text in (
            '{"_meta":{"runtimeId":"%s","runtimeId":"rt_2"},"ok":true,"result":{"projects":[]}}' % S.RUNTIME_ID,
            '{"_meta":{"runtimeId":"%s"},"ok":true,"ok":false,"result":{"projects":[]}}' % S.RUNTIME_ID,
            '{"_meta":{"runtimeId":"%s"},"ok":true,"result":{"projects":[],"n":NaN}}' % S.RUNTIME_ID,
            '{"_meta":{"runtimeId":"%s"},"ok":true,"result":{"projects":[],"n":Infinity}}' % S.RUNTIME_ID,
            '{"_meta":{"runtimeId":"%s"},"ok":true,"result":{"projects":[],"n":-Infinity}}' % S.RUNTIME_ID,
        ):
            sc = S.build_scenario()
            sc["project list"] = {"mode": "garbage", "text": text}
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().run(("project", "list"))
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.MALFORMED_RESULT, "not_json"), text)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))

    def test_interrupted_reader_kills_and_reaps_the_child(self):
        # An exception raised inside the read loop after the spawn (a signal, an unexpected OSError)
        # propagates unchanged, but not before the child's process group is killed and reaped.
        sc = S.build_scenario()
        sc["project list"] = {"mode": "fork_hang"}
        self.h.write_scenario(sc)
        for exc in (OSError(5, "injected"), KeyboardInterrupt()):
            if os.path.exists(self.h.pidfile):
                os.unlink(self.h.pidfile)

            def interrupt(_self, timeout=None, _exc=exc):
                deadline = time.monotonic() + 10
                while len(self.h.child_pids()) < 2 and time.monotonic() < deadline:
                    time.sleep(0.05)  # let the child and its helper start before the reader is interrupted
                raise _exc

            with self.subTest(exc=type(exc).__name__), mock.patch.object(selectors.DefaultSelector, "select", interrupt), \
                    self.assertRaises(type(exc)):
                self.runner(timeout=20.0, gate=()).run(("project", "list"))  # no gate: the interrupted reader is the command's
            self.assertEqual(len(self.h.child_pids()), 2, type(exc).__name__)
            self._assert_children_gone()

    def test_runtime_id_is_pinned_per_family_for_the_whole_operation(self):
        sc = S.build_scenario()
        sc["terminal list"] = {"response": S.envelope({"terminals": []}, "rt_other")}
        sc["host list"] = {"sequence": [{"response": S.host_envelope({"hosts": []})},
                                        {"response": S.host_envelope({"hosts": []}, "host_other")}]}
        self.h.write_scenario(sc)
        r = self.runner()
        r.qualify_runtime()
        self.assertEqual((r.runtime_id, r.host_id), ("rt_CANARY_PATH", None))
        r.run(("project", "list"))  # same runtime: fine
        r.run(("host", "list"))  # host family: its _meta is the local host id, never compared with the runtime
        self.assertEqual((r.runtime_id, r.host_id), ("rt_CANARY_PATH", "host_local"))
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("terminal", "list"), limit=5)
        self.assertEqual((cm.exception.code, cm.exception.stage), (errors.RUNTIME_CHANGED, "terminal list"))
        self.assertEqual(cm.exception.exit_code, errors.EXIT_FAILURE)
        S.assert_no_canaries(self, repr(cm.exception.to_json()))
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("host", "list"))  # drift inside the host family is refused as well
        self.assertEqual((cm.exception.code, cm.exception.stage), (errors.RUNTIME_CHANGED, "host list"))
        self.assertEqual(r.runtime_id, "rt_CANARY_PATH")  # a runtime-family id never absorbs a host id
        S.assert_no_canaries(self, repr(cm.exception.to_json()))

    def test_status_self_identity_must_agree_with_its_envelope(self):
        for runtime_id in (None, "", 7, "rt_other"):
            sc = S.build_scenario()
            result = S.status_result()
            result["runtime"]["runtimeId"] = runtime_id
            sc["status"] = {"response": S.envelope(result)}
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().qualify_runtime()
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.MALFORMED_RESULT, "status", "runtimeId"), runtime_id)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        for field, value in (("runtime", "CANARY_PATH"), ("target", [])):  # the self-description must be objects
            sc = S.build_scenario()
            result = S.status_result()
            result[field] = value
            sc["status"] = {"response": S.envelope(result)}
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().qualify_runtime()
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.MALFORMED_RESULT, "status", "runtime"), field)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        self.assertEqual(len(self.h.orca_calls()), 6)  # each attempt stopped after its status read

    def test_runtime_ids_must_be_wellformed_text(self):
        # An unpaired surrogate (a JSON escape the peer can emit) is refused as a malformed
        # runtime id wherever it is read, before the id can become HMAC input (the snapshot
        # API case is in test_snapshot); never a foreign exception such as UnicodeEncodeError.
        lone = "\ud800"
        sc = S.build_scenario()
        result = S.status_result()
        result["runtime"]["runtimeId"] = lone  # a consistent self-description, both reads agree
        sc["status"] = {"response": S.envelope(result, lone)}
        self.h.write_scenario(sc)
        try:
            self.runner().qualify_runtime()
        except errors.AdapterError as exc:
            self.assertEqual((exc.code, exc.stage, exc.detail), (errors.MALFORMED_RESULT, "status", "runtimeId"))
            S.assert_no_canaries(self, repr(exc.to_json()))
        else:
            self.fail("a lone surrogate runtime id was accepted")
        self.assertEqual(len(self.h.orca_calls()), 1)  # stopped after the opening status read
        sc = S.build_scenario()
        sc["project list"] = {"response": dict(sc["project list"]["response"], _meta={"runtimeId": lone})}
        self.h.write_scenario(sc)
        r = self.runner()
        r.qualify_runtime()
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail), (errors.MALFORMED_RESULT, "project list", "runtimeId"))
        self.assertEqual(r.runtime_id, "rt_CANARY_PATH")  # the pinned id is untouched by the refused read

    def test_closing_status_detects_a_restart_after_the_reads(self):
        restarted = S.status_result()
        restarted["runtime"]["runtimeId"] = "rt_restarted"
        for closing in (
            {"response": S.envelope(restarted, "rt_restarted")},  # new runtime answers consistently
            {"response": S.envelope(restarted)},  # transport id kept, self-reported id changed
            {"response": S.envelope(S.status_result(), "rt_restarted")},  # the reverse
        ):
            sc = S.build_scenario()
            sc["status"] = {"sequence": [{"response": S.envelope(S.status_result())}, closing]}
            self.h.write_scenario(sc)
            # The peer picks the nth ``sequence`` entry by counting its earlier calls in
            # FAKE_ORCA_LOG, so each iteration starts from an empty log.
            if os.path.exists(self.h.orca_log):
                os.remove(self.h.orca_log)
            r = self.runner()
            r.qualify_runtime()
            r.run(("project", "list"))
            with self.assertRaises(errors.AdapterError) as cm:
                r.close_runtime()
            self.assertEqual((cm.exception.code, cm.exception.stage), (errors.RUNTIME_CHANGED, "status"), closing)
            self.assertEqual([c[0] for c in self.h.orca_calls()], ["status", "agent-context", "project", "status"])
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        r = self.runner()
        with self.assertRaises(errors.AdapterError) as cm:
            r.close_runtime()  # nothing pinned: nothing to close, no spawn
        self.assertEqual(cm.exception.code, errors.RUNTIME_CHANGED)
        self.assertEqual(r.command_count, 0)
        os.remove(self.h.orca_log)
        self.h.write_scenario(S.build_scenario())  # the same runtime answers the closing read: a clean bracket
        r = self.runner()
        r.qualify_runtime()
        r.run(("project", "list"))
        self.assertIsNone(r.close_runtime())
        self.assertEqual([c[0] for c in self.h.orca_calls()], ["status", "agent-context", "project", "status"])
        self.assertEqual((r.runtime_id, r.command_count), ("rt_CANARY_PATH", 4))

    def test_timeout_kills_and_reaps_child(self):
        sc = S.build_scenario()
        sc["project list"] = {"mode": "hang"}
        self.h.write_scenario(sc)
        start = time.monotonic()
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner(timeout=1.0).run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_TIMEOUT, ""))  # reap confirmed: no detail
        self.assertLess(time.monotonic() - start, 10)
        self._assert_children_gone()

    def test_timeout_kills_descendants_that_inherited_the_pipes(self):
        # A CLI (or gate) that spawns a helper: after the timeout neither the child nor
        # the helper it forked may survive; the helper's pid is recorded by the peer.
        sc = S.build_scenario()
        sc["project list"] = {"mode": "fork_hang"}
        self.h.write_scenario(sc)
        start = time.monotonic()
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner(timeout=1.0).run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_TIMEOUT, ""))
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(len(self.h.child_pids()), 2)  # the child and its helper were both recorded
        self._assert_children_gone()

    def test_oversized_stdout_and_stderr_are_refused_while_reading(self):
        for stream in ("stdout", "stderr"):
            sc = S.build_scenario()
            sc["project list"] = {"mode": "flood", "stream": stream, "bytes": 8 * 1024 * 1024}
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner(max_bytes=256 * 1024).run(("project", "list"))
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_OUTPUT_TOO_LARGE, ""), stream)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        self._assert_children_gone()
        # The cap is inclusive: exactly ``max_bytes`` is read back, one byte more is refused.
        argv = [sys.executable, "-I", "-B", "-S", "-c", "import sys; sys.stdout.write('x' * 491)"]
        self.assertEqual(execute_bounded(argv, "probe", 5, 491), (0, b"x" * 491))
        with self.assertRaises(errors.AdapterError) as cm:
            execute_bounded(argv, "probe", 5, 490)
        self.assertEqual((cm.exception.code, cm.exception.stage), (errors.ORCA_OUTPUT_TOO_LARGE, "probe"))

    def _assert_children_gone(self):
        pids = self.h.child_pids()
        self.assertTrue(pids)
        deadline = time.monotonic() + 5
        for pid in pids:
            while True:  # a killed descendant is reaped by init shortly after the group kill
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), deadline, "pid %d still alive" % pid)
                time.sleep(0.05)

    def test_spawn_failure_is_bounded(self):
        with self.assertRaises(errors.AdapterError) as cm:
            execute_bounded([os.path.join(self.h.dir, "no-such-exe")], "x", 5, 1024)
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_EXEC_FAILED, "spawn_failed"))

    def test_command_budget_refuses_the_next_spawn(self):
        r = OrcaRunner(self.h.config(), max_commands=3)
        for _ in range(3):
            r.run(("project", "list"))
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.BUDGET_EXCEEDED, "commands"))
        self.assertEqual(cm.exception.exit_code, errors.EXIT_FAILURE)
        self.assertEqual(len(self.h.orca_calls()), 3)
        self.assertEqual(len(self.h.preflight_calls()), 3)  # the refused command never reached the gate

    def test_operation_deadline_clips_child_timeout_and_then_refuses(self):
        sc = S.build_scenario()
        sc["project list"] = {"mode": "hang"}
        self.h.write_scenario(sc)
        r = OrcaRunner(self.h.config(timeout=20.0), deadline_seconds=1.5)
        start = time.monotonic()
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_TIMEOUT, ""))  # 20 s bound clipped to the 1.5 s left
        self.assertLess(time.monotonic() - start, 10)
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("host", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.BUDGET_EXCEEDED, "deadline"))
        self.assertEqual(len(self.h.orca_calls()), 1)
        self._assert_children_gone()

    def test_runtime_pins(self):
        self.assertEqual(self.runner().qualify_runtime(), {
            "app_version": "1.4.205", "schema_version": 1, "target_kind": "local", "state": "ready"})
        for sc in (S.build_scenario(app_version="1.4.206"), S.build_scenario(schema_version=2),
                   S.build_scenario(schema_version=True), S.build_scenario(schema_version="1")):
            self.h.write_scenario(sc)
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner().qualify_runtime()
            self.assertEqual(cm.exception.code, errors.UNSUPPORTED_RUNTIME)
            self.assertEqual(cm.exception.exit_code, errors.EXIT_UNSUPPORTED)
        sc = S.build_scenario()
        sc["status"] = {"response": S.envelope(S.status_result(kind="ssh"))}
        self.h.write_scenario(sc)
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().qualify_runtime()
        self.assertEqual(cm.exception.code, errors.UNSUPPORTED_RUNTIME)
        sc["status"] = {"response": S.envelope(S.status_result(state="starting"))}
        self.h.write_scenario(sc)
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().qualify_runtime()
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_EXEC_FAILED, "not_ready"))
        sc = S.build_scenario()
        sc["agent-context"] = {"response": ["CANARY_PATH"]}  # the bare registry document must still be an object
        self.h.write_scenario(sc)
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().qualify_runtime()
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                         (errors.MALFORMED_RESULT, "agent-context", "envelope"))
        S.assert_no_canaries(self, repr(cm.exception.to_json()))

    def test_metacharacters_are_refused_by_shape_not_interpreted(self):
        for run_id in ("run_a$(id)", "run_a;id", "run_a x", "run_a`id`", "$HOME", "run_ok\n", "run_ok\r\n", "\nrun_ok",
                       "run_", "run_" + "a" * 65):  # the id body is 1..64 characters
            with self.assertRaises(errors.AdapterError, msg=repr(run_id)) as cm:
                self.runner().run(("orchestration", "task-list"), run=run_id, brief=True)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.COMMAND_NOT_ALLOWED, "run"))
        for cursor in ("c1\n", "c1\r", "c1\x00", "c" * (MAX_CURSOR_CHARS + 1)):  # whole-string match: no control suffix reaches argv
            with self.assertRaises(errors.AdapterError, msg=repr(cursor)) as cm:
                self.runner().run(("orchestration", "run-list"), limit=100, cursor=cursor)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.COMMAND_NOT_ALLOWED, "cursor"))
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().run(("worktree", "list"), limit=True)  # a bool is not an int here
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.COMMAND_NOT_ALLOWED, "limit"))
        self.assertEqual(self.h.orca_calls(), [])
        # The boundary values themselves pass the shape checks and reach argv unchanged.
        r = self.runner(gate=())
        r.run(("orchestration", "task-list"), run="run_" + "a" * 64, brief=True)
        r.run(("orchestration", "run-list"), limit=1, cursor="c" * MAX_CURSOR_CHARS)
        r.run(("worktree", "list"), limit=MAX_LIMIT)
        self.assertEqual(self.h.orca_calls(), [
            ["orchestration", "task-list", "--run", "run_" + "a" * 64, "--brief", "--json"],
            ["orchestration", "run-list", "--limit", "1", "--cursor", "c" * MAX_CURSOR_CHARS, "--json"],
            ["worktree", "list", "--limit", str(MAX_LIMIT), "--json"]])


class CarriedTriageRegressions(_RunnerCase):
    """A refused command never becomes a stage, and constructor budgets may only tighten."""

    def test_rejected_command_shapes_are_bounded_before_any_spawn(self):
        for command in (
            ("TRIAGE_CANARY_" + "x" * 4986,), ("CANARY_PATH" * 20000,), (7,), "status",
            ["project", "list"], (), (("status",),), None, ("repo", "list"), ("status", 1),
        ):
            r = self.runner()
            with self.subTest(command=repr(command)[:40]), self.assertRaises(errors.AdapterError) as cm:
                r.run(command)
            exc = cm.exception
            self.assertEqual((exc.code, exc.stage, exc.detail), (errors.COMMAND_NOT_ALLOWED, "command", ""))
            S.assert_no_canaries(self, repr(exc.to_json()) + str(exc))
            self.assertEqual((r.command_count, r.gate_count), (0, 0))
        self.assertEqual(self.h.orca_calls(), [])
        self.assertEqual(self.h.preflight_calls(), [])
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().run(("worktree", "list"), limit=0)
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                         (errors.COMMAND_NOT_ALLOWED, "worktree list", "limit"))

    def test_budget_overrides_are_finite_positive_and_capped(self):
        cfg = Config(executable=(sys.executable, S.FAKE_ORCA), key_file=self.h.key_file)
        for kw in (
            {"deadline_seconds": float("inf")}, {"deadline_seconds": float("nan")}, {"deadline_seconds": True},
            {"deadline_seconds": "5"}, {"deadline_seconds": 0}, {"deadline_seconds": -1},
            {"deadline_seconds": OPERATION_DEADLINE_SECONDS + 0.001}, {"deadline_seconds": 1e9},
            {"max_commands": float("inf")}, {"max_commands": True}, {"max_commands": 3.0}, {"max_commands": "3"},
            {"max_commands": 0}, {"max_commands": -1}, {"max_commands": MAX_COMMANDS_PER_OPERATION + 1},
            {"max_commands": 10 ** 9},
        ):
            with self.subTest(kw=kw), self.assertRaises(errors.AdapterError) as cm:
                OrcaRunner(cfg, **kw)
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.CONFIG_ERROR, "config", "bad_bounds"))
        self.assertEqual(self.h.orca_calls(), [])  # every refusal above happened before any spawn
        OrcaRunner(cfg, deadline_seconds=OPERATION_DEADLINE_SECONDS, max_commands=MAX_COMMANDS_PER_OPERATION)
        OrcaRunner(cfg, deadline_seconds=1)
        r = OrcaRunner(cfg, deadline_seconds=1.5, max_commands=3)  # a tightened cap is the one enforced
        for _ in range(3):
            r.run(("project", "list"))
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.BUDGET_EXCEEDED, "commands"))
        self.assertEqual(len(self.h.orca_calls()), 3)


class FinalReviewRegressions(_RunnerCase):
    """Strict numeric parsing, and wall-clock bounds that cover the spawn and the reap."""

    # json.loads(...) parses the grammatical (not NaN/Infinity) token ``1e9999``
    # to a non-finite float without ever calling parse_constant.
    def test_grammatical_numeric_overflow_is_rejected_not_json(self):
        with self.assertRaises(errors.AdapterError) as cm:
            _parse_json_object(b'{"ok":true,"result":{"x":1e9999}}', "probe")
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.MALFORMED_RESULT, "not_json"))
        # negative overflow and a deeply nested placement are refused the same way
        with self.assertRaises(errors.AdapterError):
            _parse_json_object(b'{"ok":true,"result":{"x":-1e9999}}', "probe")
        with self.assertRaises(errors.AdapterError):
            _parse_json_object(b'{"ok":true,"result":{"a":{"b":[1,1e9999]}}}', "probe")
        # an ordinary large-but-finite float still round-trips
        doc = _parse_json_object(b'{"ok":true,"result":{"x":1e300}}', "probe")
        self.assertEqual(doc["result"]["x"], 1e300)

    def test_grammatical_numeric_overflow_is_rejected_end_to_end(self):
        sc = S.build_scenario()
        sc["project list"] = {
            "mode": "garbage",
            "text": '{"_meta":{"runtimeId":"rt_CANARY_PATH"},"ok":true,"result":{"x":1e9999}}',
        }
        self.h.write_scenario(sc)
        with self.assertRaises(errors.AdapterError) as cm:
            self.runner().run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.MALFORMED_RESULT, "not_json"))

    # A Popen call that alone consumes the whole requested timeout must not be
    # allowed to then succeed on a fresh, restarted deadline. The child here
    # exits immediately on its own (``-c pass``): a deadline computed *after*
    # Popen returned would give the read loop its own full ``timeout`` window,
    # and the quick-exiting child would return rc=0 having actually run ~5x
    # longer than the caller's requested bound.
    def test_delayed_popen_is_not_silently_allowed_to_succeed_past_the_deadline(self):
        original_popen = subprocess.Popen

        def delayed_popen(*args, **kwargs):
            time.sleep(0.25)
            return original_popen(*args, **kwargs)

        start = time.monotonic()
        with mock.patch("orca_adapter.runner.subprocess.Popen", side_effect=delayed_popen):
            with self.assertRaises(errors.AdapterError) as cm:
                execute_bounded([sys.executable, "-I", "-B", "-S", "-c", "pass"], "probe", 0.05, 1024)
        elapsed = time.monotonic() - start
        self.assertEqual(cm.exception.code, errors.ORCA_TIMEOUT)
        self.assertLess(elapsed, 1.0)

    # If the post-SIGKILL reap cannot be confirmed within the bounded wait, the
    # failure raised is still the original bounded code (precedence is
    # preserved) but with an explicit "not_reaped" detail rather than a
    # silent, unconditional reap claim.
    def test_reap_confirmation_failure_is_explicit_and_preserves_precedence(self):
        sc = S.build_scenario()
        sc["project list"] = {"mode": "hang"}
        self.h.write_scenario(sc)
        spawned = []
        original_popen = subprocess.Popen

        def capture_popen(*args, **kwargs):
            spawned.append(original_popen(*args, **kwargs))
            return spawned[-1]

        # Reap whatever ``capture_popen`` captured no matter how the rest of this
        # method exits: an assertion below, ``assertRaises`` itself getting the
        # wrong exception (or none), or any other failure in the tested operation.
        # ``addCleanup`` runs during teardown regardless of the test outcome, unlike
        # a ``finally`` placed only around the later assertions.
        self.addCleanup(lambda: [p.wait(timeout=5) for p in spawned])

        # The ``wait`` patch (started first, on the real class) blocks every reap confirmation
        # for the whole run; the ``Popen`` patch only captures the instance the runner creates.
        with mock.patch("subprocess.Popen.wait", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5)), \
                mock.patch("subprocess.Popen", side_effect=capture_popen):
            with self.assertRaises(errors.AdapterError) as cm:
                self.runner(timeout=0.2, gate=()).run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_TIMEOUT, "not_reaped"))
        # The mock only blocked confirmation; the real SIGKILL still landed. Reap the child through
        # its own object (the patch is off again) so no unreaped Popen is left for the collector.
        self.assertEqual([p.pid for p in spawned], self.h.child_pids())
        self.assertEqual(spawned[0].wait(timeout=5), -9)


class ErrorEnvelopeOrdering(_RunnerCase):
    """An error envelope is classified before, and never attests, runtime identity."""

    # An exit-0 error envelope without ``_meta.runtimeId`` must surface its Orca
    # error code, not ``malformed_result/runtimeId`` from the identity read.
    def test_exit_zero_error_envelope_keeps_its_orca_code_regression(self):
        sc = S.build_scenario()
        sc["orchestration gate-list"] = {"response": {"ok": False, "error": {"code": "run_not_found", "message": "CANARY_PATH"}}, "exit": 0}
        sc["project list"] = {"response": {"ok": False, "error": {"code": "a" * 40}}, "exit": 0}
        sc["host list"] = {"response": {"ok": False, "error": {"code": "unknown_command", "message": "x"}}, "exit": 0}
        self.h.write_scenario(sc)
        r = self.runner()
        for command, flags, detail in ((("orchestration", "gate-list"), {"run": "run_aaa"}, "run_not_found"),
                                       (("project", "list"), {}, "unclassified"),
                                       (("host", "list"), {}, "unclassified")):
            with self.subTest(command=command), self.assertRaises(errors.AdapterError) as cm:
                r.run(command, **flags)
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.ORCA_ERROR, " ".join(command), detail))
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        self.assertEqual((r.runtime_id, r.host_id), (None, None))

    # An exit-0 error envelope that does carry ``_meta.runtimeId`` must not seed
    # the pin on a fresh runner, and must not be compared against an existing
    # pin either: it is classified as ``orca_error`` and nothing else.
    def test_exit_zero_error_envelope_never_pins_or_compares_identity_regression(self):
        sc = S.build_scenario()
        sc["project list"] = {"response": {"_meta": {"runtimeId": "rt_from_error"}, "ok": False,
                                           "error": {"code": "run_not_found"}}, "exit": 0}
        sc["host list"] = {"response": {"_meta": {"runtimeId": "host_from_error"}, "ok": False,
                                        "error": {"code": "run_not_found"}}, "exit": 0}
        self.h.write_scenario(sc)
        fresh = self.runner()
        with self.assertRaises(errors.AdapterError) as cm:
            fresh.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_ERROR, "run_not_found"))
        self.assertIsNone(fresh.runtime_id)  # nothing was pinned from the error envelope
        with self.assertRaises(errors.AdapterError) as cm:
            fresh.run(("host", "list"))
        self.assertEqual(cm.exception.code, errors.ORCA_ERROR)
        self.assertIsNone(fresh.host_id)
        pinned = self.runner()
        pinned.qualify_runtime()
        self.assertEqual(pinned.runtime_id, S.RUNTIME_ID)
        with self.assertRaises(errors.AdapterError) as cm:
            pinned.run(("project", "list"))  # a differing id on an error envelope is not drift here
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_ERROR, "run_not_found"))
        self.assertEqual(pinned.runtime_id, S.RUNTIME_ID)
        # both runners answered the same way on 3 orca calls each (+ qualify's status/agent-context)
        self.assertEqual(len(self.h.orca_calls()), 5)

    # An ``ok=true`` envelope whose ``result`` is not an object is malformed and
    # must not pin the runtime id it happens to carry: nothing is attested
    # until the envelope as a whole has been accepted.
    def test_malformed_result_envelope_does_not_pin_identity_regression(self):
        sc = S.build_scenario()
        sc["project list"] = {"response": {"_meta": {"runtimeId": S.RUNTIME_ID}, "ok": True, "result": "CANARY_RESULT"}}
        self.h.write_scenario(sc)
        r = self.runner()
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                         (errors.MALFORMED_RESULT, "project list", "result"))
        self.assertIsNone(r.runtime_id)
        S.assert_no_canaries(self, repr(cm.exception.to_json()))

    # Successful envelopes keep every identity rule: a missing/ill-formed id is
    # still malformed, the first good id pins, and a later different id drifts.
    # This deliberately mirrors rows of ``test_malformed_shapes_fail_closed`` and
    # ``test_runtime_id_is_pinned_per_family_for_the_whole_operation`` with the
    # full (code, stage, detail) triple: it is the control that must keep passing
    # on the runner bytes that the three regressions above fail on.
    def test_successful_envelope_identity_rules_are_unchanged_control(self):
        for entry, detail in (
            ({"response": {"ok": True, "result": {"projects": []}}}, "runtimeId"),
            ({"response": {"_meta": {"runtimeId": ""}, "ok": True, "result": {"projects": []}}}, "runtimeId"),
            ({"response": {"_meta": {"runtimeId": 7}, "ok": True, "result": {"projects": []}}}, "runtimeId"),
        ):
            sc = S.build_scenario()
            sc["project list"] = entry
            self.h.write_scenario(sc)
            r = self.runner()
            with self.subTest(entry=repr(entry)[:60]), self.assertRaises(errors.AdapterError) as cm:
                r.run(("project", "list"))
            self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail),
                             (errors.MALFORMED_RESULT, "project list", detail))
            self.assertIsNone(r.runtime_id)
            S.assert_no_canaries(self, repr(cm.exception.to_json()))
        sc = S.build_scenario()
        sc["terminal list"] = {"response": S.envelope({"terminals": []}, "rt_other")}
        self.h.write_scenario(sc)
        r = self.runner()
        r.run(("project", "list"))
        self.assertEqual(r.runtime_id, S.RUNTIME_ID)
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("terminal", "list"), limit=5)
        self.assertEqual((cm.exception.code, cm.exception.stage), (errors.RUNTIME_CHANGED, "terminal list"))

    # The nonzero-exit error path is untouched: an exit-1 ``ok=false`` envelope
    # is still ``orca_error`` and still not identity-attested.
    def test_nonzero_exit_error_envelope_path_is_unchanged_control(self):
        sc = S.build_scenario()
        sc["project list"] = {"response": {"_meta": {"runtimeId": "rt_from_error"}, "ok": False,
                                           "error": {"code": "run_not_found"}}, "exit": 1}
        self.h.write_scenario(sc)
        r = self.runner()
        with self.assertRaises(errors.AdapterError) as cm:
            r.run(("project", "list"))
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.ORCA_ERROR, "run_not_found"))
        self.assertIsNone(r.runtime_id)


if __name__ == "__main__":
    unittest.main()
