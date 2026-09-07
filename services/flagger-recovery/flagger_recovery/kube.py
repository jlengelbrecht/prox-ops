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
