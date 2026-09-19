"""Bounded execution of allowlisted, read-only Orca CLI commands.

Guarantees:

* only the commands in ``ALLOWED`` run, with only their listed flags, values
  validated by shape; ``--json`` is always appended;
* the executable is the configured argv prefix, never a shell, never remote;
* when a local gate is configured it runs before EVERY command and must exit
  0; no code path spawns an Orca command without first running it;
* each child's launch and execution together are bounded to ``timeout_seconds``
  (clipped to the operation's remaining deadline) and each of stdout/stderr to
  ``max_output_bytes`` while reading; overflow or timeout kills its whole
  process group (the child runs as a session leader, so helpers it spawned die
  with it) and attempts to reap it within a bounded wait — a synchronous spawn
  syscall cannot itself be preempted, and a reap that cannot be confirmed is
  reported explicitly rather than assumed;
* the whole operation is bounded by a wall-clock deadline and a command cap;
* runtime identity is checked per command family (qualified live on 1.4.205,
  see ``RUNTIME_FAMILY``): ``status``, ``project list``, ``worktree list``,
  ``terminal list`` and ``orchestration *`` envelopes carry the runtime id in
  ``_meta.runtimeId`` (``status`` also in ``result.runtime.runtimeId``, and
  the two must agree); ``host list`` carries the local host id there instead
  and discovery checks it against the ``kind=local`` anchor. Drift inside a
  family is ``runtime_changed``; an operation closes with a second ``status``
  read so a restart during the reads is detected;
* raw stdout/stderr never leave this module: callers get the parsed ``result``
  object or a bounded ``AdapterError``.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import time

from .config import SUPPORTED_APP_VERSION, SUPPORTED_SCHEMA_VERSION, Config
from .errors import (
    BUDGET_EXCEEDED,
    COMMAND_NOT_ALLOWED,
    CONFIG_ERROR,
    MALFORMED_RESULT,
    ORCA_ERROR,
    ORCA_EXEC_FAILED,
    ORCA_OUTPUT_TOO_LARGE,
    ORCA_TIMEOUT,
    PREFLIGHT_FAILED,
    PREFLIGHT_REJECTED,
    RUNTIME_CHANGED,
    UNSUPPORTED_RUNTIME,
    AdapterError,
)
from .fields import is_wellformed_text

MAX_LIMIT = 2000
MAX_CURSOR_CHARS = 512

# Operation-wide budget (one OrcaRunner is one snapshot or status operation).
# Gate spawns share the deadline but are not counted as commands. Like the
# child bounds in ``Config``, a constructor override may only tighten these.
OPERATION_DEADLINE_SECONDS = 180.0
MAX_COMMANDS_PER_OPERATION = 320

# command path -> flags it may carry. Anything else is refused before spawn.
# ``repo list`` and ``worktree ps`` are permitted by the spec but deliberately
# absent: ``worktree list`` rows carry ``projectId`` directly, so neither the
# repo hop nor the agent-prompt-bearing ``ps`` summary is needed. ``host list
# --page`` exists in the registry but its receipt shape is unrecorded, so it is
# not sent; discovery refuses any sign of a further host page instead.
ALLOWED: dict[tuple[str, ...], frozenset[str]] = {
    ("status",): frozenset(),
    ("agent-context",): frozenset(),
    ("host", "list"): frozenset(),
    ("project", "list"): frozenset(),
    ("worktree", "list"): frozenset({"limit"}),
    ("terminal", "list"): frozenset({"limit"}),
    ("orchestration", "run-list"): frozenset({"limit", "cursor"}),
    ("orchestration", "task-list"): frozenset({"run", "brief"}),
    ("orchestration", "worker-list"): frozenset({"run", "cursor", "limit"}),
    ("orchestration", "gate-list"): frozenset({"run"}),
}

# Run ids: the registry retains legacy rows whose ids contain ``_`` (observed
# live, ``legacy: 1``); ``-`` and anything else stay refused by shape. Every
# shape check is a whole-string ``fullmatch``: ``$`` alone would let a trailing
# newline through and into argv.
RUN_ID_RE = re.compile(r"run_[A-Za-z0-9_]{1,64}")

# Which envelope family a command's ``_meta.runtimeId`` belongs to. Qualified
# by the bounded identity probe of 2026-09-17 (equality booleans, not string
# length): every listed command carried one value equal to
# ``status.result.runtime.runtimeId``; ``host list`` carried the local host id.
RUNTIME_FAMILY: frozenset[tuple[str, ...]] = frozenset({
    ("status",), ("project", "list"), ("worktree", "list"), ("terminal", "list"),
    ("orchestration", "run-list"), ("orchestration", "task-list"),
    ("orchestration", "worker-list"), ("orchestration", "gate-list"),
})
HOST_FAMILY: frozenset[tuple[str, ...]] = frozenset({("host", "list")})
_CURSOR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+/=_.:-]{0,%d}" % (MAX_CURSOR_CHARS - 1))
_READ_CHUNK = 65536

# Orca ``error.code`` values that may be echoed as ``error.detail``. Anything
# else collapses to ``unclassified`` so the detail vocabulary stays closed.
# Seeded only from codes observed by independent live QA (2026-09-17).
KNOWN_ORCA_ERROR_CODES: frozenset[str] = frozenset({"run_not_found"})


def _stage(command: tuple[str, ...]) -> str:
    return " ".join(command)


def _validate_flags(command: tuple[str, ...], flags: dict) -> list[str]:
    # Shape and membership come before anything is derived from the command: a
    # refused command is never a stage, so the bounded error carries the fixed
    # token ``command`` rather than caller text of any length or type.
    if not isinstance(command, tuple) or not all(isinstance(part, str) for part in command):
        raise AdapterError(COMMAND_NOT_ALLOWED, "command")
    allowed = ALLOWED.get(command)
    if allowed is None:
        raise AdapterError(COMMAND_NOT_ALLOWED, "command")
    stage = _stage(command)
    argv: list[str] = []
    for name, value in flags.items():
        if name not in allowed:
            raise AdapterError(COMMAND_NOT_ALLOWED, stage, "flag")
        if name == "limit":
            if type(value) is not int or not 1 <= value <= MAX_LIMIT:
                raise AdapterError(COMMAND_NOT_ALLOWED, stage, "limit")
            argv += ["--limit", str(value)]
        elif name == "cursor":
            if not isinstance(value, str) or not _CURSOR_RE.fullmatch(value):
                raise AdapterError(COMMAND_NOT_ALLOWED, stage, "cursor")
            argv += ["--cursor", value]
        elif name == "run":
            if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
                raise AdapterError(COMMAND_NOT_ALLOWED, stage, "run")
            argv += ["--run", value]
        elif name == "brief":
            if value is not True:
                raise AdapterError(COMMAND_NOT_ALLOWED, stage, "brief")
            argv.append("--brief")
    return argv


def _kill_and_reap(proc: subprocess.Popen) -> bool:
    """SIGKILL the child's whole process group, then attempt to reap the child.

    The child was started as a session leader (``start_new_session``), so its
    pid is the group id and every descendant that did not start a session of
    its own dies here as well; nothing it spawned can keep the pipes or the
    CPU after the adapter has given up on the command. Returns whether the
    reap was confirmed within the bounded wait: SIGKILL cannot be ignored, but
    this function cannot itself guarantee the kernel reaps the zombie inside
    five seconds, so a caller that must know the child is actually gone has to
    check the return value rather than assume an unconditional reap.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        return False
    return True


