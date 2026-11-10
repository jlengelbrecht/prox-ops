"""Owner-only Grok subscription alias on the pinned LiteLLM async path.

The proxy captures the original request and authenticated virtual-key metadata
before these hooks. Keep the request proof in process; never put it in JSON.
"""

import asyncio
from collections.abc import Mapping
import copy
import json
import re

import httpx
from litellm.exceptions import CallerCredentialAuthenticationError
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.xai.oauth import XAIOAuthAuthenticator

ALIAS = "project-grok-native"
BACKEND = "xai/grok-4.7-build-fast"
BASE = "https://cli-chat-proxy.grok.com/v1"
USERINFO = "https://auth.x.ai/oauth2/userinfo"
COMPAT_VERSION = "1.0.77"
CLIENT_ID = "litellm-project-grok-native"
_PROOF = "_project_grok_native_approved"
_SUBJECT = re.compile(r"[A-Za-z0-9._@-]{1,128}\Z")
_FORBIDDEN = frozenset({"api_key", "api_base", "base_url", "litellm_credential_name",
                        "custom_llm_provider", "provider", "provider_override", "api_type",
                        "headers", "extra_headers", "provider_specific_header", "use_xai_oauth"})
_ANTHROPIC_API_SCOPE = "anthropic,bedrock,bedrock_mantle,vertex_ai"
_GENERATED_HEADERS = {
    _ANTHROPIC_API_SCOPE: frozenset({"anthropic-version", "anthropic-beta"}),
    "anthropic": frozenset({"authorization"}),
}


class _Approved:
    def __init__(self, owner, carrier):
        self.owner = owner
        self.carrier = carrier
        self.body = copy.deepcopy(carrier["body"])
        self.binding = None

    def __deepcopy__(self, memo):
        return self


def _deny():
    return CallerCredentialAuthenticationError(
        message="Project Grok request rejected", llm_provider="xai", model=ALIAS)


def _header(headers, name):
    values = [value for key, value in headers.items()
              if isinstance(key, str) and key.lower() == name]
    return values[0] if len(values) == 1 else None


def _has_header(headers, name):
    return any(isinstance(key, str) and key.lower() == name for key in headers)


def _generated_anthropic_headers(value):
    """Recognize only the pinned proxy's bounded ProviderSpecificHeader shape."""
    if isinstance(value, list) and len(value) != 2:
        return False
    entries = value if isinstance(value, list) else [value]
    if not 1 <= len(entries) <= 2:
        return False
    if isinstance(value, list) and [entry.get("custom_llm_provider")
                                    if isinstance(entry, Mapping) else None
                                    for entry in entries] != [
                                        _ANTHROPIC_API_SCOPE, "anthropic"]:
        return False
    scopes = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {
                "custom_llm_provider", "extra_headers"}:
            return False
        scope = entry["custom_llm_provider"]
        headers = entry["extra_headers"]
        if (not isinstance(scope, str) or scope not in _GENERATED_HEADERS
                or scope in scopes or not isinstance(headers, Mapping)
                or not headers or len(headers) > len(_GENERATED_HEADERS[scope])):
            return False
        if any(not isinstance(name, str) or name not in _GENERATED_HEADERS[scope]
               or not isinstance(header, str) or not header
               for name, header in headers.items()):
            return False
        if scope == "anthropic":
            auth = headers["authorization"]
            if not auth.removeprefix("Bearer ").startswith("sk-ant-oat"):
                return False
        scopes.add(scope)
    return True


def _native_headers(subject):
    return {
        "X-XAI-Token-Auth": "xai-grok-cli", "x-userid": subject,
        "x-grok-client-version": COMPAT_VERSION,
        "x-grok-client-identifier": CLIENT_ID, "User-Agent": CLIENT_ID,
        "x-grok-model-override": "grok-4.7-build-fast",
    }


def _validate_request(owner, data):
    if (getattr(owner, "via_virtual_key", False) is not True
            or not isinstance(getattr(owner, "user_id", None), str)
            or not owner.user_id.strip()
            or list(getattr(owner, "models", None) or []) != [ALIAS]
            or getattr(owner, "api_key", None) is None):
        raise _deny()
    carrier = data.get("proxy_server_request")
    if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
        raise _deny()
    body = carrier["body"]
    if (body.get("model") != ALIAS or carrier.get("credential_fields") != ()
            or "use_xai_oauth" in body
            or any(body.get(field) is not None for field in _FORBIDDEN)):
        raise _deny()
    for field in ("metadata", "litellm_metadata"):
        item = body.get(field)
        if isinstance(item, Mapping):
            forbidden = _FORBIDDEN
            # The pinned proxy writes its cleaned header snapshot here itself.
            if field == "litellm_metadata" and "headers" in item:
                if (not isinstance(carrier.get("headers"), Mapping)
                        or item["headers"] != carrier["headers"]):
                    raise _deny()
                forbidden = forbidden - {"headers"}
            if any(name in item for name in forbidden):
                raise _deny()
    secret = data.get("secret_fields")
    headers = secret.get("raw_headers") if isinstance(secret, Mapping) else None
    if not isinstance(headers, Mapping):
        raise _deny()
    gateway = _header(headers, "x-litellm-api-key")
    bearer = _header(headers, "authorization")
    if (not isinstance(gateway, str) or not gateway
            or not isinstance(bearer, str) or not bearer.startswith("Bearer sk-ant-oat")
            or _has_header(headers, "x-api-key")
            or _has_header(headers, "proxy-authorization")):
        raise _deny()
    return carrier


