"""Request boundary for the isolated project-router pilot on LiteLLM 1.108.0.

The proxy stamps ``proxy_server_request`` and authenticated key metadata before
the pre-call hook. Its ``credential_fields`` records caller transport fields
before the proxy adds its own Anthropic provider-specific header. Revalidate
these pinned contracts before changing the LiteLLM image.
"""

from collections.abc import Mapping

from litellm.integrations.custom_logger import CustomLogger
from litellm.exceptions import CallerCredentialAuthenticationError

ALIAS = "project-router"
CHATGPT_ALIAS = "project-chatgpt-responses"
CLAUDE_ALIAS = "project-claude-native"
BACKENDS = {
    CHATGPT_ALIAS: "chatgpt/gpt-6.1-sol",
    CLAUDE_ALIAS: "anthropic/claude-sonnet-5-5",
}
FORBIDDEN_BODY_FIELDS = frozenset({
    "api_base", "base_url", "api_key", "litellm_credential_name",
    "custom_llm_provider", "provider", "provider_override", "api_type",
})
FORBIDDEN_METADATA_FIELDS = FORBIDDEN_BODY_FIELDS | frozenset({
    "headers", "extra_headers", "provider_specific_header",
})


def _deny():
    # Never interpolate request data, key IDs, headers, or exception text.
    return CallerCredentialAuthenticationError(
        message="Project router request rejected",
        llm_provider="anthropic",
        model=ALIAS,
    )


def _header(headers, name):
    matches = [value for key, value in headers.items()
               if isinstance(key, str) and key.lower() == name]
    return matches[0] if len(matches) == 1 else None


def _request(data):
    carrier = data.get("proxy_server_request")
    if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
        raise _deny()
    return carrier


def _authenticated_owner(user_api_key_dict):
    owner = getattr(user_api_key_dict, "user_id", None)
    models = getattr(user_api_key_dict, "models", None)
    return (getattr(user_api_key_dict, "via_virtual_key", False) is True
            and isinstance(owner, str) and bool(owner.strip())
            and isinstance(models, (list, tuple)) and list(models) == [ALIAS]
            and getattr(user_api_key_dict, "api_key", None) is not None)


def _clean_input(data, carrier):
    body = carrier["body"]
    if body.get("model") != ALIAS:
        raise _deny()
    # Captured before proxy-created provider_specific_header is attached.
    if carrier.get("credential_fields") != ():
        raise _deny()
    if any(body.get(name) is not None for name in FORBIDDEN_BODY_FIELDS):
        raise _deny()
    # A proxy traceparent may be added internally. It cannot carry an upstream
    # credential or destination; all other extra_headers are refused.
    extra = body.get("extra_headers")
    if extra is not None and (not isinstance(extra, Mapping)
                              or set(extra) != {"traceparent"}
                              or not isinstance(extra["traceparent"], str)):
        raise _deny()
    for metadata_name in ("metadata", "litellm_metadata"):
        metadata = body.get(metadata_name)
        if isinstance(metadata, Mapping) and any(name in metadata for name in FORBIDDEN_METADATA_FIELDS):
            raise _deny()
    if data.get("model") != ALIAS:
        raise _deny()
    secret_fields = data.get("secret_fields")
    raw_headers = secret_fields.get("raw_headers") if isinstance(secret_fields, Mapping) else None
    if not isinstance(raw_headers, Mapping):
        raise _deny()
    bearer = _header(raw_headers, "authorization")
    gateway = _header(raw_headers, "x-litellm-api-key")
    if (not isinstance(bearer, str) or not bearer.startswith("Bearer sk-ant-oat")
            or not isinstance(gateway, str) or not gateway
            or _header(raw_headers, "x-api-key") is not None
            or _header(raw_headers, "proxy-authorization") is not None):
        raise _deny()


class ProjectRouterGuard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict) or data.get("model") != ALIAS:
            return data
        if not _authenticated_owner(user_api_key_dict):
            raise _deny()
        _clean_input(data, _request(data))
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        if not isinstance(kwargs, dict):
            return kwargs
        carrier = kwargs.get("proxy_server_request")
        if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
            return kwargs
        if carrier["body"].get("model") != ALIAS:
            return kwargs
        if carrier.get("credential_fields") != ():
            raise _deny()
        buckets = [item for item in (kwargs.get("metadata"), kwargs.get("litellm_metadata"))
                   if isinstance(item, Mapping)]
        selected = {item.get("deployment_model_name") for item in buckets
                    if item.get("deployment_model_name") is not None}
        if len(selected) != 1 or next(iter(selected)) not in BACKENDS:
            raise _deny()
        target = next(iter(selected))
        if kwargs.get("model") != BACKENDS[target]:
            raise _deny()
        if any(item.get("deployment") not in (None, BACKENDS[target])
               or item.get("api_base") is not None or item.get("base_url") is not None
               for item in buckets):
            raise _deny()
        if not any(isinstance(item.get("user_api_key_user_id"), str)
                   and item["user_api_key_user_id"].strip() for item in buckets):
            raise _deny()
        if any(kwargs.get(name) is not None for name in FORBIDDEN_BODY_FIELDS):
            raise _deny()
        extra = kwargs.get("extra_headers")
        if extra is not None and (not isinstance(extra, Mapping)
                                  or set(extra) != {"traceparent"}
                                  or not isinstance(extra["traceparent"], str)):
            raise _deny()
        if target == CHATGPT_ALIAS:
            # The pinned proxy creates this only for Anthropic OAuth, but
            # removing it here also prevents accidental future adapter reuse.
            kwargs.pop("provider_specific_header", None)
        return kwargs


proxy_handler_instance = ProjectRouterGuard()
