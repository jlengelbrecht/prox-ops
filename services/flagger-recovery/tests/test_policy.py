"""Enforces the README's claim that this package never shells out or
evals: no ``subprocess``/``pty`` import, no ``eval``/``exec`` call, no
``os``/``posix``/``subprocess``/``pty`` process-spawning call (``system``,
``popen``, any ``exec*``/``spawn*`` variant, ``posix_spawn[p]``), and no
alias of one of those (``from os import execve as run``) called by its
alias."""

import ast
import pathlib
import unittest

SERVICE_ROOT = pathlib.Path(__file__).resolve().parent.parent

_BANNED_IMPORT_MODULES = {"subprocess", "pty"}
_BANNED_ATTR_MODULES = {"os", "posix", "subprocess", "pty"}
_BANNED_CALL_NAMES = {"eval", "exec"}
_BANNED_PROCESS_EXACT_NAMES = {"system", "popen", "posix_spawn", "posix_spawnp"}

def _is_banned_process_name(name: str) -> bool:
    return name.startswith(("exec", "spawn")) or name in _BANNED_PROCESS_EXACT_NAMES

def violations_in_source(source: str, filename: str) -> list:
    """Return every (filename, lineno) policy violation in ``source``.

    Pure and filesystem-free so both the test suite and an external gate
    script can import and call it directly. Tracks names imported from a
    banned module under any alias (``from os import execve as run``) and
    flags calls through that alias, in addition to direct attribute calls
    (``os.execve(...)``) and bare banned builtins (``eval``/``exec``).
    """
    tree = ast.parse(source, filename=filename)

    aliased_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _BANNED_ATTR_MODULES:
            for alias in node.names:
                if _is_banned_process_name(alias.name):
                    aliased_names[alias.asname or alias.name] = alias.name

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(alias.name in _BANNED_IMPORT_MODULES for alias in node.names):
            violations.append((filename, node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module in _BANNED_IMPORT_MODULES:
            violations.append((filename, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in _BANNED_CALL_NAMES or func.id in aliased_names:
                    violations.append((filename, node.lineno))
            elif isinstance(func, ast.Attribute):
                if func.attr in _BANNED_CALL_NAMES or _is_banned_process_name(func.attr):
                    violations.append((filename, node.lineno))
    return violations

def _find_violations():
    violations = []
    for path in sorted(SERVICE_ROOT.rglob("*.py")):
        violations.extend(violations_in_source(path.read_text(encoding="utf-8"), str(path)))
    return violations

class NoExecPolicyTests(unittest.TestCase):
    def test_no_subprocess_pty_eval_exec_or_os_exec_family(self):
        violations = _find_violations()
        self.assertEqual(violations, [], f"policy violations: {violations}")

    def test_aliased_import_from_os_is_detected(self):
        source = "from os import execve as run\nrun(1)\n"
        self.assertTrue(violations_in_source(source, "x.py"))

    def test_unaliased_import_from_os_is_detected(self):
        source = "from os import execve\nexecve(1)\n"
        self.assertTrue(violations_in_source(source, "x.py"))

    def test_direct_os_spawnv_call_is_detected(self):
        source = "import os\nos.spawnv(os.P_WAIT, '/bin/sh', ['sh'])\n"
        self.assertTrue(violations_in_source(source, "x.py"))

    def test_os_posix_spawn_and_execvpe_are_detected(self):
        self.assertTrue(violations_in_source("import os\nos.posix_spawn('/bin/sh', ['sh'], {})\n", "x.py"))
        self.assertTrue(violations_in_source("import os\nos.execvpe('sh', ['sh'], {})\n", "x.py"))

    def test_os_popen_and_system_are_detected(self):
        self.assertTrue(violations_in_source("import os\nos.popen('ls')\n", "x.py"))
        self.assertTrue(violations_in_source("import os\nos.system('ls')\n", "x.py"))

    def test_unrelated_alias_import_is_not_flagged(self):
        source = "from os import path as p\np.join('a', 'b')\n"
        self.assertEqual(violations_in_source(source, "x.py"), [])

    def test_benign_code_has_no_violations(self):
        source = "def add(a, b):\n    return a + b\n"
        self.assertEqual(violations_in_source(source, "x.py"), [])

if __name__ == "__main__":
    unittest.main()