async def _resolve_identity():
    try:
        # Native refresh can perform blocking disk and HTTP I/O.
        token = await asyncio.wait_for(
            asyncio.to_thread(XAIOAuthAuthenticator().get_access_token), timeout=15)
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                     timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            async with client.stream("GET", USERINFO, headers={
                    "Authorization": "Bearer " + token, "Accept": "application/json"}) as response:
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 8192:
                        raise ValueError
                payload = json.loads(chunks)
        subject = payload.get("sub") if isinstance(payload, dict) else None
        if not isinstance(subject, str) or not _SUBJECT.fullmatch(subject):
            raise ValueError
        return token, subject
    except Exception:
        # OAuth, HTTP and parsing exceptions may carry credentials or identity.
        raise _deny() from None


class ProjectGrokNative(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict) or data.get("model") != ALIAS:
            return data
        carrier = _validate_request(user_api_key_dict, data)
        carrier[_PROOF] = _Approved(user_api_key_dict.user_id, carrier)
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        if not isinstance(kwargs, dict):
            return kwargs
        carrier = kwargs.get("proxy_server_request")
        if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
            return kwargs
        if carrier["body"].get("model") != ALIAS:
            return kwargs
        proof = carrier.get(_PROOF)
        if (not isinstance(proof, _Approved)
                or proof.carrier is not carrier
                or carrier.get("credential_fields") != ()
                # The pinned proxy refreshes this body snapshot after request
                # callbacks. Its generated metadata can change, but the
                # original request fields must retain their values.
                or any(carrier["body"].get(name) != value for name, value in
                       proof.body.items() if name not in {"metadata", "litellm_metadata"})
                or "use_xai_oauth" in carrier["body"]
                or any(carrier["body"].get(name) is not None for name in _FORBIDDEN)):
            raise _deny()
        meta = [item for item in (kwargs.get("metadata"), kwargs.get("litellm_metadata"))
                if isinstance(item, Mapping)]
        selected = {item.get("deployment_model_name") for item in meta
                    if item.get("deployment_model_name") is not None}
        if (selected != {ALIAS} or kwargs.get("model") != BACKEND
                or not any(item.get("user_api_key_user_id") == proof.owner for item in meta)
                or any(item.get("user_api_key_user_id") not in (None, proof.owner)
                       for item in meta)
                or any(item.get("deployment") not in (None, BACKEND)
                       or item.get("api_base") is not None or item.get("base_url") is not None
                       for item in meta)
                or kwargs.get("custom_llm_provider") not in (None, "xai")
                # The pinned Router materializes its default native-OAuth flag.
                # This deployment supplies its own bearer, so require False.
                or kwargs.get("use_xai_oauth") is not False
                or any(kwargs.get(name) is not None for name in _FORBIDDEN - {
                    "custom_llm_provider", "provider_specific_header", "use_xai_oauth",
                    "api_key", "api_base", "extra_headers"})):
            raise _deny()
        if proof.binding is not None:
            token, subject = proof.binding
            if (kwargs.get("provider_specific_header") is not None
                    or kwargs.get("api_key") != token
                    or kwargs.get("api_base") != BASE
                    or kwargs.get("extra_headers") != _native_headers(subject)):
                raise _deny()
            return kwargs
        if any(kwargs.get(name) is not None for name in (
                "api_key", "api_base", "extra_headers")):
            raise _deny()
        # Pinned proxy creates this Anthropic OAuth carrier after capturing the
        # request. Grok must never forward it to the xAI transport.
        generated = kwargs.get("provider_specific_header")
        if generated is None or not _generated_anthropic_headers(generated):
            raise _deny()
        kwargs.pop("provider_specific_header")
        token, subject = await _resolve_identity()
        kwargs["api_key"] = token  # OAuth bearer transport input, never an API-key credential.
        kwargs["api_base"] = BASE
        kwargs["custom_llm_provider"] = "xai"
        kwargs["extra_headers"] = _native_headers(subject)
        proof.binding = (token, subject)
        return kwargs

    async def async_post_call_failure_hook(
            self, request_data, original_exception, user_api_key_dict,
            traceback_str=None):
        # The proxy accepts a returned HTTPException as its client-facing error.
        # Never interpolate the provider exception: response bodies may contain
        # credentials or the subscription subject.
        carrier = request_data.get("proxy_server_request") if isinstance(request_data, Mapping) else None
        original_model = (carrier.get("body", {}).get("model")
                          if isinstance(carrier, Mapping)
                          and isinstance(carrier.get("body"), Mapping) else None)
        if (original_model == ALIAS or
                isinstance(request_data, Mapping) and request_data.get("model") == ALIAS):
            from fastapi import HTTPException
            return HTTPException(status_code=502, detail="Project Grok provider failed")
        return None


proxy_handler_instance = ProjectGrokNative()
