"""Compatibility token contract used by the app-backed gateway."""

import ast
import base64
import sys
from pathlib import Path

import pytest

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))
import server  # noqa: E402


@pytest.mark.parametrize('length', [32, 33, 64])
def test_canonical_random_byte_tokens_are_accepted(length):
    token = base64.urlsafe_b64encode(bytes(range(length))).rstrip(b'=').decode()
    assert server._valid_token(token)


@pytest.mark.parametrize('value', [None, '', 'a' * 42, 'a' * 43, '!' * 43,
                                   'a' * 44 + '=', 'A' * 43 + '/',
                                   base64.urlsafe_b64encode(bytes(31)).rstrip(b'=').decode()])
def test_invalid_or_noncanonical_tokens_are_rejected(value):
    assert not server._valid_token(value)


def test_compatibility_module_has_no_server_entrypoint_or_filesystem_operations():
    source = (SERVICE / 'server.py').read_text()
    tree = ast.parse(source)
    names = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert names == {'_valid_token'}
    assert all(isinstance(node, (ast.Expr, ast.Import, ast.Assign, ast.FunctionDef))
               for node in tree.body)
    assert 'open(' not in source and 'logging.disable' not in source