def execute_bounded(argv: list[str], stage: str, timeout: float, max_bytes: int) -> tuple[int, bytes]:
    """Run ``argv`` with a wall-clock and per-stream size bound.

    The deadline is established before the child is spawned, so ``timeout``
    bounds the whole launch-and-execute interval rather than only the time
    spent reading: a child whose ``Popen`` call alone consumes the budget is
    killed and reaped without ever being read from. The one thing this cannot
    promise is preempting the ``Popen`` syscall itself mid-launch; slow
    fork/exec is charged against the deadline, not exempted from it. Returns
    ``(returncode, stdout)``; stderr is read (so the child cannot block on it)
    but discarded. On timeout or overflow the child is killed and a reap is
    attempted; the original bounded failure is always what is raised (never
    replaced by a cleanup-only code), but if the reap could not be confirmed
    within the bounded wait the error carries an explicit ``not_reaped``
    detail instead of silently claiming the child is gone. Nothing read from
    the child is attached to the error.
    """
    deadline = time.monotonic() + timeout
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            shell=False,
            start_new_session=True,  # own process group: timeout/overflow kills descendants too
        )
    except OSError:
        raise AdapterError(ORCA_EXEC_FAILED, stage, "spawn_failed") from None

    buffers = {proc.stdout: bytearray(), proc.stderr: bytearray()}
    sel = selectors.DefaultSelector()
    failure = None
    returncode = None
    reaped = True
    try:
        if time.monotonic() >= deadline:
            # The launch alone already spent the whole budget: refuse before
            # registering a single read, rather than starting a read loop
            # whose very first deadline check would immediately trip anyway.
            failure = ORCA_TIMEOUT
        else:
            for stream in buffers:
                os.set_blocking(stream.fileno(), False)
                sel.register(stream, selectors.EVENT_READ)
            while sel.get_map() and failure is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = ORCA_TIMEOUT
                    break
                for key, _events in sel.select(remaining):
                    try:
                        chunk = os.read(key.fd, _READ_CHUNK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    buf = buffers[key.fileobj]
                    buf += chunk
                    if len(buf) > max_bytes:
                        failure = ORCA_OUTPUT_TOO_LARGE
                        break
            if failure is None:
                try:
                    returncode = proc.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    failure = ORCA_TIMEOUT
    except BaseException:  # an interruption of the reader itself (a signal, an unexpected OSError) must not orphan the child
        _kill_and_reap(proc)
        raise
    finally:
        sel.close()
        if failure is not None:
            reaped = _kill_and_reap(proc)
        proc.stdout.close()
        proc.stderr.close()
        buffers[proc.stderr].clear()
    if failure is not None:
        raise AdapterError(failure, stage, "" if reaped else "not_reaped")
    return returncode, bytes(buffers[proc.stdout])


def _reject_duplicate_keys(pairs: list) -> dict:
    doc: dict = {}
    for key, value in pairs:
        if key in doc:
            raise ValueError("duplicate key")
        doc[key] = value
    return doc


def _reject_constant(_name: str) -> None:
    raise ValueError("non-JSON constant")


def _parse_finite_float(text: str) -> float:
    # ``parse_constant`` only intercepts the non-standard tokens NaN/Infinity; a
    # grammatical number like ``1e9999`` is ordinary JSON syntax that still
    # overflows to a non-finite float, so it must be refused here instead.
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite number")
    return value


def _parse_json_object(stdout: bytes, stage: str) -> dict:
    # Strict like every other parser in the bridge: a duplicated key, the NaN/Infinity tokens, or a
    # grammatical number that overflows to a non-finite float is not JSON the qualified runtime emits,
    # so each is malformed rather than resolved by last-key-wins or silently carried as an out-of-band float.
    try:
        doc = json.loads(
            stdout,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
            parse_float=_parse_finite_float,
        )
    except Exception:  # noqa: BLE001 - message could embed the payload
        raise AdapterError(MALFORMED_RESULT, stage, "not_json") from None
    if not isinstance(doc, dict):
        raise AdapterError(MALFORMED_RESULT, stage, "envelope")
    return doc


def _parse_envelope(doc: dict, stage: str) -> dict:
    """Validate the public ``{ok, result}`` / ``{ok, error}`` envelope."""
    if not isinstance(doc.get("ok"), bool):
        raise AdapterError(MALFORMED_RESULT, stage, "envelope")
    if doc["ok"] is False:
        err = doc.get("error")
        code = err.get("code") if isinstance(err, dict) else None
        detail = code if isinstance(code, str) and code in KNOWN_ORCA_ERROR_CODES else "unclassified"
        raise AdapterError(ORCA_ERROR, stage, detail)
    result = doc.get("result")
    if not isinstance(result, dict):
        raise AdapterError(MALFORMED_RESULT, stage, "result")
    return result


class OrcaRunner:
    """Runs allowlisted Orca commands behind the optional gate and the budget."""

    def __init__(self, config: Config, *, deadline_seconds: float = OPERATION_DEADLINE_SECONDS,
                 max_commands: int = MAX_COMMANDS_PER_OPERATION) -> None:
        if (
            type(deadline_seconds) not in (int, float) or not math.isfinite(deadline_seconds)
            or not 0 < deadline_seconds <= OPERATION_DEADLINE_SECONDS
            or type(max_commands) is not int or not 0 < max_commands <= MAX_COMMANDS_PER_OPERATION
        ):
            raise AdapterError(CONFIG_ERROR, "config", "bad_bounds")
        self._config = config
        self._deadline = time.monotonic() + deadline_seconds
        self._max_commands = max_commands
        self.command_count = 0  # Orca commands actually spawned (tests assert on it)
        self.gate_count = 0  # gate spawns; equals command_count when a gate is configured
        self.runtime_id: str | None = None  # pinned from the first runtime-family envelope; only ever HMAC'd
        self.host_id: str | None = None  # ``host list`` ``_meta.runtimeId``; in-process anchor check only

    def _remaining(self, stage: str) -> float:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError(BUDGET_EXCEEDED, stage, "deadline")
        return min(self._config.timeout_seconds, remaining)

    def _preflight(self, stage: str) -> None:
        cfg = self._config
        if not cfg.preflight_argv:
            return
        self.gate_count += 1
        try:
            rc, _ = execute_bounded(list(cfg.preflight_argv), "preflight", self._remaining(stage), cfg.max_output_bytes)
        except AdapterError as exc:
            if exc.code == BUDGET_EXCEEDED:
                raise
            raise AdapterError(PREFLIGHT_FAILED, stage, exc.code) from None
        if rc == 0:
            return
        detail = {1: "findings", 2: "blocked"}.get(rc, "exit")
        raise AdapterError(PREFLIGHT_REJECTED, stage, detail)

    def _check_runtime(self, command: tuple[str, ...], doc: dict, stage: str) -> None:
        """Pin or compare ``_meta.runtimeId`` within the command's family."""
        meta = doc.get("_meta")
        meta_id = meta.get("runtimeId") if isinstance(meta, dict) else None
        # Well-formed bounded text like every other projected string: the id is HMAC
        # input, so an unpaired surrogate or an oversized value is refused here rather
        # than surfacing as a foreign exception from the encode.
        if not is_wellformed_text(meta_id) or not meta_id:
            raise AdapterError(MALFORMED_RESULT, stage, "runtimeId")
        if command in HOST_FAMILY:
            if self.host_id is None:
                self.host_id = meta_id
            elif meta_id != self.host_id:
                raise AdapterError(RUNTIME_CHANGED, stage)
            return
        if command not in RUNTIME_FAMILY:  # every allowlisted envelope has a qualified family
            raise AdapterError(MALFORMED_RESULT, stage, "runtimeId")
        if self.runtime_id is None:
            self.runtime_id = meta_id
        elif meta_id != self.runtime_id:
            raise AdapterError(RUNTIME_CHANGED, stage)

    def run(self, command: tuple[str, ...], **flags) -> dict:
        """Gate, then run one allowlisted command within budget; return its ``result``.

        This primitive has no qualified-state guard: calling ``qualify_runtime``
        before the first read and ``close_runtime`` after the last one are the
        caller's responsibility, not enforced here.
        """
        flag_argv = _validate_flags(command, flags)
        stage = _stage(command)
        if self.command_count >= self._max_commands:
            raise AdapterError(BUDGET_EXCEEDED, stage, "commands")
        self._preflight(stage)
        argv = list(self._config.executable) + list(command) + flag_argv + ["--json"]
        self.command_count += 1
        rc, stdout = execute_bounded(argv, stage, self._remaining(stage), self._config.max_output_bytes)
        if rc != 0:
            try:
                _parse_envelope(_parse_json_object(stdout, stage), stage)  # a clean ok=false envelope
            except AdapterError as exc:
                if exc.code == ORCA_ERROR:
                    raise
            raise AdapterError(ORCA_EXEC_FAILED, stage, "exit")
        doc = _parse_json_object(stdout, stage)
        if command == ("agent-context",):
            # The registry read is a bare document, not an {ok, result} envelope.
            return doc
        self._check_runtime(command, doc, stage)
        return _parse_envelope(doc, stage)

    def _status_runtime(self, *, closing: bool) -> dict:
        """Read ``status``; its ``result.runtime.runtimeId`` must be the pinned id.

        The envelope check above already pinned or compared ``_meta.runtimeId``;
        here the identity the runtime reports about itself must agree with it,
        so ``runtime.ref`` derives from one qualified value, never from a
        transport-level field alone. A disagreement on the opening read is a
        malformed runtime self-description; on the closing read it is drift.
        """
        status = self.run(("status",))
        runtime = status.get("runtime")
        target = status.get("target")
        if not isinstance(runtime, dict) or not isinstance(target, dict):
            raise AdapterError(MALFORMED_RESULT, "status", "runtime")
        runtime_id = runtime.get("runtimeId")
        if not is_wellformed_text(runtime_id) or not runtime_id:
            raise AdapterError(MALFORMED_RESULT, "status", "runtimeId")
        if runtime_id != self.runtime_id:
            if closing:
                raise AdapterError(RUNTIME_CHANGED, "status")
            raise AdapterError(MALFORMED_RESULT, "status", "runtimeId")
        return status

    def qualify_runtime(self) -> dict:
        """Pin the runtime identity and pins; returns bounded runtime facts for the output."""
        status = self._status_runtime(closing=False)
        runtime = status["runtime"]
        target = status["target"]
        app_version = runtime.get("appVersion")
        state = runtime.get("state")
        kind = target.get("kind")
        if app_version != SUPPORTED_APP_VERSION or kind != "local":
            raise AdapterError(UNSUPPORTED_RUNTIME, "status")
        if state != "ready":
            raise AdapterError(ORCA_EXEC_FAILED, "status", "not_ready")
        schema = self.run(("agent-context",))
        version = schema.get("schemaVersion")
        if type(version) is not int or version != SUPPORTED_SCHEMA_VERSION:
            raise AdapterError(UNSUPPORTED_RUNTIME, "agent-context")
        return {
            "app_version": SUPPORTED_APP_VERSION,
            "schema_version": SUPPORTED_SCHEMA_VERSION,
            "target_kind": "local",
            "state": "ready",
        }

    def close_runtime(self) -> None:
        """Closing bracket: one more ``status`` read must show the pinned identity.

        Every successful envelope read in between was already compared within
        its family (an ``ok=false`` envelope is bounded but not identity-
        attested by this module); this catches a restart that happened after
        the last enveloped read (or one whose new runtime happened to answer
        the same way) before the document is released.
        """
        if self.runtime_id is None:
            raise AdapterError(RUNTIME_CHANGED, "status")
        self._status_runtime(closing=True)
