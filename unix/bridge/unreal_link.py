# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Unreal Editor link: Unreal's own Python remote execution, spoken from Blender.

The Python Script Plugin (UE 4.26 and later) can run Python sent from another program when
the project has "Enable Remote Execution" on. Editors are found by UDP multicast: a ``ping``
to the group, and every editor with remote execution on answers ``pong`` with its project
folder. A command channel is TCP the other way round: we listen, send ``open_connection``
with our address, the editor connects to us, and then each ``command`` gets one
``command_result`` back. Every message is one UTF-8 JSON object with ``magic`` "ue_py".

The editor parses each chunk it receives as a whole message, so a command must arrive in one
piece: commands stay a line or two long (they run a script from a file) and everything big
travels as files on disk.

The editor reads its project's settings at start, so ``enable`` can switch the link on in the
project's files, and it works from the next start of the editor. No ``bpy`` here.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import uuid
from typing import Any, Dict, List, Optional

GROUP = ("239.0.0.1", 6766)        # the plugin's default multicast group endpoint
BIND = "0.0.0.0"                   # the plugin's default multicast bind address
PROTOCOL = 1
MAGIC = "ue_py"
EXEC_FILE = "ExecuteFile"          # a literal script of many statements, or a file with arguments
EXEC_STATEMENT = "ExecuteStatement"
EVAL_STATEMENT = "EvaluateStatement"
PING_SECONDS = 0.4
ACCEPT_SECONDS = 15.0
PLUGINS = ("PythonScriptPlugin", "EditorScriptingUtilities")
SECTION = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]"
KEY = "bRemoteExecution"


class LinkError(Exception):
    """The editor could not be reached, or it broke off."""


def message(kind: str, source: str, dest: Optional[str] = None, data: Optional[Dict[str, Any]] = None) -> bytes:
    out: Dict[str, Any] = {"version": PROTOCOL, "magic": MAGIC, "type": kind, "source": source}
    if dest:
        out["dest"] = dest
    if data:
        out["data"] = data
    return json.dumps(out, ensure_ascii=False).encode("utf-8")


def parse(raw: bytes) -> Optional[Dict[str, Any]]:
    """The message in ``raw``, or None when it is not one of ours (or not whole yet)."""
    try:
        out = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(out, dict) or out.get("version") != PROTOCOL or out.get("magic") != MAGIC:
        return None
    if not isinstance(out.get("type"), str) or not isinstance(out.get("source"), str):
        return None
    return out


def _for_me(msg: Dict[str, Any], me: str) -> bool:
    return msg["source"] != me and (not msg.get("dest") or msg["dest"] == me)


