# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Live link to Roblox Studio.

Roblox Studio has no project folder to drop files into and no command line to drive, but
it loads plugins from a local folder, and a plugin may talk to this computer over HTTP.
So the link is two halves:

* ``roblox_plugin.lua`` is installed into Studio's Plugins folder. In every open place it
  asks this server for work, builds what it is given with Studio's own APIs (Parts,
  MeshParts through EditableMesh, particle emitters), and reports back.
* This module is the server: loopback only, stdlib only, no ``bpy``. Each Studio window is
  a *session*; ``post`` queues a job for one session and ``result`` collects its report.

The plugin never receives code, only data, and this server only answers 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

PORTS = (7791, 7792, 7793)
PLUGIN_FILE = "UnixBridge.lua"
ALIVE_SECONDS = 25.0
POLL_SECONDS = 12.0
_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "roblox_plugin.lua")
# Writing into Roblox Studio's plugin folder is switched on by the running app only, so tests
# and headless runs never touch the user's Studio.
settings = {"install": False}


class RobloxLink:
    def __init__(self) -> None:
        self._server: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Condition()
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._queues: Dict[str, List[Dict[str, Any]]] = {}
        self._results: Dict[str, Dict[str, Any]] = {}
        self.port = 0

    # -- lifecycle ---------------------------------------------------------------------

    def start(self, ports=PORTS) -> bool:
        """Listen on the first free port. The plugin only knows ``PORTS``; tests pass (0,) to get a
        private port no real Studio can find."""
        if self._server is not None:
            return True
        link = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:   # quiet
                pass

            def _reply(self, code: int, body: Dict[str, Any]) -> None:
                data = json.dumps(body, separators=(",", ":")).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_POST(self) -> None:   # noqa: N802 (http.server API)
                if self.client_address[0] != "127.0.0.1" or not self.headers.get("X-Unix-Plugin"):
                    return self._reply(403, {"error": "forbidden"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > 2_000_000:
                        return self._reply(413, {"error": "too large"})
                    body = json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, OSError):
                    return self._reply(400, {"error": "bad request"})
                if self.path == "/unix/poll":
                    return self._reply(200, {"job": link._poll(body)})
                if self.path == "/unix/result":
                    link._finish(body)
                    return self._reply(200, {"ok": True})
                return self._reply(404, {"error": "not found"})

        class Server(ThreadingHTTPServer):
            # http.server asks for address reuse, and on Windows that lets a second process listen on
            # a port another Unix already owns, where it would never be reached (or would steal the
            # first one's Studio). Each Unix must own its port outright, so a taken port is skipped.
            allow_reuse_address = False

            def server_bind(self) -> None:
                exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
                if exclusive is not None:
                    self.socket.setsockopt(socket.SOL_SOCKET, exclusive, 1)
                super().server_bind()

        for port in ports:
            try:
                server = Server(("127.0.0.1", port), Handler)
            except OSError:
                continue
            server.daemon_threads = True
            self._server, self.port = server, server.server_address[1]
            threading.Thread(target=server.serve_forever, daemon=True, name="UnixRobloxLink").start()
            return True
        return False

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        with self._lock:
            self._sessions.clear()
            self._queues.clear()
            self._lock.notify_all()

    # -- the plugin's side -------------------------------------------------------------

    def _poll(self, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        session = str(body.get("session") or "")[:64]
        if not session:
            return None
        deadline = time.monotonic() + POLL_SECONDS
        with self._lock:
            while True:
                self._sessions[session] = {"session": session, "place": str(body.get("place") or "Unsaved place")[:120],
                                           "place_id": body.get("placeId") or 0, "game_id": body.get("gameId") or 0,
                                           "version": str(body.get("version") or ""), "seen": time.monotonic(),
                                           "testing": bool(body.get("testing"))}
                queue = self._queues.get(session)
                if queue:
                    return queue.pop(0)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._server is None:
                    return None
                self._lock.wait(min(remaining, 2.0))

    def _finish(self, body: Dict[str, Any]) -> None:
        job = str(body.get("job") or "")
        with self._lock:
            if job:
                self._results[job] = {"ok": bool(body.get("ok")), "report": body.get("report") or {},
                                      "message": str(body.get("message") or "")[:600]}
            self._lock.notify_all()

    # -- Unix's side ------------------------------------------------------------------

    def sessions(self) -> List[Dict[str, Any]]:
        """Studio windows that asked for work in the last few seconds, by place name."""
        now = time.monotonic()
        with self._lock:
            for key in [k for k, s in self._sessions.items() if now - s["seen"] > ALIVE_SECONDS]:
                self._sessions.pop(key, None)
                self._queues.pop(key, None)
            live = [dict(s) for s in self._sessions.values()]
        # After a plugin update Studio may keep the old copy running next to the new one for a
        # while: when the current version is connected, older copies are ignored.
        newest = plugin_version()
        if any(s["version"] == newest for s in live):
            live = [s for s in live if s["version"] == newest or s["version"] == "test"]
        names: Dict[str, int] = {}
        for item in sorted(live, key=lambda s: (s["place"].lower(), s["session"])):
            names[item["place"]] = names.get(item["place"], 0) + 1
            if names[item["place"]] > 1:           # two windows on places with the same name
                item["place"] = f"{item['place']} ({names[item['place']]})"
        return sorted(live, key=lambda s: s["place"].lower())

    def post(self, session: str, job: Dict[str, Any]) -> str:
        job = dict(job, id=uuid.uuid4().hex)
        with self._lock:
            if session not in self._sessions:
                raise KeyError(session)
            self._queues.setdefault(session, []).append(job)
            self._lock.notify_all()
        return job["id"]

    def result(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._results.pop(job_id, None)


link = RobloxLink()


def plugins_folder() -> Optional[str]:
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Roblox")
    if not os.path.isdir(base):
        return None      # Roblox Studio is not installed for this user
    return os.path.join(base, "Plugins")


def plugin_version() -> str:
    try:
        with open(_SOURCE, encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("local VERSION"):
                    return line.split('"')[1]
    except (OSError, IndexError):
        pass
    return ""


def _version_of(source: str) -> int:
    for line in source.splitlines()[:40]:
        if line.startswith("local VERSION"):
            try:
                return int(line.split('"')[1])
            except (IndexError, ValueError):
                return 0
    return 0


def install_plugin() -> str:
    """Put the bridge plugin where Studio loads local plugins. Returns what happened:
    'installed', 'updated', 'current', or 'no roblox' when Studio is not installed."""
    folder = plugins_folder()
    if folder is None:
        return "no roblox"
    if not settings["install"]:
        return "off"
    with open(_SOURCE, encoding="utf-8") as fh:
        source = fh.read()
    target = os.path.join(folder, PLUGIN_FILE)
    try:
        with open(target, encoding="utf-8") as fh:
            installed = fh.read()
        if installed == source or _version_of(installed) > _version_of(source):
            return "current"     # the same, or put there by a newer Unix on this computer: never step back
        state = "updated"
    except OSError:
        state = "installed"
    os.makedirs(folder, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(source)
    return state
