# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Run one agent turn with the Claude CLI and stream what it does.

The CLI is the agent loop: it talks to the model, calls our MCP tools, and keeps the
conversation in a session we resume on the next message. We disable its built-in
tools (no file edits, no shell) and ignore user/project settings, so the only things
the model can do are the Blender tools this add-on exposes.

No ``bpy`` import. Events are plain dicts pushed to a queue by a reader thread:
  {"kind": "session", "session_id"}            {"kind": "text", "text"}
  {"kind": "tool_use", "id", "name", "input"}  {"kind": "tool_result", "id", "is_error", "summary"}
  {"kind": "done", "text", "is_error", "turns", "cost_usd", "session_id", "seconds"}
"""

from __future__ import annotations

import collections
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MCP_SERVER_NAME = "unix"
TOOL_PREFIX = f"mcp__{MCP_SERVER_NAME}__"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def find_claude() -> Optional[str]:
    """Path to the Claude CLI, preferring the real binary over the npm .cmd shim."""
    override = os.environ.get("CLAUDE_BIN")
    if override:
        return override
    found = shutil.which("claude.exe") or shutil.which("claude")
    candidates = [Path(found)] if found else []
    home = Path.home()
    candidates += [home / "AppData/Roaming/npm/claude.cmd", home / ".local/bin/claude", home / ".claude/local/claude",
                   Path("/usr/local/bin/claude"), Path("/opt/homebrew/bin/claude")]
    for cand in candidates:
        if not cand.exists():
            continue
        real = cand.resolve().parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return str(real) if real.exists() else str(cand)
    return None


def _summarise(content: Any, limit: int = 300) -> str:
    if isinstance(content, str):
        return content[:limit]
    parts: List[str] = []
    images = 0
    for block in content or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "image":
                images += 1
    text = " ".join(parts)[:limit]
    return text + (f" [+{images} image{'s' if images > 1 else ''}]" if images else "")


class AgentRun:
    """One CLI process for one user message."""

    def __init__(self, *, claude_bin: str, prompt: str, system_prompt_file: str, mcp_config_file: str,
                 workdir: str, model: str = "sonnet", resume: Optional[str] = None,
                 effort: Optional[str] = None, max_turns: int = 80):
        self.events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._prompt = prompt
        self._started = time.monotonic()
        self._stopped = False
        self._writing = 0                      # characters of the step being written so far
        self._proc: Optional[subprocess.Popen] = None
        self._cmd = [
            claude_bin, "-p", "--output-format", "stream-json", "--verbose",
            "--model", model,
            "--system-prompt-file", system_prompt_file,
            "--mcp-config", mcp_config_file, "--strict-mcp-config",
            "--tools", "",                       # no built-in tools: Blender tools only
            "--allowedTools", f"mcp__{MCP_SERVER_NAME}",
            "--setting-sources", "",             # ignore user/project CLAUDE.md and settings
            "--disable-slash-commands",
            "--max-turns", str(max_turns),
            # the step being written streams in: the island can say what is coming while a long script
            # is still being typed, instead of "Thinking" for half a minute
            "--include-partial-messages",
        ]
        if effort:
            self._cmd += ["--effort", effort]
        if resume:
            self._cmd += ["--resume", resume]
        self._workdir = workdir

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        os.makedirs(self._workdir, exist_ok=True)
        env = dict(os.environ)
        # Simulation bakes, renders and image generation run for minutes inside one tool call.
        env.setdefault("MCP_TOOL_TIMEOUT", str(60 * 60 * 1000))
        # The CLI also gives up on a call that has been silent for five minutes: a team at work or a
        # sharp simulation is silent for longer, and was being cut off with the work half done.
        env.setdefault("CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT", "0")
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
                                      stderr=subprocess.PIPE, cwd=self._workdir, creationflags=_NO_WINDOW)
        assert self._proc.stdin is not None
        self._proc.stdin.write(self._prompt.encode("utf-8"))
        self._proc.stdin.close()
        threading.Thread(target=self._read, daemon=True, name="UnixAgentReader").start()

    def stop(self) -> None:
        self._stopped = True
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # -- stream parsing ----------------------------------------------------

    def _partial(self, event: Dict[str, Any]) -> None:
        """A piece of a message still being written. Only what it is (thinking, a reply, which tool)
        and how much of a tool call has been written are passed on: the model's thinking itself is
        not shown by the newer models, and the finished blocks arrive as usual afterwards."""
        if event.get("type") == "content_block_start":
            block = event.get("content_block") or {}
            kind = block.get("type")
            self._writing = 0
            if kind == "tool_use":
                name = block.get("name", "")
                self.events.put({"kind": "preparing", "what": "tool",
                                 "name": name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name})
            elif kind in ("thinking", "redacted_thinking", "text"):
                self.events.put({"kind": "preparing", "what": "thinking" if kind != "text" else "text", "name": ""})
        elif event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "input_json_delta":
                before = self._writing
                self._writing += len(delta.get("partial_json") or "")
                if self._writing // 500 != before // 500:
                    self.events.put({"kind": "preparing_more", "chars": self._writing})

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None and proc.stderr is not None
        finished = False
        # stderr is read as it comes, keeping the last lines: left unread, a CLI that writes more than the
        # pipe holds (a few KB of warnings over a long goal) blocks on the write and the turn hangs for good.
        errors: "collections.deque[bytes]" = collections.deque(maxlen=40)
        drain = threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True, name="UnixAgentStderr")
        drain.start()
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            finished = self._handle(event) or finished
        proc.wait()
        drain.join(2.0)
        if not finished:
            stderr = b"".join(errors).decode("utf-8", errors="replace").strip()
            if self._stopped:
                message = "Stopped."
            else:
                message = stderr[-600:] or f"The Claude CLI exited with code {proc.returncode} without a result."
            self.events.put({"kind": "done", "text": message, "is_error": not self._stopped, "stopped": self._stopped,
                             "turns": 0, "cost_usd": 0.0, "session_id": None,
                             "seconds": round(time.monotonic() - self._started, 1)})

    def _handle(self, event: Dict[str, Any]) -> bool:
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            servers = {s.get("name"): s.get("status") for s in event.get("mcp_servers") or []}
            self.events.put({"kind": "session", "session_id": event.get("session_id"),
                             "bridge": servers.get(MCP_SERVER_NAME, "missing"),
                             "tools": len([t for t in event.get("tools") or [] if t.startswith(TOOL_PREFIX)])})
        elif kind == "stream_event":
            self._partial(event.get("event") or {})
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.events.put({"kind": "text", "text": block["text"].strip()})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "")
                    # only Blender tools exist here (--tools ""): a name without the prefix is a slip of the
                    # model's, which the CLI answers with "No such tool available"
                    self.events.put({"kind": "tool_use", "id": block.get("id"),
                                     "name": name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name,
                                     "misnamed": bool(name) and not name.startswith(TOOL_PREFIX),
                                     "input": block.get("input") or {}})
        elif kind == "user":
            for block in (event.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.events.put({"kind": "tool_result", "id": block.get("tool_use_id"),
                                     "is_error": bool(block.get("is_error")),
                                     "summary": _summarise(block.get("content")),
                                     "text": _summarise(block.get("content"), limit=8000)})
        elif kind == "result":
            text = event.get("result") or ""
            if event.get("is_error") and "not logged in" in str(text).lower():
                text = "The Claude CLI is not signed in on this computer. Run `claude auth login` once in a terminal."
            self.events.put({"kind": "done", "text": text, "is_error": bool(event.get("is_error")),
                             "stopped": False, "turns": event.get("num_turns", 0),
                             "cost_usd": event.get("total_cost_usd") or 0.0, "session_id": event.get("session_id"),
                             "seconds": round(time.monotonic() - self._started, 1)})
            return True
        return False


# -- Codex CLI (OpenAI through the user's ChatGPT login) ------------------------

BRIDGE_TOKEN_ENV = "UNIX_BRIDGE_TOKEN"
_CODEX_DISABLED = ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
                   "multi_agent", "skill_search", "tool_suggest", "image_generation")


def find_codex() -> Optional[str]:
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    for cand in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"),
                 str(Path.home() / ".local/bin/codex"), "/usr/local/bin/codex", "/opt/homebrew/bin/codex"):
        if os.path.isfile(cand):
            return cand
    return None


class CodexRun(AgentRun):
    """One ``codex exec`` process for one user message, emitting the same events as AgentRun.

    Codex's shell and every other built-in tool are disabled and the user's Codex config is
    ignored, so, like the Claude runner, the model can only act through the Blender tools.
    """

    def __init__(self, *, claude_bin: str, prompt: str, system_prompt_file: str, mcp_config_file: str,
                 workdir: str, model: str = "", resume: Optional[str] = None,
                 effort: Optional[str] = None, **_ignored: Any):
        self.events = queue.Queue()
        self._prompt = prompt
        self._started = time.monotonic()
        self._stopped = False
        self._proc = None
        self._workdir = workdir
        self._last_text = ""
        self._thread_id = resume
        with open(mcp_config_file, encoding="utf-8") as fh:
            server = json.load(fh)["mcpServers"][MCP_SERVER_NAME]
        self._token = server["headers"]["Authorization"].split(" ", 1)[1]
        cmd = [claude_bin, "exec"]
        if resume:
            cmd += ["resume", resume]
        cmd += ["--skip-git-repo-check", "--json", "--ignore-user-config", "--ignore-rules"]
        if not resume:
            cmd += ["-s", "read-only", "-C", workdir]
        for feature in _CODEX_DISABLED:
            cmd += ["--disable", feature]
        cmd += ["-c", f'mcp_servers.{MCP_SERVER_NAME}.url="{server["url"]}"',
                "-c", f'mcp_servers.{MCP_SERVER_NAME}.bearer_token_env_var="{BRIDGE_TOKEN_ENV}"',
                "-c", f'mcp_servers.{MCP_SERVER_NAME}.default_tools_approval_mode="approve"',
                "-c", f'mcp_servers.{MCP_SERVER_NAME}.tool_timeout_sec=1800',
                "-c", "model_instructions_file=" + json.dumps(system_prompt_file.replace("\\", "/"))]
        if model:
            cmd += ["-m", model]
        if effort:  # Codex has no "max"; its top level is xhigh
            cmd += ["-c", f'model_reasoning_effort="{"xhigh" if effort == "max" else effort}"']
        cmd.append("-")
        self._cmd = cmd

    def start(self) -> None:
        os.makedirs(self._workdir, exist_ok=True)
        env = dict(os.environ)
        env[BRIDGE_TOKEN_ENV] = self._token
        self._proc = subprocess.Popen(self._cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, cwd=self._workdir, env=env, creationflags=_NO_WINDOW)
        self._proc.stdin.write(self._prompt.encode("utf-8"))
        self._proc.stdin.close()
        threading.Thread(target=self._read, daemon=True, name="UnixCodexReader").start()

    def _done(self, text: str, is_error: bool) -> None:
        self.events.put({"kind": "done", "text": text, "is_error": is_error, "stopped": False, "turns": 0,
                         "cost_usd": 0.0, "session_id": self._thread_id,
                         "seconds": round(time.monotonic() - self._started, 1)})

    def _handle(self, event: Dict[str, Any]) -> bool:
        kind = event.get("type")
        item = event.get("item") or {}
        if kind == "thread.started":
            self._thread_id = event.get("thread_id") or self._thread_id
            self.events.put({"kind": "session", "session_id": self._thread_id, "bridge": "connected", "tools": 0})
        elif kind == "item.started" and item.get("type") == "mcp_tool_call":
            self.events.put({"kind": "tool_use", "id": item.get("id"), "name": item.get("tool", ""),
                             "input": item.get("arguments") or {}})
        elif kind == "item.completed" and item.get("type") == "mcp_tool_call":
            result = item.get("result") or {}
            error = item.get("error")
            content = result.get("content") if isinstance(result, dict) else None
            failed = bool(error) or item.get("status") == "failed" or bool(isinstance(result, dict) and result.get("isError"))
            text = _summarise(content, limit=8000) if content else str((error or {}).get("message", ""))
            self.events.put({"kind": "tool_result", "id": item.get("id"), "is_error": failed,
                             "summary": text[:300], "text": text})
        elif kind == "item.completed" and item.get("type") == "agent_message":
            self._last_text = (item.get("text") or "").strip()
            if self._last_text:
                self.events.put({"kind": "text", "text": self._last_text})
        elif kind == "turn.completed":
            self._done(self._last_text, False)
            return True
        elif kind in {"turn.failed", "error"}:
            message = str(event.get("message") or (event.get("error") or {}).get("message") or "The Codex run failed.")
            if "login" in message.lower() or "unauthorized" in message.lower():
                message += " Sign in once with `codex login` in a terminal."
            self._done(message, True)
            return True
        return False