def _group_socket(group, listen: bool = True) -> socket.socket:
    """A UDP socket that talks to the group on this machine only (TTL 0); with ``listen`` it is
    bound to the group's port and joined, because editors answer to the group, not to us."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_REUSEPORT", socket.SO_REUSEADDR), 1)
    if listen:
        sock.bind((BIND, group[1]))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(group[0]) + socket.inet_aton(BIND))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(BIND))
    sock.settimeout(0.1)
    return sock


def same_folder(a: str, b: str) -> bool:
    return bool(a and b) and os.path.normcase(os.path.normpath(os.path.abspath(a))) == \
        os.path.normcase(os.path.normpath(os.path.abspath(b)))


def find_editors(seconds: float = 1.2, group=None) -> List[Dict[str, Any]]:
    """Every editor on this machine that has remote execution on: its pong data plus ``node``.
    Waits ``seconds`` for answers. An editor without the setting stays silent."""
    group = group or GROUP
    me = str(uuid.uuid4())
    try:
        sock = _group_socket(group)
    except OSError:
        return []
    found: Dict[str, Dict[str, Any]] = {}
    try:
        deadline, pinged = time.monotonic() + seconds, 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - pinged > PING_SECONDS:
                pinged = time.monotonic()
                try:
                    sock.sendto(message("ping", me), group)
                except OSError:
                    pass
            try:
                raw = sock.recv(65536)
            except (socket.timeout, OSError):
                continue
            msg = parse(raw)
            if msg and msg["type"] == "pong" and _for_me(msg, me) and isinstance(msg.get("data"), dict):
                found[msg["source"]] = dict(msg["data"], node=msg["source"])
    finally:
        sock.close()
    return list(found.values())


class Channel:
    """One command channel to one editor. ``open`` waits for the editor to connect back; ``run``
    sends a command and blocks until its result: call it off Blender's main thread."""

    def __init__(self, node: str, group=None) -> None:
        self.node, self.group = node, group or GROUP
        self.me = str(uuid.uuid4())
        self.server: Optional[socket.socket] = None
        self.conn: Optional[socket.socket] = None
        self.udp: Optional[socket.socket] = None

    def open(self, seconds: float = ACCEPT_SECONDS) -> None:
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        self.server.bind(("127.0.0.1", 0))      # any free port: it travels in open_connection
        self.server.listen(1)
        self.server.settimeout(1.0)
        port = self.server.getsockname()[1]
        self.udp = _group_socket(self.group, listen=False)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.udp.sendto(message("open_connection", self.me, self.node,
                                    {"command_ip": "127.0.0.1", "command_port": port}), self.group)
            try:
                self.conn = self.server.accept()[0]
            except socket.timeout:
                continue
            self.conn.setblocking(True)
            return
        self.close()
        raise LinkError("The Unreal editor did not open its command channel.")

    def run(self, command: str, mode: str = EXEC_FILE, seconds: float = 3600.0) -> Dict[str, Any]:
        """{success, result, output: [{type, output}]} of one command."""
        if self.conn is None:
            raise LinkError("The command channel is not open.")
        self.conn.settimeout(seconds)
        self.conn.sendall(message("command", self.me, self.node,
                                  {"command": command, "unattended": True, "exec_mode": mode}))
        raw = b""
        while True:
            try:
                chunk = self.conn.recv(1 << 20)
            except socket.timeout:
                raise LinkError(f"The Unreal editor gave no answer in {seconds:.0f} s.")
            except OSError as exc:
                raise LinkError(f"The Unreal editor broke off the link ({exc}).")
            if not chunk:
                raise LinkError("The Unreal editor closed the link.")
            raw += chunk
            msg = parse(raw)
            if msg is None:
                continue                         # not whole yet
            if msg["type"] == "command_result" and _for_me(msg, self.me) and isinstance(msg.get("data"), dict):
                return msg["data"]
            raw = b""

    def close(self) -> None:
        if self.udp is not None and self.conn is not None:
            try:
                self.udp.sendto(message("close_connection", self.me, self.node), self.group)
            except OSError:
                pass
        for sock in (self.conn, self.server, self.udp):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        self.conn = self.server = self.udp = None


def run_once(node: str, command: str, mode: str = EXEC_FILE, seconds: float = 3600.0, group=None) -> Dict[str, Any]:
    channel = Channel(node, group)
    channel.open()
    try:
        return channel.run(command, mode, seconds)
    finally:
        channel.close()


# -- switching the link on in a project ------------------------------------------------

def uproject(root: str) -> Optional[str]:
    try:
        names = sorted(f for f in os.listdir(root) if f.lower().endswith(".uproject"))
    except OSError:
        return None
    return os.path.join(root, names[0]) if names else None


def _read(path: str) -> str:
    with open(path, "rb") as fh:
        return fh.read().decode("utf-8")


def _write(path: str, text: str) -> None:
    with open(path, "wb") as fh:
        fh.write(text.encode("utf-8"))


def _backup(path: str) -> str:
    """Copy ``path`` to the first free ``.bak`` name next to it: an older backup is never overwritten."""
    target, number = path + ".bak", 1
    while os.path.exists(target):
        target, number = f"{path}.bak{number}", number + 1
    with open(path, "rb") as src, open(target, "wb") as dst:
        dst.write(src.read())
    return target


def _plugins_missing(text: str) -> List[str]:
    data = json.loads(text.lstrip(""))
    entries = {p.get("Name"): p for p in data.get("Plugins") or [] if isinstance(p, dict)}
    return [name for name in PLUGINS if not (name in entries and entries[name].get("Enabled", False))]


def _closing(text: str, start: int, opening: str, closing: str) -> int:
    """Index of the bracket that closes the one at ``start``, skipping strings."""
    depth, index, quoted = 0, start, False
    while index < len(text):
        char = text[index]
        if quoted:
            if char == "\\":
                index += 1
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("unbalanced brackets")


