"""Durable deployment records: one ConfigMap per record in ``flagger-system``,
named from a deterministic idempotency key. A ``409 AlreadyExists`` on create
proves only that the name is taken, not that the contents match: ``put``
follows up with a GET, checks the existing ConfigMap's ownership labels, and
reports ``PutResult.DUPLICATE`` on a match or raises ``ForeignConfigMap`` on a
mismatch — no read-modify-write either way."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .identity import CandidateIdentity

LOG = logging.getLogger("flagger_recovery.record")

_NAME_PREFIX = "flagger-recovery-"
_LABEL_ANNOTATION = "flagger-recovery/canary-full"
_PART_OF = "flagger-recovery"

CANARY_LABEL = "flagger-recovery/canary"
PHASE_LABEL = "flagger-recovery/phase"
# The webhook payload's ``checksum``, which is NOT the template hash the record
# is keyed by — see ``checksum_label()``.
CHECKSUM_LABEL = "flagger-recovery/checksum"
# ``server.PHASE_CANDIDATE``/``server.PHASE_PROMOTED``, restated here because the store is
# the lower layer.
_CANDIDATE = "candidate"
_PROMOTED = "promoted"

# Which labels must match before a 409 on create counts as *our* duplicate
# rather than someone else's ConfigMap wearing our deterministic name.
_RECORD_OWNERSHIP = ("app.kubernetes.io/part-of", "flagger-recovery/template-hash", "flagger-recovery/phase")
_DOCUMENT_OWNERSHIP = ("app.kubernetes.io/part-of", "flagger-recovery/kind")

_KIND_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")

# Kubernetes label VALUES: <=63 chars, alphanumeric/./-/_ , must start and end alphanumeric.
# ``\Z``, not ``$``: ``$`` also matches before a trailing newline, which is not "end".
_LABEL_VALUE_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?\Z")
# Kubernetes label NAMES: an optional DNS-subdomain prefix, then the name proper.
_LABEL_NAME_RE = re.compile(r"^([a-z0-9]([-a-z0-9.]*[a-z0-9])?/)?[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?\Z")

# Record keys are always a 32-hex-char make_key() digest; reject anything
# else before it reaches a URL path.
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})

class PutResult(Enum):
    CREATED = "created"
    DUPLICATE = "duplicate"

class ApiWriteError(Exception):
    """Raised when the Kubernetes API returns an unexpected status for a write/read."""

    def __init__(self, method: str, url: str, status: int, body: bytes) -> None:
        super().__init__(f"{method} {url} -> HTTP {status}: {body!r}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body

class MalformedRecord(ApiWriteError):
    """A ConfigMap this store owns did not decode. ``from_configmap`` raises ``KeyError``,
    ``TypeError`` or ``JSONDecodeError`` on a truncated or hand-edited object, which a caller
    cannot tell from a bug in its own code. It is neither: the stored object is *input* here,
    so an undecodable one is a read failure, and "unreadable" stays a decision it can make."""

    def __init__(self, name: str, exc: Exception) -> None:
        # The name and the failure's class only: its message can carry stored payload text.
        Exception.__init__(self, f"ConfigMap {name!r} did not decode ({type(exc).__name__})")
        self.method, self.url, self.status, self.body = "GET", name, 200, b""
        self.name = name

def _decoded(configmap: Mapping[str, Any], reader: Callable[[Mapping[str, Any]], Any]) -> Any:
    """``reader(configmap)``, with a decoding failure named as this store's error."""
    try:
        return reader(configmap)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        metadata = configmap.get("metadata") if isinstance(configmap, Mapping) else None
        name = metadata.get("name") if isinstance(metadata, Mapping) else ""
        raise MalformedRecord(str(name or "<unnamed>"), exc) from exc

class ForeignConfigMap(Exception):
    """Raised by ``ConfigMapStore.put`` when a ConfigMap occupies our
    deterministic name but isn't provably our record.

    A 409 on create proves only that the name is taken, not that its contents
    are our record, so ``put`` follows up with a GET and checks the
    ``app.kubernetes.io/part-of``, ``flagger-recovery/template-hash`` and
    ``flagger-recovery/phase`` labels before reporting ``PutResult.DUPLICATE``.
    This is raised instead when those labels don't match, or when the
    verification GET returns 404 (the ConfigMap was deleted between our
    failed create and our check) and a single retried create still can't
    land cleanly -- in both cases the caller must log and refuse rather than
    assume its record was ever stored."""

    def __init__(self, name: str) -> None:
        super().__init__(f"ConfigMap {name!r} exists but is not a flagger-recovery record for this key")
        self.name = name

