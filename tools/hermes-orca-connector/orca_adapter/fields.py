"""Positive field projection helpers.

Every value taken from an Orca result goes through one of these: the caller
names the field and the type it expects, and anything else is a bounded
``malformed_result``. Nothing is copied wholesale.

Presence is part of the shape: a key that is absent is never treated as an
explicit ``null``. A nullable field (``optional=True``) must still be present;
only its value may be ``None``. Strings must be well-formed text (no unpaired
surrogates), so every later encode/HMAC/serialise step is total.
"""

from __future__ import annotations

from .errors import MALFORMED_RESULT, AdapterError

# Bound on any string the adapter takes by name. Real ids and display names
# are far shorter; anything beyond this is not a field value but a payload.
MAX_STR_CHARS = 4096


def is_wellformed_text(value: object) -> bool:
    """A str with no unpaired surrogate (i.e. UTF-8 encodable), bounded in length."""
    if not isinstance(value, str) or len(value) > MAX_STR_CHARS:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def take_str(obj: dict, key: str, stage: str, *, optional: bool = False) -> str | None:
    if key not in obj:  # absent is not null
        raise AdapterError(MALFORMED_RESULT, stage, key)
    value = obj[key]
    if value is None and optional:
        return None
    if not is_wellformed_text(value) or not value:
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value


def take_bool(obj: dict, key: str, stage: str) -> bool:
    value = obj.get(key)
    if not isinstance(value, bool):
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value


def take_int(obj: dict, key: str, stage: str) -> int:
    value = obj.get(key)
    if type(value) is not int or value < 0:
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value


def take_dict(obj: dict, key: str, stage: str) -> dict:
    value = obj.get(key)
    if not isinstance(value, dict):
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value


def take_rows(obj: dict, key: str, stage: str) -> list[dict]:
    value = obj.get(key)
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value


def take_str_list(obj: dict, key: str, stage: str) -> list[str]:
    value = obj.get(key)
    if not isinstance(value, list) or not all(is_wellformed_text(item) for item in value):
        raise AdapterError(MALFORMED_RESULT, stage, key)
    return value
