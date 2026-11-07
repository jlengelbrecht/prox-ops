"""Fail-closed session binding for the one project Copilot deployment.

Pinned to LiteLLM 1.108.0's async_pre_call_deployment_hook contract. Revalidate
this contract and callback order before upgrading the image.
"""
from collections.abc import Mapping
from urllib.parse import urlparse

from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.credential_accessor import CredentialAccessor
from litellm.llms.github_copilot.per_user_auth import (
    GITHUB_COPILOT_USER_SESSION_KWARG_KEY,
    GithubCopilotUserSession,
    aattach_github_copilot_user_session,
)
from litellm.constants import GITHUB_COPILOT_AUTH_TYPE_KEY, GITHUB_COPILOT_PER_USER_AUTH_TYPE
from litellm.exceptions import CallerCredentialAuthenticationError

ALIAS = "project-copilot-explicit"
BACKEND = "github_copilot/gpt-6-luna"
CREDENTIAL = "project-github-copilot"


def _deny():
    # A fixed message prevents credentials, headers, and provider exceptions
    # from entering proxy error logs or API responses.
    return CallerCredentialAuthenticationError(
        message="Project Copilot connection unavailable",
        llm_provider="github_copilot",
        model=BACKEND,
    )


def _selected(kwargs):
    # Chat/completions routes use metadata; Responses and generic routes can
    # carry litellm_metadata. A conflicting marker is never accepted.
    buckets = [kwargs.get("metadata"), kwargs.get("litellm_metadata")]
    names = [b.get("deployment_model_name") for b in buckets if isinstance(b, Mapping)]
    return ALIAS in names or (
        kwargs.get("model") == BACKEND and kwargs.get("litellm_credential_name") == CREDENTIAL
    )


def _trusted_base(value):
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host == "githubcopilot.com" or host.endswith(".githubcopilot.com"))
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
    )


class CopilotSessionGuard(CustomLogger):
    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        if not _selected(kwargs):
            return kwargs
        try:
            for bucket in (kwargs.get("metadata"), kwargs.get("litellm_metadata")):
                if isinstance(bucket, Mapping):
                    selected = bucket.get("deployment_model_name")
                    if selected is not None and selected != ALIAS:
                        raise _deny()
                    if selected == ALIAS and bucket.get("deployment") != BACKEND:
                        raise _deny()
                    if selected == ALIAS and bucket.get("api_base") is not None:
                        raise _deny()
            if not any(
                isinstance(bucket, Mapping) and bucket.get("deployment_model_name") == ALIAS
                for bucket in (kwargs.get("metadata"), kwargs.get("litellm_metadata"))
            ):
                raise _deny()
            if kwargs.get("model") != BACKEND or kwargs.get("litellm_credential_name") != CREDENTIAL:
                raise _deny()
            if kwargs.get("api_base") is not None or kwargs.get("api_key") is not None:
                raise _deny()
            if GITHUB_COPILOT_USER_SESSION_KWARG_KEY in kwargs:
                raise _deny()
            credential = CredentialAccessor.find_credential(CREDENTIAL)
            if credential is None or credential.credential_values.get(GITHUB_COPILOT_AUTH_TYPE_KEY) != GITHUB_COPILOT_PER_USER_AUTH_TYPE:
                raise _deny()
            fields = kwargs.get("secret_fields")
            if not isinstance(fields, Mapping):
                raise _deny()
            owner = fields.get("user_provider_credentials_user_id")
            tokens = fields.get("user_provider_credentials")
            if not isinstance(owner, str) or not owner or not isinstance(tokens, Mapping):
                raise _deny()
            if not isinstance(tokens.get(CREDENTIAL), str) or not tokens.get(CREDENTIAL):
                raise _deny()
            await aattach_github_copilot_user_session(kwargs)
            session = kwargs.get(GITHUB_COPILOT_USER_SESSION_KWARG_KEY)
            if not isinstance(session, GithubCopilotUserSession) or not session.token or not _trusted_base(session.api_base):
                raise _deny()
            return kwargs
        except Exception:
            raise _deny() from None


# LiteLLM 1.108.0 loads the dotted attribute as-is; it does not instantiate
# callback classes and rejects them at startup.
proxy_handler_instance = CopilotSessionGuard()