_STEM_CHAR_RE = re.compile(r"[A-Za-z0-9._-]")

def label_value(value: str) -> str:
    """Clamp an arbitrary string to a valid Kubernetes label value.

    Kubernetes caps label values at 63 characters and restricts the charset;
    inputs that fit are used verbatim, everything else falls back to a short,
    stable hash form so that untrusted webhook fields can still be indexed.
    The stem is restricted to ASCII alphanumerics/``-``/``_``/``.`` only:
    ``str.isalnum()`` accepts Unicode (e.g. "échec"), which would let a
    non-ASCII stem back into the result and violate ``_LABEL_VALUE_RE``."""
    if len(value) <= 63 and _LABEL_VALUE_RE.match(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    stem = "".join(char for char in value[:24] if _STEM_CHAR_RE.match(char)).strip("-_.")
    return f"{stem}-{digest}" if stem else digest

def canary_label(namespace: str, canary_name: str) -> str:
    """The ``flagger-recovery/canary`` label value: unique across namespaces.
    ``canary_label_full()`` recovers the original when this shortens it."""
    return label_value(f"{namespace}.{canary_name}")

def canary_label_full(namespace: str, canary_name: str) -> Optional[str]:
    """The full ``namespace.canary_name`` value, or ``None`` when
    ``canary_label()`` returned it verbatim (no shortening needed) — the
    value stored under the ``flagger-recovery/canary-full`` annotation."""
    full = f"{namespace}.{canary_name}"
    if len(full) <= 63 and _LABEL_VALUE_RE.match(full):
        return None
    return full

def checksum_label(checksum: str) -> Optional[str]:
    """The ``flagger-recovery/checksum`` label value for a webhook payload's
    ``checksum``, or ``None`` when there is nothing to index.

    Flagger computes that field as ``ComputeHash({TrackedConfigs,
    LastAppliedSpec})`` (``pkg/controller/webhook.go``, ``canaryChecksum``), so
    it is a hash *of* ``status.lastAppliedSpec``, never that value itself — the
    two are in different hash spaces and never compare equal. Records stay keyed
    by the template hash; this label is only the index that lets a later hook,
    which carries the checksum and nothing else, find the record again."""
    return label_value(checksum) if checksum else None

def make_key_parts(namespace: str, canary_name: str, template_hash: str, phase: str) -> str:
    """The record key from its parts, for callers holding a resolved
    ``template_hash`` (``Canary.status.lastAppliedSpec``) rather than a
    ``CandidateIdentity``. The key is in the template-hash space; a caller
    holding only a webhook payload's ``checksum`` cannot build it and must
    look the record up instead, via ``RecordStore.find_candidate(canary,
    checksum)``."""
    material = f"{namespace}/{canary_name}/{template_hash}/{phase}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

def make_key(identity: CandidateIdentity, phase: str) -> str:
    """A deterministic 32-hex-char key. Changes with template hash or phase only."""
    return make_key_parts(identity.namespace, identity.canary_name, identity.template_hash, phase)

@dataclasses.dataclass(frozen=True)
class DeploymentRecord:
    phase: str
    identity: CandidateIdentity
    created_at: str  # ISO 8601; informational only, never part of the key
    checksum: str = ""  # the webhook payload's checksum for this rollout; an
    # index, never part of the key — see checksum_label().

    def to_configmap(self, *, storage_namespace: str = "flagger-system") -> dict[str, Any]:
        key = make_key(self.identity, self.phase)
        identity_payload = self.identity.to_dict()
        if self.phase == _PROMOTED:
            # F20: pre-rollout resolved this identity before the candidate was
            # promoted, so it still carries ``is_promoted: false`` and the
            # *previous* release's ``last_promoted_spec`` — a document labelled
            # ``phase=promoted`` contradicting both. NFR4 forbids re-resolving
            # live state to fix that, so the document is stamped instead; the
            # pre-rollout answer is renamed, not dropped.
            identity_payload["last_promoted_spec_at_registration"] = identity_payload.pop("last_promoted_spec")
            identity_payload["is_promoted"] = True
            identity_payload["promoted_at"] = self.created_at
        payload = {
            "phase": self.phase,
            "created_at": self.created_at,
            "checksum": self.checksum,
            "identity": identity_payload,
        }
        labels = {
            "app.kubernetes.io/part-of": "flagger-recovery",
            CANARY_LABEL: canary_label(self.identity.namespace, self.identity.canary_name),
            PHASE_LABEL: self.phase,
            "flagger-recovery/template-hash": self.identity.template_hash,
        }
        checksum = checksum_label(self.checksum)
        if checksum is not None:
            labels[CHECKSUM_LABEL] = checksum
        metadata: dict[str, Any] = {
            "name": f"{_NAME_PREFIX}{key}",
            "namespace": storage_namespace,
            "labels": labels,
        }
        full_canary = canary_label_full(self.identity.namespace, self.identity.canary_name)
        if full_canary is not None:
            metadata["annotations"] = {_LABEL_ANNOTATION: full_canary}
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata,
            "data": {"record.json": json.dumps(payload, sort_keys=True)},
        }

    @classmethod
    def from_configmap(cls, configmap: Mapping[str, Any]) -> "DeploymentRecord":
        payload = json.loads(configmap["data"]["record.json"])
        identity_payload = dict(payload["identity"])
        # The inverse of the promoted-write stamp above, so a ``promoted``
        # record's ``.identity`` means the same pre-rollout thing everywhere
        # it is read (``decide.py``, a correction proposal): the promotion
        # stamp lives in the document, not in ``CandidateIdentity``.
        if "last_promoted_spec_at_registration" in identity_payload:
            identity_payload["last_promoted_spec"] = identity_payload.pop("last_promoted_spec_at_registration")
            identity_payload.pop("promoted_at", None)
            # Recomputed as ``identity.resolve()`` derives it, not assumed false:
            # a candidate already equal to the promoted spec at registration
            # (F8's manual rollback) must round-trip that fact too.
            identity_payload["is_promoted"] = (
                identity_payload["last_promoted_spec"] is not None
                and identity_payload["last_promoted_spec"] == identity_payload["template_hash"]
            )
        return cls(
            phase=payload["phase"],
            created_at=payload["created_at"],
            # Records written before the checksum index existed have no such
            # field; they read back with an empty checksum and stay unindexed.
            checksum=str(payload.get("checksum") or ""),
            identity=CandidateIdentity.from_dict(identity_payload),
        )

