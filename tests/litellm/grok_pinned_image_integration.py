"""Closed ASGI Messages qualification in the digest-pinned LiteLLM image.

A typed virtual-key dependency replaces database lookup. All request processing,
router selection, callback dispatch and the xAI SDK remain installed code.
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import traceback
from unittest.mock import patch

CHECKS = {}
FAILURES = {}
STAGE = "imports"
CASE = "setup"
EVIDENCE = {"statuses": {"first": 599, "second": 599}, "calls": [], "outbound_count": 0,
            "destination": "none", "credential_override": False, "pre_hook_error": None,
            "callback_error": None, "denial_statuses": {}, "afterstream": {},
            "afterstream_log_sites": []}
CLASSES = {"AssertionError", "ImportError", "ModuleNotFoundError", "RuntimeError",
           "SystemExit", "KeyboardInterrupt", "FileNotFoundError", "PermissionError",
           "ValueError", "TypeError", "KeyError", "AttributeError", "OSError"}


def closed_class(exc):
    name = type(exc).__name__
    return name if name in CLASSES else "OtherError"


def check(name, predicate):
    try:
        assert predicate(), name
    except BaseException as exc:
        CHECKS[name] = "fail"
        FAILURES[name] = {"class": closed_class(exc), "predicate": name, "case": CASE}
    else:
        if CHECKS.get(name) != "fail":
            CHECKS[name] = "pass"


def denied_at_boundary(response, calls, calls_before, outbound, outbound_before, kinds,
                       marker="Project Grok request rejected"):
    return (response.status_code == 401 and marker in response.text
            and [item[0] for item in calls[calls_before:]] == kinds
            and len(outbound) == outbound_before)


def linked_tool_result(messages):
    """Return content only for the exact assistant call and its following result."""
    if not isinstance(messages, list) or len(messages) < 2:
        return None
    calls = []
    for message in messages:
        if not isinstance(message, dict):
            return None
        if message.get("role") == "assistant":
            calls.extend(message.get("tool_calls") or [])
    if len(calls) != 1:
        return None
    call = calls[0]
    function = call.get("function") or {}
    try:
        arguments = json.loads(function.get("arguments", ""))
    except (TypeError, ValueError):
        return None
    if (call.get("id") != "synthetic-call" or call.get("type") != "function"
            or function.get("name") != "Read" or arguments != {}):
        return None
    results = [message for message in messages if message.get("role") == "tool"]
    if (len(results) != 1 or results[0].get("tool_call_id") != call["id"]
            or results[0].get("content") != "cedar"
            or messages.index(results[0]) <= next(index for index, message in
                enumerate(messages) if message.get("role") == "assistant" and call in
                (message.get("tool_calls") or []))):
        return None
    return results[0]["content"]


def parsed_tool_stream(payload):
    """Validate the ordered Anthropic tool-use SSE exchange."""
    frames = []
    for raw in payload.strip().split("\n\n"):
        fields = {}
        for line in raw.splitlines():
            if not line or line.startswith(":"):
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in {"event", "data"} or key in fields:
                return False
            fields[key] = value.lstrip(" ")
        if set(fields) != {"event", "data"}:
            return False
        try:
            data = json.loads(fields["data"])
        except ValueError:
            return False
        if not isinstance(data, dict) or data.get("type") != fields["event"]:
            return False
        if fields["event"] != "ping":
            frames.append((fields["event"], data))
    kinds = [kind for kind, _ in frames]
    if (len(kinds) < 6 or kinds[:2] != ["message_start", "content_block_start"]
            or kinds[-3:] != ["content_block_stop", "message_delta", "message_stop"]
            or any(kind != "content_block_delta" for kind in kinds[2:-3])):
        return False
    start, block = frames[0][1], frames[1][1]
    deltas = [data for _, data in frames[2:-3]]
    stop, message_delta, end = [data for _, data in frames[-3:]]
    content = block.get("content_block") or {}
    fragments = [delta.get("delta") or {} for delta in deltas]
    try:
        tool_input = json.loads("".join(fragment.get("partial_json", "")
            for fragment in fragments))
    except (TypeError, ValueError):
        return False
    return (isinstance(start.get("message"), dict)
            and content.get("type") == "tool_use"
            and content.get("id") == "synthetic-call"
            and content.get("name") == "Read"
            and content.get("input") == {}
            and all(block.get("index") == delta.get("index") for delta in deltas)
            and block.get("index") == stop.get("index")
            and all(fragment.get("type") == "input_json_delta" for fragment in fragments)
            and tool_input == {}
            and (message_delta.get("delta") or {}).get("stop_reason") == "tool_use"
            and end.get("type") == "message_stop")



def terminal_error_event(payload):
    """Require one final Anthropic error frame after a started message."""
    frames = []
    for raw in payload.strip().split("\n\n"):
        fields = {}
        for line in raw.splitlines():
            if not line or line.startswith(":"):
                continue
            key, separator, value = line.partition(":")
            if not separator or key not in {"event", "data"} or key in fields:
                return False
            fields[key] = value.lstrip(" ")
        if set(fields) != {"event", "data"}:
            return False
        try:
            data = json.loads(fields["data"])
        except (ValueError, TypeError):
            return False
        if not isinstance(data, dict) or data.get("type") != fields["event"]:
            return False
        frames.append((fields["event"], data))
    if len(frames) < 2 or frames[0][0] != "message_start":
        return False
    message = frames[0][1].get("message")
    if (not isinstance(message, dict) or message.get("type") != "message"
            or message.get("role") != "assistant"
            or not isinstance(message.get("content"), list)):
        return False
    if sum(kind == "error" for kind, _ in frames) != 1 or frames[-1][0] != "error":
        return False
    if any(kind in {"message_stop", "error"} for kind, _ in frames[:-1]):
        return False
    error = frames[-1][1].get("error")
    return (isinstance(error, dict) and error.get("type") == "api_error"
            and error.get("message") == "Project Grok provider failed"
            and set(error) == {"type", "message"})


def canonical_error_body(response):
    """Compare exact stable JSON fields, excluding generated request IDs."""
    try:
        body = response.json()
    except (ValueError, TypeError):
        return hashlib.sha256(response.content).hexdigest()
    if not isinstance(body, dict):
        return hashlib.sha256(response.content).hexdigest()
    error = body.get("error")
    if isinstance(error, dict):
        error.pop("litellm_call_id", None)
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def traced_hook(base, state, record, backend):
    class Traced(base):
        async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
            state["calls"].append(("request", str(call_type), data.get("model")))
            record("request", str(call_type), data.get("model"), data.get("proxy_server_request"))
            try:
                return await super().async_pre_call_hook(
                    user_api_key_dict=user_api_key_dict, cache=cache, data=data, call_type=call_type)
            except Exception as exc:
                state["denial_provenance"].append(
                    type(exc).__name__ == "CallerCredentialAuthenticationError"
                    and getattr(exc, "message", None) ==
                    "litellm.AuthenticationError: Project Grok request rejected"
                    and getattr(exc, "llm_provider", None) == "xai"
                    and getattr(exc, "model", None) == "project-grok-native"
                    and getattr(exc, "status_code", None) == 401)
                raise

        async def async_pre_call_deployment_hook(self, kwargs, call_type):
            carrier = kwargs.get("proxy_server_request")
            state["calls"].append(("deployment", str(call_type),
                carrier.get("body", {}).get("model") if isinstance(carrier, dict) else None))
            record("deployment", str(call_type), kwargs.get("model"), carrier,
                   "provider_specific_header" in kwargs)
            try:
                result = await super().async_pre_call_deployment_hook(kwargs, call_type)
            except Exception as exc:
                if EVIDENCE["callback_error"] is None:
                    lines = [frame.lineno for frame in traceback.extract_tb(exc.__traceback__)
                             if frame.filename.endswith("/project_grok_native.py")]
                    EVIDENCE["callback_error"] = {"class": closed_class(exc),
                                                  "line": lines[-1] if lines else 0}
                raise
            carrier_body = carrier.get("body") if isinstance(carrier, dict) else None
            if (result.get("model") == backend and isinstance(carrier_body, dict)
                    and carrier_body.get("model") == "project-grok-native"):
                check("generated_anthropic_carrier_removed",
                      lambda: "provider_specific_header" not in result)
                state["bindings"].append(
                    result.get("api_key") == state["token"]
                    and result.get("api_base") == "https://cli-chat-proxy.grok.com/v1"
                    and result.get("custom_llm_provider") == "xai"
                    and result.get("extra_headers", {}).get("x-userid") == state["subject"]
                    and result.get("extra_headers", {}).get("X-XAI-Token-Auth") == "xai-grok-cli"
                    and "provider_specific_header" not in result)
            return result
    return Traced()


async def qualify():
    global STAGE, CASE
    import httpx
    import litellm
    from fastapi import FastAPI, HTTPException
    from litellm.llms.xai.oauth import XAIOAuthAuthenticator
    from litellm.types.proxy.auth.user_api_key_auth import UserAPIKeyAuth
    from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
    from litellm.proxy.anthropic_endpoints.endpoints import router as messages_router
    from litellm.proxy import proxy_server
    from litellm.proxy.common_request_processing import ProxyBaseLLMRequestProcessing
    from litellm.exceptions import CallerCredentialAuthenticationError
    import project_grok_native as grok_callback
    from project_grok_native import ProjectGrokNative, ALIAS, BACKEND, BASE, USERINFO, _PROOF
    STAGE = "setup"

    state = {"calls": [], "outbound": [], "token": "synthetic-oauth-token",
             "subject": "synthetic-subject", "grant": [ALIAS], "refresh_failure": False,
             "token_calls": 0, "bindings": [], "failure_mode": None,
             "provider_calls": 0, "linked_content": None, "hook_fault": False,
             "nonalias_hook_mode": None, "nonalias_hook_seen": False,
             "nonalias_logs_at_hook": None, "denial_provenance": []}
    fake_error = "synthetic-provider-error " + state["token"] + " " + state["subject"]
    captured_logs = []
    nonalias_diagnostics = []

    class Capture(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.INFO:
                rendered = self.format(record)
                captured_logs.append(rendered)
                if (CASE in {"nonalias_endpoint_replace", "nonalias_stream_hook_fault"}
                        and record.pathname.endswith(("/litellm/proxy/proxy_server.py",
                                                      "/litellm/proxy/common_request_processing.py"))
                        and record.exc_info is not None
                        and "Traceback (most recent call last)" in rendered
                        and fake_error in rendered):
                    nonalias_diagnostics.append((record.funcName, rendered))
                if CASE == "afterstream" and any(marker in rendered for marker in
                        ("synthetic-provider-error", state["token"], state["subject"])):
                    source = (record.pathname.split("/litellm/", 1)[1]
                              if "/litellm/" in record.pathname else "external:" + Path(record.pathname).name)
                    source = source if re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", source) else "other"
                    logger = record.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", record.name) else "other"
                    EVIDENCE["afterstream_log_sites"].append({
                        "source": source,
                        "logger": logger,
                        "line": min(record.lineno, 20000),
                        "level": record.levelname if record.levelname in {"INFO", "WARNING", "ERROR", "CRITICAL"} else "other",
                    })
                    EVIDENCE["afterstream_log_sites"] = EVIDENCE["afterstream_log_sites"][:8]

    capture = Capture(level=logging.INFO)
    capture.setFormatter(logging.Formatter("%(message)s"))
    from litellm._logging import verbose_proxy_logger, verbose_router_logger, verbose_logger
    loggers = {logging.getLogger(), verbose_proxy_logger, verbose_router_logger, verbose_logger}
    loggers.update(item for item in logging.root.manager.loggerDict.values()
                   if isinstance(item, logging.Logger))
    for logger in loggers:
        logger.addHandler(capture)
    def record(kind, call_type, model, carrier, generated=False):
        if len(EVIDENCE["calls"]) >= 12:
            return
        EVIDENCE["calls"].append({"kind": kind,
            "call_type": call_type if call_type in {"acompletion", "completion", "anthropic_messages"} else "other",
            "model": "alias" if model == ALIAS else "backend" if model == BACKEND else
                     "missing" if model is None else "other",
            "carrier": isinstance(carrier, dict),
            "proof": isinstance(carrier, dict) and _PROOF in carrier,
            "generated_carrier": generated})
    hook = traced_hook(ProjectGrokNative, state, record, BACKEND)
    original_failure_hook = hook.async_post_call_failure_hook

    async def maybe_fault_hook(*args, **kwargs):
        if state["nonalias_hook_mode"] is not None:
            state["nonalias_hook_seen"] = True
            state["nonalias_logs_at_hook"] = len(nonalias_diagnostics)
            if state["nonalias_hook_mode"] == "endpoint_replace":
                return HTTPException(status_code=418, detail="synthetic-replacement")
            raise RuntimeError("synthetic-nonalias-hook-fault")
        if state["hook_fault"]:
            raise RuntimeError("synthetic-hook-fault " + fake_error)
        return await original_failure_hook(*args, **kwargs)

    hook.async_post_call_failure_hook = maybe_fault_hook
    owner = UserAPIKeyAuth(api_key="synthetic-key-hash", user_id="synthetic-owner",
                           models=[ALIAS])
    owner.via_virtual_key = True  # server-only marker, set after typed construction
    async def synthetic_auth():
        owner.models = state["grant"]
        return owner

    app = FastAPI()
    app.include_router(messages_router)
    app.include_router(proxy_server.router)
    app.dependency_overrides[user_api_key_auth] = synthetic_auth
    proxy_server.llm_router = litellm.Router(model_list=[{"model_name": ALIAS,
        "litellm_params": {"model": BACKEND, "timeout": 10, "max_retries": 0}}],
        num_retries=0)
    litellm.callbacks = [hook]
    original_send = httpx.AsyncClient.send
    original_process = ProxyBaseLLMRequestProcessing.base_process_llm_request

    async def process_with_closed_diagnostic(self, *args, **kwargs):
        try:
            return await original_process(self, *args, **kwargs)
        except Exception as exc:
            if CASE == "generic":
                locations = [(frame.filename, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)
                             if "/litellm/" in frame.filename]
                filename, line = locations[-1] if locations else ("", 0)
                module = filename.split("/litellm/", 1)[1].removesuffix(".py") if filename else "other"
                EVIDENCE["generic_exception"] = {"class": closed_class(exc),
                    "module": module if re.fullmatch(r"[A-Za-z0-9_./:-]{1,128}", module) else "other",
                    "line": min(line, 20000)}
            if not EVIDENCE["calls"] and EVIDENCE["pre_hook_error"] is None:
                locations = [(frame.filename, frame.lineno) for frame in traceback.extract_tb(exc.__traceback__)
                             if "/litellm/" in frame.filename]
                filename, line = locations[-1] if locations else ("", 0)
                module = filename.split("/litellm/", 1)[1].removesuffix(".py") if filename else "other"
                allowed = {"proxy/common_request_processing", "proxy/litellm_pre_call_utils",
                           "proxy/utils", "proxy/proxy_server", "proxy/anthropic_endpoints/endpoints"}
                EVIDENCE["pre_hook_error"] = {"class": closed_class(exc),
                    "module": module if module in allowed else "other",
                    "line": min(line, 20000) if module in allowed else 0}
            raise

    def token(self):
        state["token_calls"] += 1
        if state["refresh_failure"]:
            raise RuntimeError("synthetic-secret-should-not-appear")
        return state["token"]

    async def send(client, request, **kwargs):
        url = str(request.url)
        if request.url.host == "asgi.local":
            return await original_send(client, request, **kwargs)
        if url == USERINFO:
            EVIDENCE["destination"] = "userinfo"
            check("userinfo_bearer", lambda: request.headers.get("authorization") ==
                  "Bearer " + state["token"])
            state["outbound"].append(("userinfo", url))
            return httpx.Response(200, request=request, json={"sub": state["subject"]})
        if url != BASE + "/chat/completions":
            EVIDENCE["destination"] = "other"
            raise AssertionError("unexpected_outbound_destination")
        EVIDENCE["destination"] = "provider"
        state["outbound"].append(("provider", url))
        if state["failure_mode"] in {"pre", "nonalias", "generic"}:
            return httpx.Response(502, request=request, json={
                "error": {"type": "server_error", "message": fake_error}})
        if state["failure_mode"] == "after":
            first_chunk = {"id": "synthetic-chat", "object": "chat.completion.chunk",
                "created": 1, "model": "grok-4.7-build-fast", "choices": [{"index": 0,
                "delta": {"role": "assistant", "content": "before-error"}, "finish_reason": None}]}
            payload = ("data: " + json.dumps(first_chunk) + "\n\n"
                       + "data: " + json.dumps({"error": {"type": "server_error",
                           "message": fake_error}}) + "\n\n")
            return httpx.Response(200, request=request,
                headers={"content-type": "text/event-stream"}, content=payload.encode())
        body = json.loads(request.content)
        check("same_token_subject", lambda: request.headers.get("authorization") ==
              "Bearer " + state["token"] and request.headers.get("x-userid") == state["subject"])
        check("fixed_transport", lambda: request.headers.get("x-xai-token-auth") == "xai-grok-cli"
              and request.headers.get("x-grok-model-override") == "grok-4.7-build-fast"
              and body.get("model") == "grok-4.7-build-fast"
              and request.headers.get("x-api-key") is None)
        EVIDENCE["credential_override"] = request.headers.get("x-api-key") is not None
        state["provider_calls"] += 1
        tools = bool(body.get("tools"))
        if tools:
            deltas = [{"role": "assistant", "content": None, "tool_calls": [{"index": 0,
                "id": "synthetic-call", "type": "function", "function": {"name": "Read", "arguments": "{}"}}]}]
            finish = "tool_calls"
        else:
            state["linked_content"] = linked_tool_result(body.get("messages"))
            deltas = [{"role": "assistant", "content": state["linked_content"] or "unlinked"}]
            finish = "stop"
        events = [{"id": "synthetic-chat", "object": "chat.completion.chunk", "created": 1,
                   "model": "grok-4.7-build-fast", "choices": [{"index": 0, "delta": delta,
                   "finish_reason": None}]} for delta in deltas]
        events.append({"id": "synthetic-chat", "object": "chat.completion.chunk", "created": 1,
                       "model": "grok-4.7-build-fast", "choices": [{"index": 0, "delta": {},
                       "finish_reason": finish}]})
        payload = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                              content=payload.encode())

    async def post(client, messages, **extra):
        body = {"model": ALIAS, "max_tokens": 32, "stream": True, "messages": messages, **extra}
        return await client.post("/v1/messages", json=body, headers={
            "authorization": "Bearer sk-ant-oat-synthetic",
            "x-litellm-api-key": "Bearer synthetic-owner-key",
            "anthropic-version": "2023-06-01"})

    with patch.object(XAIOAuthAuthenticator, "get_access_token", token), \
         patch.object(httpx.AsyncClient, "send", send), \
         patch.object(ProxyBaseLLMRequestProcessing, "base_process_llm_request",
                      process_with_closed_diagnostic):
        STAGE = "proxy_runtime"
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://asgi.local", timeout=20) as client:
            if os.environ.get("GROK_BASELINE_NONALIAS") == "1":
                CASE = "baseline_nonalias"
                state["grant"] = [ALIAS, "synthetic-other"]
                proxy_server.llm_router = litellm.Router(model_list=[
                    {"model_name": ALIAS, "litellm_params": {"model": BACKEND,
                        "timeout": 10, "max_retries": 0}},
                    {"model_name": "synthetic-other", "litellm_params": {
                        "model": "xai/grok-4.7-build-fast", "api_key": "synthetic-provider-key",
                        "api_base": BASE, "timeout": 10, "max_retries": 0}}], num_retries=0)
                state["failure_mode"] = "generic"
                generic_before = len(state["outbound"])
                baseline_generic = await client.post("/v1/chat/completions", json={
                    "model": ALIAS, "messages": [{"role": "user", "content": "provider error"}],
                    "stream": False}, headers={
                    "authorization": "Bearer sk-ant-oat-synthetic",
                    "x-litellm-api-key": "Bearer synthetic-owner-key",
                    "anthropic-version": "2023-06-01"})
                EVIDENCE["baseline_generic_outbound"] = [kind for kind, _ in state["outbound"][generic_before:]]
                EVIDENCE["baseline_generic_status"] = baseline_generic.status_code
                EVIDENCE["baseline_generic_body_sha256"] = canonical_error_body(baseline_generic)
                check("baseline_generic_denied", lambda: baseline_generic.status_code >= 400
                      and EVIDENCE["baseline_generic_outbound"] == []
                      and EVIDENCE["baseline_generic_body_sha256"] is not None)
                state["failure_mode"] = "nonalias"
                before = len(state["outbound"])
                baseline = await post(client, [{"role": "user", "content": "other error"}],
                                      model="synthetic-other", stream=False)
                EVIDENCE["nonalias_status"] = baseline.status_code
                EVIDENCE["nonalias_body_sha256"] = canonical_error_body(baseline)
                check("baseline_nonalias_reached", lambda: baseline.status_code >= 400
                      and [kind for kind, _ in state["outbound"][before:]] == ["provider"]
                      and EVIDENCE["nonalias_body_sha256"] is not None)
                return
            CASE = "positive"
            first = await post(client, [{"role": "user", "content": "Read fixture"}],
                tools=[{"name": "Read", "description": "Read fixture", "input_schema": {
                    "type": "object", "properties": {}}}])
            EVIDENCE["statuses"]["first"] = first.status_code
            check("messages_proxy_stream", lambda: first.status_code == 200 and
                  parsed_tool_stream(first.text))
            second = await post(client, [{"role": "user", "content": "Read fixture"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "synthetic-call",
                    "name": "Read", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id":
                    "synthetic-call", "content": "cedar"}]}])
            EVIDENCE["statuses"]["second"] = second.status_code
            check("linked_continuation", lambda: second.status_code == 200
                  and state["provider_calls"] == 2 and state["linked_content"] == "cedar"
                  and "cedar" in second.text)
            check("hook_sequence", lambda: [x[0] for x in state["calls"]] ==
                  ["request", "deployment", "deployment",
                   "request", "deployment", "deployment"])
            check("selected_alias", lambda: bool(state["calls"]) and
                  all(x[2] == ALIAS for x in state["calls"]))
            check("repeated_deployment_binding", lambda:
                  len(state["bindings"]) == 4 and all(state["bindings"])
                  and state["token_calls"] == 2
                  and [kind for kind, _ in state["outbound"]] ==
                  ["userinfo", "provider", "userinfo", "provider"])
            state["grant"] = ["project-router"]
            CASE = "owner_denial"
            before = len(state["outbound"])
            calls_before = len(state["calls"])
            denied = await post(client, [{"role": "user", "content": "deny"}])
            EVIDENCE["denial_statuses"]["owner"] = denied.status_code
            check("alias_only_owner", lambda: denied_at_boundary(denied, state["calls"],
                  calls_before, state["outbound"], before, ["request"]))
            check("canonical_denial_provenance", lambda: state["denial_provenance"] == [True])
            CASE = "prefixed_denial"
            logs_before = len(captured_logs)
            calls_before = len(state["calls"])
            adverse = CallerCredentialAuthenticationError(
                message=fake_error + " Project Grok request rejected",
                llm_provider="xai", model=ALIAS)
            with patch.object(grok_callback, "_deny", return_value=adverse):
                prefixed = await post(client, [{"role": "user", "content": "deny"}])
            check("prefixed_denial_closed", lambda: prefixed.status_code == 502
                  and prefixed.json() == {"type": "error", "error": {"type": "api_error",
                      "message": "Project Grok provider failed"}}
                  and [item[0] for item in state["calls"][calls_before:]] == ["request"]
                  and len(state["outbound"]) == before
                  and state["denial_provenance"] == [True, False]
                  and all(marker not in prefixed.text
                          and marker not in "\n".join(captured_logs[logs_before:])
                          for marker in ("synthetic-provider-error", state["token"], state["subject"])))
            state["grant"] = [ALIAS]
            CASE = "override_denial"
            calls_before = len(state["calls"])
            denied = await post(client, [{"role": "user", "content": "deny"}],
                                api_base="https://wrong.invalid")
            EVIDENCE["denial_statuses"]["override"] = denied.status_code
            check("caller_override_denied", lambda: denied_at_boundary(denied, state["calls"],
                  calls_before, state["outbound"], before, ["request"]))
            calls_before = len(state["calls"])
            CASE = "duplicate_denial"
            duplicate = await client.post("/v1/messages", json={"model": ALIAS,
                "max_tokens": 32, "stream": True,
                "messages": [{"role": "user", "content": "deny"}]}, headers=[
                ("authorization", "Bearer sk-ant-oat-synthetic"),
                ("x-litellm-api-key", "Bearer synthetic-owner-key"),
                ("x-api-key", "synthetic-forbidden"),
                ("X-API-KEY", "synthetic-forbidden-again")])
            EVIDENCE["denial_statuses"]["duplicate"] = duplicate.status_code
            check("http_duplicate_forbidden_denied", lambda: denied_at_boundary(duplicate,
                  state["calls"], calls_before, state["outbound"], before, ["request"]))
            state["refresh_failure"] = True
            CASE = "refresh_denial"
            calls_before = len(state["calls"])
            denied = await post(client, [{"role": "user", "content": "deny"}])
            check("refresh_failure_redacted", lambda: denied.status_code in {401, 502} and
                  "Project Grok" in denied.text and "synthetic-secret" not in denied.text
                  and [x[0] for x in state["calls"][calls_before:]] ==
                  ["request", "deployment"] and len(state["outbound"]) == before)
            state["refresh_failure"] = False
            state["failure_mode"] = "pre"
            CASE = "prestream"
            before = len(state["outbound"])
            logs_before = len(captured_logs)
            pre = await post(client, [{"role": "user", "content": "provider error"}])
            pre_body = pre.json() if pre.headers.get("content-type", "").startswith("application/json") else None
            expected_body = {"type": "error", "error": {"type": "api_error",
                "message": "Project Grok provider failed"}}
            check("prestream_fixed_502_body", lambda: pre.status_code == 502
                  and pre_body == expected_body)
            check("prestream_closed_response_and_logs", lambda:
                  all(marker not in pre.text and marker not in "\n".join(captured_logs[logs_before:])
                      for marker in ("synthetic-provider-error", state["token"], state["subject"])))
            check("prestream_single_provider_no_fallback", lambda:
                  [kind for kind, _ in state["outbound"][before:]] == ["userinfo", "provider"])

            state["failure_mode"] = "generic"
            CASE = "generic"
            before = len(state["outbound"])
            logs_before = len(captured_logs)
            generic = await client.post("/v1/chat/completions", json={
                "model": ALIAS, "messages": [{"role": "user", "content": "provider error"}],
                "stream": False}, headers={
                "authorization": "Bearer sk-ant-oat-synthetic",
                "x-litellm-api-key": "Bearer synthetic-owner-key",
                "anthropic-version": "2023-06-01"})
            generic_outbound = [kind for kind, _ in state["outbound"][before:]]
            EVIDENCE["generic_status"] = generic.status_code
            EVIDENCE["generic_outbound"] = generic_outbound
            EVIDENCE["generic_calls"] = [item[0] for item in state["calls"][-3:]]
            EVIDENCE["generic_body_sha256"] = canonical_error_body(generic)
            EVIDENCE["generic_reached"] = generic_outbound == ["userinfo", "provider"]
            check("generic_denial_preserved", lambda: generic.status_code >= 400
                  and generic_outbound == [] and EVIDENCE["generic_body_sha256"] is not None)
            check("generic_failure_closed", lambda: generic.status_code >= 400
                  and all(marker not in generic.text and marker not in "\n".join(captured_logs[logs_before:])
                          for marker in ("synthetic-provider-error", state["token"], state["subject"])))
            state["failure_mode"] = "after"
            CASE = "afterstream"
            before = len(state["outbound"])
            logs_before = len(captured_logs)
            after = await post(client, [{"role": "user", "content": "late provider error"}])
            EVIDENCE["afterstream"] = {
                "status": after.status_code,
                "prefix": "before-error" in after.text,
                "error_event": terminal_error_event(after.text),
                "fixed": "Project Grok provider failed" in after.text,
                "closed_body": all(marker not in after.text for marker in
                    ("synthetic-provider-error", state["token"], state["subject"])),
                "closed_logs": all(marker not in "\n".join(captured_logs[logs_before:]) for marker in
                    ("synthetic-provider-error", state["token"], state["subject"])),
            }
            check("afterstream_replacement", lambda: after.status_code == 200
                  and "before-error" in after.text and terminal_error_event(after.text)
                  and all(marker not in after.text and marker not in "\n".join(captured_logs[logs_before:])
                          for marker in ("synthetic-provider-error", state["token"], state["subject"])))
            check("afterstream_single_provider_no_fallback", lambda:
                  [kind for kind, _ in state["outbound"][before:]] == ["userinfo", "provider"])

            check("malformed_terminal_error_rejected", lambda: all(not terminal_error_event(value)
                for value in (
                    "event: message_start\ndata: {\"type\":\"message_start\"}\n\nevent: error\nProject Grok provider failed\n\n",
                    "event: message_start\ndata: {\"type\":\"message_start\"}\n\nevent: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"api_error\",\"message\":\"Project Grok provider failed\"}}\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
                    "event: error\ndata: {\"type\":\"error\",\"error\":{\"type\":\"api_error\",\"message\":\"Project Grok provider failed\"}}\n\n")))
            state["hook_fault"] = True
            CASE = "hook_fault"
            before = len(state["outbound"])
            logs_before = len(captured_logs)
            fault = await post(client, [{"role": "user", "content": "late hook fault"}])
            check("hook_fault_fixed_terminal", lambda: fault.status_code == 200
                  and terminal_error_event(fault.text)
                  and all(marker not in fault.text and marker not in "\n".join(captured_logs[logs_before:])
                          for marker in ("synthetic-provider-error", state["token"], state["subject"], "synthetic-hook-fault"))
                  and [kind for kind, _ in state["outbound"][before:]] == ["userinfo", "provider"])
            state["hook_fault"] = False
            state["failure_mode"] = "nonalias"
            CASE = "nonalias"
            state["grant"] = [ALIAS, "synthetic-other"]
            proxy_server.llm_router = litellm.Router(model_list=[
                {"model_name": ALIAS, "litellm_params": {"model": BACKEND,
                    "timeout": 10, "max_retries": 0}},
                {"model_name": "synthetic-other", "litellm_params": {
                    "model": "xai/grok-4.7-build-fast", "api_key": "synthetic-provider-key",
                    "api_base": BASE, "timeout": 10, "max_retries": 0}}], num_retries=0)
            before = len(state["outbound"])
            other = await post(client, [{"role": "user", "content": "other error"}],
                               model="synthetic-other", stream=False)
            EVIDENCE["nonalias_status"] = other.status_code
            EVIDENCE["nonalias_body_sha256"] = canonical_error_body(other)
            check("nonalias_error_unchanged", lambda: other.status_code >= 400
                  and "Project Grok provider failed" not in other.text
                  and EVIDENCE["nonalias_body_sha256"] is not None
                  and [kind for kind, _ in state["outbound"][before:]] == ["provider"])
            CASE = "nonalias_endpoint_replace"
            state["nonalias_hook_mode"] = "endpoint_replace"
            state["nonalias_hook_seen"] = False
            state["nonalias_logs_at_hook"] = None
            nonalias_diagnostics.clear()
            before = len(state["outbound"])
            replaced = await post(client, [{"role": "user", "content": "replace other error"}],
                                  model="synthetic-other", stream=False)
            check("nonalias_endpoint_original_logged_after_hook", lambda:
                  state["nonalias_hook_seen"] and state["nonalias_logs_at_hook"] == 0
                  and any(site == "log_llm_api_exception" and fake_error in rendered
                          for site, rendered in nonalias_diagnostics)
                  and replaced.status_code == 418
                  and "synthetic-replacement" in replaced.text
                  and [kind for kind, _ in state["outbound"][before:]] == ["provider"])
            CASE = "nonalias_stream_hook_fault"
            state["nonalias_hook_mode"] = "stream_fault"
            state["nonalias_hook_seen"] = False
            state["nonalias_logs_at_hook"] = None
            state["failure_mode"] = "after"
            nonalias_diagnostics.clear()
            before = len(state["outbound"])
            await post(client, [{"role": "user", "content": "stream other error"}],
                       model="synthetic-other")
            check("nonalias_stream_original_logged_before_hook_fault", lambda:
                  state["nonalias_hook_seen"] and state["nonalias_logs_at_hook"] >= 1
                  and any(site == "async_streaming_data_generator" and fake_error in rendered
                          for site, rendered in nonalias_diagnostics)
                  and [kind for kind, _ in state["outbound"][before:]] == ["provider"])
            state["nonalias_hook_mode"] = None
            EVIDENCE["outbound_count"] = min(len(state["outbound"]), 12)
    check("installed_import", lambda: True)


def main(result_path):
    try:
        asyncio.run(qualify())
    except BaseException as exc:
        fatal = {"stage": STAGE, "class": closed_class(exc),
                 "predicate": "uncaught_exception"}
    else:
        fatal = None
    result = {"schema": "litellm-grok-image-result/2", "checks": CHECKS,
              "failures": FAILURES, "fatal": fatal, "evidence": EVIDENCE}
    # Keep SDK stdout/stderr separate. Replace prevents a partial host receipt.
    temporary = result_path.with_suffix(".tmp")
    with temporary.open("x") as handle:
        json.dump(result, handle, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, result_path)
    return 0 if not FAILURES and fatal is None else 1


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1])))
