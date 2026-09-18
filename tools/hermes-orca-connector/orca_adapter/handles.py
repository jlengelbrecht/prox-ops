"""Opaque references and bounded display text.

Every identifier that leaves the adapter is an HMAC-SHA256 of the raw Orca
value under one local key, domain separated by a fixed prefix so a project id,
a coordinator terminal handle and an epoch can never collide or be swapped.
Raw ids, paths and URLs never leave.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

HANDLE_HEX_LEN = 32  # 128 bits of the tag; enough to be unguessable, short to carry

_DOMAIN_PREFIX = {
    "project": "prj",
    "coordinator": "co",
    "epoch": "ep",
    "runtime": "rt",
}


class HandleFactory:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("key must be 32 bytes")
        self._key = key

    def _tag(self, domain: str, *parts: str) -> str:
        # Every part is length-prefixed so parts can never shift into each
        # other, whatever bytes (including NUL) an id carries.
        msg = b""
        for part in ("hermes-orca/v1/" + domain, *parts):
            raw = part.encode("utf-8", "surrogateescape")
            msg += len(raw).to_bytes(8, "big") + raw
        digest = hmac.new(self._key, msg, hashlib.sha256).hexdigest()
        return _DOMAIN_PREFIX[domain] + "_" + digest[:HANDLE_HEX_LEN]

    def project(self, project_id: str) -> str:
        return self._tag("project", project_id)

    def coordinator(self, terminal_handle: str) -> str:
        return self._tag("coordinator", terminal_handle)

    def epoch(self, run_id: str, terminal_handle: str) -> str:
        """Changes whenever the Run or its coordinator identity changes."""
        return self._tag("epoch", run_id, terminal_handle)

    def runtime(self, runtime_id: str) -> str:
        """Lets a consumer notice a runtime restart without learning the id.

        A new runtime never remints project handles: those depend only on
        the durable project id.
        """
        return self._tag("runtime", runtime_id)


_HANDLE_RE = re.compile(r"(prj|co|ep|rt)_[0-9a-f]{%d}" % HANDLE_HEX_LEN)


def is_wellformed_handle(value: object, domain: str) -> bool:
    """Shape check for a remote-supplied opaque handle (before any lookup).

    Whole-string match: ``$`` would accept a trailing newline, so a control
    suffix could pass the shape check and trigger a discovery for nothing.
    """
    return (
        isinstance(value, str)
        and _HANDLE_RE.fullmatch(value) is not None
        and value.startswith(_DOMAIN_PREFIX[domain] + "_")
    )


# --- display text ----------------------------------------------------------

DISPLAY_MAX_CHARS = 64
REDACTED = "[redacted]"

# Conservative shapes that must never be displayed: URLs (any scheme), file
# URIs, userinfo blocks, filesystem paths, well-known token prefixes and long
# unbroken secret-looking runs. Relative names such as ``owner/repo`` stay.
_SECRET_SHAPES = (
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://"),  # scheme://
    re.compile(r"(?i)file:/"),  # file URI, including the single-slash ``file:/path`` form
    re.compile(r"(?:^|(?<=[\s(\[<\"'`=:]))/[^\s/]+/"),  # absolute POSIX path (two or more segments), also after ``label:``
    re.compile(r"^/\S+$"),  # a bare absolute path as the whole name
    re.compile(r"(?:^|(?<=[\s(\[<\"'`=]))~/"),  # home-relative path
    re.compile(r"\b[A-Za-z]:[\\/]"),  # Windows drive path
    re.compile(r"\\\\[^\s\\]+\\"),  # UNC path
    re.compile(r"[^\s@/]+:[^\s@/]+@"),  # user:pass@
    re.compile(r"[^\s@]+@[^\s@:]+:\S"),  # scp-style git@host:path, anywhere in the text
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|sk|xox[abprs]|glpat|hf)[_-][A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]+-----"),  # PEM block
    re.compile(r"[A-Za-z0-9+/=_-]{32,}"),  # long unbroken token/base64-ish run
    re.compile(r"(?i)\b(?:token|password|passwd|secret|api[_-]?key|bearer)\b\s*[:=]"),
)


def display_text(value: object) -> str:
    """Bound a display name to printable text; redact credential/URL shapes.

    Innocent slashes (``owner/repo``) are kept: only the shapes above trigger
    redaction. The result is display-only and never used for routing.
    """
    if not isinstance(value, str) or not value:
        return ""
    cleaned = "".join(
        ch for ch in value if ch.isprintable() and unicodedata.category(ch) not in ("Cc", "Cf", "Zl", "Zp")
    ).strip()
    if not cleaned:
        return ""
    for shape in _SECRET_SHAPES:
        if shape.search(cleaned):
            return REDACTED
    if len(cleaned) > DISPLAY_MAX_CHARS:
        cleaned = cleaned[: DISPLAY_MAX_CHARS - 1] + "…"
    return cleaned
