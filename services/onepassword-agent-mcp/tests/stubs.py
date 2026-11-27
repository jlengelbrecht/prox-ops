"""Shared fixtures: an in-memory stand-in for the 1Password Connect API."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
BOOTSTRAP = REPO / "scripts" / "bootstrap" / "open-connector-ai-access.py"
if str(SERVICE) not in sys.path:
    sys.path.insert(0, str(SERVICE))

from access import ConnectError  # noqa: E402


def load_bootstrap():
    """Imports the bootstrap helper once; its file name is not a module name."""
    name = "open_connector_ai_access"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, BOOTSTRAP)
        module = importlib.util.module_from_spec(spec)
        # dataclasses resolve string annotations through sys.modules.
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]

VAULT = "AI-Access"
VAULT_ID = "aiaccess" + "0" * 18
OTHER_VAULT_ID = "private0" + "0" * 18
# What Connect "generates". Tests assert it never leaves the broker.
SECRET = "Gen3ratedSentinelValue0123456789abcdefXYZ"


class StubConnect:
    """Records every call; generates SECRET for created password fields."""

    def __init__(self, vaults=None):
        self.calls = []
        self.vaults = [{"id": VAULT_ID, "name": VAULT}] if vaults is None else vaults
        self.items = {}
        self.item_vault_override = None

    def request(self, method, path, query=None, body=None):
        self.calls.append((method, path, dict(query or {}), copy.deepcopy(body)))
        if (method, path) == ("GET", "/v1/vaults"):
            if not query:
                return self.vaults
            return [v for v in self.vaults if query.get("filter") == f'name eq "{v["name"]}"']
        prefix = f"/v1/vaults/{VAULT_ID}/items"
        if (method, path) == ("GET", prefix):
            return [
                {key: value for key, value in item.items() if key != "fields"}
                for item in self.items.values()
                if query.get("filter") == f'title eq "{item["title"]}"'
            ]
        if (method, path) == ("POST", prefix):
            item_id = f"item{len(self.items) + 1:022d}"
            item = copy.deepcopy(body)
            item.update({
                "id": item_id,
                "version": 1,
                "createdAt": "2026-11-26T00:00:00Z",
                "updatedAt": "2026-11-26T00:00:00Z",
            })
            for field in item["fields"]:
                if field.pop("generate", False):
                    field.pop("recipe", None)
                    field["value"] = SECRET
            self.items[item_id] = item
            return copy.deepcopy(item)
        if method == "GET" and path.startswith(prefix + "/"):
            item = self.items.get(path.rsplit("/", 1)[1])
            if item is None:
                raise ConnectError("Connect answered HTTP 404")
            item = copy.deepcopy(item)
            if self.item_vault_override:
                item["vault"] = {"id": self.item_vault_override}
            return item
        raise AssertionError(f"unexpected Connect call {method} {path}")
