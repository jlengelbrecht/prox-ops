"""Caller authentication for the receiver: one shared token, compared in
constant time.

Flagger's webhook definition cannot reference a Secret, so the token travels
in the Canary webhook entry's ``metadata`` map (design.md, "Caller
authentication"). A ``headers`` field is preferred where the installed
Flagger exposes one, so ``presented_token`` checks a header first and falls
back to ``metadata.token``. The receiver refuses to start without a token —
an absent token must never degrade into an open endpoint.
"""

from __future__ import annotations

import hmac
import os
import pathlib
from typing import Any, Mapping, Optional

TOKEN_ENV = "FLAGGER_PILOT_WEBHOOK_TOKEN"
TOKEN_FILE_ENV = "FLAGGER_PILOT_WEBHOOK_TOKEN_FILE"
TOKEN_HEADER = "X-Flagger-Recovery-Token"

# Short enough to be a typo or a placeholder is not a token.
MIN_TOKEN_LENGTH = 16

class TokenUnavailable(Exception):
    """Raised at startup when no usable token is configured. The receiver
    exits rather than serving unauthenticated callers."""

def load_token(*, path: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """Read the shared token from a file (a mounted Secret, preferred) or the
    environment. Raises ``TokenUnavailable`` rather than returning ``""``."""
    environ = os.environ if environ is None else environ
    path = path or environ.get(TOKEN_FILE_ENV)
    if path:
        try:
            token = pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise TokenUnavailable(f"cannot read the webhook token file {path!r}: {exc}") from exc
        source = f"file {path!r}"
    else:
        token = (environ.get(TOKEN_ENV) or "").strip()
        source = f"${TOKEN_ENV}"
    if len(token) < MIN_TOKEN_LENGTH:
        raise TokenUnavailable(
            f"{source} holds no token of at least {MIN_TOKEN_LENGTH} characters; refusing to start"
        )
    return token

def presented_token(headers: Optional[Mapping[str, str]], payload: Mapping[str, Any]) -> Optional[str]:
    """The token the caller presented: the ``X-Flagger-Recovery-Token`` header
    if present, else ``metadata.token`` from the webhook payload."""
    if headers is not None:
        header = headers.get(TOKEN_HEADER)
        if isinstance(header, str) and header:
            return header
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        token = metadata.get("token")
        if isinstance(token, str):
            return token
    return None

def token_matches(expected: str, presented: Optional[str]) -> bool:
    """Constant-time comparison. A missing or non-string token is a mismatch,
    never an accident-shaped success."""
    if not isinstance(presented, str) or not presented:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), presented.encode("utf-8"))
