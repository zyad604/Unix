# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Beginner's interface and the free camera, driven with simulated mouse and keys in a real window.

Checks: Beginner's top bar menus and viewport toolbar draw; the chat closes to the bar at the bottom,
reopens from it, and can be typed into; holding the right mouse button and moving turns the view
without moving where it looks from; W flies forward, E up; a right click that does not move does not
turn the view. Screenshots go to <out_dir>. Data goes to a temp folder.
Run: blender --factory-startup --enable-event-simulate --python tests/test_ui_beginner.py -- <out_dir>"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from mathutils import Vector

from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_ui_")
import unix
from unix.ui import fly, island, simple

OUT = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else tempfile.gettempdir()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


def window():
    return bpy.context.window_manager.windows[0]


def viewport():
    area = max((a for a in window().screen.areas if a.type == 'VIEW_3D'), key=lambda a: a.width * a.height)
    return area, next(r for r in area.regions if r.type == 'WINDOW')


def shot(name):
    with bpy.context.temp_override(window=window()):
        bpy.ops.screen.screenshot(filepath=os.path.join(OUT, name))


def send(kind, value, x=None, y=None, **keys):
    w = window()
    if x is None:
        x, y = state["mouse"]
    state["mouse"] = (int(x), int(y))
    w.event_simulate(type=kind, value=value, x=int(x), y=int(y), **keys)


def part_at(name):
    """Window coordinates of the middle of an island control, from where it was last drawn."""
    _area, region = viewport()
    for part, x, y, w, h in island.hit_rects.get(region.as_pointer(), []):
        if part == name:
            return region.x + x + w / 2, region.y + y + h / 2
    return None


state = {"mouse": (0, 0)}


def steps():
    unix.register()
    yield 1.5
    island.settings["level"] = "beginner"          # as saved by a user who chose it
    simple.startup()
    yield 0.8
    kinds = {a.type for a in window().screen.areas}
    check("at start-up a saved Beginner hides Blender's panels (plain Blender: the viewport fills the window)",
          kinds == {'VIEW_3D'} and not window().screen.show_statusbar, kinds)
    simple.apply_level(window(), "beginner")
    island.settings.update(collapsed=False, dx=0.0, dy=0.0, width=0.0)
    area, region = viewport()
    space = area.spaces.active
    check("Beginner shows Unix's viewport toolbar, not Blender's tool header",
          space.show_region_header and not space.show_region_tool_header and not space.show_region_toolbar)
    check("the viewport toolbar is Unix's", bpy.types.VIEW3D_HT_header.draw is simple._draw_view_header)
    yield 1.0
    shot("ui_0_beginner_chat_open.png")

    # the chat closes to the bar and opens again
    hide = part_at("collapse")
    check("the open chat has a Hide button", hide is not None, island.hit_rects)
    if hide:
        send('LEFTMOUSE', 'PRESS', *hide)
        send('LEFTMOUSE', 'RELEASE')
    yield 0.6
    check("Hide closes the chat to the bar", island.settings["collapsed"])
    field = part_at("composer")
    check("the closed chat is a bar you can type into", field is not None)
    if field:
        send('LEFTMOUSE', 'PRESS', *field)
        send('LEFTMOUSE', 'RELEASE')
        yield 0.3
        for letter in "hi":
            send(letter.upper(), 'PRESS', unicode=letter)
            send(letter.upper(), 'RELEASE')
        yield 0.5
        check("typing in the bar fills the message", island.composer["text"] == "hi", island.composer)
        shot("ui_1_closed_to_bar.png")
        send('ESC', 'PRESS')
        send('ESC', 'RELEASE')
        yield 0.3
        check("Esc stops typing", not island.composer["focused"])
    island.composer["text"] = ""
    chat = part_at("collapse")
    if chat:
        send('LEFTMOUSE', 'PRESS', *chat)
        send('LEFTMOUSE', 'RELEASE')
    yield 0.6
    check("Chat on the bar opens the whole chat again", not island.settings["collapsed"])
    island.settings["collapsed"] = True          # out of the way of the camera checks
    yield 0.5

    # free camera
    area, region = viewport()
    rv3d = region.data
    rv3d.view_perspective = 'PERSP'
    cx, cy = region.x + region.width / 2, region.y + region.height * 0.7
    send('MOUSEMOVE', 'NOTHING', cx, cy)
    yield 0.2
    eye0, rot0 = fly._eye(rv3d).copy(), rv3d.view_rotation.copy()
    send('RIGHTMOUSE', 'PRESS', cx, cy)
    yield 0.1
    for step in range(1, 6):
        send('MOUSEMOVE', 'NOTHING', cx + 30, cy + 8)       # each move is warped back to where the button went down
        yield 0.05
    send('RIGHTMOUSE', 'RELEASE', cx, cy)
    yield 0.3
    eye1, rot1 = fly._eye(rv3d), rv3d.view_rotation
    check("holding the right button and moving turns the view", rot0.rotation_difference(rot1).angle > 0.3,
          (rot0, rot1))
    check("...from where it stands: the eye does not move", (eye1 - eye0).length < 1e-3, (eye0, eye1))
    check("...and the mouse is given back", not fly.state["look"] and not fly.state["hidden"])

    eye0 = fly._eye(rv3d).copy()
    forward = rv3d.view_rotation @ Vector((0.0, 0.0, -1.0))
    send('W', 'PRESS', cx, cy)
    yield 0.6
    send('W', 'RELEASE', cx, cy)
    yield 0.2
    moved = fly._eye(rv3d) - eye0
    check("W flies forward", moved.length > 0.5 and moved.normalized().dot(forward) > 0.95, (moved, forward))
    check("...and stops when it is let go", not fly.state["keys"] and fly.state["timer"] is None)

    eye0 = fly._eye(rv3d).copy()
    send('E', 'PRESS', cx, cy)
    yield 0.4
    send('E', 'RELEASE', cx, cy)
    yield 0.2
    moved = fly._eye(rv3d) - eye0
    check("E flies straight up", moved.z > 0.3 and abs(moved.x) + abs(moved.y) < 1e-3, moved)

    rot0 = rv3d.view_rotation.copy()
    send('RIGHTMOUSE', 'PRESS', cx, cy)
    send('RIGHTMOUSE', 'RELEASE', cx, cy)
    yield 0.6
    check("a right click that does not move leaves the view alone", rot0.rotation_difference(rv3d.view_rotation).angle < 1e-6)
    send('ESC', 'PRESS', cx, cy)                  # closes the context menu the click opened
    send('ESC', 'RELEASE', cx, cy)
    yield 0.4

    simple.apply_level(window(), "advanced")
    yield 0.4
    eye0 = fly._eye(rv3d).copy()
    send('W', 'PRESS', cx, cy)
    yield 0.4
    send('W', 'RELEASE', cx, cy)
    yield 0.2
    check("Advanced keeps W for Blender: it does not fly without the right button",
          (fly._eye(rv3d) - eye0).length < 1e-3)
    simple.apply_level(window(), "beginner")
    island.settings["collapsed"] = True
    yield 1.0
    shot("ui_2_beginner_closed.png")


def drive(gen=steps()):
    try:
        return next(gen)
    except StopIteration:
        pass
    except Exception:
        import traceback
        traceback.print_exc()
        fails.append("crashed")
    print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
    unix.unregister()
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(drive, first_interval=2.0)
