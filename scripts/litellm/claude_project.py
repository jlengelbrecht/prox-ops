#!/usr/bin/env python3
"""Launch native Claude Code against the isolated LiteLLM project pilot.

The gateway virtual key is read from an environment variable named by the
operator and passed only in the child process environment. Native Claude Code
continues to own its OAuth record. This is a product client, never an AFK worker
launcher. No network request occurs in preflight.
"""

import argparse
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time

ALIAS = "project-router"
GATEWAY = "https://ai-api.homelab0.org"
DEFAULT_KEY_ENV = "LITELLM_PROJECT_KEY"
FORBIDDEN_ENV = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_SUBAGENT_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL_FORCE",
    "ANTHROPIC_SMALL_FAST_MODEL",
})
FORBIDDEN_SETTINGS = frozenset({
    "apiKeyHelper", "fallbackModel", "modelOverrides", "modelPicker",
    "availableModels", "deniedModels", "provider",
})
FORBIDDEN_SETTINGS_ENV = FORBIDDEN_ENV | frozenset({"ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL"})
ALLOWED_PROJECT_SETTINGS_ENV = frozenset()
SAFE_SETTINGS = json.dumps({"fallbackModel": None}, separators=(",", ":"))
LOGIN_ENV = frozenset({"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL",
                       "LC_CTYPE", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                       "XDG_STATE_HOME"})
SETTINGS_MAX_BYTES = 65536
SMOKE_FIXTURE = Path(__file__).resolve().parents[2] / "tests/litellm/project_router/smoke-fixture.txt"
SMOKE_CONTENT = b"The project router pilot fact is cedar.\n"


def _settings_paths(cwd, home):
    """List user and ancestor project settings consulted by native Claude."""
    paths = [home / ".claude" / "settings.json"]
    for parent in (cwd, *cwd.parents):
        paths.extend((parent / ".claude" / "settings.json",
                      parent / ".claude" / "settings.local.json"))
        if parent == home:
            break
    return list(dict.fromkeys(paths))


def _settings_issues(cwd, home):
    """Reject unsafe or unbounded settings before passing the gateway key."""
    issues = []
    user_settings = home / ".claude" / "settings.json"
    for path in _settings_paths(cwd, home):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            issues.append("settings_unreadable_or_invalid")
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > SETTINGS_MAX_BYTES:
                raise ValueError("settings_not_bounded_regular_file")
            raw = os.read(fd, SETTINGS_MAX_BYTES + 1)
            if len(raw) > SETTINGS_MAX_BYTES:
                raise ValueError("settings_too_large")
            settings = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, ValueError):
            issues.append("settings_unreadable_or_invalid")
            continue
        finally:
            os.close(fd)
        if not isinstance(settings, dict):
            issues.append("settings_invalid_shape")
            continue
        if FORBIDDEN_SETTINGS.intersection(settings):
            issues.append("settings_provider_or_model_override")
        values = settings.get("env", {})
        if (not isinstance(values, dict)
                or (FORBIDDEN_SETTINGS_ENV.intersection(values) if path == user_settings
                    else set(values) - ALLOWED_PROJECT_SETTINGS_ENV)):
            issues.append("settings_credential_or_provider_env")
    return sorted(set(issues))


def _native_login_record(cli, env):
    """Optional, non-live diagnostic; never gates the native CLI launch."""
    try:
        result = subprocess.run([cli, "auth", "status"], env=env, capture_output=True,
                                text=True, timeout=8, check=False)
        if result.returncode != 0 or len(result.stdout) > 8192:
            return False
        status = json.loads(result.stdout)
        return (isinstance(status, dict) and status.get("loggedIn") is True
                and status.get("authMethod") == "claude.ai")
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return False


def _login_env(env, key_env):
    """Pass only native login-record lookup context, never project credentials."""
    return {name: value for name, value in env.items()
            if name in LOGIN_ENV and name != key_env}


def _trusted_cli(cli, home):
    """Resolve the native Claude install and reject project-controlled executables."""
    if not cli or not os.path.isabs(cli):
        return None
    try:
        entry = Path(cli)
        canonical = entry.resolve(strict=True)
        install = home.resolve(strict=True) / ".local" / "share" / "claude" / "versions"
        if (entry != home / ".local" / "bin" / "claude" and entry != canonical):
            return None
        if canonical.parent != install or not canonical.name or canonical.name.startswith("."):
            return None
        # The native installer uses ~/.local/bin/claude -> ~/.local/share/claude/versions/<version>.
        # Restrict every writable component of that install, including HOME itself.
        paths = [home, home / ".local", home / ".local" / "share",
                 home / ".local" / "share" / "claude", install, canonical]
        if entry != canonical:
            paths.append(home / ".local" / "bin")
        for path in paths:
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o022:
                return None
        target = canonical.stat()
        if not stat.S_ISREG(target.st_mode) or not os.access(canonical, os.X_OK):
            return None
        return str(canonical)
    except (OSError, RuntimeError, ValueError):
        return None