def with_plugins(text: str) -> str:
    """The .uproject text with PythonScriptPlugin and EditorScriptingUtilities enabled. Only what
    has to change is touched: the file's indentation, line ends and every other entry stay."""
    missing = _plugins_missing(text)
    if not missing:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    indented = re.search(r"\n([ \t]+)\S", text)
    unit = indented.group(1) if indented else "\t"
    for name in list(missing):                       # listed but switched off: switch it on in place
        entry = re.search(r'\{[^{}]*"Name"\s*:\s*"' + re.escape(name) + r'"[^{}]*\}', text)
        if entry and re.search(r'"Enabled"\s*:\s*false', entry.group(0)):
            fixed = re.sub(r'"Enabled"\s*:\s*false', '"Enabled": true', entry.group(0), count=1)
            text = text[:entry.start()] + fixed + text[entry.end():]
            missing.remove(name)
        elif entry and '"Enabled"' not in entry.group(0):
            fixed = entry.group(0)[:-1].rstrip() + f',{newline}{unit * 3}"Enabled": true{newline}{unit * 2}}}'
            text = text[:entry.start()] + fixed + text[entry.end():]
            missing.remove(name)

    def block(indent: str) -> str:
        return f",{newline}".join(f'{indent}{{{newline}{indent}{unit}"Name": "{name}",{newline}'
                                   f'{indent}{unit}"Enabled": true{newline}{indent}}}' for name in missing)
    if missing:
        array = re.search(r'"Plugins"\s*:\s*\[', text)
        if array:
            begin = array.end() - 1
            end = _closing(text, begin, "[", "]")
            inside = text[begin + 1:end]
            first = re.search(r"\n([ \t]*)\{", inside)
            line_start = text.rfind("\n", 0, array.start()) + 1
            base = re.match(r"[ \t]*", text[line_start:]).group(0)
            indent = first.group(1) if first else base + unit
            body = inside.rstrip()
            text = (text[:begin + 1] + body + ("," if body.strip() else "") + newline + block(indent) + newline
                    + base + text[end:])
        else:
            end = text.rstrip().rfind("}")
            body = text[:end].rstrip()
            comma = "" if body.endswith("{") else ","
            text = (body + comma + newline + f'{unit}"Plugins": [{newline}' + block(unit * 2) + newline
                    + f"{unit}]{newline}" + text[end:])
    json.loads(text.lstrip(""))               # never write a file Unreal could not read
    return text


def remote_on(text: str) -> bool:
    section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped.lower() == SECTION.lower()
        elif section and re.match(KEY + r"\s*=", stripped, re.I):
            return stripped.split("=", 1)[1].strip().lower() in ("true", "1", "yes")
    return False


def with_remote(text: str) -> str:
    """DefaultEngine.ini text with remote execution on. Just that key is added or changed; every
    other byte of the file stays as it was."""
    if remote_on(text):
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if line.strip().lower() == SECTION.lower()), None)
    if header is not None:
        for index in range(header + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("["):
                break
            if re.match(KEY + r"\s*=", stripped, re.I):
                ending = lines[index][len(lines[index].rstrip("\r\n")):]
                lines[index] = f"{KEY}=True" + (ending or newline)
                return "".join(lines)
        if not lines[header].endswith(("\n", "\r")):
            lines[header] += newline
        lines.insert(header + 1, f"{KEY}=True{newline}")
        return "".join(lines)
    if text and not text.endswith(("\n", "\r")):
        text += newline
    return text + (newline if text.strip() else "") + f"{SECTION}{newline}{KEY}=True{newline}"


def link_state(root: str) -> Dict[str, Any]:
    """What the project's own files say about the link: {uproject, plugins_missing, remote_on}."""
    path = uproject(root)
    if path is None:
        return {"uproject": None, "plugins_missing": list(PLUGINS), "remote_on": False}
    try:
        missing = _plugins_missing(_read(path))
    except (OSError, ValueError, UnicodeDecodeError):
        missing = list(PLUGINS)
    ini = os.path.join(root, "Config", "DefaultEngine.ini")
    try:
        on = remote_on(_read(ini))
    except (OSError, UnicodeDecodeError):
        on = False
    return {"uproject": path, "plugins_missing": missing, "remote_on": on}


def enable(root: str) -> List[str]:
    """Switch the link on in the project's files: the two plugins in the .uproject and remote
    execution in Config/DefaultEngine.ini, each file backed up first. Returns the files changed
    (none when the project already has it). The editor picks it up at its next start."""
    path = uproject(root)
    if path is None:
        raise LinkError(f"'{root}' has no .uproject file.")
    changed: List[str] = []
    text = _read(path)
    fixed = with_plugins(text)
    if fixed != text:
        _backup(path)
        _write(path, fixed)
        changed.append(path)
    ini = os.path.join(root, "Config", "DefaultEngine.ini")
    text = _read(ini) if os.path.isfile(ini) else ""
    fixed = with_remote(text)
    if fixed != text:
        if os.path.isfile(ini):
            _backup(ini)
        os.makedirs(os.path.dirname(ini), exist_ok=True)
        _write(ini, fixed)
        changed.append(ini)
    return changed
