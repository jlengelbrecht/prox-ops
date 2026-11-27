"""MCP endpoint for the AI-Access broker.

Streamable HTTP with JSON responses only: no sessions, no server-initiated
streams. The only caller is the dedicated AI-Access Open Connector runtime,
which authenticates with a bearer token. AgentGateway has already authorised
the agent identity and the runtime has already checked its action allowlist.
"""

from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import sys
import urllib.parse
from typing import Any, Callable, Mapping

from access import Broker, ConnectClient, ConnectError, Rejected, TITLE

LOG = logging.getLogger("onepassword_agent_mcp")

MAX_BODY = 64 * 1024
MAX_DRAIN = 1024 * 1024
MIN_CALLER_TOKEN = 32
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SERVER_INFO = {"name": "onepassword-agent-mcp", "version": "0.1.0"}


class McpApp:
    """JSON-RPC handling for the two broker tools."""

    def __init__(self, broker: Broker, caller_token: str):
        if len(caller_token) < MIN_CALLER_TOKEN:
            raise ValueError(f"the caller token must be at least {MIN_CALLER_TOKEN} characters")
        self._broker = broker
        self._token = caller_token.encode()
        self._tools: dict[str, tuple[tuple[str, ...], Callable[[Mapping[str, Any]], Any]]] = {
            "create_generated_item": (
                ("vault", "title"),
                lambda args: broker.create_generated_item(args["vault"], args["title"]),
            ),
            "deliver_item_to_consumer": (
                ("vault", "item_id", "destination"),
                lambda args: broker.deliver_item_to_consumer(
                    args["vault"], args["item_id"], args["destination"]),
            ),
        }

    def authorized(self, header: str | None) -> bool:
        scheme, _, credential = (header or "").partition(" ")
        return scheme == "Bearer" and hmac.compare_digest(credential.encode(), self._token)

    def tool_definitions(self) -> list[dict[str, Any]]:
        vault = {
            "type": "string",
            "enum": [self._broker.vault_name],
            "description": "The vault this broker is bound to. Any other value is refused.",
        }
        return [
            {
                "name": "create_generated_item",
                "description": (
                    "Create an item whose password 1Password generates. Returns item "
                    "metadata only. If the title already exists, reports that item "
                    "instead of creating a duplicate."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "vault": vault,
                        "title": {"type": "string", "pattern": f"^{TITLE.pattern}$"},
                    },
                    "required": ["vault", "title"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "deliver_item_to_consumer",
                "description": (
                    "Read an item's password inside the broker and hand it to an "
                    "approved server-side consumer. The value is never returned."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "vault": vault,
                        "item_id": {"type": "string", "pattern": "^[a-z0-9]{26}$"},
                        "destination": {"type": "string", "enum": self._broker.destinations},
                    },
                    "required": ["vault", "item_id", "destination"],
                    "additionalProperties": False,
                },
            },
        ]

    def handle(self, message: Any) -> tuple[int, Any]:
        """Returns the HTTP status and JSON body (None for no body)."""
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            msg_id = message.get("id") if isinstance(message, dict) else None
            return 400, _error(msg_id, -32600, "Invalid Request")
        if "id" not in message:
            return 202, None
        msg_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return 200, _error(msg_id, -32602, "params must be an object")
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return 200, _response(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
        if method == "ping":
            return 200, _response(msg_id, {})
        if method == "tools/list":
            return 200, _response(msg_id, {"tools": self.tool_definitions()})
        if method == "tools/call":
            return 200, self._call(msg_id, params)
        return 200, _error(msg_id, -32601, "Method not found")

    def _call(self, msg_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        tool = self._tools.get(name) if isinstance(name, str) else None
        if tool is None:
            LOG.info("op=tools/call outcome=unknown_tool")
            return _error(msg_id, -32602, "Unknown tool")
        expected, run = tool
        args = params.get("arguments", {})
        try:
            if not isinstance(args, dict) or set(args) != set(expected):
                raise Rejected(f"arguments must be exactly: {', '.join(expected)}")
            result = run(args)
        except Rejected as exc:
            LOG.info("op=%s outcome=rejected reason=%s", name, exc)
            return _tool_result(msg_id, {"error": str(exc)}, is_error=True)
        except ConnectError as exc:
            LOG.warning("op=%s outcome=connect_error reason=%s", name, exc)
            return _tool_result(msg_id, {"error": "the 1Password Connect request failed"},
                                is_error=True)
        return _tool_result(msg_id, result)


def _response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_result(msg_id: Any, payload: Mapping[str, Any], is_error: bool = False) -> dict[str, Any]:
    return _response(msg_id, {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": is_error,
    })


class _BodyError(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "onepassword-agent-mcp"
    sys_version = ""
    timeout = 30

    def do_GET(self):
        if self._path() == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._refuse_method()

    def do_POST(self):
        # The body is read before any refusal: closing a socket with unread
        # input resets the connection and can discard the refusal itself.
        try:
            body = self._read_body()
        except _BodyError as exc:
            return self._send(exc.status, {"error": "unacceptable request body"}, close=True)
        if self._path() != "/mcp":
            return self._send(404, {"error": "not found"})
        if self.headers.get("Origin"):
            return self._send(403, {"error": "browser origins are not accepted"})
        if not self.server.app.authorized(self.headers.get("Authorization")):
            return self._send(401, {"error": "unauthorized"})
        try:
            message = json.loads(body)
        except ValueError:
            return self._send(400, _error(None, -32700, "Parse error"))
        try:
            status, payload = self.server.app.handle(message)
        except Exception as exc:  # noqa: BLE001 - the class name is all that is safe to log
            LOG.error("op=handle outcome=internal_error type=%s", type(exc).__name__)
            return self._send(500, _error(None, -32603, "Internal error"), close=True)
        self._send(status, payload)

    def do_DELETE(self):
        self._refuse_method()

    def do_PUT(self):
        self._refuse_method()

    def do_PATCH(self):
        self._refuse_method()

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by the base class
        LOG.info("http " + format, *args)

    def _path(self) -> str:
        return urllib.parse.urlsplit(self.path).path

    def _refuse_method(self):
        if self._path() == "/mcp":
            self._send(405, {"error": "method not allowed"}, close=True, allow="POST")
        else:
            self._send(404, {"error": "not found"}, close=True)

    def _read_body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked()
        length = self.headers.get("Content-Length")
        if length is None or not length.isascii() or not length.isdigit():
            raise _BodyError(411)
        size = int(length)
        if size > MAX_BODY:
            if size <= MAX_DRAIN:
                while size > 0:
                    chunk = self.rfile.read(min(size, 65536))
                    if not chunk:
                        break
                    size -= len(chunk)
            raise _BodyError(413)
        return self.rfile.read(size)

    def _read_chunked(self) -> bytes:
        body = bytearray()
        while True:
            line = self.rfile.readline(64).split(b";", 1)[0].strip()
            try:
                size = int(line, 16)
            except ValueError:
                raise _BodyError(400) from None
            if size < 0:
                raise _BodyError(400)
            if size == 0:
                # Discard any trailer lines up to the terminating blank line.
                while self.rfile.readline(1024).strip():
                    pass
                return bytes(body)
            if len(body) + size > MAX_BODY:
                raise _BodyError(413)
            body += self.rfile.read(size)
            self.rfile.readline(2)

    def _send(self, status: int, payload: Any, close: bool = False, allow: str | None = None):
        data = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if allow:
            self.send_header("Allow", allow)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(data)


def build_server(app: McpApp, host: str = "0.0.0.0", port: int = 8000):
    server = http.server.ThreadingHTTPServer((host, port), Handler)
    server.app = app
    return server


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        broker = Broker(
            ConnectClient(_required("OP_CONNECT_HOST"), _required("OP_CONNECT_TOKEN")),
            _required("OP_BOUND_VAULT"),
        )
        app = McpApp(broker, _required("MCP_CALLER_TOKEN"))
        port = int(os.environ.get("PORT", "8000"))
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    server = build_server(app, port=port)
    LOG.info("serving vault=%s port=%d destinations=%s", broker.vault_name, port,
             ",".join(broker.destinations))
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
