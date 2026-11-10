#!/usr/bin/env python3
"""Offline Codex-to-LiteLLM auth conversion and isolated browser login ceremony.

No credentials are read until an operator explicitly invokes a command. The
login command must only be run by the owner after review, on the browser host.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

MAX_RECORD = 65536
MAX_TOKEN = 32768
MAX_CLAIMS = 16384


class ClosedFailure(Exception):
    """A fixed, nonsecret failure classification."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _claims(token):
    parts = token.split('.')
    if (len(parts) != 3 or any(not part or len(part) > MAX_TOKEN or
            re.fullmatch(r'[A-Za-z0-9_-]+', part) is None for part in parts) or
            len(parts[1]) > MAX_CLAIMS * 2):
        raise ClosedFailure('invalid_token_claims')
    try:
        raw = base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4))
        if len(raw) > MAX_CLAIMS:
            raise ValueError()
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, base64.binascii.Error):
        raise ClosedFailure('invalid_token_claims') from None


def convert_record(record, now=None):
    """Pure conversion of the expected nested native Codex schema."""
    if not isinstance(record, dict) or record.get('auth_mode') != 'chatgpt':
        raise ClosedFailure('invalid_record_schema')
    tokens = record.get('tokens')
    if not isinstance(tokens, dict):
        raise ClosedFailure('invalid_record_schema')
    values = {}
    for field in ('access_token', 'refresh_token', 'id_token'):
        value = tokens.get(field)
        if not isinstance(value, str) or not value or len(value) > MAX_TOKEN or any(c.isspace() for c in value):
            raise ClosedFailure('missing_or_invalid_token')
        values[field] = value
    access = _claims(values['access_token'])
    identity = _claims(values['id_token'])
    clock = time.time() if now is None else now
    expiry = access.get('exp')
    if type(expiry) is not int or not clock + 60 < expiry:
        raise ClosedFailure('invalid_expiry')
    def account(claims):
        section = claims.get('https://api.openai.com/auth')
        return section.get('chatgpt_account_id') if isinstance(section, dict) else None
    candidates = (tokens.get('account_id'), account(access), account(identity))
    present = [value for value in candidates if value is not None]
    if not present or any(not isinstance(v, str) or not v or len(v) > 256 or not v.isascii() or any(c.isspace() for c in v) for v in present) or len(set(present)) != 1:
        raise ClosedFailure('invalid_account_mapping')
    return {**values, 'account_id': present[0], 'expires_at': expiry}


def _mount_type(path):
    best = ('', '')
    with open('/proc/self/mountinfo', encoding='utf-8') as mounts:
        for line in mounts:
            left, marker, right = line.partition(' - ')
            if not marker:
                continue
            mount = left.split()[4].replace('\\040', ' ')
            if (str(path) == mount or str(path).startswith(mount.rstrip('/') + '/')) and len(mount) > len(best[0]):
                best = (mount, right.split()[0])
    return best[1]


def _safe_dir(path):
    if not path.is_absolute() or '..' in path.parts or _mount_type(path) != 'tmpfs':
        raise ClosedFailure('unsafe_runtime_path')
    # The explicitly selected task directory is under an owner-only runtime root.
    anchor = Path('/run/user') / str(os.getuid())
    if path.parent != anchor and path.parent != Path('/dev/shm'):
        raise ClosedFailure('unsafe_runtime_path')
    for item in (path.parent, path):
        info = item.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ClosedFailure('unsafe_runtime_path')
        if item == Path('/dev/shm'):
            mode = stat.S_IMODE(info.st_mode)
            # Standard /dev/shm is root-owned and sticky. Some isolated
            # runtimes expose an owner-owned, non-writable mount instead.
            if not ((info.st_uid == 0 and mode == 0o1777) or
                    (info.st_uid == os.getuid() and mode & 0o022 == 0)):
                raise ClosedFailure('unsafe_runtime_path')
        elif info.st_uid != os.getuid():
            raise ClosedFailure('unsafe_runtime_path')
        if item == path and stat.S_IMODE(info.st_mode) != 0o700:
            raise ClosedFailure('unsafe_runtime_path')
        if item == anchor and stat.S_IMODE(info.st_mode) != 0o700:
            raise ClosedFailure('unsafe_runtime_path')


def _safe_file(path, task, source):
    if not path.is_absolute() or '..' in path.parts or path.parent != task:
        raise ClosedFailure('unsafe_file_path')
    if source:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_RECORD:
            os.close(fd)
            raise ClosedFailure('unsafe_source')
        return fd
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    raise ClosedFailure('destination_exists')


