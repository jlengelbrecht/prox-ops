"""Offline, synthetic-only checks for the offline conversion helper."""
import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
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
        self.runtime_environment = patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.task),
                                                           'DISPLAY': ''})
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


if __name__ == '__main__':
    unittest.main()