@dataclasses.dataclass(frozen=True)
class Document:
    """A non-record ConfigMap the receiver owns: an inbox ``event`` or a
    correction ``proposal``. Same create-only, deterministically named
    storage as ``DeploymentRecord``, but a free-form JSON payload."""

    kind: str  # "event" | "proposal"
    key: str  # 32 hex chars, the caller's idempotency key
    labels: Mapping[str, str]  # merged over the base ownership labels
    payload: Mapping[str, Any]

    @property
    def name(self) -> str:
        return f"{_NAME_PREFIX}{self.kind}-{self.key}"

    def to_configmap(self, *, storage_namespace: str = "flagger-system") -> dict[str, Any]:
        if not _KIND_RE.match(self.kind):
            raise ValueError(f"invalid document kind: {self.kind!r}")
        if not _KEY_RE.match(self.key):
            raise ValueError(f"invalid document key: {self.key!r}")
        labels = {}
        for name, value in self.labels.items():
            if not _LABEL_VALUE_RE.match(value):
                raise ValueError(f"label {name!r} has an invalid value: {value!r}")
            labels[name] = value
        # The ownership labels are set last so a caller cannot override them.
        labels["app.kubernetes.io/part-of"] = _PART_OF
        labels["flagger-recovery/kind"] = self.kind
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": self.name, "namespace": storage_namespace, "labels": labels},
            "data": {"document.json": json.dumps(self.payload, sort_keys=True)},
        }

    @classmethod
    def from_configmap(cls, configmap: Mapping[str, Any]) -> "Document":
        labels = dict(configmap["metadata"].get("labels") or {})
        payload = json.loads(configmap["data"]["document.json"])
        if not isinstance(payload, Mapping):
            # ``json.loads`` decodes a list, a string, a number, ``true`` or ``null`` just as
            # cleanly as an object: the first caller to treat ``payload`` as one would raise
            # ``AttributeError``, which ``_decoded`` does not catch (a bug must still 500).
            # A ``TypeError`` here does, so a non-object payload is unreadable, not a crash.
            raise TypeError(f"document payload is not an object: {type(payload).__name__}")
        return cls(
            kind=labels.get("flagger-recovery/kind", ""),
            key=configmap["metadata"]["name"].rsplit("-", 1)[-1],
            labels=labels,
            payload=payload,
        )

