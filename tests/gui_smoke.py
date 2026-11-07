# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""GUI smoke test: opens Blender, sends one prompt through the real panel, screenshots, quits.
Run: blender --factory-startup --python tests/gui_smoke.py -- <out_dir> ["prompt"]"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import unix
from unix.core import engine

argv = [a for a in (sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []) if a not in ("--theme", "--online")]
from unix.tools import assets
assets.settings["enabled"] = "--online" in sys.argv  # no downloads unless asked for
OUT = argv[0] if argv else bpy.app.tempdir
PROMPT = argv[1] if len(argv) > 1 else "Add a low-poly pine tree about 3 m tall next to the cube, with a brown trunk and green foliage. Verify it."
state = {"phase": 0, "t0": 0.0, "mid": False}

def shot(name):
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.ops.screen.screenshot(filepath=os.path.join(OUT, name))

def drive():
    try:
        window = bpy.context.window_manager.windows[0]
        if state["phase"] == 0:
            unix.register()
            if "--theme" in sys.argv:
                with bpy.context.temp_override(window=window):
                    bpy.ops.unix.apply_theme('EXEC_DEFAULT')
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    area.spaces[0].show_region_ui = True
            state["phase"] = 1
            return 1.0
        if state["phase"] == 1:
            for area in window.screen.areas:
                if area.type == 'VIEW_3D':
                    for region in area.regions:
                        if region.type == 'UI':
                            try: region.active_panel_category = "Unix"
                            except Exception as e: print("CATEGORY", e)
            state["phase"] = 2
            return 1.0
        if state["phase"] == 2:
            shot("gui_0_empty.png")
            bpy.context.window_manager.unix_prompt = PROMPT
            with bpy.context.temp_override(window=window):
                print("SEND", bpy.ops.unix.send())
            state["phase"], state["t0"] = 3, time.time()
            return 1.0
        if state["phase"] == 3:
            elapsed = time.time() - state["t0"]
            if engine.busy and not state["mid"] and elapsed > 12:
                shot("gui_1_working.png"); state["mid"] = True
            if engine.busy and elapsed < 420:
                return 0.5
            for message in engine.messages:
                for part in message.get("parts", []):
                    if part["type"] == "steps": part["open"] = True
            state["phase"] = 4
            return 1.0
        if state["phase"] == 4:
            shot("gui_2_done.png")
            print("DONE", engine.messages[-1].get("meta"), "| objects:", [o.name for o in bpy.data.objects])
            print("FINAL", next((p["text"] for p in reversed(engine.messages[-1]["parts"]) if p["type"] != "steps"), "")[:700])
            state["phase"] = 5
            return 0.5
        unix.unregister()
        bpy.ops.wm.quit_blender()
    except Exception as exc:
        import traceback; traceback.print_exc()
        bpy.ops.wm.quit_blender()
    return None

bpy.app.timers.register(drive, first_interval=2.0)
