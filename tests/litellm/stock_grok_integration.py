"""Exercise the installed Messages endpoint with synthetic Grok credentials."""

import asyncio
import json
import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import litellm
from fastapi import FastAPI
from litellm._logging import verbose_logger, verbose_proxy_logger, verbose_router_logger
from litellm.integrations.langfuse.langfuse_otel import LangfuseOtelLogger
from litellm.integrations.opentelemetry import OpenTelemetryConfig
from litellm.proxy import proxy_server
from litellm.proxy.anthropic_endpoints.endpoints import router
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.types.proxy.auth.user_api_key_auth import UserAPIKeyAuth
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from project_grok_native import ALIAS, BACKEND, BASE, USERINFO, ProjectGrokNative

TOKEN = "synthetic-grok-bearer"
SUBJECT = "synthetic-grok-subject"
ERROR = "synthetic-upstream-error"
PROMPT = "synthetic-grok-prompt"
ANSWER = "synthetic-grok-output"
SAFE = "Project Grok provider failed"
REFRESH = "synthetic-grok-refresh"
ROTATED = "synthetic-grok-rotated"
TOKEN_URL = "https://auth.x.ai/oauth2/token"
PRIVATE = (TOKEN, SUBJECT, ERROR, PROMPT, ANSWER, REFRESH, ROTATED)


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


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


