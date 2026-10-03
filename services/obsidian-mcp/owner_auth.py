"""Independent Authentik browser session check for exact owner approvals."""

from __future__ import annotations

import asyncio
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request

from approval import OwnerContext


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_CHECK_PATH = "/outpost.goauthentik.io/auth/nginx"
_OWNER_CHECK_TIMEOUT = 2.0


class OwnerAuthError(Exception):
    pass


@dataclass(frozen=True)
class OwnerAuthConfig:
    check_url: str
    origin: str
    owner_ids: frozenset[str]
    vault_ids: frozenset[str]
    # Vaults appear and disappear at runtime; when set, this supplies the current IDs.
    vault_source: Callable[[], Iterable[str]] | None = None

    def __post_init__(self) -> None:
        check = urlsplit(self.check_url)
        origin = urlsplit(self.origin)
        if (check.scheme not in {"http", "https"}
                or not check.hostname or not check.hostname.endswith(".svc.cluster.local")
                or check.path != _CHECK_PATH or check.username or check.password
                or check.query or check.fragment
                or origin.scheme != "https" or not origin.hostname or origin.path
                or origin.query or origin.fragment or origin.username or origin.password
                or not self.owner_ids or (not self.vault_ids and self.vault_source is None)
                or any(not _ID.fullmatch(value) for value in self.owner_ids | self.vault_ids)):
            raise ValueError("invalid owner authentication configuration")


class OwnerAuthenticator:
    def __init__(self, config: OwnerAuthConfig, client: httpx.AsyncClient | None = None):
        self.config = config
        self._owned_client = client is None
        self._client = client or httpx.AsyncClient(timeout=2.0, follow_redirects=False,
                                                   trust_env=False)

    async def verify(self, request: Request) -> OwnerContext:
        cookies = request.headers.getlist("cookie")
        cookie = cookies[0] if len(cookies) == 1 else ""
        if not cookie or len(cookie) > 4096 or "\n" in cookie or "\r" in cookie:
            raise OwnerAuthError("owner_session_required")
        origin = urlsplit(self.config.origin)
        headers = {
            "Cookie": cookie,
            "Host": origin.netloc,
            "X-Forwarded-Host": origin.netloc,
            "X-Forwarded-Proto": "https",
            "X-Original-URL": self.config.origin + "/owner/",
        }
        try:
            async with asyncio.timeout(_OWNER_CHECK_TIMEOUT):
                async with self._client.stream("GET", self.config.check_url, headers=headers,
                                               follow_redirects=False,
                                               timeout=_OWNER_CHECK_TIMEOUT) as response:
                    owner = response.headers.get("x-authentik-uid", "")
                    if (response.status_code != 200 or not _ID.fullmatch(owner)
                            or owner not in self.config.owner_ids):
                        raise OwnerAuthError("owner_session_required")
        except (httpx.HTTPError, TimeoutError):
            raise OwnerAuthError("owner_session_required") from None
        vaults = (frozenset(self.config.vault_source()) if self.config.vault_source is not None
                  else self.config.vault_ids)
        return OwnerContext(owner, frozenset(vault for vault in vaults if _ID.fullmatch(vault)))

    async def aclose(self) -> None:
        if self._owned_client:
            await self._client.aclose()


class CsrfLedger:
    def __init__(self, *, ttl: int = 120, max_pending: int = 128):
        if not 1 <= ttl <= 600 or not 1 <= max_pending <= 1024:
            raise ValueError("invalid CSRF configuration")
        self._ttl = ttl
        self._max_pending = max_pending
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, str, str, float]] = {}

    def issue(self, owner: str, pending: str, digest: str) -> str:
        now = time.monotonic()
        with self._lock:
            self._tokens = {token: record for token, record in self._tokens.items()
                            if record[3] > now}
            if len(self._tokens) >= self._max_pending:
                raise OwnerAuthError("csrf_capacity")
            nonce = secrets.token_urlsafe(32)
            self._tokens[nonce] = (owner, pending, digest, now + self._ttl)
            return nonce

    def consume(self, owner: str, pending: str, digest: str, submitted: str,
                cookie: str, origin: str, expected_origin: str) -> bool:
        if (origin != expected_origin or not submitted or len(submitted) > 128
                or not cookie or len(cookie) > 128
                or not secrets.compare_digest(submitted, cookie)):
            return False
        with self._lock:
            record = self._tokens.get(submitted)
            if (record is None or record[3] <= time.monotonic()
                    or record[:3] != (owner, pending, digest)):
                return False
            del self._tokens[submitted]
            return True
