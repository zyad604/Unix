# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Smoke test for the standalone app: screenshots of start-up and, optionally, of one real turn.
Run through the launcher:
  "app/Unix/Unix.exe" --python tests/app_smoke.py -- <out_dir> ["prompt"] [--studio]
"""
import os
import sys
import time

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
studio = "--studio" in argv
argv = [a for a in argv if a != "--studio"]
OUT = argv[0] if argv else bpy.app.tempdir
PROMPT = argv[1] if len(argv) > 1 else ""
state = {"phase": 0, "t0": 0.0, "mid": False}


def shot(name):
    window = bpy.context.window_manager.windows[0]
    with bpy.context.temp_override(window=window):
        bpy.ops.screen.screenshot(filepath=os.path.join(OUT, name))


def drive():
    try:
        from unix.core import engine
        from unix.tools import assets
        window = bpy.context.window_manager.windows[0]
        phase = state["phase"]
        if phase == 0:
            prefs = bpy.context.preferences
            print("APP template:", repr(prefs.app_template), "| addon:", "unix" in prefs.addons,
                  "| workspaces:", [w.name for w in bpy.data.workspaces if w.name in ("Zen", "Studio")],
                  "| active:", window.workspace.name, "| config:", bpy.utils.user_resource('CONFIG'))
            from unix.ui import island
            zen = bpy.data.workspaces.get("Zen")
            areas = [a.type for a in zen.screens[0].areas] if zen else []
            space = next((a.spaces.active for a in zen.screens[0].areas if a.type == 'VIEW_3D'), None) if zen else None
            print("APP zen areas:", areas, "| toolbar/sidebar hidden:",
                  (not space.show_region_toolbar, not space.show_region_ui) if space else None,
                  "| studio areas:", sorted({a.type for a in bpy.data.workspaces["Studio"].screens[0].areas}))
            print("APP splash:", bpy.types.WM_MT_splash.__module__, "| set_mode op:", hasattr(bpy.ops.unix, "set_mode"),
                  "| island events running:", island.UNIX_OT_island_events.running, "| island visible:", island.settings["visible"],
                  "| theme accent:", tuple(round(c, 2) for c in bpy.context.preferences.themes[0].user_interface.wcol_regular.inner_sel[:3]),
                  "| objects:", [o.name for o in bpy.data.objects])
            shot("app_0_start.png")
            if studio:
                with bpy.context.temp_override(window=window):
                    bpy.ops.unix.set_mode(mode="Studio")
            state["phase"] = 1 if PROMPT else 9
            return 1.5
        if phase == 1:
            assets.settings["enabled"] = False  # no downloads in the smoke test
            bpy.context.window_manager.unix_prompt = PROMPT
            with bpy.context.temp_override(window=window):
                print("SEND", bpy.ops.unix.send())
            state["phase"], state["t0"] = 2, time.time()
            return 1.0
        if phase == 2:
            elapsed = time.time() - state["t0"]
            if engine.busy and not state["mid"] and elapsed > 10:
                shot("app_1_working.png")
                state["mid"] = True
            if engine.busy and elapsed < 420:
                return 0.5
            state["phase"] = 3
            return 1.5
        if phase == 3:
            shot("app_2_done.png")
            print("DONE", engine.messages[-1].get("meta"), "| objects:", [o.name for o in bpy.data.objects][:14])
            state["phase"] = 9
            return 0.5
        if phase == 9:
            if studio:
                shot("app_3_studio.png")
            bpy.ops.wm.quit_blender()
    except Exception:
        import traceback
        traceback.print_exc()
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(drive, first_interval=6.0)
