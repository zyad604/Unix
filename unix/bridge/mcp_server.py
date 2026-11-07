# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Minimal MCP server (Streamable HTTP, JSON responses) on a daemon thread.

This is the bridge between the model and Blender: the Claude CLI connects here as an
MCP client and calls our tools natively. Stdlib only, no ``bpy`` imports, so it can
be unit-tested outside Blender. Tool execution is delegated to ``call_tool``, which
the add-on wires to the main-thread dispatcher.

Security: loopback bind, random port, and a per-session bearer token. Any other
local process that does not hold the token gets 401.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "china-unix", "version": "0.1.0"}
TOOL_TIMEOUT_MS = 60 * 60 * 1000     # a bake, a render or a team at work answers after minutes of silence

ListTools = Callable[[], List[Dict[str, Any]]]
CallTool = Callable[[str, Dict[str, Any]], Dict[str, Any]]


caller = threading.local()      # caller.guest: the name behind the request being handled on this thread ('' = the lead)


class McpServer:
    """Owns the HTTP server thread. ``list_tools`` returns MCP tool descriptors and
    ``call_tool(name, arguments)`` returns ``{"content": [...], "isError": bool}``.

    Several agents can use the server at once. The lead holds ``token``; each helper is given a
    token of its own (``guest_config``), so a call can be told apart by who made it: while it is
    handled, ``caller.guest`` is that helper's name on the handling thread."""

    def __init__(self, list_tools: ListTools, call_tool: CallTool, host: str = "127.0.0.1", port: int = 0):
        self._list_tools = list_tools
        self._call_tool = call_tool
        self._host = host
        self._port = port
        self.token = secrets.token_urlsafe(24)
        self.guests: Dict[str, str] = {}        # token -> helper name
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(_Handler):
            mcp = server

        self._httpd = _QuietServer((self._host, self._port), Handler)
        self._httpd.daemon_threads = True
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True, name="UnixMcpServer")
        self._thread.start()

    def stop(self) -> None:
        httpd, self._httpd, self._thread = self._httpd, None, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}/mcp"

    def client_config(self, server_name: str = "unix") -> Dict[str, Any]:
        """The ``--mcp-config`` JSON for the Claude CLI."""
        return {"mcpServers": {server_name: {
            "type": "http", "url": self.url, "timeout": TOOL_TIMEOUT_MS,
            "headers": {"Authorization": f"Bearer {self.token}"},
        }}}

    def guest_config(self, name: str, server_name: str = "unix") -> Dict[str, Any]:
        """A config like ``client_config`` for a helper, with a token of its own."""
        token = secrets.token_urlsafe(24)
        self.guests[token] = name
        return {"mcpServers": {server_name: {"type": "http", "url": self.url, "timeout": TOOL_TIMEOUT_MS,
                                             "headers": {"Authorization": f"Bearer {token}"}}}}

    def dismiss(self, name: str) -> None:
        for token in [t for t, who in self.guests.items() if who == name]:
            self.guests.pop(token, None)

    # -- JSON-RPC ----------------------------------------------------------

    def handle(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """One JSON-RPC message in, one response out (None for notifications)."""
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # notification
            return None
        try:
            if method == "initialize":
                requested = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL_VERSION
                result = {"protocolVersion": requested, "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": SERVER_INFO}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self._list_tools()}
            elif method == "tools/call":
                params = msg.get("params") or {}
                result = self._call_tool(params.get("name", ""), params.get("arguments") or {})
            else:
                return {"jsonrpc": "2.0", "id": msg_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"}}
        except Exception as exc:  # never let a tool crash the transport
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # client hang-ups are routine
        pass


class _Handler(BaseHTTPRequestHandler):
    mcp: McpServer
    server_version = "china-unix-mcp/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default stderr access log
        pass

    def _reply(self, status: int, payload: Any = None) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self) -> bool:
        given = self.headers.get("Authorization", "")
        caller.guest = ""
        if secrets.compare_digest(given, f"Bearer {self.mcp.token}"):
            return True
        for token, name in list(self.mcp.guests.items()):
            if secrets.compare_digest(given, f"Bearer {token}"):
                caller.guest = name
                return True
        return False

    def do_GET(self):  # no server-initiated stream
        self._reply(405, {"error": "SSE stream not offered"})

    def do_DELETE(self):
        self._reply(200 if self._authorized() else 401)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        if isinstance(msg, list):
            replies = [r for r in (self.mcp.handle(m) for m in msg) if r is not None]
            return self._reply(200, replies) if replies else self._reply(202)
        reply = self.mcp.handle(msg)
        self._reply(202) if reply is None else self._reply(200, reply)
