# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Unix: an AI 3D artist inside Blender, driven by your own Claude CLI.

Layout:
  bridge/   model <-> Blender transport (MCP server) and main-thread dispatch
  agent/    the Claude CLI runner and event stream
  tools/    what the model can do in Blender (inspect, script, look, check, files...)
  skills/   expert playbooks the model loads per stage of work
  prompts/  the system prompt
  ui/       chat panel, operators, preferences, viewport halo
  core.py   the engine that ties the above together
"""

bl_info = {
    "name": "Unix",
    "author": "Larpbase",
    "version": (0, 1, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar (N) > Unix",
    "description": "An AI 3D artist that builds, checks and fixes your scene through your own Claude CLI",
    "category": "3D View",
}


_redrawn = [0.0]


def _tick():
    """Persistent timer: pump tool calls, drain agent events, redraw the panel."""
    import time
    import bpy
    from .core import engine
    from .ui import window_pump
    try:
        # Tool calls run inside the window pump, which gives them a real window context: from here they
        # would need temp_override, and operators under it crashed Blender while other threads ran (see
        # ui/window_pump.py). This timer pumps only when no pump can run (background mode, no 3D viewport).
        if window_pump.covering():
            changed = window_pump.take_changed()
        else:
            changed = engine.tick()
        window_pump.ensure()
        if not bpy.app.background:
            from . import recovery
            from .ui import island
            changed = recovery.tick() or changed        # the offer after a crash, and the safety saves
            changed = island.tick() or changed
    except BaseException as exc:  # the timer must never die (a script's late ScriptTimeout is a BaseException)
        print(f"[Unix] tick error: {exc!r}")
        changed = True
    # A big scene's viewport takes tens of milliseconds to draw. While long work runs in slices, ten redraws a
    # second keep the island moving without spending the main thread drawing the same scene over and over;
    # while a render job runs, the viewport waits for the GPU it shares with the render, so even fewer.
    now = time.monotonic()
    pace = 0.25 if engine.dispatcher.waiting else (0.1 if engine.dispatcher.pending else 0.0)
    if (changed or engine.busy) and now - _redrawn[0] >= pace:
        _redrawn[0] = now
        wm = bpy.context.window_manager
        for window in (wm.windows if wm else []):
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.tag_redraw()
    # work waiting for the main thread (the next slice of a long tool) comes back almost at once: the window
    # still redraws and answers between two slices
    # (the window pump comes back for those itself)
    return 0.01 if engine.dispatcher.pending and not window_pump.covering() else (0.05 if engine.busy else 0.3)


def _start_roblox_link():
    """Open the local connection Roblox Studio's Unix plugin talks to, and keep that plugin installed."""
    import bpy
    from .bridge import roblox_link
    addon = bpy.context.preferences.addons.get(__name__)
    if addon is not None and not getattr(addon.preferences, "roblox_link", True):
        return
    try:
        roblox_link.settings["install"] = True
        roblox_link.link.start()
        state = roblox_link.install_plugin()
        if state in ("installed", "updated"):
            print(f"[Unix] Roblox Studio plugin {state}: restart Studio once to load it.")
    except Exception as exc:
        print(f"[Unix] Roblox link not started: {exc!r}")


def _no_ghost_window():
    """Windows swaps a window that has not answered for five seconds for a faded copy titled
    "(Not Responding)" and offers to close it. Blender renders, saves and compiles shaders on the
    thread that answers, so any of those lasting longer gets that label although the app is
    working, and closing it then loses the scene. This turns the swap off for this process: the
    window keeps its own title and picture while it is busy."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import ctypes
        user32 = ctypes.WinDLL("user32")            # private: no declaration leaks into ctypes.windll
        user32.DisableProcessWindowsGhosting.argtypes, user32.DisableProcessWindowsGhosting.restype = [], None
        user32.DisableProcessWindowsGhosting()
    except Exception as exc:
        print(f"[Unix] could not turn off window ghosting: {exc!r}")


def register():
    import bpy
    from . import ui
    from .core import engine
    from . import recovery
    from .ui import window_pump
    recovery.start()            # first: a crash from here on leaves a traceback of every thread
    ui.register()
    window_pump.register()
    engine.start()
    import os
    if os.environ.get("UNIX_THREAD_CHECK") == "1":     # debug sessions: report bpy used off the main thread
        from . import threadcheck
        threadcheck.install()
    if not bpy.app.background:
        _no_ghost_window()
        _start_roblox_link()
        from . import brand
        brand.start()
    if not bpy.app.timers.is_registered(_tick):
        bpy.app.timers.register(_tick, first_interval=0.3, persistent=True)


def unregister():
    import bpy
    from . import ui
    from .core import engine
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    from . import brand
    brand.stop()
    engine.shutdown()
    from .bridge import roblox_link
    roblox_link.link.stop()
    from .ui import window_pump
    window_pump.unregister()
    ui.unregister()
