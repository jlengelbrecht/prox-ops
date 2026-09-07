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
