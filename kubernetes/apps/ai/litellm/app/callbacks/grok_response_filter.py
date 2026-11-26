"""Normalize Grok Build errors before LiteLLM can log or relay them."""

import json

import httpx
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

GROK_BASE = "https://cli-chat-proxy.grok.com/v1"
GROK_COMPLETIONS = httpx.URL(f"{GROK_BASE}/chat/completions")
SAFE_ERROR = {"error": {"type": "server_error", "message": "Project Grok provider failed"}}
SAFE_ERROR_FRAME = f"data: {json.dumps(SAFE_ERROR)}\n\n".encode()


def _contains_identity(value, identity):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and any(marker in item for marker in identity):
            return True
    return False


def _is_completion(payload, *, stream):
    kind = "chat.completion.chunk" if stream else "chat.completion"
    field = "delta" if stream else "message"
    if (not isinstance(payload, dict) or payload.get("object") != kind
            or "error" in payload or not isinstance(payload.get("choices"), list)):
        return False
    choices = payload["choices"]
    return len(choices) <= 1 and all(
        isinstance(choice, dict) and choice.get("index", 0) == 0 and isinstance(choice.get(field), dict)
        and isinstance(choice[field].get("content"), (str, type(None))) for choice in choices
    )


def _stream_fragments(chunk):
    for choice in chunk["choices"]:
        delta = choice["delta"]
        content = delta.get("content")
        reasoning = delta.get("reasoning", delta.get("reasoning_content"))
        for name, fragment in (("content", content), ("reasoning_content", reasoning)):
            if isinstance(fragment, str):
                yield (name,), fragment
        for tool_position, tool in enumerate(delta.get("tool_calls") or []):
            if not isinstance(tool, dict) or not isinstance(tool.get("function"), dict):
                continue
            index = tool.get("index")
            if not isinstance(index, int):
                index = tool_position
            for name in ("name", "arguments"):
                fragment = tool["function"].get(name)
                if isinstance(fragment, str):
                    yield (index, name), fragment


class _FilteredStream(httpx.AsyncByteStream):
    def __init__(self, upstream, identity):
        self.upstream = upstream
        self.identity = identity
        self.identity_tails = {}

    def _reflects_identity(self, chunk):
        if _contains_identity(chunk, self.identity):
            return True
        # The Messages adapter assembles these deltas per choice and tool index.
        # Keep only enough of each stream to detect an identity split across frames.
        limit = max(map(len, self.identity)) - 1
        for channel, fragment in _stream_fragments(chunk):
            joined = self.identity_tails.get(channel, "") + fragment
            if any(marker in joined for marker in self.identity):
                return True
            tail = joined[-limit:] if limit else ""
            prefix = max(
                (tail[-size:] for marker in self.identity
                 for size in range(1, min(len(marker) - 1, len(tail)) + 1)
                 if marker.startswith(tail[-size:])),
                key=len, default="",
            )
            if prefix:
                self.identity_tails[channel] = prefix
            else:
                self.identity_tails.pop(channel, None)
        return len(self.identity_tails) > 128

    async def __aiter__(self):
        terminal_frames = []
        try:
            async for line in self.upstream.aiter_lines():
                if line.startswith("event:") and line[6:].strip() == "error":
                    yield SAFE_ERROR_FRAME
                    return
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    if not terminal_frames:
                        yield SAFE_ERROR_FRAME
                        return
                    for frame in terminal_frames:
                        yield frame
                    yield b"data: [DONE]\n\n"
                    return
                try:
                    chunk = json.loads(data)
                except ValueError:
                    chunk = SAFE_ERROR
                if not _is_completion(chunk, stream=True) or self._reflects_identity(chunk):
                    yield SAFE_ERROR_FRAME
                    return
                frame = f"data: {data}\n\n".encode()
                if terminal_frames or any(choice.get("finish_reason") for choice in chunk["choices"]):
                    if len(terminal_frames) == 2:
                        yield SAFE_ERROR_FRAME
                        return
                    terminal_frames.append(frame)
                else:
                    yield frame
            yield SAFE_ERROR_FRAME
        except (httpx.HTTPError, TypeError, UnicodeError, RecursionError):
            yield SAFE_ERROR_FRAME
        finally:
            await self.aclose()

    async def aclose(self):
        await self.upstream.aclose()


class GrokResponseFilter(httpx.AsyncBaseTransport):
    def __init__(self, upstream=None):
        self.upstream = upstream if upstream is not None else httpx.AsyncHTTPTransport(trust_env=False)

    @staticmethod
    def _error(request, status=502):
        return httpx.Response(status, request=request, json=SAFE_ERROR)

    async def handle_async_request(self, request):
        if request.method != "POST" or request.url != GROK_COMPLETIONS:
            return self._error(request)
        authorization = request.headers.get("authorization", "")
        token = authorization.removeprefix("Bearer ")
        subject = request.headers.get("x-userid", "")
        if not token or token == authorization or not subject:
            return self._error(request)
        identity = (token, subject)
        try:
            response = await self.upstream.handle_async_request(request)
            response.request = request
            if not response.is_success:
                await response.aclose()
                status = response.status_code
                return self._error(request, status if status >= 400 else 502)
            if response.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream":
                return httpx.Response(
                    response.status_code,
                    request=request,
                    headers={"content-type": "text/event-stream"},
                    stream=_FilteredStream(response, identity),
                )
            try:
                payload = json.loads(await response.aread())
                if _is_completion(payload, stream=False) and not _contains_identity(payload, identity):
                    return httpx.Response(response.status_code, request=request, json=payload)
            finally:
                await response.aclose()
        except (httpx.HTTPError, ValueError, RecursionError):
            pass
        return self._error(request)

    async def aclose(self):
        await self.upstream.aclose()


def grok_http_handler(upstream=None):
    return AsyncHTTPHandler(transport=GrokResponseFilter(upstream), follow_redirects=False)
