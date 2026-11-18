"""Offline, synthetic-only checks for the dedicated gateway login helper."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/litellm/chatgpt_auth.py'
spec = importlib.util.spec_from_file_location('chatgpt_auth', MODULE)
auth = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auth)
MARKER = 'SYNTHETIC_SECRET_MUST_NOT_PRINT'
NOW = 1800000000

def jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'


def record(exp=NOW + 3600, account='acct_synthetic'):
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': account}}
    return {'auth_mode': 'chatgpt', 'tokens': {
        'access_token': jwt({**claims, 'exp': exp}),
        'id_token': jwt(claims), 'refresh_token': MARKER,
        'account_id': account}}


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Path('/dev/shm').is_dir() or auth._mount_type(Path('/dev/shm')) != 'tmpfs':
            raise AssertionError('core path tests require /dev/shm tmpfs')

    def setUp(self):
        self.task = Path(tempfile.mkdtemp(prefix='chatgpt-auth-test-', dir='/dev/shm'))
        os.chmod(self.task, 0o700)
        self.runtime_environment = patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.task)})
        self.runtime_environment.start()
        self.source = self.task / 'source.json'
        self.target = self.task / 'flat.json'

    def tearDown(self):
        self.runtime_environment.stop()
        if self.task.exists():
            shutil.rmtree(self.task)

    def write_source(self, value, mode=0o600):
        self.source.write_text(json.dumps(value))
        os.chmod(self.source, mode)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = auth.main(list(args))
        self.assertNotIn(MARKER, out.getvalue() + err.getvalue())
        self.assertNotIn('Traceback', out.getvalue() + err.getvalue())
        return result, out.getvalue(), err.getvalue()

    def test_pure_conversion_preserves_exact_token_bytes(self):
        original = record()
        flat = auth.convert_record(original, now=NOW)
        self.assertEqual(flat['refresh_token'], MARKER)
        self.assertEqual(flat['access_token'], original['tokens']['access_token'])
        self.assertEqual(flat['id_token'], original['tokens']['id_token'])
        self.assertEqual(flat['account_id'], 'acct_synthetic')
        self.assertEqual(flat['expires_at'], NOW + 3600)
        self.assertEqual(set(flat), {'access_token', 'refresh_token', 'id_token', 'account_id', 'expires_at'})

    def test_valid_expiry_has_no_invented_lifetime_limit(self):
        self.assertEqual(auth.convert_record(record(exp=NOW + 2 * 86400), now=NOW)['expires_at'], NOW + 2 * 86400)
        with self.assertRaisesRegex(auth.ClosedFailure, 'invalid_expiry'):
            auth.convert_record(record(exp=NOW + 60), now=NOW)

    def test_claim_derived_account_and_ambiguity(self):
        original = record()
        del original['tokens']['account_id']
        self.assertEqual(auth.convert_record(original, now=NOW)['account_id'], 'acct_synthetic')
        original['tokens']['account_id'] = 'acct_other'
        with self.assertRaisesRegex(auth.ClosedFailure, 'invalid_account_mapping'):
            auth.convert_record(original, now=NOW)

    def test_conversion_writes_private_flat_file_and_refuses_existing(self):
        original = record(exp=int(__import__('time').time()) + 3600)
        self.write_source(original)
        args = ('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        result, out, err = self.call(*args)
        self.assertEqual((result, out, err), (0, 'AUTH_PREP_READY\n', ''))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.target.read_text())['refresh_token'], MARKER)
        before = self.target.read_bytes()
        result, _, err = self.call(*args)
        self.assertEqual(result, 1)
        self.assertEqual(err, 'AUTH_PREP_DESTINATION_EXISTS\n')
        self.assertEqual(self.target.read_bytes(), before)

    def test_invalid_records_and_failures_never_create_target_or_print_secret(self):
        variants = [record(exp=NOW - 1),
                    {'auth_mode': 'chatgpt', 'tokens': {'refresh_token': MARKER}},
                    {'auth_mode': 'api_key', 'tokens': record()['tokens']},
                    {**record(), 'tokens': {**record()['tokens'], 'account_id': 'other'}}]
        for value in variants:
            with self.subTest(value=str(value.get('auth_mode'))):
                with self.assertRaises(auth.ClosedFailure):
                    auth.convert_record(value, now=NOW)
        self.source.write_text(MARKER)
        os.chmod(self.source, 0o600)
        result, out, err = self.call('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        self.assertEqual(result, 1)
        self.assertTrue(err.startswith('AUTH_PREP_'))
        self.assertFalse(self.target.exists())
        self.source.write_bytes(b' ' * (auth.MAX_RECORD + 1))
        result, _, _ = self.call('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        self.assertEqual(result, 1)
        self.assertFalse(self.target.exists())

    def test_jwt_envelope_rejects_empty_and_malformed_segments(self):
        for field in ('access_token', 'id_token'):
            for token in ('.payload.signature', 'header.payload.', 'bad!.payload.signature',
                          'header.payload.bad!'):
                with self.subTest(field=field, token=token):
                    value = record()
                    value['tokens'][field] = token
                    with self.assertRaisesRegex(auth.ClosedFailure, 'invalid_token_claims'):
                        auth.convert_record(value, now=NOW)

    def test_atomic_private_output_and_exact_mode_under_restrictive_umask(self):
        self.write_source(record(exp=int(time.time()) + 3600))
        args = ('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        original_dump = auth.json.dump
        def interrupted_dump(value, stream, **kwargs):
            self.assertFalse(self.target.exists())
            stream.write(MARKER)
            raise ValueError('synthetic serialization failure')
        with patch.object(auth.json, 'dump', interrupted_dump):
            result, _, err = self.call(*args)
        self.assertEqual((result, err), (1, 'AUTH_PREP_OPERATION_FAILED\n'))
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.task.glob('.auth-prep-*')))
        self.assertIs(auth.json.dump, original_dump)
        old_umask = os.umask(0o277)
        try:
            result, out, err = self.call(*args)
        finally:
            os.umask(old_umask)
        self.assertEqual((result, out, err), (0, 'AUTH_PREP_READY\n', ''))
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertFalse(list(self.task.glob('.auth-prep-*')))

    def test_atomic_publish_never_replaces_racing_destination(self):
        self.write_source(record(exp=int(time.time()) + 3600))
        args = ('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        original_link = auth.os.link
        def racing_link(source, target, **kwargs):
            Path(target).write_text('existing synthetic session')
            return original_link(source, target, **kwargs)
        with patch.object(auth.os, 'link', racing_link):
            result, _, err = self.call(*args)
        self.assertEqual((result, err), (1, 'AUTH_PREP_DESTINATION_EXISTS\n'))
        self.assertEqual(self.target.read_text(), 'existing synthetic session')
        self.assertFalse(list(self.task.glob('.auth-prep-*')))

    def test_symlink_modes_parent_and_non_tmpfs_refused_without_mutation(self):
        original = record(exp=int(__import__('time').time()) + 3600)
        self.write_source(original)
        link = self.task / 'link.json'
        link.symlink_to(self.source)
        result, _, _ = self.call('convert', '--runtime-dir', str(self.task), '--source', str(link), '--output', str(self.target))
        self.assertEqual(result, 1)
        self.assertFalse(self.target.exists())
        os.chmod(self.source, 0o644)
        result, _, _ = self.call('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        self.assertEqual(result, 1)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o644)
        os.chmod(self.source, 0o600)
        os.chmod(self.task, 0o755)
        result, _, _ = self.call('convert', '--runtime-dir', str(self.task), '--source', str(self.source), '--output', str(self.target))
        self.assertEqual(result, 1)
        self.assertEqual(stat.S_IMODE(self.task.stat().st_mode), 0o755)
        os.chmod(self.task, 0o700)
        with tempfile.TemporaryDirectory() as disk:
            with self.assertRaises(auth.ClosedFailure):
                auth._safe_dir(Path(disk))
        self.assertFalse(self.target.exists())

    def test_standard_root_owned_sticky_tmpfs_parent_is_accepted(self):
        original_lstat = Path.lstat
        def parent_lstat(path):
            info = original_lstat(path)
            if path == Path('/dev/shm'):
                fields = list(info)
                fields[0] = stat.S_IFDIR | 0o1777
                fields[4] = 0
                return os.stat_result(fields)
            return info
        with patch.object(Path, 'lstat', parent_lstat):
            auth._safe_dir(self.task)

        def unsafe_parent_lstat(path):
            info = parent_lstat(path)
            if path == Path('/dev/shm'):
                fields = list(info)
                fields[0] = stat.S_IFDIR | 0o0777
                return os.stat_result(fields)
            return info
        with patch.object(Path, 'lstat', unsafe_parent_lstat):
            with self.assertRaisesRegex(auth.ClosedFailure, 'unsafe_runtime_path'):
                auth._safe_dir(self.task)

    def test_login_failure_and_timeout_cleanup_with_suppressed_child_output(self):
        shutil.rmtree(self.task)
        cli = Path('/usr/bin/true')
        class Failed:
            pid = os.getpid()
            def __init__(self, *args, **kwargs): pass
            def wait(self, timeout=None): return 1
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', Failed), patch.object(auth.os, 'killpg'):
            result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli))
        self.assertEqual((result, out, err), (1, '', 'AUTH_PREP_LOGIN_FAILED_OR_CANCELLED\n'))
        self.assertFalse(self.task.exists())
        class Timeout(Failed):
            waits = []
            def wait(self, timeout=None):
                self.waits.append(timeout)
                if timeout == 60:
                    raise subprocess.TimeoutExpired('synthetic ' + MARKER, timeout)
                return 0
        child = Timeout()
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', return_value=child), patch.object(auth.os, 'killpg') as killed:
            result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli), '--timeout', '60')
        self.assertEqual((result, out, err), (1, '', 'AUTH_PREP_LOGIN_TIMEOUT\n'))
        killed.assert_called_once_with(child.pid, auth.signal.SIGKILL)
        self.assertEqual(child.waits, [60, 5])
        self.assertFalse(self.task.exists())
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', Failed), patch.object(auth.os, 'killpg', side_effect=PermissionError('synthetic')):
            result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli))
        self.assertEqual((result, out, err), (1, '', 'AUTH_PREP_CLEANUP_REQUIRED\n'))
        self.assertTrue(self.task.exists())

    def test_sigint_during_spawn_reaps_child_before_task_cleanup(self):
        shutil.rmtree(self.task)
        cli = Path('/usr/bin/true')
        native_popen = subprocess.Popen
        child = None
        original_remove = auth._remove_task
        def spawn_and_interrupt(*args, **kwargs):
            nonlocal child
            child = native_popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            os.kill(os.getpid(), signal.SIGINT)
            return child
        def remove(task):
            self.assertIsNotNone(child.returncode)
            original_remove(task)
        try:
            with patch.object(auth, '_trusted_cli', return_value=str(cli)), \
                    patch.object(auth.subprocess, 'Popen', spawn_and_interrupt), \
                    patch.object(auth, '_remove_task', remove):
                result, out, err = self.call('login', '--runtime-dir', str(self.task),
                                             '--output', str(self.target), '--cli', str(cli))
            self.assertEqual((result, out, err), (1, '', 'AUTH_PREP_CANCELLED\n'))
            self.assertFalse(self.task.exists())
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def test_failed_leader_and_actual_signals_stop_synthetic_descendant(self):
        for termination in (None, signal.SIGTERM, signal.SIGHUP):
            with self.subTest(termination=termination):
                shutil.rmtree(self.task, ignore_errors=True)
                marker = Path('/dev/shm') / f'auth-signal-child-{os.getpid()}-{termination}'
                fake = Path('/dev/shm') / f'auth-signal-cli-{os.getpid()}-{termination}'
                child_code = 'import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)'
                ending = 'sleep 0.2; exit 1\n' if termination is None else 'wait\n'
                fake.write_text('#!/bin/sh\n' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(child_code) + ' ' + shlex.quote(str(marker)) + ' &\n' + ending)
                fake.chmod(0o700)
                driver = ('import importlib.util,sys; '
                          f's=importlib.util.spec_from_file_location("auth",{str(MODULE)!r}); '
                          'a=importlib.util.module_from_spec(s);s.loader.exec_module(a); '
                          f'a._trusted_cli=lambda path:{str(fake)!r}; '
                          'sys.exit(a.main(sys.argv[1:]))')
                runner = subprocess.Popen([sys.executable, '-c', driver, 'login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(fake)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                descendant = None
                try:
                    deadline = time.monotonic() + 4
                    while not marker.exists() and runner.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(marker.exists(), 'synthetic login did not start')
                    descendant = int(marker.read_text())
                    if termination is not None:
                        runner.send_signal(termination)
                    out, err = runner.communicate(timeout=6)
                    expected = b'AUTH_PREP_LOGIN_FAILED_OR_CANCELLED\n' if termination is None else b'AUTH_PREP_CANCELLED\n'
                    self.assertEqual((runner.returncode, out, err), (1, b'', expected))
                    self.assertFalse(self.task.exists())
                    self.assert_descendant_stopped(descendant)
                finally:
                    if runner.poll() is None:
                        runner.kill()
                        runner.communicate(timeout=5)
                    if descendant is not None:
                        status = Path(f'/proc/{descendant}/stat')
                        if status.exists() and status.read_text().split()[2] != 'Z':
                            os.kill(descendant, signal.SIGKILL)
                    fake.unlink(missing_ok=True)
                    marker.unlink(missing_ok=True)

    def test_restrictive_child_cleanup_and_retained_state_status(self):
        shutil.rmtree(self.task)
        cli = Path('/usr/bin/true')
        class Failed:
            pid = os.getpid()
            def __init__(self, args, **kwargs):
                locked = kwargs['cwd'] / 'locked'
                locked.mkdir(mode=0o700)
                (locked / 'placeholder').write_text(MARKER)
                locked.chmod(0)
            def wait(self, timeout=None):
                return 1
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', Failed), patch.object(auth.os, 'killpg'):
            result, _, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli))
        self.assertEqual((result, err), (1, 'AUTH_PREP_LOGIN_FAILED_OR_CANCELLED\n'))
        self.assertFalse(self.task.exists())
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', Failed), patch.object(auth.os, 'killpg'), patch.object(auth.shutil, 'rmtree', side_effect=PermissionError('synthetic')):
            result, _, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli))
        self.assertEqual((result, err), (1, 'AUTH_PREP_CLEANUP_REQUIRED\n'))
        self.assertTrue(self.task.exists())
        (self.task / 'locked').chmod(0o700)

    def test_login_rejects_staging_name_before_child_launch(self):
        shutil.rmtree(self.task)
        cli = Path('/usr/bin/true')
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen') as child:
            result, _, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.task / 'native.json'), '--cli', str(cli))
        self.assertEqual((result, err), (1, 'AUTH_PREP_UNSAFE_FILE_PATH\n'))
        child.assert_not_called()
        self.assertFalse(self.task.exists())

    def test_login_env_excludes_billing_keys_and_child_streams_are_suppressed(self):
        env = auth._login_env(self.task, {'OPENAI_API_KEY': MARKER, 'CODEX_API_KEY': MARKER, 'DISPLAY': ':1'})
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertNotIn('CODEX_API_KEY', env)
        self.assertEqual(env['DISPLAY'], ':1')
        self.assertEqual(env['CODEX_HOME'], str(self.task / 'codex'))

    def test_real_synthetic_child_output_is_never_forwarded(self):
        shutil.rmtree(self.task)
        fake = Path('/dev/shm') / ('chatgpt-auth-fake-' + str(os.getpid()))
        try:
            fake.write_text('#!/bin/sh\nprintf "' + MARKER + '"\nprintf "' + MARKER + '" >&2\nexit 1\n')
            os.chmod(fake, 0o700)
            with patch.object(auth, '_trusted_cli', return_value=str(fake)):
                result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(fake))
            self.assertEqual((result, out, err), (1, '', 'AUTH_PREP_LOGIN_FAILED_OR_CANCELLED\n'))
            self.assertFalse(self.task.exists())
        finally:
            fake.unlink(missing_ok=True)

    def test_simulated_native_login_creates_flat_record_only_in_task_tmpfs(self):
        shutil.rmtree(self.task)
        cli = Path('/usr/bin/true')
        original = record(exp=int(__import__('time').time()) + 3600)
        runtime_dir = self.task
        class Success:
            pid = os.getpid()
            def __init__(self, args, **kwargs):
                self.env = kwargs['env']
                assert args == [str(cli), 'login']
                assert kwargs['stdout'] == subprocess.DEVNULL
                assert kwargs['stderr'] == subprocess.DEVNULL
                assert 'OPENAI_API_KEY' not in self.env
                assert self.env['XDG_RUNTIME_DIR'] == str(runtime_dir)
            def wait(self, timeout=None):
                native = Path(self.env['CODEX_HOME']) / 'auth.json'
                native.write_text(json.dumps(original))
                os.chmod(native, 0o600)
                return 0
        with patch.object(auth, '_trusted_cli', return_value=str(cli)), patch.object(auth.subprocess, 'Popen', Success), patch.object(auth.os, 'killpg') as killed:
            result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(cli))
        self.assertEqual((result, out, err), (0, 'AUTH_PREP_READY\n', ''))
        killed.assert_called_once_with(Success.pid, signal.SIGKILL)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(self.target.read_text())['refresh_token'], MARKER)
        self.assertFalse((self.task / 'native.json').exists())

    def test_successful_login_stops_surviving_session_descendant(self):
        shutil.rmtree(self.task)
        fake = Path('/dev/shm') / f'auth-success-cli-{os.getpid()}'
        marker = Path('/dev/shm') / f'auth-success-child-{os.getpid()}'
        payload = json.dumps(record(exp=int(time.time()) + 3600))
        child_code = 'import os,sys,time;open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)'
        writer = 'import os,pathlib,sys;pathlib.Path(os.environ["CODEX_HOME"],"auth.json").write_text(sys.argv[1]);os.chmod(pathlib.Path(os.environ["CODEX_HOME"],"auth.json"),0o600)'
        fake.write_text('#!/bin/sh\n' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(child_code) + ' ' + shlex.quote(str(marker)) + ' &\n' + shlex.quote(sys.executable) + ' -c ' + shlex.quote(writer) + ' ' + shlex.quote(payload) + '\nsleep 0.2\n')
        fake.chmod(0o700)
        descendant = None
        try:
            with patch.object(auth, '_trusted_cli', return_value=str(fake)):
                result, out, err = self.call('login', '--runtime-dir', str(self.task), '--output', str(self.target), '--cli', str(fake))
            self.assertEqual((result, out, err), (0, 'AUTH_PREP_READY\n', ''))
            descendant = int(marker.read_text())
            self.assert_descendant_stopped(descendant)
            self.assertEqual(json.loads(self.target.read_text())['refresh_token'], MARKER)
        finally:
            if descendant is not None:
                status = Path(f'/proc/{descendant}/stat')
                if status.exists() and status.read_text().split()[2] != 'Z':
                    os.kill(descendant, signal.SIGKILL)
            fake.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)

    def assert_descendant_stopped(self, pid):
        status = Path(f'/proc/{pid}/stat')
        deadline = time.monotonic() + 3
        while status.exists() and time.monotonic() < deadline:
            if status.read_text().split()[2] == 'Z':
                return
            time.sleep(0.02)
        if status.exists():
            self.assertEqual(status.read_text().split()[2], 'Z')


if __name__ == '__main__':
    unittest.main()
