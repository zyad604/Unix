# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""run_python: execute a model-written script on Blender's main thread.

What makes this robust:
- a static guard and a safe-iteration rewrite (``guard.py``);
- a real deadline: a watchdog injects ``ScriptTimeout`` into the main thread, which
  stops runaway Python loops (it cannot interrupt a single long C call);
- captured stdout, a JSON ``result`` convention, and a traceback that quotes the
  failing line of the script;
- a before/after diff of the scene so the model sees what it actually changed.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import math
import threading
import time
import traceback
from typing import Any, Dict, Optional, Tuple

import bmesh
import bpy
import mathutils

from . import _turn, guard
from ._util import ui_context
from .registry import ToolError, tool

MAX_STDOUT = 6000
_settings = {"unrestricted": False}


class ScriptTimeout(BaseException):
    """Raised inside a script that ran past its deadline."""


def set_unrestricted(value: bool) -> None:
    _settings["unrestricted"] = bool(value)


# -- scene diff ------------------------------------------------------------

def _snapshot() -> Dict[str, Tuple]:
    snap = {}
    for obj in bpy.data.objects:
        data = obj.data
        geo = (len(data.vertices), len(data.polygons)) if obj.type == 'MESH' else None
        snap[obj.name] = (
            obj.type, geo, tuple(round(v, 5) for row in obj.matrix_world for v in row),
            tuple(m.type for m in obj.modifiers),
            tuple(s.material.name if s.material else None for s in obj.material_slots),
            obj.parent.name if obj.parent else None,
        )
    return snap


def _diff(before: Dict[str, Tuple], after: Dict[str, Tuple]) -> Dict[str, Any]:
    created = sorted(n for n in after if n not in before)
    deleted = sorted(n for n in before if n not in after)
    modified = sorted(n for n in after if n in before and after[n] != before[n])
    out: Dict[str, Any] = {}
    for key, names in (("created", created), ("modified", modified), ("deleted", deleted)):
        if names:
            out[key] = names[:40] + ([f"...+{len(names) - 40} more"] if len(names) > 40 else [])
    return out or {"note": "no object-level changes detected"}


# -- deadline --------------------------------------------------------------

class _Deadline:
    """Injects ScriptTimeout into the calling (main) thread after ``seconds``."""

    def __init__(self, seconds: float):
        self._seconds = seconds
        self._tid = threading.get_ident()
        self._lock = threading.Lock()
        self._finished = False
        self._timer = threading.Timer(seconds, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        with self._lock:
            if not self._finished:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(self._tid), ctypes.py_object(ScriptTimeout))

    def __enter__(self) -> "_Deadline":
        self._timer.start()
        return self

    def __exit__(self, *exc) -> bool:
        with self._lock:
            self._finished = True
        self._timer.cancel()
        return False


def _format_error(code: str) -> str:
    """Traceback trimmed to the script's own frames, quoting the failing line."""
    etype, value, tb = traceback.sys.exc_info()
    lines = code.splitlines()
    frames = [f for f in traceback.extract_tb(tb) if f.filename == "<unix>"]
    parts = []
    for frame in frames[-3:]:
        src = lines[frame.lineno - 1].strip() if 0 < frame.lineno <= len(lines) else ""
        parts.append(f"  line {frame.lineno}, in {frame.name}: {src}")
    return f"{etype.__name__}: {value}\n" + "\n".join(parts)


def _to_jsonable(value: Any, depth: int = 0) -> Any:
    if isinstance(value, (str, int, bool, type(None))):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if depth > 6:
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v, depth + 1) for k, v in list(value.items())[:200]}
    if isinstance(value, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion, mathutils.Color)):
        return [round(v, 6) for v in value]
    if isinstance(value, mathutils.Matrix):
        return [[round(v, 6) for v in row] for row in value]
    if isinstance(value, bpy.types.ID):
        return f"<{type(value).__name__} '{value.name}'>"
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(v, depth + 1) for v in list(value)[:500]]
    return repr(value)


