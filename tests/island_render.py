# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Render the Unix island off-screen to PNG files, with no window needed.

Draws the real island code into a GPU off-screen buffer for several states (empty,
typing, working, finished, question) so the layout can be checked in any session.
Run: blender --factory-startup --python tests/island_render.py -- <out_dir>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import tempfile
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_island_")   # the layout it saves must not land in the app's own
import gpu
import numpy as np
from mathutils import Matrix

import unix
from unix.core import engine
from unix.tools import interact
from unix.ui import island

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else bpy.app.tempdir
W, H = 1100, 640


class FakeRegion:
    width, height, x, y, type = W, H, 0, 0, 'WINDOW'

    def as_pointer(self):
        return 1


class FakeArea:
    regions = [FakeRegion()]


def render(name):
    offscreen = gpu.types.GPUOffScreen(W, H)
    with offscreen.bind():
        fb = gpu.state.active_framebuffer_get()
        fb.clear(color=(0.055, 0.053, 0.066, 1.0))
        with gpu.matrix.push_pop():
            gpu.matrix.load_matrix(Matrix.Identity(4))
            gpu.matrix.load_projection_matrix(Matrix(((2 / W, 0, 0, -1), (0, 2 / H, 0, -1), (0, 0, -1, 0), (0, 0, 0, 1))))
            gpu.state.blend_set('ALPHA')
            island._draw_island(FakeRegion(), FakeArea())
            gpu.state.blend_set('NONE')
        buffer = fb.read_color(0, 0, W, H, 4, 0, 'FLOAT')
    offscreen.free()
    buffer.dimensions = W * H * 4
    pixels = np.array(buffer.to_list(), dtype=np.float32)
    pixels[3::4] = 1.0
    rgb = pixels.reshape(-1, 4)
    rgb[:, :3] = np.power(np.clip(rgb[:, :3], 0, 1), 1 / 2.2)  # linear buffer -> display
    image = bpy.data.images.new("island", W, H, alpha=False)
    image.pixels.foreach_set(pixels)
    path = os.path.join(OUT, name)
    image.filepath_raw, image.file_format = path, 'PNG'
    image.save()
    bpy.data.images.remove(image)
    names = [r[0] for r in island.hit_rects.get(1, [])]
    print("RENDERED", name, "| controls:", ", ".join(n for n in names if n != "card"))


