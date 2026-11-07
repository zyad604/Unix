# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Tool registry: declaration, argument checking, and MCP result encoding.

A tool is a plain function ``fn(**arguments)`` that returns JSON-able data or a
``ToolOutput`` (data plus image files to show the model). ``Registry.call`` turns
that into an MCP ``tools/call`` result and converts every failure into a readable
``isError`` result, so the model can recover instead of the transport dying.

No ``bpy`` import here: main-thread hopping is injected via ``run_on_main``.

A main-thread tool that can take long is written as a generator: it ``yield``s wherever it may
be paused, and ``yield WAIT`` while something else does the work (a render job on Blender's own
thread). The dispatcher runs it a slice at a time, so the window redraws and the user can
orbit, scroll and press Stop between slices; called inline (tests, background mode) it simply
runs to the end. Its ``return`` value is the tool's result.
"""

from __future__ import annotations

import base64
import json
import os
import time
import traceback
from types import GeneratorType
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

MAX_TEXT_CHARS = 24_000
MAX_IMAGE_BYTES = 3_500_000
WAIT = "wait"           # the same marker as bridge.main_thread.WAIT: end this slice, nothing to do yet


@dataclass
class ToolOutput:
    data: Any = None
    images: List[str] = field(default_factory=list)  # file paths (png/jpg)


class ToolError(Exception):
    """Raise from a tool for an expected, model-actionable failure (no traceback shown)."""


@dataclass
class Pending:
    """Returned by a main-thread tool that has started a long job in Blender (a simulation
    bake) instead of blocking on it. ``poll`` is called on the main thread every ``every``
    seconds; it returns None while the job runs and the tool's result when it is done. The
    main thread is free in between, so the window keeps redrawing and answering the user.
    ``cancel`` is called (on the main thread) if the turn ends or is stopped before the job does."""
    poll: Callable[[], Any]
    every: float = 0.5
    cancel: Optional[Callable[[], Any]] = None


@dataclass
class Tool:
    name: str
    description: str
    schema: Dict[str, Any]
    fn: Callable[..., Any]
    main_thread: bool = True
    timeout: float = 300.0
    mutates: bool = False  # changes the scene (shown as "acted" rather than "observed")
    label: str = ""        # friendly past-tense label for the UI


class Registry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}
        # Injected by the add-on: runs fn on Blender's main thread with a usable context.
        self.run_on_main: Callable[[Callable[[], Any], float], Any] = lambda fn, timeout: fn()
        self.on_event: Optional[Callable[[Dict[str, Any]], None]] = None
        self._turn = 0      # goes up when a turn ends: long jobs started before that are nobody's any more
        # Injected by the tools: goes up when Blender replaces its data (undo, redo, a file opened). A long
        # job must not touch what it held from before that: the objects it points at may be gone.
        self.data_epoch: Callable[[], int] = lambda: 0

    def abandon(self) -> None:
        """The agent that was waiting for the long jobs now running is gone (its turn ended or was
        stopped). They stop at their next step instead of working on for minutes behind the next turn."""
        self._turn += 1

    # -- declaration -------------------------------------------------------

    def tool(self, name: str, description: str, properties: Optional[Dict[str, Any]] = None,
             required: Optional[List[str]] = None, *, main_thread: bool = True, timeout: float = 300.0,
             mutates: bool = False, label: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        schema = {"type": "object", "properties": properties or {}, "required": required or [],
                  "additionalProperties": False}

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            if name in self._tools:
                raise ValueError(f"duplicate tool: {name}")
            self._tools[name] = Tool(name, " ".join(description.split()), schema, fn, main_thread, timeout,
                                     mutates, label or name.replace("_", " "))
            return fn

        return decorate

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def names(self) -> List[str]:
        return sorted(self._tools)

    def descriptors(self) -> List[Dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.schema}
                for t in self._tools.values()]

    # -- execution ---------------------------------------------------------

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tool = self._tools.get(name)
        if tool is None:
            return _error(f"Unknown tool '{name}'. Available: {', '.join(self.names())}")
        problem = _check_arguments(tool, arguments)
        if problem:
            return _error(problem)
        started, turn = time.monotonic(), self._turn
        self._emit({"kind": "tool_start", "name": name})
        images: List[str] = []

        def run() -> Any:
            out = tool.fn(**arguments)
            return self._watched(name, out, started, turn, tool.timeout) if isinstance(out, GeneratorType) else _detached(out)
        run.label = name        # type: ignore[attr-defined]  # what the dispatcher's stall log calls it

        try:
            if tool.main_thread:
                out = self.run_on_main(run, tool.timeout)
            else:
                out = tool.fn(**arguments)
            if isinstance(out, GeneratorType):      # run_on_main ran inline and handed the steps back
                out = _drain(out)
            epoch = self.data_epoch()
            while isinstance(out, Pending):
                if time.monotonic() - started > tool.timeout:
                    raise ToolError(f"{name} did not finish within {tool.timeout:.0f}s.")
                waiting = out
                if self.data_epoch() != epoch:      # what the job holds may be gone: neither poll nor cancel it
                    raise ToolError(f"{name} stopped: the scene was undone or another file was opened while it ran.")
                if self._turn != turn:
                    if waiting.cancel is not None:
                        self.run_on_main(waiting.cancel, 120.0)
                    raise ToolError(f"{name} was stopped before it finished.")
                time.sleep(waiting.every)
                step = self.run_on_main(lambda: _detached(waiting.poll()), 120.0)
                out = waiting if step is None else step
            if isinstance(out, ToolOutput):
                images = list(out.images)
            result = _encode(out)
        except ToolError as exc:
            result = _error(str(exc))
        except Exception as exc:
            result = _error(f"{type(exc).__name__}: {exc}\n{_short_traceback()}")
        self._emit({"kind": "tool_end", "name": name, "is_error": result["isError"], "images": images,
                    "seconds": round(time.monotonic() - started, 2)})
        return result

    def _watched(self, name: str, steps: Any, started: float, turn: int, timeout: float) -> Any:
        """A sliced tool's steps, stopped between two slices when its turn ends, when it runs out of
        time, or when Blender's data was replaced under it (an undo). The tool's own ``finally``
        blocks run when it is stopped; they must check ``data_epoch`` before touching what they held."""
        epoch = self.data_epoch()
        try:
            while True:
                try:
                    signal = next(steps)
                except StopIteration as done:
                    return _detached(done.value)
                yield signal
                if self._turn != turn:
                    raise ToolError(f"{name} was stopped before it finished.")
                if time.monotonic() - started > timeout:
                    raise ToolError(f"{name} did not finish within {timeout:.0f}s.")
                if self.data_epoch() != epoch:
                    raise ToolError(f"{name} stopped: the scene was undone or another file was opened while it ran. "
                                    f"Call it again if it is still needed.")
        finally:
            steps.close()

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:
                pass


# -- helpers ---------------------------------------------------------------

def _drain(steps: Any) -> Any:
    """Run a sliced job to its end right here (see bridge.main_thread.drain)."""
    while True:
        try:
            signal = next(steps)
        except StopIteration as done:
            return done.value
        if signal == WAIT:
            time.sleep(0.02)


_JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
               "array": list, "object": dict}


def _check_arguments(tool: Tool, arguments: Dict[str, Any]) -> Optional[str]:
    props = tool.schema["properties"]
    missing = [k for k in tool.schema["required"] if k not in arguments]
    if missing:
        return f"{tool.name}: missing required argument(s): {', '.join(missing)}"
    unknown = [k for k in arguments if k not in props]
    if unknown:
        return f"{tool.name}: unknown argument(s): {', '.join(unknown)}. Allowed: {', '.join(props) or 'none'}"
    for key, value in arguments.items():
        want = props[key].get("type")
        py = _JSON_TYPES.get(want) if isinstance(want, str) else None
        if py and value is not None and (not isinstance(value, py) or (want != "boolean" and isinstance(value, bool))):
            return f"{tool.name}: argument '{key}' must be {want}, got {type(value).__name__}"
        allowed = props[key].get("enum")
        if allowed and value not in allowed:
            return f"{tool.name}: argument '{key}' must be one of {allowed}"
    return None


def _encode(out: Any) -> Dict[str, Any]:
    images: List[str] = []
    if isinstance(out, ToolOutput):
        out, images = out.data, out.images
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=_jsonable, separators=(",", ":"))
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + f"\n...[truncated {len(text) - MAX_TEXT_CHARS} chars; narrow the query]"
    content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
    for path in images:
        try:
            if os.path.getsize(path) > MAX_IMAGE_BYTES:
                content.append({"type": "text", "text": f"[image too large to attach: {path}]"})
                continue
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode("ascii")
            mime = "image/jpeg" if path.lower().endswith((".jpg", ".jpeg")) else "image/png"
            content.append({"type": "image", "data": data, "mimeType": mime})
        except OSError as exc:
            content.append({"type": "text", "text": f"[could not read image {path}: {exc}]"})
    return {"content": content, "isError": False}


def _detached(out: Any) -> Any:
    """Main thread: the result as plain data. A tool may hand back a vector that reads an object's
    location, or a bpy struct; encoded on the request thread, those read Blender's memory while the main
    thread changes it (and are freed there). Nothing that points into Blender leaves the main thread."""
    if out is None or isinstance(out, (str, int, float, bool, Pending, GeneratorType)):
        return out
    if isinstance(out, ToolOutput):
        return ToolOutput(_detached(out.data), list(out.images))
    return json.loads(json.dumps(out, ensure_ascii=False, default=_jsonable))


def _error(message: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": message[:MAX_TEXT_CHARS]}], "isError": True}


def _short_traceback(limit: int = 6) -> str:
    lines = traceback.format_exc().strip().splitlines()
    return "\n".join(lines[-limit * 2:])


def _jsonable(value: Any) -> Any:
    """Fallback encoder for mathutils types and other non-JSON values."""
    for attr in ("to_tuple", "to_list"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return list(fn())
            except Exception:
                pass
    try:
        return [_jsonable(v) if not isinstance(v, (int, float, str, bool, type(None))) else v for v in value]
    except TypeError:
        return repr(value)


registry = Registry()
tool = registry.tool