def preflight(env, cwd, home, cli=None, login_check=None, key_env=DEFAULT_KEY_ENV):
    """Check local launch constraints without treating the login record as live auth."""
    failures = []
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key_env):
        failures.append("invalid_key_reference")
    key = env.get(key_env) if key_env not in FORBIDDEN_ENV else None
    key_present = (isinstance(key, str) and key.startswith("sk-") and len(key) > 6
                   and not any(char.isspace() or char in ":\r\n" for char in key))
    if not key_present:
        failures.append("owner_gateway_key_missing_or_invalid")
    if any(env.get(name) for name in FORBIDDEN_ENV):
        failures.append("credential_or_provider_environment_override")
    if any(name.startswith("ANTHROPIC_DEFAULT_") and env.get(name) for name in env):
        failures.append("default_model_environment_override")
    if any(env.get(name) for name in ("AFK_DISPATCH_ID", "AFK_WORKER_IDENTITY")):
        failures.append("worker_dispatch_context")
    failures.extend(_settings_issues(cwd, home))
    cli = _trusted_cli(shutil.which("claude") if cli is None else cli, home)
    if cli is None:
        failures.append("native_claude_cli_missing")
    login = False
    if cli:
        login = (login_check or _native_login_record)(cli, _login_env(env, key_env))
    return {
        "alias": ALIAS,
        "gateway": GATEWAY,
        "gateway_key_present": bool(key_present),
        "native_login_record_present": bool(login),
        "native_login_live_verified": False,
        "ready": not failures,
        "failures": sorted(set(failures)),
    }


def _child_env(env, key_env):
    """Route native primary, background, and default subagent models through the alias."""
    child = dict(env)
    key = child.pop(key_env)
    child["ANTHROPIC_BASE_URL"] = GATEWAY
    child["ANTHROPIC_MODEL"] = ALIAS
    for name in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL",
                 "CLAUDE_CODE_SUBAGENT_MODEL"):
        child[name] = ALIAS
    child["ANTHROPIC_CUSTOM_HEADERS"] = "x-litellm-api-key: Bearer " + key
    return child


def _command(cli, smoke=False, prompt=None):
    """Build a native command, restricting headless smoke to a single Read."""
    args = [cli, "--model", ALIAS, "--settings", SAFE_SETTINGS,
            "--no-chrome"]
    if smoke:
        args += ["--strict-mcp-config", "--print", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
                 "--setting-sources", "user",
                 "--tools", "Read",
                 "--permission-mode", "dontAsk", "--permission-prompts", "none",
                 "--safe-mode", "--no-session-persistence", "--max-turns", "2"]
    if prompt:
        args.append(prompt)
    return args


def _smoke_prompt(fixture, cwd):
    """Accept only the committed fixture before constructing the Read prompt."""
    path = fixture.resolve(strict=True)
    if (path != SMOKE_FIXTURE or not path.is_relative_to(cwd.resolve())
            or path.stat().st_size != len(SMOKE_CONTENT)
            or path.read_bytes() != SMOKE_CONTENT):
        raise ValueError("fixture_must_be_committed_proof")
    return ("Use Read once on " + str(path.relative_to(cwd.resolve()))
            + ". Return PROJECT_ROUTER_SMOKE and one fact from the file. "
              "Do not use any other tool.")


