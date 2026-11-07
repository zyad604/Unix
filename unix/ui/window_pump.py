# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Runs the engine's main-thread work from inside a window instead of from a bare timer.

Why this exists: Blender crashed in long sessions (blender_crash_20261016_1559: an access
violation in python311.dll, on one of Unix's socketserver threads, with the main thread idle).

A tool's operators need a window and a 3D viewport in the context. A ``bpy.app.timers``
callback has neither, so tools borrowed them with ``bpy.context.temp_override``. That puts a
Python dict on Blender's context, and from then on every context lookup an operator makes
(``CTX_data_scene`` and friends) asks Python. ``bpy.ops`` lets other threads run while the
operator runs (it releases the GIL), and Blender 4.5 decides whether that lookup must take the
GIL by asking whether *any* thread holds it, not whether this one does. So whenever one of
Unix's threads ran Python at that moment (the MCP server, a CLI reader, a helper agent's call),
the main thread used Python without the GIL, the two corrupted Python's memory together, and
the process died later in whichever thread next touched the damaged memory. Measured with one
busy Python thread: operators under ``temp_override`` crashed Blender within 100 calls (twice);
the same operators from a modal operator's own context ran 1,390 calls in two minutes, and from
a bare timer without an override 1,440, without a crash (tests/stress_threads.py).

A modal operator's events arrive with the window, area and region it was started in, and no
Python dict. Tool jobs pumped from its timer events therefore have a real context, and
``tools._util.ui_context`` uses it as it is. The add-on timer pumps only while no window pump is
alive: background mode, just after a file was loaded (that ends every modal operator) and while
a modal tool of Blender's (a transform the user is dragging) holds the events.
"""

from __future__ import annotations

import time

import bpy

# generation: the newest pump; an older one that is still installed ends at its next event. beat: when the
# pump last finished a tick. changed: the engine asked for a redraw since the add-on timer last looked.
# started: the last start worked (False: no 3D viewport to start in, or the start failed).
state = {"generation": 0, "beat": 0.0, "changed": False, "starting": 0.0, "started": False, "stop": False}
# A pump that has had no event for RESTART_S (a modal tool of Blender's took the events: a new modal handler
# goes in front of the older ones) is replaced by a new one, which gets the events first again. Until then
# the tool calls wait: pumping them from the bare timer would bring back the override.
ALIVE_S = 0.5
RESTART_S = 3.0


def alive() -> bool:
    """The pump ticked a moment ago."""
    return time.monotonic() - state["beat"] < ALIVE_S and not state["stop"]


def covering() -> bool:
    """A pump runs the engine (or one has just been started): the add-on timer must not."""
    now = time.monotonic()
    return (not state["stop"] and state["started"]
            and (now - state["beat"] < RESTART_S or now - state["starting"] < RESTART_S))


def take_changed() -> bool:
    changed, state["changed"] = state["changed"], False
    return changed


def _viewport():
    """The first 3D viewport of the first window, with its main region (where tools work)."""
    wm = bpy.context.window_manager
    for window in (wm.windows if wm else []):
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                region = next((r for r in area.regions if r.type == 'WINDOW'), None)
                if region is not None:
                    return window, area, region
    return None


def ensure() -> None:
    """Start a pump if none has pumped for a while. Called from the add-on timer; cheap when one runs."""
    now = time.monotonic()
    if bpy.app.background or state["stop"] or now - state["beat"] < RESTART_S or now - state["starting"] < RESTART_S:
        return
    found = _viewport()
    state.update(starting=now, started=False)
    if found is None:
        return                  # no 3D viewport open: the add-on timer pumps meanwhile
    window, area, region = found
    try:
        # The one operator called under an override here: it only installs the handler.
        with bpy.context.temp_override(window=window, screen=window.screen, area=area, region=region):
            state["started"] = 'RUNNING_MODAL' in bpy.ops.unix.window_pump('INVOKE_DEFAULT')
    except Exception as exc:
        print(f"[Unix] could not start the window pump: {exc!r}")


@bpy.app.handlers.persistent
def _on_load(_dummy=None) -> None:
    state.update(beat=0.0, starting=0.0, started=False)     # loading a file ended every modal operator: start one


def register() -> None:
    state.update(beat=0.0, starting=0.0, started=False, stop=False)
    bpy.utils.register_class(UNIX_OT_window_pump)
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)


def unregister() -> None:
    state.update(stop=True, beat=0.0)          # a running pump ends at its next event
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    bpy.utils.unregister_class(UNIX_OT_window_pump)


class UNIX_OT_window_pump(bpy.types.Operator):
    """Runs Unix's tool calls inside the 3D viewport's own context (internal)"""
    bl_idname = "unix.window_pump"
    bl_label = "Unix window pump"
    bl_options = {'INTERNAL'}

    _timer = None
    _fast = False
    _generation = 0

    def invoke(self, context, event):
        if state["stop"] or context.window is None or context.area is None:
            return {'CANCELLED'}
        state["generation"] += 1
        self._generation = state["generation"]
        self._fast = True
        self._timer = context.window_manager.event_timer_add(0.01, window=context.window)
        context.window_manager.modal_handler_add(self)
        state["beat"] = time.monotonic()
        return {'RUNNING_MODAL'}

    def _end(self, context):
        context.window_manager.event_timer_remove(self._timer)
        if self._generation == state["generation"]:
            state.update(beat=0.0, started=False)
        return {'CANCELLED'}

    def _retime(self, context, fast: bool) -> None:
        if fast == self._fast:
            return
        context.window_manager.event_timer_remove(self._timer)
        self._timer = context.window_manager.event_timer_add(0.01 if fast else 0.1, window=context.window)
        self._fast = fast

    def modal(self, context, event):
        if state["stop"] or self._generation != state["generation"]:
            return self._end(context)       # unregistered, or replaced by a newer pump
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        area = context.area
        if area is None or area.type != 'VIEW_3D' or context.region is None:
            return self._end(context)       # its viewport was closed or changed: the add-on timer starts another
        from ..core import engine
        try:
            if engine.tick():
                state["changed"] = True
        except BaseException as exc:  # the pump must never die on a tool's error (a late ScriptTimeout included)
            print(f"[Unix] tick error: {exc!r}")
            state["changed"] = True
        self._retime(context, bool(engine.dispatcher.pending or engine.busy))
        state["beat"] = time.monotonic()         # after the tick: a long tool must not make it look dead
        return {'PASS_THROUGH'}

