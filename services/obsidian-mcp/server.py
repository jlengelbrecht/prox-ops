"""Compatibility values shared with the app-backed HTTP gateway."""

import base64
import re


MCP_PATH = "/mcp"
MAX_HTTP_BODY = 1024 * 1024


def _valid_token(value: str) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,}", value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError:
        return False
    return len(decoded) >= 32 and base64.urlsafe_b64encode(decoded).rstrip(b"=").decode() == value
