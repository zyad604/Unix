# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Visual smoke test of the Unix island: screenshots of its states and one real turn.
Run: blender --factory-startup <file.blend> --python tests/island_smoke.py -- <out_dir> ["prompt"]
(Opening a .blend avoids the splash covering the viewport.)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import unix
from unix.core import engine

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[0] if argv else bpy.app.tempdir
PROMPT = argv[1] if len(argv) > 1 else "A wooden treasure chest with metal bands, 50 cm wide"
state = {"phase": 0, "t0": 0.0, "mid": False}


def shot(name):
    window = bpy.context.window_manager.windows[0]
    for area in window.screen.areas:
        area.tag_redraw()
    with bpy.context.temp_override(window=window):
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
        bpy.ops.screen.screenshot(filepath=os.path.join(OUT, name))


def drive():
    try:
        from unix.tools import assets
        from unix.ui import island
        window = bpy.context.window_manager.windows[0]
        phase = state["phase"]
        state["phase"] += 1
        if phase == 0:
            unix.register()
            with bpy.context.temp_override(window=window):
                bpy.ops.unix.apply_theme('EXEC_DEFAULT')
            assets.settings["enabled"] = False
            return 1.5
        if phase == 1:
            print("ISLAND events running:", island.UNIX_OT_island_events.running)
            shot("island_0_empty.png")
            island.settings["mode"] = "model"
            island.composer.update(text=PROMPT, focused=True)
            bpy.context.window_manager.unix_effort = "low"
            return 0.8
        if phase == 2:
            shot("island_1_typing.png")
            with bpy.context.temp_override(window=window):
                island.submit(bpy.context)
            print("SENT busy:", engine.busy, "| notice:", island.settings["notice"])
            state["t0"] = time.time()
            return 1.0
        if phase == 3:
            elapsed = time.time() - state["t0"]
            if engine.busy and not state["mid"] and elapsed > 14:
                shot("island_2_working.png")
                state["mid"] = True
            if engine.busy and elapsed < 420:
                state["phase"] = 3
                return 0.5
            return 1.5
        if phase == 4:
            shot("island_3_done.png")
            print("DONE", engine.messages[-1].get("meta"), "| mode:", engine.messages[-2].get("mode"),
                  "| objects:", [o.name for o in bpy.data.objects][:12])
            return 0.3
        unix.unregister()
        bpy.ops.wm.quit_blender()
    except Exception:
        import traceback
        traceback.print_exc()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(drive, first_interval=2.5)
