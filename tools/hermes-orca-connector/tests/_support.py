"""Shared test harness: synthetic peers, key files and scenario builders.

Nothing here touches a real Orca runtime or a real secret guard. Canary
strings are placed in every field the adapter must never emit; tests assert
they are absent from all output and exceptions.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from orca_adapter.config import Config  # noqa: E402

FAKE_ORCA = os.path.join(HERE, "fake_orca.py")
FAKE_PREFLIGHT = os.path.join(HERE, "fake_preflight.py")
ADAPTER_CLI = os.path.join(ROOT, "adapter.py")

CANARIES = (
    "CANARY_URL_PASS", "CANARY_PATH", "CANARY_OBJECTIVE", "CANARY_PROMPT", "CANARY_TITLE",
    "CANARY_SPEC", "CANARY_RESULT", "CANARY_ARGV", "CANARY_FLOOD_TOKEN", "CANARY_ATTENTION",
    "github:acme/widgets", "repo_1", "wt_main", "term_pm", "run_aaa", "host_local", "task_1", "dsp_1",
)


RUNTIME_ID = "rt_CANARY_PATH"  # runtime family: status/project/worktree/terminal/orchestration envelopes
LOCAL_HOST_ID = "host_local"  # host family: the ``host list`` envelope carries the local host id


def envelope(result: dict, runtime_id: str = RUNTIME_ID) -> dict:
    return {"_meta": {"runtimeId": runtime_id}, "id": "req_1", "ok": True, "result": result}


def host_envelope(result: dict, host_id: str = LOCAL_HOST_ID) -> dict:
    """``host list`` is the one command whose ``_meta.runtimeId`` is the local host id (observed)."""
    return envelope(result, host_id)


def worktree_id(name: str) -> str:
    """Observed worktree id shape: an opaque part, ``::`` and an absolute path. Never emitted."""
    return "wt_%s::/home/CANARY_PATH/%s" % (name, name)


def assert_no_canaries(testcase, text: str) -> None:
    for canary in CANARIES:
        testcase.assertNotIn(canary, text)


def terminal(handle: str, worktree: str, connected: bool = True, host_id: str = LOCAL_HOST_ID) -> dict:
    """``worktree`` is the short fixture name; the row carries the full observed id under ``worktreeId``."""
    return {
        "handle": handle, "worktreeId": worktree_id(worktree), "connected": connected, "agentIdentity": "claude",
        "branch": "main", "executionHostId": host_id, "title": "CANARY_TITLE",
        "preview": "CANARY_PROMPT", "worktreePath": "/home/CANARY_PATH/w", "writable": True,
    }


def run(run_id: str, coordinator: str | None, legacy: int = 0) -> dict:
    return {
        "id": run_id, "coordinator_handle": coordinator, "objective": "CANARY_OBJECTIVE",
        "created_at": "2026-09-17T00:00:00Z", "updated_at": "2026-09-17T00:00:00Z",
        "consumer_generation": 1, "legacy": legacy,
    }


def worktree(name: str, project_id: str | None = "github:acme/widgets", host_id: str = LOCAL_HOST_ID,
             archived: bool = False) -> dict:
    """Observed row shape: the identity key is ``id`` (there is no ``worktreeId`` on a worktree row)."""
    return {
        "id": worktree_id(name), "projectId": project_id, "repoId": "repo_1", "hostId": host_id,
        "identity": {"executionHostId": host_id, "instanceId": "inst_1", "key": "CANARY_PATH"},
        "isMainWorktree": name == "wt_main", "isArchived": archived, "isBare": False, "isPinned": False,
        "path": "/home/CANARY_PATH/w", "branch": "feature/x", "displayName": "w", "workspaceStatus": "working",
        "childWorktreeIds": [], "parentWorktreeId": None, "linkedPR": None,
    }


def host(host_id: str, kind: str = "local") -> dict:
    return {"id": host_id, "kind": kind, "name": "CANARY_PATH", "platform": "linux", "selector": kind}


def status_result(app_version: str = "1.4.205", state: str = "ready", kind: str = "local") -> dict:
    return {
        "app": {"running": True, "pid": 1, "desktopWindowStatus": "x"},
        "graph": {"state": state},
        "runtime": {"appVersion": app_version, "state": state, "capabilities": ["orchestration.contract.v1"],
                    "runtimeId": "rt_CANARY_PATH", "reachable": True, "connectionState": "connected"},
        "target": {"kind": kind},
    }


def build_scenario(
    *,
    terminals: list[dict] | None = None,
    runs: list[dict] | None = None,
    worktrees: list[dict] | None = None,
    projects: list[dict] | None = None,
    truncated: bool = False,
    omitted_hosts: tuple[str, ...] = (),
    host_ids: tuple[str, ...] = ("host_local",),
    hosts: list[dict] | None = None,
    schema_version: int = 1,
    app_version: str = "1.4.205",
    project_display: str = "acme/widgets",
) -> dict:
    """A live-shaped baseline: one project with joined worktrees/terminals/runs."""
    if terminals is None:
        terminals = [terminal("term_pm", "wt_main"), terminal("term_x", "wt_a")]
    if runs is None:  # the live registry retains a legacy row whose id carries an underscore
        runs = [run("run_aaa", "term_pm"), run("run_bbb", None), run("run_old_x", None, legacy=1)]
    if worktrees is None:
        worktrees = [worktree("wt_main"), worktree("wt_a")]
    if projects is None:
        projects = [{
            "id": "github:acme/widgets", "displayName": project_display, "kind": "git",
            "sourceRepoIds": ["repo_1"], "badgeColor": "x", "createdAt": 1, "updatedAt": 1,
            "gitRemoteIdentity": {"canonicalKey": "github:acme/widgets", "remoteName": "origin",
                                  "remoteUrl": "https://user:CANARY_URL_PASS@github.com/acme/widgets.git"},
            "providerIdentity": {"owner": "acme", "provider": "github", "repo": "widgets"},
        }]
    host_scope = {"hostIds": list(host_ids), "omittedHostIds": list(omitted_hosts)}
    if hosts is None:
        hosts = [host("host_local")]
    return {
        "status": {"response": envelope(status_result(app_version=app_version))},
        "agent-context": {"response": {"schemaVersion": schema_version, "commandCount": 1, "commands": []}},
        "host list": {"response": host_envelope({"hosts": hosts})},
        "project list": {"response": envelope({"projects": projects})},
        "worktree list": {"response": envelope({"worktrees": worktrees, "hostScope": host_scope,
                                                "totalCount": len(worktrees), "truncated": truncated})},
        "terminal list": {"response": envelope({"terminals": terminals, "hostScope": host_scope,
                                                "totalCount": len(terminals), "truncated": truncated,
                                                "topologyRevisions": {"/home/CANARY_PATH/w": 3}})},
        "orchestration run-list": {"response": envelope({"runs": runs, "nextCursor": None})},
        "orchestration task-list": {"response": envelope({"runId": "run_aaa", "count": 0, "tasks": [],
                                                          "legacyReadOnly": False})},
        "orchestration worker-list": {"response": envelope({
            "workers": [], "counts": {}, "scope": {"run": "run_aaa", "source": "flag"},
            "page": {"hasMore": False, "nextCursor": None, "limit": 100, "total": 0}})},
        "orchestration gate-list": {"response": envelope({"runId": "run_aaa", "count": 0, "gates": []})},
    }


def two_project_scenario() -> dict:
    """Two registered projects with one worktree, terminal and coordinator Run each."""
    base = build_scenario()["project list"]["response"]["result"]["projects"][0]
    other = {"id": "github:other/x", "displayName": "other/x", "kind": "git", "sourceRepoIds": ["repo_2"]}
    return build_scenario(
        projects=[base, other],
        worktrees=[worktree("wt_main"), worktree("wt_o", "github:other/x")],
        terminals=[terminal("term_pm", "wt_main"), terminal("term_o", "wt_o", connected=False)],
        runs=[run("run_aaa", "term_pm"), run("run_ooo", "term_o")])


class Harness:
    """Temp dir with a valid key file, scenario file, logs and env for the fakes."""

    def __init__(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="hermes-orca-test-")
        self.key_file = os.path.join(self.dir, "key")
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b"\x11" * 32)
        os.close(fd)
        self.repo = os.path.join(self.dir, "repo")
        os.mkdir(self.repo)
        # A synthetic "main checkout" holding the fake guard at the canonical guard location,
        # the only shape the controller wrapper accepts (a bare fake_preflight.py path is refused).
        self.checkout = os.path.join(self.dir, "checkout")
        self.guard = os.path.join(self.checkout, ".claude", "scripts", "pre_pr_secret_check.py")
        os.makedirs(os.path.dirname(self.guard))
        shutil.copyfile(FAKE_PREFLIGHT, self.guard)
        os.chmod(self.guard, 0o700)
        self.scenario_file = os.path.join(self.dir, "scenario.json")
        self.orca_log = os.path.join(self.dir, "orca.log")
        self.preflight_log = os.path.join(self.dir, "preflight.log")
        self.pidfile = os.path.join(self.dir, "pids")
        self.preflight_exit = 0

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_scenario(self, scenario: dict) -> None:
        with open(self.scenario_file, "w") as fh:
            json.dump(scenario, fh)

    def config(self, timeout: float = 20.0, max_bytes: int = 4 * 1024 * 1024,
               gate: tuple[str, ...] | None = None) -> Config:
        """Config against the fakes; the synthetic gate is on unless ``gate=()``."""
        return Config(
            executable=(sys.executable, FAKE_ORCA), key_file=self.key_file,
            preflight_argv=(sys.executable, FAKE_PREFLIGHT) if gate is None else gate,
            timeout_seconds=timeout, max_output_bytes=max_bytes,
        )

    def env(self) -> dict:
        """CLI environment: synthetic executable and gate, plus the controller
        variables the controller wrapper test needs (adapter.py ignores them);
        the wrapper's guard is the fake guard at its canonical path."""
        env = dict(os.environ)
        env.update({
            "FAKE_ORCA_SCENARIO": self.scenario_file, "FAKE_ORCA_LOG": self.orca_log,
            "FAKE_ORCA_PIDFILE": self.pidfile, "FAKE_PREFLIGHT_LOG": self.preflight_log,
            "FAKE_PREFLIGHT_EXIT": str(self.preflight_exit),
            "HERMES_ORCA_KEY_FILE": self.key_file, "HERMES_ORCA_COMMAND_GATE": FAKE_PREFLIGHT,
            "HERMES_ORCA_PROXOPS_REPO": self.repo, "HERMES_ORCA_GUARD_SCRIPT": self.guard,
        })
        return env

    def patch_env(self):
        return mock.patch.dict(os.environ, self.env(), clear=True)

    def orca_calls(self) -> list[list[str]]:
        if not os.path.exists(self.orca_log):
            return []
        with open(self.orca_log) as fh:
            return [json.loads(line) for line in fh]

    def preflight_calls(self) -> list[list[str]]:
        if not os.path.exists(self.preflight_log):
            return []
        with open(self.preflight_log) as fh:
            return [json.loads(line) for line in fh]

    def child_pids(self) -> list[int]:
        if not os.path.exists(self.pidfile):
            return []
        with open(self.pidfile) as fh:
            return [int(line) for line in fh if line.strip()]
