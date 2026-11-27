"""Vault-bound 1Password Connect operations for agents.

Agents may create an item whose password 1Password generates, and may have an
item's password handed to a fixed server-side consumer. No response or log
line carries a secret value, and every request outside that shape is rejected
before Connect is contacted.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping

LOG = logging.getLogger("onepassword_agent_mcp")

ONEPASSWORD_ID = re.compile(r"[a-z0-9]{26}")
TITLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,99}")
AGENT_TAG = "agent-created"
PASSWORD_RECIPE = {"length": 40, "characterSets": ["LETTERS", "DIGITS"]}


class Rejected(Exception):
    """A request outside the fixed policy. Its message is safe to return."""


class ConnectError(Exception):
    """Connect failed or answered unexpectedly. The message never carries a body."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Following a redirect would replay the Connect token to another origin.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ConnectClient:
    """Minimal 1Password Connect API client pinned to one HTTPS origin."""

    def __init__(self, origin: str, token: str, timeout: float = 10.0, opener=None):
        parts = urllib.parse.urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise ValueError("the Connect origin must be a bare https:// origin")
        if not token:
            raise ValueError("the Connect token is empty")
        self._origin = f"https://{parts.netloc}"
        self._token = token
        self._timeout = timeout
        # No proxy handler: the token goes to the configured origin and nowhere else.
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _NoRedirect,
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def request(self, method: str, path: str, query: Mapping[str, str] | None = None,
                body: Any = None) -> Any:
        url = self._origin + path
        if query:
            url += "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise ConnectError(f"Connect answered HTTP {exc.code}") from None
        except (OSError, http.client.HTTPException):
            raise ConnectError("Connect is unreachable") from None
        try:
            return json.loads(payload)
        except ValueError:
            raise ConnectError("Connect returned a body that is not JSON") from None


Consumer = Callable[[str], Mapping[str, Any]]


def connectivity_check(value: str) -> dict[str, Any]:
    """Proves the broker can use a stored value server-side without releasing it."""
    return {"value_present": bool(value)}


DESTINATIONS: Mapping[str, Consumer] = {"connectivity-check": connectivity_check}


class Broker:
    """The only operations agents get on the bound vault."""

    def __init__(self, connect, vault_name: str,
                 destinations: Mapping[str, Consumer] = DESTINATIONS):
        if not isinstance(vault_name, str) or not TITLE.fullmatch(vault_name):
            raise ValueError("the bound vault name is not a plain vault name")
        self._connect = connect
        self._vault_name = vault_name
        self._destinations = dict(destinations)
        self._vault_id: str | None = None
        # One process serves all calls, so this lock makes the duplicate check
        # and the create a single step.
        self._lock = threading.Lock()

    @property
    def vault_name(self) -> str:
        return self._vault_name

    @property
    def destinations(self) -> list[str]:
        return sorted(self._destinations)

    def create_generated_item(self, vault: Any, title: Any) -> dict[str, Any]:
        self._check_vault(vault)
        if not isinstance(title, str) or not TITLE.fullmatch(title):
            raise Rejected(
                "title must be 1-100 letters, digits, spaces, dots, underscores or "
                "hyphens, starting with a letter or digit"
            )
        with self._lock:
            vault_id = self._resolve_vault()
            existing = [
                item
                for item in self._list(f"/v1/vaults/{vault_id}/items",
                                       {"filter": f'title eq "{title}"'})
                if item.get("title") == title
            ]
            if existing:
                LOG.info("op=create_generated_item outcome=exists item=%s",
                         existing[0].get("id"))
                return {
                    "created": False,
                    "reason": "an item with this title already exists; nothing was created",
                    "item": self._metadata(existing[0], vault_id),
                }
            item = self._connect.request("POST", f"/v1/vaults/{vault_id}/items",
                                         body=_generated_item(vault_id, title))
        self._require_vault(item, vault_id)
        LOG.info("op=create_generated_item outcome=created item=%s", item.get("id"))
        return {"created": True, "item": self._metadata(item, vault_id)}

    def deliver_item_to_consumer(self, vault: Any, item_id: Any,
                                 destination: Any) -> dict[str, Any]:
        self._check_vault(vault)
        if not isinstance(item_id, str) or not ONEPASSWORD_ID.fullmatch(item_id):
            raise Rejected("item_id must be a 26-character 1Password item ID")
        consumer = self._destinations.get(destination) if isinstance(destination, str) else None
        if consumer is None:
            raise Rejected("destination is not an approved consumer")
        vault_id = self._resolve_vault()
        item = self._connect.request("GET", f"/v1/vaults/{vault_id}/items/{item_id}")
        self._require_vault(item, vault_id)
        value = _password(item)
        if value is None:
            raise Rejected("the item has no password to deliver")
        receipt = dict(consumer(value))
        LOG.info("op=deliver_item_to_consumer outcome=delivered item=%s destination=%s",
                 item_id, destination)
        return {
            "delivered": True,
            "destination": destination,
            "item": self._metadata(item, vault_id),
            "receipt": receipt,
        }

    def _check_vault(self, vault: Any) -> None:
        # Exact name only: no IDs, aliases, case folding or fallback vaults.
        if vault != self._vault_name:
            raise Rejected(f"only the {self._vault_name} vault is permitted")

    def _resolve_vault(self) -> str:
        if self._vault_id is None:
            vaults = self._list("/v1/vaults", {})
            if len(vaults) != 1:
                raise ConnectError("the token must have access to exactly one vault")
            vault = vaults[0]
            vault_id = vault.get("id")
            if (vault.get("name") != self._vault_name
                    or not isinstance(vault_id, str) or not ONEPASSWORD_ID.fullmatch(vault_id)):
                raise ConnectError("the token must have access to the bound vault")
            self._vault_id = vault_id
        return self._vault_id

    def _list(self, path: str, query: Mapping[str, str]) -> list[dict[str, Any]]:
        listing = self._connect.request("GET", path, query)
        if not isinstance(listing, list) or not all(isinstance(entry, dict) for entry in listing):
            raise ConnectError("Connect returned an unexpected listing")
        return listing

    @staticmethod
    def _require_vault(item: Any, vault_id: str) -> None:
        if not isinstance(item, dict) or (item.get("vault") or {}).get("id") != vault_id:
            raise ConnectError("Connect returned an item outside the bound vault")

    def _metadata(self, item: Mapping[str, Any], vault_id: str) -> dict[str, Any]:
        # Allowlist, never a denylist: anything not named here stays server-side.
        return {
            "id": item.get("id"),
            "title": item.get("title"),
            "vault": {"id": vault_id, "name": self._vault_name},
            "category": item.get("category"),
            "tags": [tag for tag in item.get("tags") or [] if isinstance(tag, str)],
            "version": item.get("version"),
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "fields": [
                {key: field.get(key) for key in ("id", "label", "type", "purpose")}
                for field in item.get("fields") or []
                if isinstance(field, dict)
            ],
        }


def _generated_item(vault_id: str, title: str) -> dict[str, Any]:
    return {
        "vault": {"id": vault_id},
        "title": title,
        "category": "PASSWORD",
        "tags": [AGENT_TAG],
        "fields": [
            {
                "id": "password",
                "label": "password",
                "type": "CONCEALED",
                "purpose": "PASSWORD",
                "generate": True,
                "recipe": PASSWORD_RECIPE,
            }
        ],
    }


def _password(item: Mapping[str, Any]) -> str | None:
    for field in item.get("fields") or []:
        if isinstance(field, dict) and field.get("purpose") == "PASSWORD":
            value = field.get("value")
            if isinstance(value, str) and value:
                return value
    return None
