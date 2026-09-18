"""Local config, key file and gate checks, opaque handles, display text."""

import os
import tempfile
import unittest

import _support as S
from orca_adapter import errors
from orca_adapter.config import check_gate_file, config_from_env, load_key
from orca_adapter.handles import REDACTED, HandleFactory, display_text, is_wellformed_handle


class KeyFileTests(unittest.TestCase):
    def setUp(self):
        self.h = S.Harness()
        self.addCleanup(self.h.close)

    def test_valid_key_loads(self):
        self.assertEqual(load_key(self.h.key_file), b"\x11" * 32)

    def _expect(self, path, detail):
        with self.assertRaises(errors.AdapterError) as cm:
            load_key(path)
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, detail))
        self.assertEqual(cm.exception.exit_code, errors.EXIT_CONFIG)

    def test_unsafe_key_files_are_refused(self):
        self._expect(os.path.join(self.h.dir, "missing"), "key_unreadable")
        os.chmod(self.h.key_file, 0o640)
        self._expect(self.h.key_file, "key_bad_mode")
        os.chmod(self.h.key_file, 0o600)
        link = os.path.join(self.h.dir, "link")
        os.symlink(self.h.key_file, link)
        self._expect(link, "key_unreadable")  # O_NOFOLLOW refuses the symlink
        short = os.path.join(self.h.dir, "short")
        fd = os.open(short, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"\x11" * 31)
        os.close(fd)
        self._expect(short, "key_bad_size")
        self._expect(self.h.dir, "key_not_regular")

    def test_foreign_owned_key_and_gate_are_refused(self):
        # Otherwise-valid fixtures whose stat result names another uid: the key is refused on the
        # owner boundary alone (``fstat`` on the opened descriptor), the gate likewise (``lstat``).
        # ``test_snapshot`` proves the same key refusal stops ``adapter.snapshot`` before any spawn.
        from unittest import mock
        foreign_uid = os.getuid() + 1

        def foreign(st: os.stat_result) -> os.stat_result:
            fields = list(st)
            fields[4] = foreign_uid
            return os.stat_result(tuple(fields))

        real_fstat, real_lstat = os.fstat, os.lstat
        with mock.patch.object(os, "fstat", lambda fd: foreign(real_fstat(fd))):
            self._expect(self.h.key_file, "key_wrong_owner")
        gate = os.path.join(self.h.dir, "gate")
        with open(gate, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(gate, 0o755)
        env = {"HERMES_ORCA_KEY_FILE": self.h.key_file, "HERMES_ORCA_COMMAND_GATE": gate}
        self.assertEqual(config_from_env(env).preflight_argv, (gate,))
        with mock.patch.object(os, "lstat", lambda path: foreign(real_lstat(path))), self.assertRaises(errors.AdapterError) as cm:
            config_from_env(env)
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, "gate_unsafe"))
        self.assertEqual(load_key(self.h.key_file), b"\x11" * 32)  # the real owner still loads it

    def test_config_from_env(self):
        env = {"HERMES_ORCA_KEY_FILE": self.h.key_file}
        cfg = config_from_env(env)
        self.assertEqual(cfg.executable, ("orca-ide",))
        self.assertEqual(cfg.preflight_argv, ())  # the product carries no repository guard
        self.assertEqual((cfg.timeout_seconds, cfg.max_output_bytes), (20.0, 4 * 1024 * 1024))
        cfg = config_from_env({**env, "HERMES_ORCA_COMMAND_GATE": S.FAKE_PREFLIGHT})
        self.assertEqual(cfg.preflight_argv, (S.FAKE_PREFLIGHT,))
        for bad, detail in (
            ({}, "missing_setting"),
            ({"HERMES_ORCA_KEY_FILE": "rel/key"}, "relative_path"),
            ({**env, "HERMES_ORCA_EXECUTABLE": ""}, "bad_executable"),
            ({**env, "HERMES_ORCA_EXECUTABLE": "-x"}, "bad_executable"),
            ({**env, "HERMES_ORCA_COMMAND_GATE": "gate.py"}, "relative_path"),
            ({**env, "HERMES_ORCA_COMMAND_GATE": os.path.join(self.h.dir, "absent")}, "gate_unsafe"),
            ({**env, "HERMES_ORCA_COMMAND_GATE": self.h.dir}, "gate_unsafe"),
        ):
            with self.assertRaises(errors.AdapterError, msg=bad) as cm:
                config_from_env(bad)
            self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, detail))

    def test_gate_file_must_be_trusted_local(self):
        gate = os.path.join(self.h.dir, "gate")
        with open(gate, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        env = {"HERMES_ORCA_KEY_FILE": self.h.key_file, "HERMES_ORCA_COMMAND_GATE": gate}
        os.chmod(gate, 0o755)
        self.assertEqual(config_from_env(env).preflight_argv, (gate,))
        for mode in (0o775, 0o757, 0o666):
            os.chmod(gate, mode)
            with self.assertRaises(errors.AdapterError, msg=oct(mode)) as cm:
                config_from_env(env)
            self.assertEqual(cm.exception.detail, "gate_unsafe")
        os.chmod(gate, 0o755)
        link = os.path.join(self.h.dir, "gate-link")
        os.symlink(gate, link)
        with self.assertRaises(errors.AdapterError) as cm:
            config_from_env({**env, "HERMES_ORCA_COMMAND_GATE": link})
        self.assertEqual(cm.exception.detail, "gate_unsafe")


class GateExecuteBit(unittest.TestCase):
    """PR1362 triage F1/F3: an owner-writable-only gate silently passed
    configuration and then failed every command as ``orca_exec_failed`` (fail
    closed, but a late and confusing failure). Fixed by requiring
    ``stat.S_IXUSR`` on the ``lstat`` result."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir))
        self.gate = os.path.join(self.dir, "gate")
        with open(self.gate, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        self.key = os.path.join(self.dir, "key")
        fd = os.open(self.key, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"\x11" * 32)
        os.close(fd)

    def _refused(self, mode):
        os.chmod(self.gate, mode)
        with self.assertRaises(errors.AdapterError, msg=oct(mode)) as cm:
            check_gate_file(self.gate)
        self.assertEqual((cm.exception.code, cm.exception.stage, cm.exception.detail), (errors.CONFIG_ERROR, "config", "gate_unsafe"))
        self.assertEqual(cm.exception.exit_code, errors.EXIT_CONFIG)

    def test_owner_execute_bit_is_required(self):
        for mode in (0o600, 0o644, 0o640, 0o400, 0o604):  # readable but not owner-executable
            self._refused(mode)
        for mode in (0o700, 0o750, 0o755, 0o500, 0o711):  # owner-executable, not group/other writable
            os.chmod(self.gate, mode)
            check_gate_file(self.gate)  # accepted

    def test_execute_bit_does_not_relax_the_other_checks(self):
        for mode in (0o777, 0o775, 0o757, 0o722):  # executable but group/other writable
            self._refused(mode)
        os.chmod(self.gate, 0o755)
        link = os.path.join(self.dir, "link")
        os.symlink(self.gate, link)
        with self.assertRaises(errors.AdapterError) as cm:
            check_gate_file(link)
        self.assertEqual(cm.exception.detail, "gate_unsafe")
        with self.assertRaises(errors.AdapterError) as cm:
            check_gate_file(self.dir)
        self.assertEqual(cm.exception.detail, "gate_unsafe")

    def test_config_from_env_surfaces_the_refusal_before_any_spawn(self):
        env = {"HERMES_ORCA_KEY_FILE": self.key, "HERMES_ORCA_COMMAND_GATE": self.gate}
        os.chmod(self.gate, 0o600)
        with self.assertRaises(errors.AdapterError) as cm:
            config_from_env(env)
        self.assertEqual((cm.exception.code, cm.exception.detail), (errors.CONFIG_ERROR, "gate_unsafe"))
        os.chmod(self.gate, 0o700)
        self.assertEqual(config_from_env(env).preflight_argv, (self.gate,))


class HandleTests(unittest.TestCase):
    def test_domain_separation_and_determinism(self):
        f = HandleFactory(b"k" * 32)
        g = HandleFactory(b"j" * 32)
        p, c, e = f.project("github:acme/widgets"), f.coordinator("term_pm"), f.epoch("run_aaa", "term_pm")
        self.assertEqual(p, f.project("github:acme/widgets"))
        self.assertNotEqual(p, g.project("github:acme/widgets"))
        self.assertNotEqual(f.project("term_pm")[4:], c[3:])  # same input, different domain
        self.assertNotEqual(e, f.epoch("run_aab", "term_pm"))
        self.assertNotEqual(e, f.epoch("run_aaa", "term_pn"))
        self.assertNotEqual(f.epoch("ab", "c"), f.epoch("a", "bc"))  # length-prefixed parts
        self.assertNotEqual(f.epoch("a\x00b", "c"), f.epoch("a", "b\x00c"))  # NUL cannot shift a part
        self.assertNotEqual(f.epoch("a\x00", "c"), f.epoch("a", "\x00c"))
        for value, domain in ((p, "project"), (c, "coordinator"), (e, "epoch")):
            self.assertTrue(is_wellformed_handle(value, domain))
        self.assertFalse(is_wellformed_handle(c, "project"))
        self.assertFalse(is_wellformed_handle("prj_" + "z" * 32, "project"))
        self.assertFalse(is_wellformed_handle(None, "project"))
        for suffix in ("\n", "\r\n", " ", "\x00"):  # whole-string shape: no control suffix passes
            self.assertFalse(is_wellformed_handle(p + suffix, "project"), repr(suffix))
        self.assertFalse(is_wellformed_handle("\n" + p, "project"))
        for bad in ("\ud800", "a\udfffb"):  # ids reach the HMAC only after the projection refused surrogates
            with self.assertRaises(UnicodeEncodeError):
                f.project(bad)
        S.assert_no_canaries(self, p + c + e)
        with self.assertRaises(ValueError):
            HandleFactory(b"short")

    def test_display_text_keeps_slashes_and_redacts_secret_shapes(self):
        for good, shown in (
            ("acme/widgets", "acme/widgets"), ("  team / infra  ", "team / infra"),
            ("line\x00one\x1b[31m", "lineone[31m"), ("release-2026.09", "release-2026.09"),
            ("AKIA short", "AKIA short"), ("mail me: a@b.c", "mail me: a@b.c"),
            ("docs/ops/runbooks", "docs/ops/runbooks"), ("1/2 done", "1/2 done"), ("widgets (v2)", "widgets (v2)"),
            ("re: notes", "re: notes"), ("ab\udcffc", "abc"),  # a lone surrogate is not printable and is dropped
        ):
            self.assertEqual(display_text(good), shown)
        self.assertEqual(len(display_text("long name " * 50)), 64)
        for bad in (
            "https://user:pw@github.com/x", "ssh://git@host/x", "git@github.com:a/b", "user:CANARY@host",
            "a/git@github.com:acme/widgets", "x@git@github.com:a/b", "widgets (git@github.com:acme/widgets)",
            "ghp_abcdefghijklmnop1234", "sk-abcdefghijklmnop", "xoxb-1234567890-abcdef",
            "AKIAIOSFODNN7EXAMPLE", "deploy AKIAIOSFODNN7EXAMPLE key", "ASIAIOSFODNN7EXAMPLE",
            "AIzaSyA-abcdefghijklmnopqrstuvwxyz01234", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig",
            "-----BEGIN RSA PRIVATE KEY-----", "token=abc", "Bearer: x", "A" * 40,
            "aGVsbG8gd29ybGQgaGVsbG8gd29ybGQgaGVsbG8=",
            # filesystem paths: absolute, home-relative, drive and UNC forms, alone or inside a name
            "/home/alice/private/client", "/tmp/x", "/x", "widgets (/srv/x)", "see /etc/passwd now",
            "repo=/srv/git/x", "~/projects/x", "C:\\Users\\alice\\x", "c:/x", "\\\\server\\share\\x", "file:///x",
        ):
            self.assertEqual(display_text(bad), REDACTED, bad)
        self.assertEqual(display_text(None), "")
        self.assertEqual(display_text(""), "")
        self.assertEqual(display_text(42), "")


class FileUriRedaction(unittest.TestCase):
    """PR1362 triage F5: single-slash ``file:/path`` and ``label:/multi/segment``
    forms leaked past redaction (only ``scheme://`` and the two-slash ``file://``
    form were caught). Fixed by an explicit ``file:/`` rule and adding ``:`` to
    the absolute-path lookbehind."""

    def test_file_uri_forms_are_redacted(self):
        for bad in (
            "file:/tmp/key", "file:/key", "file:///x", "file://host/share/x", "FILE:/srv/x", "File:/x",
            "key file:/srv/keys/orca", "see file:/x now", "(file:/x)",
            # any ``label:`` immediately followed by an absolute multi-segment path
            "key:/srv/git/x", "repo:/home/alice/x", "path:/etc/ssl/private", "x:/srv/y/",
        ):
            self.assertEqual(display_text(bad), REDACTED, bad)

    def test_non_path_uri_like_text_still_displays(self):
        for good, shown in (
            ("urn:example:secret", "urn:example:secret"), ("re: notes", "re: notes"),
            ("team:/infra", "team:/infra"),  # single segment after a label keeps the existing two-segment rule
            ("10:30/day", "10:30/day"), ("acme/widgets", "acme/widgets"),
            ("profile", "profile"), ("filed under ops", "filed under ops"), ("file: notes", "file: notes"),
            ("docs/ops/runbooks", "docs/ops/runbooks"), ("widgets (v2)", "widgets (v2)"),
        ):
            self.assertEqual(display_text(good), shown, good)

    def test_existing_redactions_unchanged(self):
        for bad in ("https://user:pw@github.com/x", "git@github.com:a/b", "/tmp/x", "/x", "widgets (/srv/x)",
                    "~/projects/x", "C:\\Users\\alice\\x", "c:/x", "\\\\server\\share\\x", "token=abc", "A" * 40,
                    "mailto:a@b.c"):  # pre-existing: the ``user:pass@`` rule already matches ``mailto:a@``
            self.assertEqual(display_text(bad), REDACTED, bad)


if __name__ == "__main__":
    unittest.main()
