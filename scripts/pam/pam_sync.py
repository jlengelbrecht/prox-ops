#!/usr/bin/env python3
"""Copies kv/pam one way into the 1Password vault OpenBao-Records. Run as a job.

One Secure Note per path, titled pam/<path> and tagged openbao-sync, with a concealed field per
KV key plus bao_path, bao_version and bao_hash. What was copied is recorded in KV custom metadata
as pam_synced_version, pam_item_updated and pam_content_hash. The hash is of the whole secret, so
it stays where the value can already be read: agents have no read on kv/metadata/pam.

Nothing flows back from 1Password, and items are never deleted. An item edited by hand, one ahead
of OpenBao after a restore from an older snapshot, or one at the same version with other content
is left alone and fails the job, so a person decides. Agents choose inbox names, so a name or key
outside the bounds below holds that path. Values reach op through stdin only, and only names and
versions are logged.

BAO_ADDR and BAO_CACERT name the server. BAO_TOKEN is used if set, otherwise the job logs in with
its projected service account token (BAO_ROLE, BAO_JWT_PATH). OP_SERVICE_ACCOUNT_TOKEN reaches op.
"""
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ADDR = os.environ.get("BAO_ADDR", "https://openbao-active.openbao.svc:8200").rstrip("/")
ROLE = os.environ.get("BAO_ROLE", "pam-sync")
JWT_PATH = os.environ.get("BAO_JWT_PATH", "/var/run/secrets/openbao/token")
VAULT = os.environ.get("PAM_RECORDS_VAULT", "OpenBao-Records")
TAG = "openbao-sync"
DELETED_TAG = "deleted-in-openbao"
RESERVED = {"bao_path", "bao_version", "bao_hash"}
PATH_NAME = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
KEY_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}")
MAX_PATH = 200
MAX_DEPTH = 8
MAX_KEYS = 50
CONTEXT = ssl.create_default_context(cafile=os.environ.get("BAO_CACERT"))


class Hold(Exception):
    """This path needs a person. The rest of the run carries on."""


