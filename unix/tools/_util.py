# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Shared helpers for bpy tools (context override, lookups, rounding)."""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
from typing import Iterable, List, Optional

import bpy

from .registry import ToolError, registry as _registry


_base_dir: Optional[str] = None
# What a long-running tool is doing right now ("Simulating fire  42%"), shown on the island.
status = {"text": ""}
_epoch = [0]


def data_epoch() -> int:
    """Goes up whenever Blender replaces its data: an undo or redo, a file opened. Python references
    to objects, scenes and settings taken before then may point at freed memory, and touching them
    can crash Blender. A job that runs in slices (the window is answering the user in between)
    compares this before it uses what it held."""
    return _epoch[0]


@bpy.app.handlers.persistent
def _data_replaced(*_args) -> None:
    _epoch[0] += 1


def _watch_data() -> None:
    handlers = bpy.app.handlers
    for name in ("undo_pre", "undo_post", "redo_pre", "redo_post", "load_pre", "load_post"):
        chain = getattr(handlers, name, None)
        if chain is None:
            continue
        for old in [h for h in chain if getattr(h, "__name__", "") == "_data_replaced" and h is not _data_replaced]:
            chain.remove(old)           # left by an earlier load of this module (the add-on was reloaded)
        if _data_replaced not in chain:
            chain.append(_data_replaced)


_watch_data()
_registry.data_epoch = data_epoch


def data_dir(*parts: str) -> str:
    """Per-user working directory for renders, snapshots, chats and assets.

    The base path is resolved once (on the main thread, at engine start) and cached, so
    worker threads can call this without touching ``bpy``.
    """
    global _base_dir
    if _base_dir is None:
        base = os.path.join(tempfile.gettempdir(), "unix")
        try:
            base = bpy.utils.user_resource('CONFIG', path="unix", create=True) or base
        except Exception:
            pass
        _base_dir = base
    path = os.path.join(_base_dir, *parts)
    os.makedirs(path, exist_ok=True)
    return path


def write_kept(path: str, record) -> None:
    """Write JSON so a crash of the whole computer cannot destroy it: the new file is forced onto the disk
    before it takes the old one's place, and the last good one stays next to it as ``.bak``. Without the
    forcing, a PC that died seconds after a save left a chat file of zeros (24 Oct, 02:23)."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    if os.path.isfile(path):
        os.replace(path, path + ".bak")
    os.replace(temporary, path)


def read_kept(path: str):
    """JSON written by ``write_kept``: the file, or the last good one when the file is damaged or missing."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        with open(path + ".bak", encoding="utf-8") as fh:
            return json.load(fh)


# Operators run under bpy.context.temp_override while another Python thread is busy crashed Blender
# (ui/window_pump.py says why). Counted so the stability test can see that tool calls no longer need one.
overrides = {"count": 0}


def _in_viewport() -> bool:
    """Is the current context already a 3D viewport's main region in a window (the window pump, a
    button on the island)? Then operators have what they need and no override is wanted."""
    ctx = bpy.context
    area, region = ctx.area, ctx.region
    return (ctx.window is not None and area is not None and area.type == 'VIEW_3D'
            and region is not None and region.type == 'WINDOW')


@contextlib.contextmanager
def ui_context():
    """Operators need a window and a 3D viewport. Tool calls get them from the window pump; work
    started from a bare timer (or before the pump is up) borrows the first 3D viewport. Nothing in
    background mode."""
    if _in_viewport():
        yield
        return
    wm = bpy.context.window_manager
    window = bpy.context.window or (wm.windows[0] if wm and wm.windows else None)
    if window is None:
        yield
        return
    overrides["count"] += 1
    area = next((a for a in window.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        with bpy.context.temp_override(window=window):
            yield
        return
    region = next((r for r in area.regions if r.type == 'WINDOW'), None)
    with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
        yield


def window_context(receiver=None, **members):
    """``bpy.context.temp_override(**members)``, or nothing when the context already holds exactly
    those members: the usual case inside the window pump, where the override would only bring back
    the crash it exists to avoid. Model scripts' own ``temp_override`` calls are routed here by the
    script guard. Members other than the window, screen, area and region (``selected_objects=``)
    can only be had through a real override."""
    ctx = bpy.context
    current = {"window": ctx.window, "screen": ctx.screen, "area": ctx.area, "region": ctx.region}
    if members and all(key in current and value == current[key] for key, value in members.items()):
        return contextlib.nullcontext()
    overrides["count"] += 1
    return (receiver if receiver is not None else ctx).temp_override(**members)


def r(value: float, digits: int = 4) -> float:
    value = round(float(value), digits)
    return 0.0 if value == 0 else value


def vec(values: Iterable[float], digits: int = 4) -> List[float]:
    return [r(v, digits) for v in values]


def degrees(values: Iterable[float]) -> List[float]:
    return [r(math.degrees(v), 2) for v in values]


def get_object(name: str) -> bpy.types.Object:
    obj = bpy.data.objects.get(name)
    if obj is None:
        close = [o.name for o in bpy.data.objects if name.lower() in o.name.lower()][:8]
        hint = f" Did you mean: {', '.join(close)}?" if close else " Call scene_info to list objects."
        raise ToolError(f"No object named '{name}'.{hint}")
    return obj


def resolve_objects(names: Optional[List[str]], types: Optional[set] = None,
                    default_visible: bool = True) -> List[bpy.types.Object]:
    """Named objects, or (when names is empty) every object in the scene of ``types``."""
    if names:
        objs = [get_object(n) for n in names]
    else:
        objs = [o for o in bpy.context.scene.objects if not default_visible or not o.hide_render]
    if types:
        objs = [o for o in objs if o.type in types]
    return objs


def ensure_object_mode() -> None:
    obj = bpy.context.view_layer.objects.active
    if obj is not None and obj.mode != 'OBJECT':
        try:
            with ui_context():
                bpy.ops.object.mode_set(mode='OBJECT')
        except Exception:
            pass


def eevee_engine_id() -> str:
    """'BLENDER_EEVEE_NEXT' on 4.2-4.5, 'BLENDER_EEVEE' before and after."""
    items = {i.identifier for i in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
    for ident in ('BLENDER_EEVEE_NEXT', 'BLENDER_EEVEE'):
        if ident in items:
            return ident
    return 'BLENDER_EEVEE_NEXT' if (4, 2) <= bpy.app.version[:2] < (5, 0) else 'BLENDER_EEVEE'