def _stop_smoke_group(process):
    """Stop the isolated process group, then reap its direct child."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    # Keep the direct child unreaped until the group is stopped, so its PID
    # cannot be reused as an unrelated process-group ID.
    time.sleep(0.5)
    # The direct child may exit while a descendant still holds the pipes.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def _bounded_smoke(command, env, cwd, timeout_s=120, output_cap=65536, fixture=None):
    """Run one isolated smoke child within time and output limits, then reap it."""
    def on_sigterm(_signum, _frame):
        """Convert SIGTERM into an exception that reaches child cleanup."""
        raise SystemExit(128 + signal.SIGTERM)

    previous_sigterm = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        return _bounded_smoke_inner(command, env, cwd, timeout_s, output_cap, fixture)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _bounded_smoke_inner(command, env, cwd, timeout_s, output_cap, fixture):
    """Keep SIGTERM inside the cleanup path while the smoke child runs."""
    selector = None
    streams = {"stdout": bytearray(), "stderr": bytearray()}
    reason = None
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, start_new_session=True)
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        deadline = time.monotonic() + timeout_s
        while selector.get_map() and reason is None:
            if time.monotonic() >= deadline:
                reason = "timeout"
                break
            for key, _ in selector.select(min(0.25, max(0, deadline - time.monotonic()))):
                chunk = key.fileobj.read1(8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.data].extend(chunk)
                if sum(map(len, streams.values())) > output_cap:
                    reason = "output_limit"
                    break
        if reason is not None:
            _stop_smoke_group(process)
        else:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                reason = "timeout"
                _stop_smoke_group(process)
        code = process.returncode
    finally:
        # poll() would reap an exited child before the group is signaled.
        if process.returncode is None:
            _stop_smoke_group(process)
        if selector is not None:
            selector.close()
        process.stdout.close()
        process.stderr.close()
    # No raw response, error, prompt or header is persisted or printed.
    evidence = _smoke_evidence(streams["stdout"].decode("utf-8", "replace").splitlines(),
                               fixture, cwd)
    return {"exit_code": code, "bounded": reason is None, "failure": reason, **evidence}


def _smoke_evidence(lines, fixture, cwd):
    """Require a linked successful Read and matching terminal answer in the stream."""
    event_types = []
    tool_uses = []
    tool_results = []
    final_answer = False
    final_marker = False
    final_fact = False
    terminal_success = False
    expected_path = fixture.resolve()
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            event_types.append(event.get("type"))
            if event.get("type") == "result":
                answer = event.get("result")
                terminal_success = (event.get("is_error") is not True
                                    and event.get("subtype") in (None, "success"))
                final_answer = terminal_success and isinstance(answer, str) and bool(answer.strip())
                final_marker = final_answer and "PROJECT_ROUTER_SMOKE" in answer
                final_fact = final_answer and bool(re.search(r"\bcedar\b", answer, re.IGNORECASE))
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_input = block.get("input")
                        raw_path = (tool_input.get("file_path")
                                    if isinstance(tool_input, dict) else None)
                        resolved = None
                        if isinstance(raw_path, str):
                            read_path = Path(raw_path)
                            resolved = (read_path if read_path.is_absolute() else cwd / read_path).resolve()
                        tool_uses.append((block.get("name"), block.get("id"), resolved,
                                          len(event_types)))
                    if block.get("type") == "tool_result":
                        tool_results.append((block.get("tool_use_id"), block.get("is_error") is not True,
                                             len(event_types)))
    linked = (len(tool_uses) == len(tool_results) == 1
              and tool_uses[0][0] == "Read"
              and isinstance(tool_uses[0][1], str)
              and tool_uses[0][1] == tool_results[0][0]
              and tool_uses[0][2] == expected_path
              and tool_uses[0][3] < tool_results[0][2]
              and tool_results[0][2] < len(event_types)
              and event_types.count("result") == 1
              and event_types[-1:] == ["result"])
    return {"stream_event_seen": "stream_event" in event_types,
            "linked_read_tool": linked and tool_results[0][1] if linked else False,
            "final_answer_nonempty": bool(final_answer),
            "terminal_success": terminal_success and event_types[-1:] == ["result"],
            "final_answer_marker": bool(final_marker),
            "final_answer_fact": bool(final_fact)}


def main(argv=None):
    """Preflight, then launch native Claude or one bounded smoke session."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-key-env", default=DEFAULT_KEY_ENV,
                        help="name of an already populated owner-scoped virtual-key variable")
    parser.add_argument("--dry-run", action="store_true", help="secret-free, no-inference preflight")
    parser.add_argument("--smoke-test", metavar="FIXTURE", type=Path,
                        help="one bounded read-only native test on a small project fixture")
    parser.add_argument("--prompt", help="optional ordinary interactive start prompt")
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    env = dict(os.environ)
    home = Path.home()
    cli = _trusted_cli(shutil.which("claude"), home)
    report = preflight(env, cwd, home, cli=cli or "", key_env=args.gateway_key_env)
    if not report["ready"] or args.dry_run:
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ready"] else 2
    if not args.smoke_test and not sys.stdout.isatty():
        print(json.dumps({"ready": False, "failure": "interactive_tty_required_for_workspace_trust"}))
        return 2
    child = _child_env(env, args.gateway_key_env)
    if args.smoke_test:
        try:
            prompt = _smoke_prompt(args.smoke_test, cwd)
        except (OSError, ValueError):
            print(json.dumps({"ready": False, "failure": "invalid_smoke_fixture"}))
            return 2
        result = _bounded_smoke(_command(cli, smoke=True, prompt=prompt), child, cwd,
                                fixture=args.smoke_test)
        print(json.dumps(result, sort_keys=True))
        return 0 if (result["exit_code"] == 0 and result["bounded"]
                     and result["stream_event_seen"] and result["linked_read_tool"]
                     and result["final_answer_nonempty"] and result["terminal_success"]
                     and result["final_answer_marker"] and result["final_answer_fact"]) else 1
    os.execvpe(cli, _command(cli, prompt=args.prompt), child)
    return 1


if __name__ == "__main__":
    sys.exit(main())
