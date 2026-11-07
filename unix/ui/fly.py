# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Free camera, the way game editors move: hold the right mouse button and move the mouse to look
around, W A S D to fly, E and Q to go up and down, the wheel (while held) for speed, Shift to go
slow. A right click that does not move still opens Blender's context menu.

In Beginner, W A S D Q E fly without the right button too (in Object Mode, with the mouse over
the viewport), as in Roblox Studio; Advanced keeps those keys for Blender's own shortcuts (A selects
all, S scales) and flies only while the right button is held. F frames the selection in Beginner.

Run from the island's always-on event operator (``island.UNIX_OT_island_events``), which sees every
event in the window before Blender's keymaps do.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

import bpy
from mathutils import Quaternion, Vector

from ..tools import _util

KEYS = {'W': (0, 1, 0), 'S': (0, -1, 0), 'A': (-1, 0, 0), 'D': (1, 0, 0), 'E': (0, 0, 1), 'Q': (0, 0, -1),
        'UP_ARROW': (0, 1, 0), 'DOWN_ARROW': (0, -1, 0), 'LEFT_ARROW': (-1, 0, 0), 'RIGHT_ARROW': (1, 0, 0)}
LOOK = 0.0045           # radians a pixel
CLICK = 4               # pixels the mouse may move for a right click to still be a click
CONTEXT_MENUS = {'OBJECT': "VIEW3D_MT_object_context_menu", 'EDIT_MESH': "VIEW3D_MT_edit_mesh_context_menu",
                 'EDIT_CURVE': "VIEW3D_MT_edit_curve_context_menu", 'EDIT_ARMATURE': "VIEW3D_MT_armature_context_menu",
                 'POSE': "VIEW3D_MT_pose_context_menu", 'SCULPT': "VIEW3D_PT_sculpt_context_menu"}

state: Dict[str, Any] = {"look": False, "keys": set(), "area": None, "region": None, "start": (0, 0),
                         "travel": 0, "flew": False, "last": 0.0, "timer": None, "speed": 1.0, "hidden": False}


def enabled() -> bool:
    from .operators import preferences
    prefs = preferences(bpy.context)
    return bool(getattr(prefs, "free_camera", True))


def _beginner() -> bool:
    from . import simple
    return simple.level() == "beginner"


def _viewport_at(context, event):
    for area in context.window.screen.areas:
        if area.type != 'VIEW_3D':
            continue
        for region in area.regions:
            if region.type == 'WINDOW' and 0 <= event.mouse_x - region.x <= region.width \
                    and 0 <= event.mouse_y - region.y <= region.height:
                return area, region
    return None, None


def _rv3d(region) -> Optional[bpy.types.RegionView3D]:
    try:
        return region.data
    except ReferenceError:
        return None


def _eye(rv3d) -> Vector:
    return rv3d.view_location + rv3d.view_rotation @ Vector((0.0, 0.0, rv3d.view_distance))


def _free(rv3d) -> None:
    """Out of the camera's or an orthographic view: looking around one would move the camera or tilt a plan."""
    if rv3d.view_perspective != 'PERSP':
        rv3d.view_perspective = 'PERSP'


def _start_timer(context) -> None:
    if state["timer"] is None:
        state["timer"] = context.window_manager.event_timer_add(1 / 60, window=context.window)
        state["last"] = time.monotonic()


def _stop_timer(context) -> None:
    if state["timer"] is not None and not state["keys"]:
        context.window_manager.event_timer_remove(state["timer"])
        state["timer"] = None


def _end_look(context) -> None:
    state["look"] = False
    if state["hidden"]:
        context.window.cursor_modal_restore()
        state["hidden"] = False


def _turn(rv3d, dx: float, dy: float) -> None:
    eye = _eye(rv3d)
    turned = Quaternion((0.0, 0.0, 1.0), -dx * LOOK) @ rv3d.view_rotation
    pitched = Quaternion(turned @ Vector((1.0, 0.0, 0.0)), dy * LOOK) @ turned
    if abs((pitched @ Vector((0.0, 0.0, -1.0))).z) < 0.995:     # not over the top: the view would flip
        turned = pitched
    turned.normalize()
    rv3d.view_rotation = turned
    rv3d.view_location = eye - turned @ Vector((0.0, 0.0, rv3d.view_distance))