def convert_file(task, source, target):
    _safe_dir(task)
    fd = _safe_file(source, task, True)
    try:
        with os.fdopen(fd, 'rb') as stream:
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise ClosedFailure('invalid_record_schema')
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ClosedFailure('invalid_record_schema') from None
        output = convert_record(record)
        _safe_file(target, task, False)
        # Prepare privately, then publish with link(2), which cannot replace an
        # existing destination even when another process creates it concurrently.
        out, temporary = tempfile.mkstemp(prefix='.auth-prep-', dir=task)
        published = False
        try:
            os.fchmod(out, 0o600)  # Exact mode even under a restrictive umask.
            with os.fdopen(out, 'w', encoding='utf-8') as stream:
                json.dump(output, stream, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target, follow_symlinks=False)
            published = True
            directory = os.open(task, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except FileExistsError:
            raise ClosedFailure('destination_exists') from None
        except BaseException:
            if published:
                target.unlink(missing_ok=True)
            raise
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        raise ClosedFailure('file_operation_failed') from None


def _login_env(task, ambient):
    home = task / 'home'
    codex_home = task / 'codex'
    for path in (home, codex_home, task / 'config', task / 'data', task / 'state'):
        path.mkdir(mode=0o700)
        path.chmod(0o700)
    env = {'HOME': str(home), 'CODEX_HOME': str(codex_home),
           'XDG_CONFIG_HOME': str(task / 'config'), 'XDG_DATA_HOME': str(task / 'data'),
           'XDG_STATE_HOME': str(task / 'state'), 'TMPDIR': str(task),
           'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS'):
        if key in ambient:
            env[key] = ambient[key]
    return env


def _trusted_cli(path):
    if not path.is_absolute() or '..' in path.parts:
        raise ClosedFailure('unsafe_cli')
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022 or not os.access(resolved, os.X_OK):
        raise ClosedFailure('unsafe_cli')
    # No project-controlled binary or writable ancestor may supply the login executable.
    if any(part.stat().st_mode & 0o022 for part in (resolved.parent, *resolved.parents[1:-1])):
        raise ClosedFailure('unsafe_cli')
    return str(resolved)


def _stop_child(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        raise ClosedFailure('cleanup_required') from None
    try:
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise ClosedFailure('cleanup_required') from None


def _remove_task(task):
    try:
        visited = 0
        for root, dirs, files in os.walk(task, topdown=True, followlinks=False):
            visited += len(dirs) + len(files)
            if visited > 10000:
                raise ClosedFailure('cleanup_required')
            for name in dirs:
                child = Path(root) / name
                info = child.lstat()
                if stat.S_ISDIR(info.st_mode):
                    if info.st_uid != os.getuid():
                        raise ClosedFailure('cleanup_required')
                    child.chmod(0o700)
        shutil.rmtree(task)
    except (OSError, RuntimeError):
        raise ClosedFailure('cleanup_required') from None


def login(task, target, cli, timeout, ambient=None):
    if not 60 <= timeout <= 900:
        raise ClosedFailure('invalid_timeout')
    if not task.is_absolute() or '..' in task.parts or task.exists():
        raise ClosedFailure('unsafe_runtime_path')
    if _mount_type(task) != 'tmpfs' or task.parent not in (Path('/dev/shm'), Path('/run/user') / str(os.getuid())):
        raise ClosedFailure('unsafe_runtime_path')
    executable = _trusted_cli(cli)
    task.mkdir(mode=0o700)
    proc = None
    old_handlers = {}
    termination = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    try:
        task.chmod(0o700)
        _safe_dir(task)
        if target.parent != task or target.name in ('auth.json', 'native.json', 'codex', 'home', 'config', 'data', 'state'):
            raise ClosedFailure('unsafe_file_path')
        _safe_file(target, task, False)
        env = _login_env(task, os.environ if ambient is None else ambient)
        # Block termination across spawn so no signal can land before proc is
        # assigned. The handler then turns a signal during login into cleanup.
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, termination)
        try:
            def cancel(signum, frame):
                for sig in termination:
                    signal.signal(sig, signal.SIG_IGN)
                raise KeyboardInterrupt()
            for sig in termination:
                old_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, cancel)
            proc = subprocess.Popen([executable, 'login'], cwd=task, env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ClosedFailure('login_timeout') from None
        if code is None or code != 0:
            raise ClosedFailure('login_failed_or_cancelled')
        # A successful CLI leader may leave children in its login session.
        # Stop them before reading credentials or handing off the output.
        _stop_child(proc)
        codex_dir = task / 'codex'
        directory = codex_dir.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise ClosedFailure('unsafe_source')
        source = codex_dir / 'auth.json'
        # Native auth lives one level below task; copy its validated bytes into
        # an owner-only task-level source for the bounded converter.
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_RECORD:
                raise ClosedFailure('unsafe_source')
            data = os.read(fd, MAX_RECORD + 1)
            if len(data) > MAX_RECORD:
                raise ClosedFailure('unsafe_source')
        finally:
            os.close(fd)
        staged = task / 'native.json'
        out = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(out, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        convert_file(task, staged, target)
        staged.unlink()
    except BaseException as failure:
        # This task directory was created by this invocation; no pre-existing
        # runtime tree is touched. The operator cleans a successful handoff.
        for sig in old_handlers:
            signal.signal(sig, signal.SIG_IGN)
        if isinstance(failure, ClosedFailure) and failure.code == 'cleanup_required':
            raise
        if proc is not None:
            _stop_child(proc)
        _remove_task(task)
        raise
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def main(argv=None):
    class SafeParser(argparse.ArgumentParser):
        def error(self, message):
            raise ClosedFailure('invalid_arguments')

    parser = SafeParser(description='Isolated Codex browser login for LiteLLM')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('convert', 'login'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--runtime-dir', type=Path, required=True)
        cmd.add_argument('--output', type=Path, required=True)
        if name == 'convert':
            cmd.add_argument('--source', type=Path, required=True)
        else:
            cmd.add_argument('--cli', type=Path, required=True)
            cmd.add_argument('--timeout', type=int, default=300)
    try:
        args = parser.parse_args(argv)
        if args.command == 'convert':
            convert_file(args.runtime_dir, args.source, args.output)
        else:
            login(args.runtime_dir, args.output, args.cli, args.timeout)
    except ClosedFailure as exc:
        print('AUTH_PREP_' + exc.code.upper(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('AUTH_PREP_CANCELLED', file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError):
        print('AUTH_PREP_OPERATION_FAILED', file=sys.stderr)
        return 1
    print('AUTH_PREP_READY')
    return 0


if __name__ == '__main__':
    sys.exit(main())
