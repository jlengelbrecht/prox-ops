"""Enforces the README's claim that this package never shells out or
evals: no ``subprocess``/``pty`` import, no ``eval``/``exec`` call, and no
``os.system``/``popen``/``spawn``/``execv*`` call anywhere under the service
directory."""

import ast
import pathlib
import unittest

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent

_BANNED_IMPORT_MODULES = {"subprocess", "pty"}
_BANNED_CALL_NAMES = {"eval", "exec"}
_BANNED_CALL_ATTRS = {"system", "popen", "spawn", "execv", "execvp"}

def _find_violations():
    violations = []
    for path in sorted(SERVICE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(alias.name in _BANNED_IMPORT_MODULES for alias in node.names):
                violations.append((str(path), node.lineno))
            elif isinstance(node, ast.ImportFrom) and node.module in _BANNED_IMPORT_MODULES:
                violations.append((str(path), node.lineno))
            elif isinstance(node, ast.Call):
                func = node.func
                if getattr(func, "id", None) in _BANNED_CALL_NAMES or getattr(func, "attr", None) in _BANNED_CALL_ATTRS:
                    violations.append((str(path), node.lineno))
    return violations

class NoExecPolicyTests(unittest.TestCase):
    def test_no_subprocess_pty_eval_exec_or_os_exec_family(self):
        violations = _find_violations()
        self.assertEqual(violations, [], f"policy violations: {violations}")

if __name__ == "__main__":
    unittest.main()
