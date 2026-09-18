"""Bounded failure type. Carries fixed codes and stage names only.

A code is one of the constants below; a stage is the adapter's own name for the
command that failed (for example ``project list``). Neither may ever carry
subprocess output, paths, ids, or an exception message from another library.
"""

from __future__ import annotations

# Exit codes required by the spec matrix.
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILURE = 2  # executable failure, timeout, oversized output, malformed
EXIT_UNSUPPORTED = 3  # unsupported_orca_runtime
EXIT_NOT_FOUND = 4  # unknown opaque project handle
EXIT_CONFIG = 5  # unsafe key file, bad config, rejected preflight

# Failure codes. Keep this set closed: the CLI only ever prints one of these.
ORCA_EXEC_FAILED = "orca_exec_failed"
ORCA_TIMEOUT = "orca_timeout"
ORCA_OUTPUT_TOO_LARGE = "orca_output_too_large"
ORCA_ERROR = "orca_error"  # the CLI returned ok=false
MALFORMED_RESULT = "malformed_result"
TRUNCATED_RESULT = "truncated_result"
OMITTED_HOSTS = "omitted_hosts"
PAGINATION_BOUND = "pagination_bound"
REPEATED_CURSOR = "repeated_cursor"
RUNTIME_CHANGED = "runtime_changed"  # _meta.runtimeId drifted within one operation
BUDGET_EXCEEDED = "budget_exceeded"  # operation-wide deadline / command / candidate cap
UNSUPPORTED_RUNTIME = "unsupported_orca_runtime"
NOT_FOUND = "not_found"
CONFIG_ERROR = "config_error"
PREFLIGHT_REJECTED = "preflight_rejected"
PREFLIGHT_FAILED = "preflight_failed"
COMMAND_NOT_ALLOWED = "command_not_allowed"
INTERNAL_ERROR = "internal_error"

_EXIT_FOR = {
    UNSUPPORTED_RUNTIME: EXIT_UNSUPPORTED,
    NOT_FOUND: EXIT_NOT_FOUND,
    CONFIG_ERROR: EXIT_CONFIG,
    PREFLIGHT_REJECTED: EXIT_CONFIG,
    PREFLIGHT_FAILED: EXIT_CONFIG,
}


class AdapterError(Exception):
    """A bounded, serialisable failure.

    ``detail`` is an optional short token from a closed vocabulary (a known
    Orca error code, a preflight/config reason label, or a field name). It is
    never free text.
    """

    def __init__(self, code: str, stage: str = "", detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.detail = detail

    @property
    def exit_code(self) -> int:
        return _EXIT_FOR.get(self.code, EXIT_FAILURE)

    def to_json(self) -> dict:
        err = {"code": self.code}
        if self.stage:
            err["stage"] = self.stage
        if self.detail:
            err["detail"] = self.detail
        return {"ok": False, "error": err}

    def __str__(self) -> str:  # never anything but the bounded code
        return self.code
