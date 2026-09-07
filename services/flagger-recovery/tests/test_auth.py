import tempfile
import unittest
from pathlib import Path

from flagger_recovery.auth import (
    MIN_TOKEN_LENGTH,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
    TOKEN_HEADER,
    TokenUnavailable,
    load_token,
    presented_token,
    token_matches,
)

TOKEN = "s" * MIN_TOKEN_LENGTH

class LoadTokenTests(unittest.TestCase):
    def test_reads_a_mounted_secret_file_and_strips_the_trailing_newline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(TOKEN + "\n", encoding="utf-8")
            self.assertEqual(load_token(path=str(path), environ={}), TOKEN)
            self.assertEqual(load_token(environ={TOKEN_FILE_ENV: str(path)}), TOKEN)

    def test_reads_the_environment_when_no_file_is_configured(self):
        self.assertEqual(load_token(environ={TOKEN_ENV: TOKEN}), TOKEN)

    def test_file_wins_over_the_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("f" * MIN_TOKEN_LENGTH, encoding="utf-8")
            self.assertEqual(
                load_token(path=str(path), environ={TOKEN_ENV: TOKEN}), "f" * MIN_TOKEN_LENGTH
            )

    def test_no_token_refuses_rather_than_returning_empty(self):
        with self.assertRaises(TokenUnavailable):
            load_token(environ={})

    def test_short_token_is_refused(self):
        with self.assertRaises(TokenUnavailable):
            load_token(environ={TOKEN_ENV: "short"})

    def test_unreadable_file_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TokenUnavailable):
                load_token(path=str(Path(directory) / "absent"), environ={})

class PresentedTokenTests(unittest.TestCase):
    def test_header_is_preferred_over_payload_metadata(self):
        headers = {TOKEN_HEADER: "from-header"}
        payload = {"metadata": {"token": "from-metadata"}}
        self.assertEqual(presented_token(headers, payload), "from-header")

    def test_falls_back_to_metadata_token(self):
        self.assertEqual(presented_token({}, {"metadata": {"token": TOKEN}}), TOKEN)
        self.assertEqual(presented_token(None, {"metadata": {"token": TOKEN}}), TOKEN)

    def test_missing_or_wrongly_typed_token_is_none(self):
        self.assertIsNone(presented_token({}, {}))
        self.assertIsNone(presented_token({}, {"metadata": {}}))
        self.assertIsNone(presented_token({}, {"metadata": "not-an-object"}))
        self.assertIsNone(presented_token({}, {"metadata": {"token": 1234}}))

class TokenMatchesTests(unittest.TestCase):
    def test_exact_match_only(self):
        self.assertTrue(token_matches(TOKEN, TOKEN))
        self.assertFalse(token_matches(TOKEN, TOKEN + "x"))
        self.assertFalse(token_matches(TOKEN, TOKEN[:-1]))

    def test_missing_empty_or_non_string_is_a_mismatch(self):
        for presented in (None, "", 1234, b"bytes"):
            with self.subTest(presented=presented):
                self.assertFalse(token_matches(TOKEN, presented))