def _unique_candidate(matches: Sequence[DeploymentRecord], checksum: str) -> Optional[DeploymentRecord]:
    """The single candidate record for a checksum, or ``None``. Two records
    sharing one should be impossible, but ``label_value()`` can clamp two hostile
    checksums onto one label; picking an arbitrary one would break NFR4, so
    refuse. ``attribution-pending`` is recoverable, a wrong attribution is not."""
    if len(matches) > 1:
        # %r on the checksum, an unclamped payload field. The template hashes
        # need none: to_configmap() writes them as label values unclamped, so
        # the API server has already rejected anything a log could be forged with.
        LOG.warning(
            "refusing to attribute checksum %r: %d candidate records carry it (%s)",
            checksum, len(matches), ", ".join(sorted(record.identity.template_hash for record in matches)),
        )
        return None
    return matches[0] if matches else None

class RecordStore(Protocol):
    def put(self, record: DeploymentRecord) -> PutResult: ...
    def get(self, key: str) -> Optional[DeploymentRecord]: ...
    def find_candidate(self, canary: str, checksum: str) -> Optional[DeploymentRecord]: ...
    def list(self, canary: str) -> Sequence[DeploymentRecord]: ...
    def put_document(self, document: Document) -> PutResult: ...
    def get_document(self, kind: str, key: str) -> Optional[Document]: ...
    def list_documents(self, kind: str, *, canary: Optional[str] = None,
                       labels: Optional[Mapping[str, str]] = None,
                       skip_malformed: bool = False) -> Any: ...

class Transport(Protocol):
    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: Optional[bytes],
        ca_file: Optional[str], timeout: float,
    ) -> tuple[int, bytes]: ...

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. ``urllib``'s default handler re-sends the request to the
    new location with the original headers — ``Authorization`` included, host change or
    not — and hands the caller the result as if it came from the host it asked for. That
    header carries a Kubernetes service-account token, and from FRP-007b a GitHub token
    with write authority over this repository. Nothing this package calls redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib's signature
        fp.close()
        raise ApiWriteError(req.get_method(), req.full_url, code, b"refused a redirect")

