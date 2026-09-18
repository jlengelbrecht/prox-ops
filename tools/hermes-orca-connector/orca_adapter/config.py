"""Local configuration and key loading.

Configuration is local operator input only. Nothing in it may ever be supplied
by a remote peer: the executable, the optional pre-command gate and the key
file are all fixed before any Orca command runs.

The product has no dependency on any development-machine guard. The optional
``preflight_argv`` is a trusted local gate the operator chooses; the prox-ops
controller runs its own exact-repo secret guard through
``controller/proxops_qa.py``, which sets this field and refuses to start
without the guard. See README "Controller policy vs runtime product".
"""

from __future__ import annotations

import math
import os
import stat
from dataclasses import dataclass

from .errors import CONFIG_ERROR, AdapterError

# Exact runtime pins. Unsupported builds fail closed until qualified.
SUPPORTED_APP_VERSION = "1.4.205"
SUPPORTED_SCHEMA_VERSION = 1

DEFAULT_EXECUTABLE = "orca-ide"  # never falls back to a bare ``orca`` shim
KEY_SIZE = 32

# Documented child bounds; every constructor path must keep inside them.
DEFAULT_TIMEOUT_SECONDS = 20.0
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_TIMEOUT_SECONDS = DEFAULT_TIMEOUT_SECONDS
MAX_MAX_OUTPUT_BYTES = DEFAULT_MAX_OUTPUT_BYTES

ENV_EXECUTABLE = "HERMES_ORCA_EXECUTABLE"
ENV_KEY_FILE = "HERMES_ORCA_KEY_FILE"
ENV_COMMAND_GATE = "HERMES_ORCA_COMMAND_GATE"


def _valid_argv(argv: tuple[str, ...]) -> bool:
    return (
        isinstance(argv, tuple)
        and len(argv) > 0
        and all(isinstance(a, str) and a for a in argv)
        and not argv[0].startswith("-")
    )


@dataclass(frozen=True)
class Config:
    """Adapter configuration.

    ``executable`` is the argv prefix of the selected Orca CLI; production is
    ``("orca-ide",)``. Tests inject a synthetic peer here. ``preflight_argv``
    is an optional local gate run (as argv, never a shell) before EVERY Orca
    command; it must exit 0 or the command is refused. Empty means no gate is
    configured. Tests inject a harmless synthetic gate through the same field.

    ``timeout_seconds`` and ``max_output_bytes`` may only be tightened: they
    must be finite, positive and at most the documented 20 s / 4 MiB, on every
    constructor path (``config_error/bad_bounds`` otherwise).
    """

    executable: tuple[str, ...]
    key_file: str
    preflight_argv: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES

    def __post_init__(self) -> None:
        if not _valid_argv(self.executable):
            raise AdapterError(CONFIG_ERROR, "config", "bad_executable")
        if self.preflight_argv and (not _valid_argv(self.preflight_argv) or not os.path.isabs(self.preflight_argv[0])):
            raise AdapterError(CONFIG_ERROR, "config", "bad_gate")
        timeout, max_bytes = self.timeout_seconds, self.max_output_bytes
        if (
            type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT_SECONDS
            or type(max_bytes) is not int or not 0 < max_bytes <= MAX_MAX_OUTPUT_BYTES
        ):
            raise AdapterError(CONFIG_ERROR, "config", "bad_bounds")


def check_gate_file(path: str) -> None:
    """A gate named by the environment must be a trusted local executable file.

    Regular file (not a symlink), owned by the current user, executable by that
    owner and not writable by group or others. Anything else is
    ``config_error/gate_unsafe``. The gate is spawned directly as ``argv[0]``,
    so a file without the owner execute bit is refused here rather than on the
    first command.
    """
    try:
        st = os.lstat(path)
    except OSError:
        raise AdapterError(CONFIG_ERROR, "config", "gate_unsafe") from None
    if (
        not stat.S_ISREG(st.st_mode)
        or st.st_uid != os.getuid()
        or not st.st_mode & stat.S_IXUSR
        or stat.S_IMODE(st.st_mode) & 0o022
    ):
        raise AdapterError(CONFIG_ERROR, "config", "gate_unsafe")


def config_from_env(environ=None) -> Config:
    """Build a Config from local environment variables; fail closed on gaps."""
    env = os.environ if environ is None else environ
    key_file = env.get(ENV_KEY_FILE, "")
    if not key_file:
        raise AdapterError(CONFIG_ERROR, "config", "missing_setting")
    if not os.path.isabs(key_file):
        raise AdapterError(CONFIG_ERROR, "config", "relative_path")
    executable = env.get(ENV_EXECUTABLE, DEFAULT_EXECUTABLE)
    if not executable or executable.startswith("-"):
        raise AdapterError(CONFIG_ERROR, "config", "bad_executable")
    gate = env.get(ENV_COMMAND_GATE, "")
    preflight_argv: tuple[str, ...] = ()
    if gate:
        if not os.path.isabs(gate):
            raise AdapterError(CONFIG_ERROR, "config", "relative_path")
        check_gate_file(gate)
        preflight_argv = (gate,)
    return Config(executable=(executable,), key_file=key_file, preflight_argv=preflight_argv)


def load_key(path: str) -> bytes:
    """Read the local HMAC key with strict file checks. Never creates or prints it.

    The file must be a regular file (not a symlink), owned by the current user,
    mode 0600 and exactly 32 bytes long.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise AdapterError(CONFIG_ERROR, "key", "key_unreadable") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AdapterError(CONFIG_ERROR, "key", "key_not_regular")
        if st.st_uid != os.getuid():
            raise AdapterError(CONFIG_ERROR, "key", "key_wrong_owner")
        if stat.S_IMODE(st.st_mode) != 0o600:
            raise AdapterError(CONFIG_ERROR, "key", "key_bad_mode")
        if st.st_size != KEY_SIZE:
            raise AdapterError(CONFIG_ERROR, "key", "key_bad_size")
        data = os.read(fd, KEY_SIZE + 1)
    finally:
        os.close(fd)
    if len(data) != KEY_SIZE:
        raise AdapterError(CONFIG_ERROR, "key", "key_bad_size")
    return data