def chat_chunk(delta, finish=None):
    return {"id": "fixture", "object": "chat.completion.chunk", "created": 1,
            "model": BACKEND.removeprefix("xai/"),
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


async def qualify(directory):
    mode = "stream"
    calls = []
    user = UserAPIKeyAuth(api_key="synthetic-key-hash", user_id="synthetic-owner", models=[ALIAS])
    user.via_virtual_key = True
    log = LogCapture()
    for logger in (verbose_logger, verbose_proxy_logger, verbose_router_logger):
        logger.setLevel(logging.INFO)
        logger.addHandler(log)
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    langfuse = LangfuseOtelLogger(
        callback_name="langfuse_otel", config=OpenTelemetryConfig(exporter=exporter), tracer_provider=tracer,
    )

    async def provider(request):
        assert str(request.url) == f"{BASE}/chat/completions"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-userid"] == SUBJECT
        assert request.headers["x-xai-token-auth"] == "xai-grok-cli"
        assert request.headers["x-grok-model-override"] == BACKEND.removeprefix("xai/")
        assert "x-api-key" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == BACKEND.removeprefix("xai/")
        calls.append("provider")
        if isinstance(mode, int):
            return httpx.Response(mode, json={"error": {"message": ERROR}})
        if mode == "unexpected_json":
            return httpx.Response(200, json={"message": ERROR})
        if mode in ("unary", "reflected_json"):
            content = ANSWER if mode == "unary" else f"{TOKEN} {SUBJECT}"
            return httpx.Response(200, json={
                "id": "fixture", "object": "chat.completion", "created": 1, "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })
        delta = {"role": "assistant", "content": ANSWER}
        finish = "stop"
        if mode == "tool":
            delta = {"role": "assistant", "content": None, "tool_calls": [{
                "index": 0, "id": "synthetic-call", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }]}
            finish = "tool_calls"
        if mode == "continuation":
            assert linked_tool_result(payload["messages"]) == "cedar"
        terminal = {"error": {"message": ERROR}} if mode == "late" else chat_chunk({}, finish)
        if mode == "late_reflected":
            terminal = chat_chunk({"content": f"{TOKEN} {SUBJECT}"})
        if mode == "late_malformed":
            terminal = chat_chunk({"content": {"message": ERROR}})
        if mode == "late_named_event":
            first = f"data: {json.dumps(chat_chunk(delta))}\n\n"
            payload = first + f"event: error\ndata: {json.dumps({'message': ERROR})}\n\n"
            return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        chunks = [chat_chunk(delta), terminal]
        if mode == "late_split_identity":
            chunks[1:1] = [chat_chunk({"content": TOKEN[:10]}), chat_chunk({"content": TOKEN[10:]})]
        if mode == "late_split_choice":
            other_choice = chat_chunk({"content": TOKEN[10:]})
            other_choice["choices"][0]["index"] = 1
            chunks[1:1] = [chat_chunk({"content": TOKEN[:10]}), other_choice]
        if mode == "late_split_reasoning":
            chunks[1:1] = [chat_chunk({"reasoning": TOKEN[:10], "reasoning_content": "fixture"}),
                           chat_chunk({"reasoning_content": TOKEN[10:]})]
        if mode == "late_missing_finish":
            chunks.pop()
        data = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        if mode != "late_missing_done":
            data += "data: [DONE]\n\n"
        return httpx.Response(200, text=data, headers={"content-type": "text/event-stream"})

    hook = ProjectGrokNative(transport=httpx.MockTransport(provider))
    litellm.callbacks = [hook, langfuse]
    litellm.turn_off_message_logging = True
    proxy_server.llm_router = litellm.Router(model_list=[{
        "model_name": ALIAS, "litellm_params": {"model": BACKEND, "use_xai_oauth": False, "max_retries": 0},
    }], num_retries=0, disable_cooldowns=True)
    app = FastAPI()
    app.include_router(router)

    async def authorized():
        return user

    app.dependency_overrides[user_api_key_auth] = authorized
    original_send = httpx.AsyncClient.send

    async def send(client, request, **kwargs):
        if str(request.url) == USERINFO:
            assert request.headers["authorization"] == f"Bearer {TOKEN}"
            calls.append("userinfo")
            return httpx.Response(200, request=request, json={"sub": SUBJECT})
        assert request.url.host == "asgi.local" or client is hook._http_handler.client
        return await original_send(client, request, **kwargs)

    headers = {"anthropic-version": "2023-06-01", "authorization": "Bearer sk-ant-oat-fixture",
               "x-litellm-api-key": "fixture-virtual-key"}
    messages = [{"role": "user", "content": PROMPT}]
    tools = [{"name": "Read", "description": "Read a test resource", "input_schema": {"type": "object"}}]
    auth_file = Path(directory) / "auth.json"
    auth_file.write_text(json.dumps({
        "access_token": "expired-fixture", "refresh_token": REFRESH,
        "expires_at": 0, "token_endpoint": TOKEN_URL,
    }))
    auth_file.chmod(0o600)
    refreshes = []

    def refresh(client, request, **kwargs):
        assert str(request.url) == TOKEN_URL and request.method == "POST"
        assert not client.follow_redirects and not client.trust_env
        body = parse_qs(request.content.decode())
        assert body["grant_type"] == ["refresh_token"] and body["refresh_token"] == [REFRESH]
        refreshes.append(request.url)
        return httpx.Response(200, request=request, json={
            "access_token": TOKEN, "refresh_token": ROTATED, "expires_in": 3600,
        })

    with patch.dict("os.environ", {"XAI_OAUTH_TOKEN_DIR": directory}), \
         patch.object(httpx.Client, "send", refresh), \
         patch.object(httpx.AsyncClient, "send", send):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://asgi.local") as client:
            async def post(**extra):
                return await client.post("/v1/messages", headers=headers, json={
                    "model": ALIAS, "max_tokens": 32, "messages": messages,
                    "stream": mode not in ("unary", "reflected_json"), **extra,
                })

            for mode in ("unary", "stream", "tool", "continuation", 401, 429, 502,
                         "unexpected_json", "reflected_json", "late", "late_named_event",
                         "late_reflected", "late_malformed", "late_split_identity", "late_split_choice", "late_split_reasoning",
                         "late_missing_done", "late_missing_finish"):
                calls.clear()
                log.records.clear()
                if mode == "tool":
                    response = await post(tools=tools)
                    assert parsed_tool_stream(response.text)
                elif mode == "continuation":
                    response = await post(tools=tools, messages=[
                        *messages,
                        {"role": "assistant", "content": [{"type": "tool_use", "id": "synthetic-call",
                                                           "name": "Read", "input": {}}]},
                        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "synthetic-call",
                                                      "content": "cedar"}]},
                    ])
                    assert ANSWER in response.text
                else:
                    response = await post()
                assert calls == ["userinfo", "provider"], (mode, calls)
                expected_status = mode if isinstance(mode, int) else 200
                if mode in ("unexpected_json", "reflected_json"):
                    expected_status = 502
                assert response.status_code == expected_status, (mode, response.status_code)
                if expected_status != 200:
                    assert response.json()["type"] == "error" and SAFE in response.json()["error"]["message"]
                if isinstance(mode, str) and mode.startswith("late"):
                    frames = response.text.strip().split("\n\n")
                    errors = [frame for frame in frames if frame.startswith("event: error\n")]
                    assert errors == [frames[-1]] and ANSWER in response.text
                    assert json.loads(errors[0].split("\ndata: ", 1)[1])["type"] == "error"
                    assert SAFE in errors[0] and "event: message_stop" not in response.text
                    output = "".join(json.loads(frame.split("\ndata: ", 1)[1])["delta"].get("text", "")
                                     for frame in frames if frame.startswith("event: content_block_delta\n"))
                    assert TOKEN not in output and SUBJECT not in output
                if mode in ("unary", "stream"):
                    assert ANSWER in response.text
                for marker in (TOKEN, SUBJECT, ERROR, REFRESH, ROTATED):
                    assert marker not in response.text and all(marker not in row for row in log.records), mode

            mode = "stream"
            for grant, overrides in (([], {}), ([ALIAS, "project-router"], {}), ([ALIAS], {"api_key": "fixture"})):
                calls.clear()
                user.models = grant
                response = await post(**overrides)
                assert response.status_code == 401 and calls == [], response.status_code
    assert len(refreshes) == 1 and json.loads(auth_file.read_text())["refresh_token"] == ROTATED
    for _ in range(100):
        if len(exporter.get_finished_spans()) >= 8:
            break
        await asyncio.sleep(0.02)
    spans = exporter.get_finished_spans()
    assert len(spans) >= 8
    assert any(span.attributes.get("langfuse.observation.type") == "generation" for span in spans)
    assert any(span.status.status_code.name == "ERROR" for span in spans)
    assert any(span.end_time > span.start_time and span.attributes.get("llm.token_count.prompt") == 1
               and span.attributes.get("llm.token_count.completion") == 1 for span in spans)
    assert all(all(item not in span.to_json() for item in PRIVATE) for span in spans)
    tracer.shutdown()
    await hook._http_handler.client.aclose()
    print("Stock LiteLLM: owner and tool streaming, errors, redaction and Langfuse passed.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="grok-oauth-fixture-") as directory:
        asyncio.run(qualify(directory))