class Bao:
    def __init__(self):
        self.token = os.environ.get("BAO_TOKEN")
        self.owned = not self.token
        if self.owned:
            with open(JWT_PATH) as f:
                jwt = f.read().strip()
            _, reply = self.call("POST", "auth/kubernetes/login", {"role": ROLE, "jwt": jwt})
            self.token = reply["auth"]["client_token"]

    def call(self, method, path, body=None, content_type="application/json", missing_ok=False):
        req = urllib.request.Request(
            f"{ADDR}/v1/{path}", method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": content_type, **({"X-Vault-Token": self.token} if self.token else {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=20, context=CONTEXT if ADDR.startswith("https") else None) as f:
                raw = f.read()
                return f.status, json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            if e.code == 404 and missing_ok:
                return 404, {}
            raise RuntimeError(f"OpenBao answered {e.code} on {method} {path}")

    def paths(self, prefix=""):
        try:
            status, reply = self.call("GET", f"kv/metadata/pam/{urllib.parse.quote(prefix)}?list=true", missing_ok=True)
        except (RuntimeError, OSError) as error:
            raise RuntimeError(f"could not list {shown(prefix)}: {type(error).__name__}") from None
        if status == 404:
            return
        for key in reply["data"]["keys"]:
            if key.endswith("/") and prefix.count("/") < MAX_DEPTH:
                yield from self.paths(prefix + key)
            else:
                yield prefix + key

    def metadata(self, path):
        return self.call("GET", f"kv/metadata/pam/{path}")[1]["data"]

    def values(self, path):
        status, reply = self.call("GET", f"kv/data/pam/{path}", missing_ok=True)
        return None if status == 404 else reply["data"]["data"]

    def record(self, path, version, updated, content):
        self.call("PATCH", f"kv/metadata/pam/{path}",
                  {"custom_metadata": {"pam_synced_version": str(version), "pam_item_updated": updated,
                                       "pam_content_hash": content}},
                  content_type="application/merge-patch+json")

    def close(self):
        if self.owned:
            self.call("POST", "auth/token/revoke-self", {})


def op(*args, stdin=None):
    # op reads a template from a piped stdin, so give it one only when there is one.
    source = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        result = subprocess.run(["op", *args, "--format", "json"], capture_output=True, text=True, timeout=60, **source)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"op {args[0]} {args[1]} timed out")
    if result.returncode != 0:
        # stderr is dropped: it could quote what went in.
        raise RuntimeError(f"op {args[0]} {args[1]} failed with exit code {result.returncode}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def field(item, name):
    return next((f.get("value") for f in item.get("fields", []) if f.get("id") == name or f.get("label") == name), None)


def shown(name):
    """A name fit for the log, whatever an agent put in it."""
    return ascii(name[:80])


def check_path(path):
    if len(path) > MAX_PATH or path.count("/") > MAX_DEPTH or not PATH_NAME.fullmatch(path):
        raise Hold(f"{shown(path)}: the name is outside the allowed bounds")


def check_keys(path, values):
    if len(values) > MAX_KEYS:
        raise Hold(f"{path}: more than {MAX_KEYS} keys")
    for key in values:
        if key in RESERVED:
            raise Hold(f"{path}: the key {key} is reserved")
        if not KEY_NAME.fullmatch(key):
            raise Hold(f"{path}: the key {shown(key)} is outside the allowed bounds")


def version_number(text, path, what):
    try:
        return int(text)
    except (TypeError, ValueError):
        raise Hold(f"{path}: {what} isn't a version number")


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def tags_for(item, *extra):
    """The sync's tags, added to whatever a person put on the item."""
    kept = [t for t in (item or {}).get("tags") or [] if t not in (TAG, DELETED_TAG)]
    return [TAG, *extra, *kept]


def template(path, version, content, values, item=None):
    """Replaces the item's fields, so a key removed in OpenBao goes too. Built-in fields stay."""
    fields = [f for f in (item or {}).get("fields", []) if f.get("purpose")]
    fields += [{"id": "bao_path", "label": "bao_path", "type": "STRING", "value": path},
               {"id": "bao_version", "label": "bao_version", "type": "STRING", "value": str(version)},
               {"id": "bao_hash", "label": "bao_hash", "type": "STRING", "value": content}]
    for key, value in values.items():
        fields.append({"id": key, "label": key, "type": "CONCEALED",
                       "value": value if isinstance(value, str) else json.dumps(value)})
    return json.dumps({**(item or {}), "title": f"pam/{path}", "category": "SECURE_NOTE",
                       "tags": tags_for(item), "fields": fields})


def create(path, version, content, values):
    return op("item", "create", "--vault", VAULT, "--category", "Secure Note", "--title", f"pam/{path}",
              "--tags", TAG, "-", stdin=template(path, version, content, values))


def update(item, path, version, content, values):
    return op("item", "edit", item["id"], "--vault", VAULT, stdin=template(path, version, content, values, item))


def set_tags(item, tags):
    return op("item", "edit", item["id"], "--vault", VAULT, "--tags", ",".join(tags))


def sync_path(bao, path, item):
    """Returns the action taken, or None if there was nothing to do."""
    check_path(path)
    meta = bao.metadata(path)
    current = meta["current_version"]
    custom = meta.get("custom_metadata") or {}
    synced = version_number(custom.get("pam_synced_version", 0), path, "pam_synced_version")
    recorded = custom.get("pam_item_updated", "")
    recorded_hash = custom.get("pam_content_hash", "")
    latest = meta["versions"].get(str(current), {})
    deleted = bool(latest.get("deletion_time") or latest.get("destroyed"))
    tagged = item is not None and DELETED_TAG in (item.get("tags") or [])

    if deleted:
        if item is None or tagged:
            return None
        bao.record(path, synced, set_tags(item, tags_for(item, DELETED_TAG))["updated_at"], recorded_hash)
        return "marked deleted"

    values = bao.values(path)
    if values is None:
        return None
    check_keys(path, values)
    content = digest(values)
    if item is not None and not tagged and synced == current and item["updated_at"] == recorded and content == recorded_hash:
        return None
    if item is None:
        bao.record(path, current, create(path, current, content, values)["updated_at"], content)
        return f"created at version {current}"

    full = op("item", "get", item["id"], "--vault", VAULT)
    held = version_number(field(full, "bao_version") or 0, path, "bao_version")
    if held > current:
        raise Hold(f"{path}: item is at version {held}, ahead of OpenBao at {current}; was OpenBao restored?")
    # After a run that died between writing the item and recording it, held is ahead of synced
    # and the timestamp can't be compared: only a matching version shows a hand edit.
    if held == synced and recorded and item["updated_at"] != recorded:
        raise Hold(f"{path}: the item was edited in 1Password; OpenBao stays the source")
    if held == current:
        if field(full, "bao_hash") != content:
            raise Hold(f"{path}: same version as the item but different content; was OpenBao restored?")
        updated = set_tags(item, tags_for(item))["updated_at"] if tagged else item["updated_at"]
        bao.record(path, current, updated, content)
        return f"recorded version {current}"
    bao.record(path, current, update(full, path, current, content, values)["updated_at"], content)
    return f"updated to version {current}"


def run(bao):
    items = {}
    duplicates = set()
    failures = 0
    for item in op("item", "list", "--vault", VAULT, "--tags", TAG) or []:
        if item["title"] in items:
            duplicates.add(item["title"])
        items[item["title"]] = item
    for title in sorted(duplicates):
        print(f"hold {title}: more than one item has this title")
        failures += 1

    # A partial listing must stop here: below, an item with no path is marked deleted.
    try:
        paths = list(bao.paths())
    except (RuntimeError, KeyError, ValueError) as error:
        print(f"hold run: {error if isinstance(error, RuntimeError) else type(error).__name__}")
        return failures + 1
    # OpenBao answers 404 for an empty listing, and for a mount that's missing or restored empty.
    # Tagging every record as deleted on that is a decision for a person.
    if not paths and items:
        print(f"hold run: OpenBao lists nothing under kv/pam, but {len(items)} item(s) exist")
        return failures + 1

    for path in paths:
        title = f"pam/{path}"
        if title in duplicates:
            continue
        try:
            action = sync_path(bao, path, items.pop(title, None))
            if action:
                print(f"{title}: {action}")
        except Hold as hold:
            print(f"hold pam/{hold}")
            failures += 1
        except (RuntimeError, ValueError, KeyError, OSError) as error:
            print(f"failed {shown(title)}: {type(error).__name__}: {error}" if isinstance(error, RuntimeError)
                  else f"failed {shown(title)}: {type(error).__name__}")
            failures += 1

    # What's left has no path in OpenBao at all.
    for title, item in items.items():
        if title not in duplicates and DELETED_TAG not in (item.get("tags") or []):
            try:
                set_tags(item, tags_for(item, DELETED_TAG))
                print(f"{title}: marked deleted")
            except RuntimeError as error:
                print(f"failed {title}: {error}")
                failures += 1
    return failures


def main():
    bao = Bao()
    try:
        failures = run(bao)
    finally:
        bao.close()
    if failures:
        print(f"{failures} path(s) need attention", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
