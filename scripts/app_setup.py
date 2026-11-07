# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Runs INSIDE the app's Blender (with its window) to produce the start-up file and preferences.

Called by build_app.py:  "Unix\\blender.exe" --factory-startup --python scripts/app_setup.py -- <template dir>

Steps, one per timer tick so the UI can settle between them:
  1. duplicate the Layout workspace as "Zen" and strip it to a single clean 3D viewport;
  2. rename Layout to "Studio"; put Zen first; remove the default cube;
  3. save the template's startup.blend;
  4. enable the add-on, apply the Unix theme, save this template's preferences; quit.
"""
import os
import sys

import bpy

TEMPLATE_DIR = sys.argv[sys.argv.index("--") + 1]
state = {"step": 0}


def window():
    return bpy.context.window_manager.windows[0]


def view3d_of(screen):
    return max((a for a in screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)


def step():
    try:
        win = window()
        s = state["step"]
        state["step"] += 1
        if s == 0:
            with bpy.context.temp_override(window=win):
                bpy.context.window.workspace = bpy.data.workspaces["Layout"]
            return 0.5
        if s == 1:
            with bpy.context.temp_override(window=win):
                bpy.ops.workspace.duplicate()
            return 0.8
        if s == 2:
            ws = win.workspace
            ws.name = "Zen"
            bpy.data.workspaces["Layout"].name = "Studio"
            return 0.3
        if 3 <= s < 12:  # close every area except the main 3D viewport, one per tick
            screen = win.screen
            keep = view3d_of(screen)
            others = [a for a in screen.areas if a != keep]
            if others:
                with bpy.context.temp_override(window=win, screen=screen, area=others[0]):
                    bpy.ops.screen.area_close()
                state["step"] = s  # repeat this step until only the viewport is left
                state["guard"] = state.get("guard", 0) + 1
                if state["guard"] > 12:
                    state["step"] = 12
                return 0.3
            state["step"] = 12
            return 0.3
        if s == 12:
            area = view3d_of(win.screen)
            space = area.spaces.active
            space.show_region_toolbar = False
            space.show_region_ui = False
            space.show_region_tool_header = False
            space.shading.type = 'SOLID'
            space.overlay.show_floor = True
            with bpy.context.temp_override(window=win):
                bpy.ops.workspace.reorder_to_front()
            return 0.3
        if s == 13:
            cube = bpy.data.objects.get("Cube")
            if cube is not None:
                mesh = cube.data
                bpy.data.objects.remove(cube)
                bpy.data.meshes.remove(mesh)
            return 0.2
        if s == 14:
            os.makedirs(TEMPLATE_DIR, exist_ok=True)
            bpy.ops.wm.save_as_mainfile(filepath=os.path.join(TEMPLATE_DIR, "startup.blend"), copy=True, compress=True)
            print("SETUP startup.blend saved; workspaces:", [w.name for w in bpy.data.workspaces][:4])
            return 0.3
        if s == 15:
            import addon_utils
            addon_utils.enable("unix", default_set=True, persistent=True)
            import unix
            theme = os.path.join(os.path.dirname(unix.__file__), "theme", "Unix.xml")
            with bpy.context.temp_override(window=win):
                bpy.ops.script.execute_preset(filepath=theme, menu_idname="USERPREF_MT_interface_theme_presets")
            prefs = bpy.context.preferences
            prefs.view.show_splash = True
            prefs.view.show_developer_ui = False
            bpy.ops.wm.save_userpref()
            # With an app template active Blender takes the theme, add-ons and keymaps from the
            # template's own userpref.blend (factory defaults if there is none), so ship one.
            import shutil
            saved = os.path.join(bpy.utils.user_resource('CONFIG'), "userpref.blend")
            shutil.copyfile(saved, os.path.join(TEMPLATE_DIR, "userpref.blend"))
            print("SETUP preferences saved; template:", repr(prefs.app_template),
                  "addon:", "unix" in prefs.addons)
            return 0.3
        bpy.ops.wm.quit_blender()
    except Exception:
        import traceback
        traceback.print_exc()
        print("SETUP FAILED at step", state["step"] - 1)
        bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(step, first_interval=1.5)