@tool(
    "run_python",
    """Run a Python script inside Blender (main thread) to create or edit the scene. Available
    names: bpy, bmesh, mathutils, math, Vector, Matrix, Euler, Quaternion, Color, C (bpy.context),
    D (bpy.data). Assign a JSON-able value to a variable named `result` to return data; print()
    output is captured. The reply includes created/modified/deleted object names. Keep each
    script focused on one step (roughly under 80 lines), make it safe to re-run (get-or-create
    by name), and prefer the bpy.data / bmesh API over bpy.ops where practical. File writes,
    network, subprocess and file-open/save operators are refused: use import_file / export_file.
    Blender's window is frozen while a script runs, and the reply says how long it took. On a big
    scene keep each script under a few seconds: touch only the objects the step is about (find
    them by name or collection, not by looping over every object), build with bpy.data and bmesh
    rather than one bpy.ops call per object (each operator call re-evaluates the scene), avoid
    calling view_layer.update() inside loops, and split a large build into several calls.""",
    {
        "code": {"type": "string", "description": "The Python source to execute."},
        "purpose": {"type": "string", "description": "A few words on what this step does, shown to the user."},
        "timeout_s": {"type": "integer", "description": "Deadline in seconds (default 120, max 600)."},
    },
    ["code"],
    timeout=660.0,
    mutates=True,
    label="Ran a script",
)
def run_python(code: str, purpose: str = "", timeout_s: int = 120) -> Dict[str, Any]:
    if bpy.app.is_job_running('RENDER'):
        raise ToolError("A render is running; scripts are refused until it finishes. Wait, then retry.")
    compiled, problem = guard.prepare(code, unrestricted=_settings["unrestricted"])
    if problem:
        raise ToolError(problem)

    namespace: Dict[str, Any] = {
        "__name__": "__unix__", "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math,
        "Vector": mathutils.Vector, "Matrix": mathutils.Matrix, "Euler": mathutils.Euler,
        "Quaternion": mathutils.Quaternion, "Color": mathutils.Color, "C": bpy.context, "D": bpy.data,
    }
    _turn.mark_mutation()
    before = _snapshot()
    stdout = io.StringIO()
    error: Optional[str] = None
    seconds = max(5, min(int(timeout_s), 600))
    began = time.monotonic()
    try:
        with ui_context(), contextlib.redirect_stdout(stdout), _Deadline(seconds):
            exec(compiled, namespace)  # noqa: S102 - this is the tool's purpose
            for _ in range(64):  # give a just-fired deadline a place to land inside this try
                pass
    except ScriptTimeout:
        error = (f"ScriptTimeout: the script ran past {seconds}s and was stopped. "
                 "Split the work into smaller steps or reduce the loop size.")
    except Exception:
        error = _format_error(code)

    ran = time.monotonic() - began
    try:
        bpy.context.view_layer.update()
    except Exception:
        pass
    out: Dict[str, Any] = {"ok": error is None, "seconds": round(ran, 2)}
    if ran > 3.0:               # the window was frozen all that time
        out["slow"] = (f"This script held Blender for {ran:.1f}s, and the window could not answer meanwhile. Make the "
                       f"next ones lighter: only the objects the step needs, bpy.data/bmesh instead of bpy.ops in loops, "
                       f"no view_layer.update() inside loops, smaller batches.")
    if error:
        out["error"] = error
    printed = stdout.getvalue()
    if printed:
        out["stdout"] = printed if len(printed) <= MAX_STDOUT else printed[:MAX_STDOUT] + "\n...[stdout truncated]"
    if "result" in namespace:
        out["result"] = _to_jsonable(namespace["result"])
    out["changes"] = _diff(before, _snapshot())
    # a face of n corners makes n - 2 triangles: counted from the totals, without visiting each face
    counts = {o.name: len(o.data.loops) - 2 * len(o.data.polygons) for o in bpy.context.scene.objects if o.type == 'MESH'}
    out["scene_triangles"] = sum(counts.values())
    heavy = {name: count for name, count in counts.items() if count > 150_000 and before.get(name, (None, (0, 0)))[1] != (
        len(bpy.data.objects[name].data.vertices), len(bpy.data.objects[name].data.polygons))}
    if heavy:                 # said at once, to whoever made it: thinning a mesh afterwards wrecks its shape
        out["too_heavy"] = {name: f"{count:,} triangles" for name, count in sorted(heavy.items(), key=lambda item: -item[1])[:8]}
        out["too_heavy_note"] = ("A prop needs 10,000 to 50,000 triangles. Rebuild these lighter now (fewer subdivisions, fewer "
                                 "segments, fewer instances made real), instead of decimating them later.")
    if error:
        out["hint"] = "The scene may be partially changed; check `changes`, fix the cause, and re-run."
    return out