def main():
    try:
        unix.register()
        island.save_layout = lambda: None  # keep the test from writing the user's layout file
        island.settings.update(scale=1.0, dx=0.0, dy=0.0, width=0.0, view_h=300.0, collapsed=False, auto=False)
        wm = bpy.context.window_manager
        island._cli.update(checked=time.monotonic() + 1e9, claude=True, codex=True)

        render("state_0_empty.png")

        island.settings["mode"] = "model"
        island.composer.update(text="A wooden treasure chest with metal bands, 50 cm wide", focused=True)
        wm.unix_effort = "high"
        render("state_1_typing.png")

        step = lambda label, detail, acted, status, result="": {"name": "x", "label": label, "detail": detail, "acted": acted,  # noqa: E731
                                                                "status": status, "summary": "", "result": result, "images": []}
        island.composer.update(text="", focused=False)
        engine.messages = [
            {"role": "user", "text": "A wooden treasure chest with metal bands, 50 cm wide", "attachments": [], "snapshot": "", "mode": "model"},
            {"role": "assistant", "meta": "", "parts": [{"type": "steps", "open": False, "steps": [
                step("Read the scene", "", False, "ok"), step("Loaded skills", "blockout-proportions, hard-surface-bevel", False, "ok"),
                step("Ran a script", "Block out body and lid", True, "ok", "3 created"),
                step("Looked at the scene", "Chest_Root", False, "running")]}]},
        ]
        engine.run, engine.status, engine.started_at = object(), "working", time.monotonic() - 14
        interact._plan[:] = [{"title": "Block out", "status": "done"}, {"title": "Detail", "status": "in_progress"}, {"title": "Verify", "status": "pending"}]
        render("state_2_working.png")

        from unix.tools import team
        team.state["workers"] = [
            {"name": "House facades", "status": "working", "started": time.monotonic() - 41, "steps": 6, "doing": "Ran a script", "colour": team.COLOURS[0]},
            {"name": "House site", "status": "working", "started": time.monotonic() - 41, "steps": 4, "doing": "Looked at the scene", "colour": team.COLOURS[1]},
            {"name": "Sun sky", "status": "done", "started": time.monotonic() - 41, "steps": 3, "doing": "", "colour": team.COLOURS[2]}]
        render("state_2b_team.png")
        team.state["workers"] = []

        engine.run, engine.status = None, "idle"
        interact._plan[:] = []
        engine.messages[1] = {"role": "assistant", "meta": "27s · $0.065 · Ctrl+Z undoes this turn", "parts": [
            {"type": "steps", "open": False, "steps": [step("Ran a script", "", True, "ok")] * 6},
            {"type": "text", "text": "I built the treasure chest: 50 × 30 × 37 cm, a wooden box with a curved lid and four dark metal "
                                     "bands, plus a small lock on the front.\n\n- Everything is parented to Chest_Root in its own collection.\n"
                                     "- I checked it in two rendered views; the bands sit flush and the lid meets the body with no gap.\n"
                                     "- The mesh check is clean on all nine parts."}]}
        island.settings["auto"] = True
        wm.unix_model = "claude-opus-5-5"
        render("state_3_done.png")
        island.settings["collapsed"] = True          # the chat closed to the bar, the last reply showing above it
        render("state_3b_closed_bar.png")
        island.settings["collapsed"] = False

        engine.messages.append({"role": "user", "text": "now make three of them", "attachments": [], "snapshot": "", "mode": "agent"})
        engine.messages.append({"role": "assistant", "meta": "", "parts": [{"type": "error", "text": "The Claude CLI is not signed in on this computer. Run `claude auth login` once in a terminal."}]})
        interact._question.update(text="Should the three chests be identical or different sizes?", options=["Identical", "Small, medium, large"], event=object())
        engine.status = "waiting"
        render("state_4_question.png")
        interact._question.update(text="", options=[], event=None)

        # moved, widened and scaled up
        island.settings.update(scale=1.3, dx=160.0, dy=40.0, width=700.0, view_h=180.0)
        island._wrap_cache.clear()
        render("state_5_moved_scaled.png")

        # drag logic, driven with fake events
        from types import SimpleNamespace as NS
        ctx = NS(window=NS(screen=NS(areas=[])), preferences=bpy.context.preferences, workspace=None)
        island.settings.update(scale=1.0, dx=0.0, dy=0.0, width=0.0, view_h=300.0)
        op = NS(_drag={"kind": "move", "mx": 100, "my": 100, "dx": 0.0, "dy": 0.0, "width": 580.0, "view_h": 300.0})
        island.UNIX_OT_island_events._dragging(op, ctx, NS(type='MOUSEMOVE', value='NOTHING', mouse_x=160, mouse_y=70))
        print("DRAG move ->", island.settings["dx"], island.settings["dy"])
        island.UNIX_OT_island_events._dragging(op, ctx, NS(type='LEFTMOUSE', value='RELEASE', mouse_x=160, mouse_y=70))
        print("DRAG released:", op._drag is None)
        ui = bpy.context.preferences.system.ui_scale
        op = NS(_drag={"kind": "resize", "mx": 0, "my": 0, "dx": 0.0, "dy": 0.0, "width": 580.0, "view_h": 300.0})
        island.UNIX_OT_island_events._dragging(op, ctx, NS(type='MOUSEMOVE', value='NOTHING', mouse_x=int(120 * ui), mouse_y=int(-50 * ui)))
        print("DRAG resize ->", round(island.settings["width"]), round(island.settings["view_h"]))
        island.UNIX_OT_island_events._dragging(op, ctx, NS(type='ESC', value='PRESS', mouse_x=0, mouse_y=0))
        print("DRAG cancelled ->", round(island.settings["width"]), round(island.settings["view_h"]))
        island.set_scale(5.0); big = island.settings["scale"]; island.set_scale(0.1)
        print("SCALE clamps:", big, island.settings["scale"])

        # the Engine tab with detected targets, at the default size
        island.settings.update(scale=1.0, dx=0.0, dy=0.0, width=0.0, view_h=300.0, mode="engine", auto=False, notice="")
        island._wrap_cache.clear()
        engine.messages, engine.status = [], "idle"
        wm.unix_model, wm.unix_effort = "claude-sonnet-5-5", "medium"
        island._targets["list"] = [
            {"engine": "unreal", "label": "Unreal", "project": "x", "name": "MyProject2", "running": True},
            {"engine": "godot", "label": "Godot", "project": "x", "name": "new-game-project", "running": False},
            {"engine": "roblox", "label": "Roblox Studio", "project": "", "name": "open place", "running": True}]
        render("state_6_engine.png")
        island.settings["mode"] = "vfx"
        render("state_7_vfx.png")
        unix.unregister()
    except Exception:
        import traceback
        traceback.print_exc()
    bpy.ops.wm.quit_blender()
    return None


# GPU drawing needs a window session (it works while the window is minimized), not -b.
bpy.app.timers.register(main, first_interval=2.0)