class _UrllibTransport:
    """The real transport: stdlib ``urllib``, GET and POST, never more, never a redirect."""

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str], body: Optional[bytes],
        ca_file: Optional[str], timeout: float,
    ) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        handlers = [_NoRedirects()]
        if ca_file:
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)))
        try:
            with urllib.request.build_opener(*handlers).open(request, timeout=timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                status, raw = exc.code, exc.read()
        # A 3xx with no ``Location`` never reaches the handler above, and is no more
        # legitimate than one that does.
        if 300 <= status < 400:
            raise ApiWriteError(method, url, status, b"refused a redirect")
        return status, raw

class ConfigMapStore:
    """Create and read only: ``put`` is a POST create, ``get``/``list`` are
    GETs. Never a PATCH, PUT or DELETE.

    A bearer ``token`` is refused at construction unless ``base_url`` is
    ``https://`` — sending it over plain HTTP would leak it in cleartext.
    The one exception is the ``kubectl proxy`` case: a loopback
    ``http://127.0.0.1`` or ``http://localhost`` base URL is allowed, but
    only with no token at all."""

    def __init__(
        self, base_url: str, *, namespace: str = "flagger-system", token: Optional[str] = None,
        ca_file: Optional[str] = None, timeout: float = 10.0, transport: Optional[Transport] = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        is_loopback_http = parsed.scheme == "http" and parsed.hostname in _LOOPBACK_HOSTS
        if parsed.scheme != "https" and not (is_loopback_http and token is None):
            raise ValueError(
                f"ConfigMapStore requires an https:// base_url (got {base_url!r}); "
                "http:// is only allowed for loopback (127.0.0.1/localhost) and only without a token"
            )
        self._base_url = base_url.rstrip("/")
        self._namespace = namespace
        self._token = token
        self._ca_file = ca_file
        self._timeout = timeout
        self._transport = transport or _UrllibTransport()

    def put(self, record: DeploymentRecord) -> PutResult:
        return self._create(record.to_configmap(storage_namespace=self._namespace), _RECORD_OWNERSHIP)

    def put_document(self, document: Document) -> PutResult:
        return self._create(document.to_configmap(storage_namespace=self._namespace), _DOCUMENT_OWNERSHIP)

    def _create(self, configmap: Mapping[str, Any], ownership: Sequence[str]) -> PutResult:
        """The one write in this package: a ConfigMap ``create``, never a
        PATCH, PUT or DELETE. A 409 only proves the name is taken, not that
        the contents match, so a follow-up GET checks ``ownership`` labels."""
        name = configmap["metadata"]["name"]
        create_url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps"
        create_body = json.dumps(configmap).encode("utf-8")

        status, body = self._request("POST", create_url, create_body)
        if status == 201:
            return PutResult.CREATED
        if status != 409:
            raise ApiWriteError("POST", create_url, status, body)

        get_url = f"{create_url}/{name}"
        status, body = self._request("GET", get_url, None)
        if status == 404:
            # Deleted between our failed create and this check. Retry once;
            # a second conflict means something is actively racing us for
            # this name, so give up rather than loop.
            status, body = self._request("POST", create_url, create_body)
            if status == 201:
                return PutResult.CREATED
            if status == 409:
                raise ForeignConfigMap(name)
            raise ApiWriteError("POST", create_url, status, body)
        if status != 200:
            raise ApiWriteError("GET", get_url, status, body)

        existing_labels = json.loads(body).get("metadata", {}).get("labels", {})
        wanted_labels = configmap["metadata"]["labels"]
        if all(existing_labels.get(label) == wanted_labels[label] for label in ownership):
            # Every accepted hook logs "202 -" whether or not this branch is
            # taken, so without this line a duplicate write and a real write
            # are indistinguishable in the log.
            LOG.info(
                "configmap %s already exists (kind=%s): collapsed onto the existing document, nothing written",
                name, wanted_labels.get("flagger-recovery/kind", "record"),
            )
            return PutResult.DUPLICATE
        raise ForeignConfigMap(name)

    def get(self, key: str) -> Optional[DeploymentRecord]:
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid record key: {key!r}")
        body = self._get_configmap(f"{_NAME_PREFIX}{key}")
        return None if body is None else _decoded(body, DeploymentRecord.from_configmap)

    def get_document(self, kind: str, key: str) -> Optional[Document]:
        if not _KIND_RE.match(kind):
            raise ValueError(f"invalid document kind: {kind!r}")
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid document key: {key!r}")
        body = self._get_configmap(f"{_NAME_PREFIX}{kind}-{key}")
        return None if body is None else _decoded(body, Document.from_configmap)

    def find_candidate(self, canary: str, checksum: str) -> Optional[DeploymentRecord]:
        """The ``candidate`` record a webhook payload's ``checksum`` points at,
        by label selector rather than by key: only the pre-rollout hook ever
        sees both hashes at once, so the checksum it stored is the only bridge
        a later hook has back to the record. ``phase=candidate`` alone would not
        be enough — an event document's ``phase`` label is the payload's phase —
        but only a record ever carries ``CHECKSUM_LABEL``, which pins it."""
        label = checksum_label(checksum)
        if label is None:
            return None
        items = self._list({CANARY_LABEL: canary, PHASE_LABEL: _CANDIDATE, CHECKSUM_LABEL: label})
        return _unique_candidate([_decoded(item, DeploymentRecord.from_configmap) for item in items], checksum)

    def list(self, canary: str) -> Sequence[DeploymentRecord]:
        """*Records* for this canary: events, proposals and alerts carry the canary label too
        and hold no ``record.json``, so the absence of a kind is what tells the two apart."""
        return [_decoded(item, DeploymentRecord.from_configmap)
                for item in self._list({CANARY_LABEL: canary}, absent=("flagger-recovery/kind",))]

    def list_documents(self, kind: str, *, canary: Optional[str] = None,
                       labels: Optional[Mapping[str, str]] = None,
                       skip_malformed: bool = False) -> Any:
        """``labels`` narrows further, by whatever index the caller stamped on the
        document — the alert path's fingerprint, say. Selectors, so the API server does
        the filtering and one lookup does not have to list a kind in full.

        ``skip_malformed`` trades the default fail-closed listing (one ``MalformedRecord``
        aborts the whole selection) for a tolerant one: it returns ``(documents, names)``,
        where ``documents`` are the ones that decoded and ``names`` are the ConfigMap names
        of the ones that did not — never silently dropped, so the caller can count and log
        them. Only the alert sweep's own listing asks for this; every other reader stays
        fail-closed."""
        if not _KIND_RE.match(kind):
            raise ValueError(f"invalid document kind: {kind!r}")
        for name, value in (labels or {}).items():
            # The expressions ``to_configmap`` applies on the way in: a value carrying
            # ``,`` or ``=`` would add selector terms of its own, a second
            # ``flagger-recovery/kind`` among them, overriding the scoping checked above.
            if not _LABEL_NAME_RE.match(name) or not _LABEL_VALUE_RE.match(value) or len(value) > 63:
                raise ValueError(f"invalid selector label: {name!r}={value!r}")
        # Scoping last, so a caller's own ``flagger-recovery/kind`` key cannot win the merge.
        selectors = {**(labels or {}), "flagger-recovery/kind": kind}
        if canary is not None:
            selectors["flagger-recovery/canary"] = canary
        items = self._list(selectors)
        if not skip_malformed:
            return [_decoded(item, Document.from_configmap) for item in items]
        documents, unreadable = [], []
        for item in items:
            try:
                documents.append(_decoded(item, Document.from_configmap))
            except MalformedRecord as exc:
                unreadable.append(exc.name)
        return documents, unreadable

    def _get_configmap(self, name: str) -> Optional[Mapping[str, Any]]:
        url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps/{name}"
        status, body = self._request("GET", url, None)
        if status == 404:
            return None
        if status != 200:
            raise ApiWriteError("GET", url, status, body)
        return json.loads(body)

    def _list(self, selectors: Mapping[str, str], absent: Sequence[str] = ()) -> Sequence[Mapping[str, Any]]:
        selector = ",".join([f"{label}={value}" for label, value in sorted(selectors.items())]
                            + [f"!{label}" for label in absent])
        query = urllib.parse.urlencode({"labelSelector": selector})
        url = f"{self._base_url}/api/v1/namespaces/{self._namespace}/configmaps?{query}"
        status, body = self._request("GET", url, None)
        if status != 200:
            raise ApiWriteError("GET", url, status, body)
        return json.loads(body).get("items", [])

    def _request(self, method: str, url: str, body: Optional[bytes]) -> tuple[int, bytes]:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return self._transport.request(
            method, url, headers=headers, body=body, ca_file=self._ca_file, timeout=self._timeout
        )

class InMemoryStore:
    """The same ``RecordStore`` protocol, held in a dict — for the receiver's
    own tests (FRP-006), never for production durability."""

    def __init__(self) -> None:
        self._by_key: dict[str, DeploymentRecord] = {}
        self._by_canary: dict[str, list[str]] = {}
        self._documents: dict[tuple[str, str], Document] = {}
        self.writes = 0  # every accepted create, so tests can assert "nothing was written"

    def put_document(self, document: Document) -> PutResult:
        document.to_configmap()  # same validation the real store applies
        if (document.kind, document.key) in self._documents:
            return PutResult.DUPLICATE
        self._documents[(document.kind, document.key)] = document
        self.writes += 1
        return PutResult.CREATED

    def get_document(self, kind: str, key: str) -> Optional[Document]:
        return self._documents.get((kind, key))

    def list_documents(self, kind: str, *, canary: Optional[str] = None,
                       labels: Optional[Mapping[str, str]] = None,
                       skip_malformed: bool = False) -> Any:
        documents = [
            document
            for (document_kind, _), document in sorted(self._documents.items())
            if document_kind == kind
            and (canary is None or document.labels.get("flagger-recovery/canary") == canary)
            and all(document.labels.get(name) == value for name, value in (labels or {}).items())
        ]
        # Nothing here is ever undecoded -- every entry is a ``Document`` already -- so the
        # tolerant mode has no names to report; it exists only to keep the sweep's call
        # shape the same across both stores.
        return (documents, []) if skip_malformed else documents

    def put(self, record: DeploymentRecord) -> PutResult:
        key = make_key(record.identity, record.phase)
        if key in self._by_key:
            return PutResult.DUPLICATE
        self._by_key[key] = record
        label = canary_label(record.identity.namespace, record.identity.canary_name)
        self._by_canary.setdefault(label, []).append(key)
        self.writes += 1
        return PutResult.CREATED

    def get(self, key: str) -> Optional[DeploymentRecord]:
        return self._by_key.get(key)

    def find_candidate(self, canary: str, checksum: str) -> Optional[DeploymentRecord]:
        label = checksum_label(checksum)
        if label is None:
            return None
        return _unique_candidate(
            [record for record in self.list(canary)
             if record.phase == _CANDIDATE and checksum_label(record.checksum) == label],
            checksum,
        )

    def list(self, canary: str) -> Sequence[DeploymentRecord]:
        return [self._by_key[key] for key in self._by_canary.get(canary, [])]
