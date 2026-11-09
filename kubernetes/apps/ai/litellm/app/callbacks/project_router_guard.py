"""Request boundary for the isolated project-router pilot on LiteLLM 1.108.0.

The proxy stamps ``proxy_server_request`` and authenticated key metadata before
the pre-call hook. Its ``credential_fields`` records caller transport fields
before the proxy adds its own Anthropic provider-specific header. Revalidate
these pinned contracts before changing the LiteLLM image.
"""

from collections.abc import Mapping

from litellm.integrations.custom_logger import CustomLogger
from litellm.exceptions import CallerCredentialAuthenticationError
from litellm.llms.chatgpt.common_utils import CHATGPT_API_BASE

ALIAS = "project-router"
CHATGPT_ALIAS = "project-chatgpt-responses"
CLAUDE_ALIAS = "project-claude-native"
BACKENDS = {
    CHATGPT_ALIAS: "chatgpt/gpt-6.1-sol",
    CLAUDE_ALIAS: "anthropic/claude-sonnet-5-5",
}
PROVIDERS = {CHATGPT_ALIAS: "chatgpt", CLAUDE_ALIAS: "anthropic"}
FORBIDDEN_BODY_FIELDS = frozenset({
    "api_base", "base_url", "api_key", "litellm_credential_name",
    "custom_llm_provider", "provider", "provider_override", "api_type",
})
FORBIDDEN_METADATA_FIELDS = FORBIDDEN_BODY_FIELDS | frozenset({
    "headers", "extra_headers", "provider_specific_header",
})


class _ApprovedRequest:
    """In-process proof that the original proxy request passed the pre-call hook.

    LiteLLM copies request kwargs between Messages, completion, and Responses;
    JSON input cannot create this object, and deepcopy must retain its identity.
    """

    def __init__(self):
        self.phases = set()

    def __deepcopy__(self, memo):
        return self


_PROVENANCE = "_project_router_approved_request"


def _deny():
    """Return a fixed denial that cannot leak request or credential values."""
    # Never interpolate request data, key IDs, headers, or exception text.
    return CallerCredentialAuthenticationError(
        message="Project router request rejected",
        llm_provider="anthropic",
        model=ALIAS,
    )


def _header(headers, name):
    """Read a unique case-insensitive header from the captured request."""
    matches = [value for key, value in headers.items()
               if isinstance(key, str) and key.lower() == name]
    return matches[0] if len(matches) == 1 else None


def _request(data):
    """Require the proxy's captured original request body."""
    carrier = data.get("proxy_server_request")
    if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
        raise _deny()
    return carrier


def _authenticated_owner(user_api_key_dict):
    """Check the exact owner-scoped virtual-key model grant."""
    owner = getattr(user_api_key_dict, "user_id", None)
    models = getattr(user_api_key_dict, "models", None)
    return (getattr(user_api_key_dict, "via_virtual_key", False) is True
            and isinstance(owner, str) and bool(owner.strip())
            and isinstance(models, (list, tuple)) and list(models) == [ALIAS]
            and getattr(user_api_key_dict, "api_key", None) is not None)


def _clean_input(data, carrier):
    """Validate the proxy snapshot, separating its headers from caller metadata."""
    body = carrier["body"]
    if body.get("model") != ALIAS:
        raise _deny()
    # Captured before proxy-created provider_specific_header is attached.
    if carrier.get("credential_fields") != ():
        raise _deny()
    if any(body.get(name) is not None for name in FORBIDDEN_BODY_FIELDS):
        raise _deny()
    # Anthropic Messages has a top-level system field, not instructions. The
    # pinned completion-to-Responses bridge can overwrite an instructions
    # kwarg when it extracts a leading system message.
    if "instructions" in body:
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
        if isinstance(metadata, Mapping):
            forbidden = FORBIDDEN_METADATA_FIELDS
            if metadata_name == "litellm_metadata" and "headers" in metadata:
                # LiteLLM 1.108.0 overwrites this slot with its cleaned,
                # redacted headers before taking proxy_server_request.body.
                proxy_headers = carrier.get("headers")
                if (not isinstance(proxy_headers, Mapping)
                        or not isinstance(metadata["headers"], Mapping)
                        or metadata["headers"] != proxy_headers):
                    raise _deny()
                forbidden = forbidden - {"headers"}
            if any(name in metadata for name in forbidden):
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


def _normalize_chatgpt_system(messages):
    """Make Claude text blocks usable by the pinned Responses instruction bridge.

    That bridge extracts leading string system content into ``instructions`` but
    leaves list content as a system-role input, which ChatGPT rejects.
    """
    if not isinstance(messages, list):
        raise _deny()
    normalized = list(messages)
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise _deny()
        if message.get("role") != "system":
            break
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list) or not content:
            raise _deny()
        texts = []
        for block in content:
            if (not isinstance(block, Mapping) or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    or set(block) - {"type", "text", "cache_control"}):
                raise _deny()
            texts.append(block["text"])
        normalized[index] = {**message, "content": "\n".join(texts)}
    return normalized


class ProjectRouterGuard(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Enforce the single-model owner and clean request boundary before routing."""
        if not isinstance(data, dict) or data.get("model") != ALIAS:
            return data
        if not _authenticated_owner(user_api_key_dict):
            raise _deny()
        carrier = _request(data)
        _clean_input(data, carrier)
        carrier[_PROVENANCE] = _ApprovedRequest()
        return data

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        """Bind the selected backend to the router's original authorized request."""
        if not isinstance(kwargs, dict):
            return kwargs
        carrier = kwargs.get("proxy_server_request")
        if not isinstance(carrier, Mapping) or not isinstance(carrier.get("body"), Mapping):
            return kwargs
        if carrier["body"].get("model") != ALIAS:
            return kwargs
        if carrier.get("credential_fields") != ():
            raise _deny()
        if any(carrier["body"].get(name) is not None for name in FORBIDDEN_BODY_FIELDS):
            raise _deny()
        if "instructions" in carrier["body"]:
            raise _deny()
        approved = carrier.get(_PROVENANCE)
        if not isinstance(approved, _ApprovedRequest):
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
        phase = getattr(call_type, "value", call_type)
        generated_chatgpt = (phase == "aresponses" and target == CHATGPT_ALIAS
                             and {"anthropic_messages", "acompletion"} <= approved.phases
                             and kwargs.get("api_base") == CHATGPT_API_BASE
                             and isinstance(kwargs.get("api_key"), str)
                             and bool(kwargs["api_key"]))
        allowed_generated = {"api_base", "api_key"} if generated_chatgpt else set()
        if (kwargs.get("custom_llm_provider") not in (None, PROVIDERS[target])
                or any(kwargs.get(name) is not None for name in
                       FORBIDDEN_BODY_FIELDS - {"custom_llm_provider"} - allowed_generated)):
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
            if phase == "acompletion" and "messages" in kwargs:
                kwargs["messages"] = _normalize_chatgpt_system(kwargs["messages"])
        if phase in ("anthropic_messages", "acompletion"):
            approved.phases.add(phase)
        return kwargs


proxy_handler_instance = ProjectRouterGuard()