def _fly(context, event) -> None:
    now = time.monotonic()
    dt, state["last"] = min(now - state["last"], 0.1), now
    rv3d = _rv3d(state["region"]) if state["region"] is not None else None
    if rv3d is None or not state["keys"]:
        return
    move = Vector((0.0, 0.0, 0.0))
    for key in state["keys"]:
        move += Vector(KEYS[key])
    if move.length == 0.0:
        return
    rot = rv3d.view_rotation
    step = (rot @ Vector((1.0, 0.0, 0.0))) * move.x + (rot @ Vector((0.0, 0.0, -1.0))) * move.y \
        + Vector((0.0, 0.0, 1.0)) * move.z
    # as fast as the view is far from what it looks at: a room and a city both cross in a few seconds
    speed = min(max(rv3d.view_distance * 0.9, 1.5), 400.0) * state["speed"] * (0.25 if event.shift else 1.0)
    rv3d.view_location += step.normalized() * speed * dt
    state["flew"] = True
    try:
        state["area"].tag_redraw()
    except ReferenceError:
        pass


def _context_menu(context, area, region) -> None:
    menu = CONTEXT_MENUS.get(context.mode)
    if menu is None:
        return
    try:
        with _util.window_context(window=context.window, area=area, region=region):
            bpy.ops.wm.call_menu(name=menu)
    except RuntimeError:
        pass


def handle(context, event, over_island: bool, typing: bool) -> Optional[set]:
    """The modal result when the free camera takes this event, None to leave it to the island and Blender."""
    kind, value = event.type, event.value
    if kind == 'TIMER':
        if state["keys"]:
            _fly(context, event)
        return None                          # timers are everyone's: always passed on
    if not enabled() or context.window is None:
        return None
    if kind == 'WINDOW_DEACTIVATE':           # a key let go in another window is never seen: stop here
        state["keys"].clear()
        _stop_timer(context)
        if state["look"]:
            _end_look(context)
        return None

    if state["look"]:
        if kind in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            sx, sy = state["start"]
            dx, dy = event.mouse_x - sx, event.mouse_y - sy
            if dx or dy:
                state["travel"] += abs(dx) + abs(dy)
                if state["travel"] > CLICK:
                    if not state["hidden"]:
                        context.window.cursor_modal_set('NONE')
                        state["hidden"] = True
                    rv3d = _rv3d(state["region"])
                    if rv3d is not None:
                        _free(rv3d)
                        _turn(rv3d, dx, dy)
                        state["area"].tag_redraw()
                    context.window.cursor_warp(sx, sy)    # the mouse stays put: the view turns as far as wanted
            return {'RUNNING_MODAL'}
        if kind == 'RIGHTMOUSE' and value == 'RELEASE':
            clicked = state["travel"] <= CLICK and not state["flew"]
            area, region = state["area"], state["region"]
            _end_look(context)
            if clicked:
                _context_menu(context, area, region)
            return {'RUNNING_MODAL'}
        if kind in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and value == 'PRESS':
            state["speed"] = min(max(state["speed"] * (1.25 if kind == 'WHEELUPMOUSE' else 0.8), 0.05), 20.0)
            return {'RUNNING_MODAL'}
        if kind == 'ESC':
            _end_look(context)
            return {'RUNNING_MODAL'}

    if kind in KEYS:
        if value == 'RELEASE':
            if kind in state["keys"]:
                state["keys"].discard(kind)
                _stop_timer(context)
                return {'RUNNING_MODAL'}
            return None
        if value != 'PRESS' or typing or event.ctrl or event.alt or event.oskey:
            return None
        if not state["look"]:
            if event.shift or not _beginner() or context.mode != 'OBJECT' or over_island:
                return None
            if kind.endswith("_ARROW"):
                return None                  # arrows are the timeline's unless the right button is held
            area, region = _viewport_at(context, event)
            if region is None:
                return None
            state.update(area=area, region=region)
        rv3d = _rv3d(state["region"])
        if rv3d is None:
            return None
        _free(rv3d)
        state["keys"].add(kind)
        _start_timer(context)
        return {'RUNNING_MODAL'}

    if kind == 'RIGHTMOUSE' and value == 'PRESS' and not over_island and not event.ctrl and not event.alt:
        area, region = _viewport_at(context, event)
        if region is None or _rv3d(region) is None:
            return None
        state.update(look=True, area=area, region=region, start=(event.mouse_x, event.mouse_y), travel=0, flew=False)
        return {'RUNNING_MODAL'}

    if kind == 'F' and value == 'PRESS' and not typing and not over_island and _beginner() and context.mode == 'OBJECT' \
            and not (event.ctrl or event.alt or event.shift or event.oskey) and context.selected_objects:
        area, region = _viewport_at(context, event)
        if region is None:
            return None
        with _util.window_context(window=context.window, area=area, region=region):
            bpy.ops.view3d.view_selected(use_all_regions=False)
        return {'RUNNING_MODAL'}
    return None


def reset(context=None) -> None:
    """After a file is loaded: the regions held are gone."""
    if state["timer"] is not None and context is not None:
        try:
            context.window_manager.event_timer_remove(state["timer"])
        except (ValueError, ReferenceError):
            pass
    state.update(look=False, keys=set(), area=None, region=None, timer=None, hidden=False, flew=False)
