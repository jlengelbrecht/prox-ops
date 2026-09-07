"""A GET-only client for the Kubernetes API. No writes live here on purpose,
so the AST safety gate and a reviewer both have one small place to check."""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Optional

class ApiError(Exception):
    """Raised when the Kubernetes API returns a non-2xx response."""

    def __init__(self, status: int, url: str, body: str = "") -> None:
        super().__init__(f"GET {url} -> HTTP {status}: {body}")
        self.status = status
        self.url = url
        self.body = body

class ApiReader:
    """Reads Kubernetes objects as plain dicts. ``base_url`` is typically
    ``http://127.0.0.1:8001`` from ``kubectl proxy`` for local smoke testing,
    or the in-cluster API server with a bearer token and CA file."""

    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        ca_file: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._ca_file = ca_file
        self._timeout = timeout

    def get(self, path: str, *, params: Optional[Mapping[str, str]] = None) -> Any:
        """GET a path such as ``/apis/flagger.app/v1beta1/namespaces/ns/canaries/name``."""
        url = self._base_url + path
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        context = ssl.create_default_context(cafile=self._ca_file) if self._ca_file else None
        try:
            with urllib.request.urlopen(request, timeout=self._timeout, context=context) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, url, exc.read().decode("utf-8", "replace")) from exc
        return json.loads(payload)

class CandidateReader:
    """Fetches exactly the objects ``identity.resolve()`` takes, keyed by its
    parameter names, so a caller can write ``resolve(**reader.read(ns, name))``.
    GET only — this class adds no write path."""

    def __init__(
        self,
        api: ApiReader,
        *,
        kustomization_namespace: str = "flagger-system",
        kustomization_name: str = "flagger-pilot-app",
    ) -> None:
        self._api = api
        self._kustomization_namespace = kustomization_namespace
        self._kustomization_name = kustomization_name

    def read(self, namespace: str, canary_name: str) -> dict[str, Any]:
        namespace_path = urllib.parse.quote(namespace, safe="")
        name_path = urllib.parse.quote(canary_name, safe="")
        canary = self._api.get(
            f"/apis/flagger.app/v1beta1/namespaces/{namespace_path}/canaries/{name_path}"
        )
        target = urllib.parse.quote(canary["spec"]["targetRef"]["name"], safe="")
        kustomization_namespace = urllib.parse.quote(self._kustomization_namespace, safe="")
        kustomization_name = urllib.parse.quote(self._kustomization_name, safe="")
        return {
            "canary": canary,
            "deployment": self._api.get(f"/apis/apps/v1/namespaces/{namespace_path}/deployments/{target}"),
            "candidate_replicasets": self._api.get(
                f"/apis/apps/v1/namespaces/{namespace_path}/replicasets"
            ).get("items", []),
            "candidate_pods": self._api.get(f"/api/v1/namespaces/{namespace_path}/pods").get("items", []),
            "helmrelease": self._api.get(
                f"/apis/helm.toolkit.fluxcd.io/v2/namespaces/{namespace_path}/helmreleases/{name_path}"
            ),
            "ocirepository": self._api.get(
                f"/apis/source.toolkit.fluxcd.io/v1/namespaces/{namespace_path}/ocirepositories/{name_path}"
            ),
            "kustomization": self._api.get(
                f"/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/{kustomization_namespace}"
                f"/kustomizations/{kustomization_name}"
            ),
        }

def _observed(deployment: Mapping[str, Any]) -> bool:
    """True when the controller has seen the object's current spec, so Flagger's hash of it is settled."""
    generation = deployment.get("metadata", {}).get("generation")
    return generation is None or deployment.get("status", {}).get("observedGeneration") == generation

def _serving(deployment: Mapping[str, Any]) -> bool:
    """True when a Deployment runs exactly the replicas it wants, all ready.
    Asked of the primary only: Flagger scales the *target* to zero after every
    rollout, so "no replicas" is its normal state. ``replicas != spec.replicas``
    catches a rollout in flight, because the surge runs pods for both specs at
    once; ``updatedReplicas`` is checked only when reported (``omitempty``)."""
    status = deployment.get("status", {})
    wanted = deployment.get("spec", {}).get("replicas", 1)
    if not _observed(deployment) or status.get("replicas") != wanted:
        return False
    updated = status.get("updatedReplicas")
    return status.get("readyReplicas") == wanted and (updated is None or updated == wanted)

class LiveCanary:
    """A ``decide.LiveState`` over the live cluster. GET only, and one read per
    object per instance — a decision is made against a single snapshot, so a
    new instance is built per event rather than reused. Both hash methods answer
    in **Flagger's own hash space**; no second hasher produces comparable output
    (``identity`` explains why the pod-template-hash label does not). What they
    add over reading ``status`` directly is the serving check — Flagger writes
    ``lastAppliedSpec`` and ``lastPromotedSpec`` when it *decides* something,
    and the Deployment behind it may not have caught up — so each answers
    ``None`` rather than let ``decide`` act on a spec nothing is serving."""

    def __init__(self, api: ApiReader, namespace: str, canary_name: str) -> None:
        self._api = api
        self._namespace = urllib.parse.quote(namespace, safe="")
        self._name = urllib.parse.quote(canary_name, safe="")
        self._cache: dict[str, Any] = {}

    def _get(self, path: str) -> Any:
        if path not in self._cache:
            self._cache[path] = self._api.get(path)
        return self._cache[path]

    def _canary(self) -> Mapping[str, Any]:
        return self._get(f"/apis/flagger.app/v1beta1/namespaces/{self._namespace}/canaries/{self._name}")

    def _deployment(self, suffix: str = "") -> Mapping[str, Any]:
        target = urllib.parse.quote(self._canary()["spec"]["targetRef"]["name"] + suffix, safe="")
        return self._get(f"/apis/apps/v1/namespaces/{self._namespace}/deployments/{target}")

    def canary_status(self) -> Mapping[str, Any]:
        return self._canary().get("status", {})

    def deployment_template_hash(self) -> Optional[str]:
        """The hash of the spec the target Deployment now carries — ``None`` while
        its controller has not observed it, because Flagger's hash of that spec
        is then about to change."""
        if not _observed(self._deployment()):
            return None
        return self.canary_status().get("lastAppliedSpec") or None

    def primary_template_hash(self) -> Optional[str]:
        """The hash of the spec the ``-primary`` Deployment is serving now —
        ``None`` while it is mid-rollout or degraded, which is exactly when
        "the primary serves the last promoted spec" is false."""
        if not _serving(self._deployment("-primary")):
            return None
        return self.canary_status().get("lastPromotedSpec") or None

    def functional_check(self) -> Optional[bool]:
        """``None``: the receiver runs no prober of its own. The apex's
        semantic check is the Canary's ``functional-check`` rollout webhook,
        which runs on the loadtester during an analysis and has no path back
        here. ``decide`` reads ``None`` as "not consulted", never as a pass."""
        return None
