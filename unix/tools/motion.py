# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Seeing and measuring animation.

A single rendered frame cannot show what is wrong with a move: a walk whose feet sink into the
floor, a sidestep where one foot passes through the other, a loop that hitches, a run that never
leaves the ground. An agent that only looks at single poses ships all of that.

``look_motion`` renders a move as a strip of frames with one fixed framing and a floor, so the
agent reads it left to right the way an animator reads a contact sheet. ``check_motion``
measures the same move: contact with the ground, sliding, parts passing through each other,
limbs out of reach of their IK targets, pops, the loop seam, and bones that never move. Both
work for any animated thing: a character, a creature, a machine, a single object.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

from ._util import data_dir, ensure_object_mode, get_object, r
from .registry import ToolError, ToolOutput, tool
from .vision import _RENDERABLE, _VIEWS, look

MAX_FRAMES = 400
_OPPOSITE = {"front": "back", "back": "front", "left": "right", "right": "left", "top": "bottom", "bottom": "top",
             "persp": "persp_back", "persp_back": "persp"}
_DIGITS = {"0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111",
           "4": "101101111001001", "5": "111100111001111", "6": "111100111101111", "7": "111001001001001",
           "8": "111101111101111", "9": "111101111001111"}


# ---------------------------------------------------------------------------------------------
# actions


def action_named(name: str) -> bpy.types.Action:
    action = bpy.data.actions.get(name)
    if action is None:
        known = [a.name for a in bpy.data.actions][:40]
        raise ToolError(f"No action named '{name}'. Actions: {', '.join(known) or 'none'}")
    return action


def assign(obj: bpy.types.Object, action: Optional[bpy.types.Action]) -> None:
    """Put an action on an object, with the slot Blender 4.4+ needs for it to have any effect."""
    data = obj.animation_data or obj.animation_data_create()
    data.action = action
    if action is not None and hasattr(data, "action_slot") and data.action_slot is None:
        fitting = [s for s in action.slots if s.target_id_type in {'OBJECT', 'UNSPECIFIED'}]
        if fitting:
            data.action_slot = fitting[0]


class Showing:
    """For the length of a tool call: show `action` on `obj`. Afterwards the object has the action
    it had before and the scene the frames are played in (the user's, unless another is given) is back on
    the frame it was on. Only that scene is put back: setting the frame of a world of 3,500 objects took
    0.4 s, and sampling creatures in a scene of their own did it 737 times, five of a send's minutes."""

    def __init__(self, obj: bpy.types.Object, action: Optional[bpy.types.Action],
                 scene: Optional[bpy.types.Scene] = None) -> None:
        self.obj, self.action, self.scene = obj, action, scene

    def __enter__(self) -> "Showing":
        scene = self.scene or bpy.context.scene
        self.frame = (scene.frame_current, scene.frame_subframe)
        data = self.obj.animation_data
        self.before = (data.action, getattr(data, "action_slot", None)) if data else None
        if self.action is not None:
            assign(self.obj, self.action)
        return self

    def __exit__(self, *exc) -> bool:
        if self.action is not None:
            data = self.obj.animation_data
            if self.before is None:
                data.action = None
            else:
                data.action = self.before[0]
                if self.before[1] is not None and hasattr(data, "action_slot"):
                    try:
                        data.action_slot = self.before[1]
                    except Exception:
                        pass
        (self.scene or bpy.context.scene).frame_set(self.frame[0], subframe=self.frame[1])
        return False


def frame_span(obj: bpy.types.Object, action: Optional[bpy.types.Action], start: Optional[int],
               end: Optional[int]) -> Tuple[int, int, bool]:
    """First frame, last frame, and whether the move is marked as a loop."""
    scene = bpy.context.scene
    action = action or (obj.animation_data.action if obj.animation_data else None)
    first, last, cyclic = scene.frame_start, scene.frame_end, False
    if action is not None:
        low, high = action.frame_range
        first, last = int(round(low)), int(round(high))
        cyclic = bool(getattr(action, "use_cyclic", False))
    if start is not None:
        first = int(start)
    if end is not None:
        last = int(end)
    if last < first:
        raise ToolError(f"The frame range is empty ({first} to {last}).")
    return first, last, cyclic


def rig_of(obj: bpy.types.Object) -> Optional[bpy.types.Object]:
    if obj.type == 'ARMATURE':
        return obj
    for mod in obj.modifiers if obj.type == 'MESH' else []:
        if mod.type == 'ARMATURE' and mod.object is not None:
            return mod.object
    return obj.parent if obj.parent is not None and obj.parent.type == 'ARMATURE' else None


def bound_meshes(rig: bpy.types.Object) -> List[bpy.types.Object]:
    return [o for o in bpy.context.scene.objects if o.type == 'MESH'
            and any(m.type == 'ARMATURE' and m.object == rig for m in o.modifiers)]


def _paths(action: bpy.types.Action) -> List[str]:
    """What an action animates (a sample of its channels' data paths)."""
    paths: List[str] = []
    try:                                                 # layered actions (4.4+)
        for layer in action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    paths += [fc.data_path for fc in bag.fcurves[:40]]
    except AttributeError:
        try:
            paths = [fc.data_path for fc in action.fcurves[:40]]
        except Exception:
            paths = []
    return paths


def _made_for_others(action: bpy.types.Action, obj: bpy.types.Object) -> bool:
    """An action keyed on another object names it in its slots ("OB" + its name). Thirteen creatures with
    the same bone names each took every creature's 130 actions: 1,630 to sample instead of 130, and every
    creature in the engine carried all the others' moves. One whose slots name no other object still goes."""
    owners = [slot.identifier for slot in getattr(action, "slots", ())]
    return bool(owners) and all(owner.startswith("OB") and owner[2:] != obj.name and owner[2:] in bpy.data.objects
                                for owner in owners)


def actions_for(obj: bpy.types.Object) -> List[str]:
    """Names of the actions that could play on this object (they animate its bones, or it uses them)."""
    current = obj.animation_data.action if obj.animation_data else None
    bones = set(obj.pose.bones.keys()) if obj.type == 'ARMATURE' else set()
    out = []
    for action in bpy.data.actions:
        if action == current:
            out.append(action.name)
            continue
        if _made_for_others(action, obj):
            continue
        keyed = {p.split('"')[1] for p in _paths(action) if p.startswith('pose.bones["')}
        if bones and keyed and len(keyed & bones) >= 0.6 * len(keyed):      # made for this skeleton, not another
            out.append(action.name)
    return out


def body_actions() -> Dict[str, Tuple[float, float]]:
    """Every action that moves a skeleton, with the frames it covers. The engine compares this before
    and after a turn to know whether the turn made or changed character animation."""
    return {a.name: (round(a.frame_range[0], 2), round(a.frame_range[1], 2)) for a in bpy.data.actions
            if any(p.startswith("pose.bones[") for p in _paths(a))}


# ---------------------------------------------------------------------------------------------
# look_motion


def _stamp(tile, text: str) -> None:
    """Write a frame number into the top-left corner of a tile (rows run top to bottom)."""
    scale, x0, y0 = 3, 6, 6
    width = len(text) * 4 * scale + scale
    tile[y0 - 3:y0 + 5 * scale + 3, x0 - 3:x0 + width, :3] = 0.05
    for index, char in enumerate(text):
        bits = _DIGITS.get(char)
        if bits is None:
            continue
        for row in range(5):
            for col in range(3):
                if bits[row * 3 + col] == "1":
                    y, x = y0 + row * scale, x0 + (index * 4 + col) * scale
                    tile[y:y + scale, x:x + scale, :3] = (1.0, 0.92, 0.3)


def _facing_view(owner: bpy.types.Object) -> str:
    """The view that looks a rig in the face ('front' for a body facing -Y, 'back' for one facing +Y...)."""
    if owner.type != 'ARMATURE':
        return "front"
    from . import _mocap
    bones = owner.data.bones
    found = _mocap.roles([b.name for b in bones], {b.name: (b.parent.name if b.parent else None) for b in bones},
                         {b.name: b.use_deform for b in bones})
    if "thigh.L" not in found or "thigh.R" not in found:
        return "front"
    side = owner.matrix_world @ bones[found["thigh.L"]].head_local - owner.matrix_world @ bones[found["thigh.R"]].head_local
    ahead = Vector((side.y, -side.x, 0.0))
    if ahead.length < 1e-9:
        return "front"
    return max(("front", "back", "left", "right"), key=lambda v: _VIEWS[v][0].dot(ahead.normalized()))


def _row_label(owner: bpy.types.Object, view: str) -> str:
    if view in {"top", "bottom"}:
        return f"{view} view (from {'above' if view == 'top' else 'below'})"
    if owner.type != 'ARMATURE':
        return f"{view} view"
    face = _facing_view(owner)
    if view == face:
        return f"{view} view (the character faces the camera)"
    if view == _OPPOSITE.get(face):
        return f"{view} view (its back to the camera)"
    return f"{view} view (side on)"


def _world_box(objects: List[bpy.types.Object]) -> Tuple[Vector, Vector]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = []
    for obj in objects:
        posed = obj.evaluated_get(depsgraph)
        if posed.type == 'ARMATURE':
            points += [posed.matrix_world @ p for b in posed.pose.bones for p in (b.head, b.tail)]
        else:
            points += [posed.matrix_world @ Vector(corner) for corner in posed.bound_box]
    if not points:
        return Vector((0.0, 0.0, 0.0)), Vector((0.0, 0.0, 0.0))
    return (Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points))),
            Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points))))


@tool(
    "look_motion",
    """SEE an animation: a strip of frames in one picture, read left to right, with the same
    framing in every tile and the floor drawn in, so movement, weight and contact are visible.
    A single `look` shows one pose and hides everything that is wrong with a move. Use it after
    keying or retargeting any motion and before you report, for every action you made. Each row
    is one view (default: from the side and from the front); each tile carries its frame number.
    Judge it the way an animator reads a contact sheet: does the body lean and reach the way a
    real one would, do the feet meet the floor and stay on it, do the arms clear the body, does
    anything pass through anything, does the last tile lead back into the first for a loop.
    Works for a rigged character, a creature, a machine or a single moving object. `action` shows
    a stored action without changing which one the object is using.""",
    {
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "What moves: the armature (its meshes come along), or objects."},
        "action": {"type": "string", "description": "Action to show (default: the one in use)."},
        "count": {"type": "integer", "description": "Frames in the strip, evenly spaced (default 8, max 12)."},
        "frames": {"type": "array", "items": {"type": "integer"}, "description": "Exact frames to show instead of an even spread."},
        "start": {"type": "integer", "description": "First frame (default: the action's first)."},
        "end": {"type": "integer", "description": "Last frame (default: the action's last)."},
        "views": {"type": "array", "items": {"type": "string", "enum": sorted(_VIEWS)},
                  "description": "One or two views, one row each (default ['right', 'front'])."},
        "shading": {"type": "string", "enum": ["solid", "rendered"], "description": "Default 'solid'."},
        "show_bones": {"type": "boolean", "description": "Draw the skeleton inside a see-through mesh (default off)."},
        "ground": {"type": "number", "description": "Height of the floor to draw (default 0). Pass the height the feet stand on."},
        "size": {"type": "integer", "description": "Pixels per tile (default 280, max 400)."},
    },
    ["targets"],
    timeout=900.0,
    label="Watched the animation",
)
def look_motion(targets: List[str], action: str = "", count: int = 8, frames: Optional[List[int]] = None,
                start: Optional[int] = None, end: Optional[int] = None, views: Optional[List[str]] = None,
                shading: str = "solid", show_bones: bool = False, ground: float = 0.0, size: int = 280) -> ToolOutput:
    import numpy as np
    from .reference import _load_pixels

    ensure_object_mode()
    scene = bpy.context.scene
    named = [get_object(name) for name in targets]
    if not named:
        raise ToolError("Name what moves.")
    owner = next((rig_of(o) for o in named if rig_of(o) is not None), named[0])
    shown: List[bpy.types.Object] = []
    for obj in named:
        family = [obj] + list(obj.children_recursive) + (bound_meshes(obj) if obj.type == 'ARMATURE' else [])
        shown += [o for o in family if o.type in _RENDERABLE and o.name in scene.objects and o not in shown]
    bare = not shown                                    # a skeleton with nothing bound to it: draw its bones
    if bare and owner.type != 'ARMATURE':
        raise ToolError("Nothing renderable to watch. Name the armature or the objects that move.")
    wanted = action_named(action) if action else None
    if not views:                                        # from the side, and from where the character's face is
        views = ["right", _facing_view(owner)]
    views = views[:2]
    size = max(160, min(int(size), 400))
    with Showing(owner, wanted):
        first, last, cyclic = frame_span(owner, wanted, start, end)
        if frames:
            picked = [int(f) for f in frames][:12]
        else:
            count = max(2, min(int(count), 12, last - first + 1))
            span = last - first
            picked = [first + int(round(span * i / (count if cyclic else count - 1))) for i in range(count)]
            picked = list(dict.fromkeys(picked))
        boxes = []
        for frame in picked:
            scene.frame_set(frame)
            boxes.append(_world_box(shown or [owner]))
        low = Vector((min(lo.x for lo, _hi in boxes), min(lo.y for lo, _hi in boxes), min(lo.z for lo, _hi in boxes)))
        high = Vector((max(hi.x for _lo, hi in boxes), max(hi.y for _lo, hi in boxes), max(hi.z for _lo, hi in boxes)))
        body = max(max((hi - lo).length for lo, hi in boxes), 1e-3)
        centres = [(lo + hi) / 2 for lo, hi in boxes]
        # A move that covers more ground than the body is tall would be a speck in one fixed frame: the
        # camera then travels with the body, at one scale, and the squares of the floor show the travel.
        follow = max((c - centres[0]).length for c in centres) > 0.8 * body
        if follow:
            half = Vector((max(hi.x - lo.x for lo, hi in boxes), max(hi.y - lo.y for lo, hi in boxes), 0.0)) / 2
            frame_low = [Vector((c.x - half.x, c.y - half.y, low.z)) for c in centres]
            frame_high = [Vector((c.x + half.x, c.y + half.y, high.z)) for c in centres]
        else:
            frame_low, frame_high = [low] * len(picked), [high] * len(picked)
        floor = None
        if "bottom" not in views:
            pad = 0.6 * body
            cell = 0.5
            while (high.x - low.x + 2 * pad) / cell > 48 or (high.y - low.y + 2 * pad) / cell > 48:
                cell *= 2.0
            x0, y0 = math.floor((low.x - pad) / cell), math.floor((low.y - pad) / cell)
            x1, y1 = math.ceil((high.x + pad) / cell), math.ceil((high.y + pad) / cell)
            verts, faces, shades = [], [], []
            for ix in range(x0, x1):                      # squares fixed to the world, so travel over them shows
                for iy in range(y0, y1):
                    at = len(verts)
                    verts += [(ix * cell, iy * cell, ground), ((ix + 1) * cell, iy * cell, ground),
                              ((ix + 1) * cell, (iy + 1) * cell, ground), (ix * cell, (iy + 1) * cell, ground)]
                    faces.append((at, at + 1, at + 2, at + 3))
                    shades.append((ix + iy) % 2)
            at, deep = len(verts), ground - 0.5 * body
            verts += [(x0 * cell, y0 * cell, z) for z in (deep, ground)] + [(x1 * cell, y0 * cell, z) for z in (deep, ground)] \
                + [(x1 * cell, y1 * cell, z) for z in (deep, ground)] + [(x0 * cell, y1 * cell, z) for z in (deep, ground)]
            faces += [(at + 2 * k, at + (2 * k + 2) % 8, at + (2 * k + 3) % 8, at + 2 * k + 1) for k in range(4)]
            shades += [0] * 4
            mesh = bpy.data.meshes.new("_unix_floor")
            mesh.from_pydata(verts, [], faces)
            for shade in ((0.085, 0.095, 0.115, 1.0), (0.13, 0.14, 0.165, 1.0)):
                paint = bpy.data.materials.new("_unix_floor")
                paint.diffuse_color = shade
                mesh.materials.append(paint)
            mesh.polygons.foreach_set("material_index", shades)
            floor = bpy.data.objects.new("_unix_floor", mesh)
            scene.collection.objects.link(floor)
        rows: List[List[Any]] = [[] for _ in views]
        try:
            names = [o.name for o in shown] + ([owner.name] if bare else []) + ([floor.name] if floor else [])
            for frame, lo, hi in zip(picked, frame_low, frame_high):
                scene.frame_set(frame)
                lo = Vector((lo.x, lo.y, min(lo.z, ground - 0.04 * body)))
                coords = [c for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z) for c in (x, y, z)]
                shot = look(targets=names, views=views, shading=shading, isolate=True, size=size,
                            show_bones=True if bare else bool(show_bones), _coords=coords)
                for row, path in zip(rows, shot.images):
                    tile = _load_pixels(path, size)[:, :size]
                    _stamp(tile, str(frame))
                    row.append(tile)
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        finally:
            if floor is not None:
                data, mats = floor.data, list(floor.data.materials)
                bpy.data.objects.remove(floor, do_unlink=True)
                bpy.data.meshes.remove(data)
                for mat in mats:
                    bpy.data.materials.remove(mat)
    gap = 4
    line = np.full((size, gap, 4), 0.02, dtype=np.float32)
    strips = []
    for row in rows:
        parts: List[Any] = []
        for tile in row:
            parts += [tile, line]
        strips.append(np.concatenate(parts[:-1], axis=1))
    band = np.full((gap, strips[0].shape[1], 4), 0.02, dtype=np.float32)
    stacked: List[Any] = []
    for strip in strips:
        stacked += [strip, band]
    sheet = np.concatenate(stacked[:-1], axis=0)[::-1]
    out = os.path.join(data_dir("looks"), f"motion_{time.strftime('%H%M%S')}.jpg")
    image = bpy.data.images.new("_unix_motion", sheet.shape[1], sheet.shape[0], alpha=False)
    try:
        image.pixels.foreach_set(np.ascontiguousarray(sheet).ravel())
        image.filepath_raw, image.file_format = out, 'JPEG'
        image.save()
    finally:
        bpy.data.images.remove(image)
    fps = scene.render.fps / scene.render.fps_base
    used = wanted or (owner.animation_data.action if owner.animation_data else None)
    return ToolOutput({
        "action": used.name if used else None, "frames": picked, "range": [first, last], "fps": r(fps, 2),
        "seconds": r((last - first) / fps, 2), "rows": [_row_label(owner, view) for view in views],
        "floor_at": ground, "camera": "travels with the body; the floor's squares show the ground it covers" if follow
        else "fixed for the whole move",
        "other_actions": [n for n in actions_for(owner) if not used or n != used.name][:40],
        "judge": "Read each row left to right as time. The scale is the same in every tile, so anything that drifts, sinks "
                 "into the dark floor, hangs above it or snaps between two tiles is really doing that. Name what a stranger "
                 "would call wrong (lean, reach, feet, arms through the body, a head tilted or turned for no reason, toes "
                 "curled up, stiffness, a jump at the loop), then check_motion for the numbers, and fix before you report. "
                 "A recording can carry its actor's oddities or a glitch: if the motion itself is wrong, take another clip.",
    }, [out])


# ---------------------------------------------------------------------------------------------
# check_motion


def _runs(frames: List[int]) -> str:
    """[3, 4, 5, 9] -> '3-5, 9'."""
    if not frames:
        return ""
    out, first, last = [], frames[0], frames[0]
    for frame in frames[1:]:
        if frame == last + 1:
            last = frame
            continue
        out.append(f"{first}-{last}" if last > first else str(first))
        first = last = frame
    out.append(f"{first}-{last}" if last > first else str(first))
    return ", ".join(out[:6]) + (" ..." if len(out) > 6 else "")


def _between(a0, a1, b0, b1):
    """Shortest distance between two line segments, for arrays of segments (numpy, shape (n, 3))."""
    import numpy as np
    d1, d2, w = a1 - a0, b1 - b0, a0 - b0
    a = np.einsum("ij,ij->i", d1, d1)
    e = np.einsum("ij,ij->i", d2, d2)
    f = np.einsum("ij,ij->i", d2, w)
    c = np.einsum("ij,ij->i", d1, w)
    b = np.einsum("ij,ij->i", d1, d2)
    denom = a * e - b * b
    safe = np.where(denom > 1e-12, denom, 1.0)
    s = np.clip(np.where(denom > 1e-12, (b * f - c * e) / safe, 0.0), 0.0, 1.0)
    t = (b * s + f) / np.where(e > 1e-12, e, 1.0)
    t = np.where(e > 1e-12, t, 0.0)
    low, high = t < 0.0, t > 1.0
    t = np.clip(t, 0.0, 1.0)
    s = np.where(low, np.clip(-c / np.where(a > 1e-12, a, 1.0), 0.0, 1.0), s)
    s = np.where(high, np.clip((b - c) / np.where(a > 1e-12, a, 1.0), 0.0, 1.0), s)
    return np.linalg.norm((a0 + d1 * s[:, None]) - (b0 + d2 * t[:, None]), axis=1)


class _Skin:
    """Points on the meshes bound to a rig, with their bone weights, so their posed positions can be
    worked out from bone matrices alone (no mesh evaluation, whatever other modifiers there are)."""

    def __init__(self, rig: bpy.types.Object, meshes: List[bpy.types.Object], index: Dict[str, int]) -> None:
        import numpy as np
        to_rig = rig.matrix_world.inverted()
        points, bones, weights, lead = [], [], [], []
        for obj in meshes:
            groups = {g.index: index[g.name] for g in obj.vertex_groups if g.name in index}
            if not groups:
                continue
            place = to_rig @ obj.matrix_world
            verts = obj.data.vertices
            for at in range(0, len(verts), max(1, len(verts) // 20000)):
                vert = verts[at]
                held = sorted(((g.weight, groups[g.group]) for g in vert.groups if g.group in groups and g.weight > 1e-4),
                              reverse=True)[:4]
                if not held:
                    continue
                total = sum(w for w, _b in held)
                held += [(0.0, held[0][1])] * (4 - len(held))
                points.append(tuple(place @ vert.co) + (1.0,))
                bones.append([b for _w, b in held])
                weights.append([w / total for w, _b in held])
                lead.append(held[0][1])
        self.points = np.array(points, dtype=np.float64).reshape(-1, 4)
        self.bones = np.array(bones, dtype=np.int64).reshape(-1, 4)
        self.weights = np.array(weights, dtype=np.float64).reshape(-1, 4)
        self.lead = np.array(lead, dtype=np.int64)

    def posed(self, pick, turn):
        """World positions of the picked points, given every bone's rest-to-posed world matrix (nb, 4, 4)."""
        import numpy as np
        pts = self.points[pick]
        out = np.zeros((len(pts), 3))
        for k in range(4):
            moved = np.einsum("nij,nj->ni", turn[self.bones[pick, k]], pts)[:, :3]
            out += moved * self.weights[pick, k][:, None]
        return out


@tool(
    "check_motion",
    """Measure an animation and list what is wrong with it. Run it after keying or retargeting any
    motion, for each action, and fix what it reports before saying done. It plays the action
    through and reports: parts that sink below the floor, or that never reach it; feet that
    slide while they are planted (for a move on the spot: whether the floor would pass under the
    planted feet at one steady speed, and how fast that is); body parts that pass through each
    other (a hand through a thigh, one foot through the other); limbs stretched past their IK
    target; single-frame pops; for a loop, a last frame that does not lead into the first; bones
    in the middle of the body that never turn while the limbs below them move (a rigid torso);
    and actions that will be lost when the file is saved. Works for any rig, and for a plain
    animated object (floor, pops and loop only). It measures; whether the move looks alive is
    for look_motion and your eyes.""",
    {
        "target": {"type": "string", "description": "The armature (or a mesh bound to it), or an animated object."},
        "action": {"type": "string", "description": "Action to measure (default: the one in use)."},
        "start": {"type": "integer", "description": "First frame (default: the action's first)."},
        "end": {"type": "integer", "description": "Last frame (default: the action's last)."},
        "ground": {"type": "number", "description": "Floor height. Default: where the lowest part stands in the rest pose."},
        "contacts": {"type": "array", "items": {"type": "string"},
                     "description": "Bones that meet the floor (feet, paws, wheels). Default: the bones nearest the floor in the rest pose."},
        "loop": {"type": "boolean", "description": "True when the action is meant to repeat (default: decided from the action)."},
    },
    ["target"],
    timeout=900.0,
    label="Measured the animation",
)
def check_motion(target: str, action: str = "", start: Optional[int] = None, end: Optional[int] = None,
                 ground: Optional[float] = None, contacts: Optional[List[str]] = None,
                 loop: Optional[bool] = None) -> Dict[str, Any]:
    import numpy as np

    ensure_object_mode()
    scene = bpy.context.scene
    obj = get_object(target)
    rig = rig_of(obj)
    owner = rig or obj
    wanted = action_named(action) if action else None
    used = wanted or (owner.animation_data.action if owner.animation_data else None)
    if used is None:
        raise ToolError(f"'{owner.name}' has no action. Actions that fit it: {', '.join(actions_for(owner)) or 'none'}")
    fps = scene.render.fps / scene.render.fps_base
    issues: List[str] = []
    measured: Dict[str, Any] = {}

    with Showing(owner, wanted):
        first, last, cyclic = frame_span(owner, wanted, start, end)
        step = max(1, math.ceil((last - first + 1) / MAX_FRAMES))
        frames = list(range(first, last + 1, step))
        if len(frames) < 2:
            raise ToolError("The action is a single frame: there is no motion to measure.")
        rate = fps / step

        if rig is None:
            names, parents, deform = [owner.name], [-1], [0]
            rest_head = np.array([owner.matrix_world.translation], dtype=np.float64)
            rest_tail = rest_head + np.array([[0.0, 0.0, 1e-3]])
            corners = np.array([tuple(corner) + (1.0,) for corner in owner.bound_box], dtype=np.float64)
        else:
            bones = list(rig.data.bones)
            names = [b.name for b in bones]
            index = {name: i for i, name in enumerate(names)}
            parents = [index[b.parent.name] if b.parent else -1 for b in bones]
            deform = [i for i, b in enumerate(bones) if b.use_deform] or list(range(len(bones)))
            world0 = np.array(rig.matrix_world, dtype=np.float64)
            rest = np.array([np.array(b.matrix_local, dtype=np.float64) for b in bones])
            rest_inv = np.linalg.inv(rest)
            rest_head = np.array([rig.matrix_world @ b.head_local for b in bones], dtype=np.float64)
            rest_tail = np.array([rig.matrix_world @ b.tail_local for b in bones], dtype=np.float64)
            skin = _Skin(rig, bound_meshes(rig), index)
        count = len(names)

        # play it through once and keep what every measurement needs
        heads = np.zeros((len(frames), count, 3))
        tails = np.zeros((len(frames), count, 3))
        local = np.zeros((len(frames), count, 4))
        turns = np.zeros((len(frames), count, 4, 4))
        reach: Dict[str, List[float]] = {}
        for at, frame in enumerate(frames):
            scene.frame_set(frame)
            posed = owner.evaluated_get(bpy.context.evaluated_depsgraph_get())
            world = np.array(posed.matrix_world, dtype=np.float64)
            if rig is None:
                turns[at, 0] = world
                heads[at, 0] = world[:3, 3]
                tails[at, 0] = heads[at, 0] + world[:3, :3] @ np.array([0.0, 0.0, 1.0])
                local[at, 0] = tuple(posed.matrix_world.to_quaternion())
                continue
            for i, name in enumerate(names):
                pbone = posed.pose.bones[name]
                matrix = np.array(pbone.matrix, dtype=np.float64)
                turns[at, i] = world @ matrix @ rest_inv[i]
                heads[at, i] = world[:3, :3] @ np.array(pbone.head) + world[:3, 3]
                tails[at, i] = world[:3, :3] @ np.array(pbone.tail) + world[:3, 3]
                own = (pbone.parent.matrix.inverted() @ pbone.matrix) if pbone.parent else pbone.matrix
                local[at, i] = tuple(own.to_quaternion())
            for pbone in posed.pose.bones:
                for con in pbone.constraints:
                    if con.type == 'IK' and not con.mute and con.influence > 0.5 and con.target is not None \
                            and con.target.original == rig and con.subtarget in index:
                        goal = posed.pose.bones[con.subtarget]
                        reach.setdefault(f"{pbone.name} -> {con.subtarget}", []).append((pbone.tail - goal.head).length)

    lowest = float(min(rest_head[deform, 2].min(), rest_tail[deform, 2].min()))
    height = max(float(max(rest_head[deform, 2].max(), rest_tail[deform, 2].max())) - lowest, 1e-3)
    if rig is None:
        height = max(float(np.linalg.norm(corners[:, :3].max(axis=0) - corners[:, :3].min(axis=0))), 1e-3)
    tol = 0.02 * height

    # -- what meets the floor ----------------------------------------------------------------
    groups: List[Tuple[str, Any, Any]] = []          # name, (frames, points, 3) positions, rest positions
    if rig is None:
        spots = np.einsum("fij,nj->fni", turns[:, 0], corners)[:, :, :3]
        base = spots[0]
        keep = base[:, 2] <= base[:, 2].min() + 0.25 * max(float(base[:, 2].max() - base[:, 2].min()), 1e-6)
        groups.append((owner.name, spots[:, keep], base[keep]))
    else:
        if contacts:
            unknown = [n for n in contacts if n not in index]
            if unknown:
                raise ToolError(f"No bone named {', '.join(unknown)}. Bones: {', '.join(names[:60])}")
            seeds = [index[n] for n in contacts]
        else:
            near = [i for i in deform if (rest_head[i, 2] + rest_tail[i, 2]) / 2 <= lowest + 0.1 * height]
            seeds = [i for i in near if parents[i] not in near]
        children: Dict[int, List[int]] = {}
        for i, p in enumerate(parents):
            children.setdefault(p, []).append(i)
        for seed in seeds:
            family, stack = [], [seed]
            while stack:
                i = stack.pop()
                family.append(i)
                stack.extend(children.get(i, []))
            pick = np.nonzero(np.isin(skin.lead, family))[0] if len(skin.points) else np.array([], dtype=np.int64)
            if len(pick):
                at_rest = skin.posed(pick, np.broadcast_to(world0, (count, 4, 4)))
                order = np.argsort(at_rest[:, 2])[:max(12, int(0.3 * len(pick)))]
                pick = pick[order[::max(1, len(order) // 150)]]
                spots = np.stack([skin.posed(pick, turns[at]) for at in range(len(frames))])
                base = skin.posed(pick, np.broadcast_to(world0, (count, 4, 4)))
            else:                                    # no mesh on these bones: use the joints themselves
                spots = np.concatenate([heads[:, family], tails[:, family]], axis=1)
                base = np.concatenate([rest_head[family], rest_tail[family]])
            groups.append((names[seed], spots, base))
    floor = float(ground) if ground is not None else float(min(base[:, 2].min() for _n, _s, base in groups))
    measured["floor"] = r(floor, 3)

    centre = heads[:, deform].mean(axis=1)
    def depth_of(i: int) -> int:
        steps = 0
        while parents[i] >= 0:
            i, steps = parents[i], steps + 1
        return steps
    hub = min(deform, key=depth_of)                      # the hips: leaning over moves the middle of the body, not them
    travel = heads[-1, hub, :2] - heads[0, hub, :2]
    wander = float(np.linalg.norm(heads[:, hub, :2] - heads[0, hub, :2], axis=1).max())
    seconds = (frames[-1] - frames[0]) / fps
    moving = float(np.linalg.norm(travel)) > 0.15 * height or wander > 0.4 * height
    measured["travel_m"] = r(float(np.linalg.norm(travel)), 3)
    measured["plays"] = "across the floor" if moving else "on the spot"
    pose = heads[:, deform] - centre[:, None, :]
    gap = float(np.linalg.norm(pose[-1] - pose[0], axis=1).max())
    looping = loop if loop is not None else (cyclic or gap < 0.02 * height)

    planted_any = np.zeros(len(frames), dtype=bool)
    slips, speeds, report = [], [], {}
    body = np.gradient(centre[:, :2], axis=0) * rate
    width = max(1, int(round(0.2 * rate)))
    kernel = np.ones(2 * width + 1) / (2 * width + 1)
    steady = np.stack([np.convolve(np.pad(body[:, k], width, mode="edge"), kernel, mode="valid") for k in range(2)], axis=1)
    for name, spots, _base in groups:
        up = spots[:, :, 2] - floor
        down = up < 0.008 * height                           # resting on the floor, not skimming over it
        planted = down.any(axis=1)
        planted_any |= planted
        deepest = float(up.min())
        entry: Dict[str, Any] = {"lowest_cm": r(deepest * 100, 1), "on_floor_frames": _runs([f for f, p in zip(frames, planted) if p])}
        if deepest < -0.015 * height:
            sunk = [f for f, row in zip(frames, up) if row.min() < -0.015 * height]
            issues.append(f"{name} sinks {abs(deepest) * 100:.1f} cm below the floor (frames {_runs(sunk)}): raise it, or flatten "
                          f"the foot as it lands and leaves.")
        if not planted.any() and deepest > tol:
            issues.append(f"{name} never reaches the floor: its lowest point stays {deepest * 100:.1f} cm above it.")
        # what carries the weight is the patch at the very bottom; points a little higher are rolling onto
        # or off the floor (heel to toe, or a pivot on the ball) and move although the foot does not slide
        patch = down & (up <= up.min(axis=1, keepdims=True) + 0.002 * height)
        both = patch[:-1] & patch[1:]
        settled = planted.copy()                           # the frame a foot lands and the frame it leaves are not
        settled[1:] &= planted[:-1]                          # judged: its points are still arriving at, or leaving, the floor
        settled[:-1] &= planted[1:]
        if looping:                                          # a loop's first frame follows its last
            settled[0] &= bool(planted[-1])
            settled[-1] &= bool(planted[0])
        carried = []
        for at in range(len(frames) - 1):
            held = both[at]
            if not held.any() or not (settled[at] and settled[at + 1]):
                continue
            under = ((spots[at + 1, held, :2] - spots[at, held, :2]) * rate).mean(axis=0)
            carried.append((at, under))
        if carried:
            under = np.array([v for _at, v in carried])
            ats = [at for at, _v in carried]
            speeds.append(steady[ats] - np.median(under, axis=0))     # how fast the body passes over the planted part
            wrong = under if moving else under - np.median(under, axis=0)
            slide = np.linalg.norm(wrong, axis=1)
            slips.append((name, [frames[a] for a in ats], slide, np.linalg.norm(steady[ats] - under, axis=1)))
            entry["slide_cm"] = r(float(slide.sum() / rate) * 100, 1)
        report[name] = entry
    measured["contacts"] = report
    air = [f for f, p in zip(frames, planted_any) if not p]
    if air:
        measured["nothing_on_floor_frames"] = _runs(air)
    if speeds:
        over = np.concatenate(speeds)
        pace = float(np.linalg.norm(over.mean(axis=0)))
        measured["ground_speed_m_s"] = r(pace, 2)
        going = over.mean(axis=0) if not moving else travel
        if rig is not None and pace > 0.15 and float(np.linalg.norm(going)) > 1e-6:
            from . import _mocap                         # which way that is, seen from the character
            parent_of = {n: (names[p] if p >= 0 else None) for n, p in zip(names, parents)}
            found = _mocap.roles(names, parent_of, {n: rig.data.bones[n].use_deform for n in names})
            if "thigh.L" in found and "thigh.R" in found:
                side = rest_head[index[found["thigh.L"]]] - rest_head[index[found["thigh.R"]]]
                ahead = np.array([side[1], -side[0]])        # (left - right) x up, flattened
                if float(np.linalg.norm(ahead)) > 1e-9:
                    ahead /= np.linalg.norm(ahead)
                    unit = going / np.linalg.norm(going)
                    fore, port = float(unit @ ahead), float(unit @ np.array([-ahead[1], ahead[0]]))
                    measured["moves"] = (f"{pace:.2f} m/s " + ("forward" if fore > 0.7 else "backward" if fore < -0.7 else
                                         "to its left" if port > 0.7 else "to its right" if port < -0.7 else
                                         ("forward" if fore > 0 else "backward") + " and to its " + ("left" if port > 0 else "right"))
                                         + ("" if moving else " (on the spot: the floor passes under it the other way)"))
        for name, at_frames, slide, each in slips:
            limit = max(0.35 * pace, 0.1 * height)
            if not moving and not looping:                   # on the spot and changing pace (a start, a stop): the
                continue                                     # floor speed is meant to change, so there is no one speed
            bad = [f for f, s in zip(at_frames, slide) if s > limit]
            if float(slide.mean()) > limit and len(bad) >= 3:      # two odd frames are a foot settling, not skating
                if moving:
                    issues.append(f"{name} slides while it is on the floor: about {float(slide.mean()):.2f} m/s on frames "
                                  f"{_runs(bad)}. A planted foot stays where it landed; hold its control still for those frames.")
                else:
                    issues.append(f"The floor would pass under {name} at an uneven speed ({float(each.min()):.2f} to "
                                  f"{float(each.max()):.2f} m/s, frames {_runs(bad)}): in a game the foot skates. A planted foot "
                                  f"moves back in a straight line at one speed.")

    # -- parts passing through each other ------------------------------------------------------
    if rig is not None and len(skin.points):
        at_rest = skin.posed(np.arange(len(skin.points)), np.broadcast_to(world0, (count, 4, 4)))
        radius = np.zeros(count)
        for i in deform:
            mine = at_rest[skin.lead == i]
            span = rest_tail[i] - rest_head[i]
            if len(mine) < 4 or float(span @ span) < 1e-12:
                radius[i] = 0.0
                continue
            along = np.clip(((mine - rest_head[i]) @ span) / float(span @ span), 0.0, 1.0)
            # the part's core thickness: a foot or a torso is flatter than a tube, and a tube as wide as its
            # widest side would collide with things that only pass close to it
            radius[i] = float(np.percentile(np.linalg.norm(mine - (rest_head[i] + np.outer(along, span)), axis=1), 30))

        def apart(i: int, j: int) -> int:
            up_i, up_j = [i], [j]
            while parents[up_i[-1]] >= 0:
                up_i.append(parents[up_i[-1]])
            while parents[up_j[-1]] >= 0:
                up_j.append(parents[up_j[-1]])
            shared = next((a for a in up_i if a in up_j), None)
            return 99 if shared is None else up_i.index(shared) + up_j.index(shared)

        def limbs(i: int, j: int) -> Tuple[int, int]:       # the two branches the bones sit on, where they part
            up_i, up_j = [i], [j]
            while parents[up_i[-1]] >= 0:
                up_i.append(parents[up_i[-1]])
            while parents[up_j[-1]] >= 0:
                up_j.append(parents[up_j[-1]])
            shared = next((a for a in up_i if a in up_j), None)
            if shared is None:
                return up_i[-1], up_j[-1]
            return up_i[max(up_i.index(shared) - 1, 0)], up_j[max(up_j.index(shared) - 1, 0)]

        solid = [i for i in deform if radius[i] > 0.0]
        clashes = []
        for a_at, i in enumerate(solid):
            for j in solid[a_at + 1:]:
                if apart(i, j) <= 2:
                    continue
                room = radius[i] + radius[j]
                gap0 = float(_between(rest_head[i][None], rest_tail[i][None], rest_head[j][None], rest_tail[j][None])[0])
                gaps = _between(heads[:, i], tails[:, i], heads[:, j], tails[:, j])
                depth = room - gaps
                limit = max(room - gap0, 0.0) + 0.6 * min(radius[i], radius[j])
                bad = depth > max(limit, 0.01 * height)
                if bad.any():
                    clashes.append((float(depth.max()), i, j, [f for f, b in zip(frames, bad) if b]))
        clashes.sort(reverse=True)
        told = set()
        for depth, i, j, at_frames in clashes:               # one line per pair of limbs, the worst overlap of each
            pair = frozenset(limbs(i, j))
            if pair in told or len(told) >= 5:
                continue
            told.add(pair)
            issues.append(f"{names[i]} passes through {names[j]} (they overlap by about {depth * 100:.0f} cm on frames "
                          f"{_runs(at_frames)}): move it clear.")
        if clashes:
            measured["parts_passing_through_each_other"] = len(told)

    # -- limbs that cannot reach their targets -------------------------------------------------
    for label, gaps in reach.items():
        far = [f for f, g in zip(frames, gaps) if g > 0.012 * height]
        if far:
            issues.append(f"{label}: the limb is stretched straight and still {max(gaps) * 100:.1f} cm short of its target on "
                          f"frames {_runs(far)}. Bring the target closer, or lower the hips on those frames.")

    # -- pops ----------------------------------------------------------------------------------
    # A frame that does not lie between its neighbours: however fast a move is, each pose sits near the
    # middle of the poses either side of it. One that does not is a snap.
    if len(frames) > 2:
        same = np.sign(np.einsum("fbi,fbi->fb", local[:-2], local[2:]))
        same[same == 0] = 1.0
        middle = local[:-2] + local[2:] * same[:, :, None]
        middle /= np.maximum(np.linalg.norm(middle, axis=2, keepdims=True), 1e-12)
        off = np.degrees(2.0 * np.arccos(np.abs(np.einsum("fbi,fbi->fb", local[1:-1], middle)).clip(0.0, 1.0)))
        jump = np.linalg.norm(centre[1:-1] - (centre[:-2] + centre[2:]) / 2, axis=1)
        pops = []
        for i in np.nonzero(off.max(axis=0) > 45.0 * step)[0]:      # a foot striking the floor turns sharply too
            at = int(off[:, i].argmax())
            pops.append((float(off[at, i]), f"{names[i]} snaps {off[at, i]:.0f} degrees out of line on frame {frames[at + 1]}"))
        if float(jump.max()) > 0.03 * height:
            at = int(jump.argmax())
            pops.append((1000.0, f"the whole body jumps {jump[at] * 100:.0f} cm out of line on frame {frames[at + 1]}"))
        for _size, text in sorted(pops, reverse=True)[:5]:
            issues.append(f"Pop: {text}. Look at the keys around that frame.")

    # -- the loop seam -------------------------------------------------------------------------
    measured["loop"] = bool(looping)
    measured["last_frame_differs_from_first_cm"] = r(gap * 100, 1)
    if looping:
        if gap > 0.01 * height:
            worst = deform[int(np.linalg.norm(pose[-1] - pose[0], axis=1).argmax())]
            issues.append(f"The loop does not close: on the last frame {names[worst]} is {gap * 100:.1f} cm from where it is on "
                          f"the first. Key the first pose again on the frame after the last.")
        elif len(frames) > 4:
            pace_of = np.diff(pose, axis=0) * rate                   # every joint's velocity between frames
            jolt = np.linalg.norm(pace_of[0] - pace_of[-1], axis=1)   # change of velocity across the seam
            usual = np.linalg.norm(np.diff(pace_of, axis=0), axis=2).max(axis=0)   # the sharpest change inside the loop
            over = jolt - 2.0 * usual
            if float(over.max()) > 0.25 * height:                     # a landing foot changes speed sharply too: compare
                worst = int(over.argmax())
                issues.append(f"The loop hitches: {names[deform[worst]]} changes speed by {float(jolt[worst]):.2f} m/s where the "
                              f"last frame meets the first, far more than anywhere inside the loop. The frames either side of the "
                              f"seam must carry on the same movement.")

    # -- a rigid middle ------------------------------------------------------------------------
    if rig is not None:
        swing = np.degrees(2.0 * np.arccos(np.abs(np.einsum("fbi,bi->fb", local, local[0])).clip(0.0, 1.0))).max(axis=0)
        alive = swing > 1.0
        def moves_below(start: int) -> bool:
            stack = [start]
            while stack:
                j = stack.pop()
                if alive[j] and j in deform:
                    return True
                stack.extend(children.get(j, []))
            return False

        rigid = []                                       # a still link that carries two or more moving branches
        for i in deform:                                 # (a spine under arms and head; not a collarbone)
            if alive[i] or parents[i] < 0:
                continue
            if sum(1 for c in children.get(i, []) if moves_below(c)) >= 2:
                rigid.append(names[i])
        measured["bones_that_move"] = f"{int(alive[deform].sum())} of {len(deform)}"
        if rigid and alive[deform].sum() >= 0.5 * len(deform):       # a whole-body move with a stiff link in it
            issues.append(f"{', '.join(rigid[:8])} never turn while the bones below them move: the body reads as rigid. In a real "
                          f"move the weight shifts through the whole chain.")

    # -- kept on file --------------------------------------------------------------------------
    stored = actions_for(owner)
    loose = [n for n in stored if bpy.data.actions[n].users == 0 and not bpy.data.actions[n].use_fake_user]
    if loose:
        issues.append(f"{len(loose)} action(s) have no user and are thrown away when the file is saved: {', '.join(loose[:8])}. "
                      f"Set use_fake_user = True on each.")

    return {"target": owner.name, "action": used.name, "frames": [frames[0], frames[-1]], "fps": r(fps, 2),
            "seconds": r(seconds, 2), "measured": measured, "issues": issues,
            "other_actions": [n for n in stored if n != used.name][:40],
            "next": "Fix each issue, then run this again. Numbers do not show whether a move looks alive: look_motion does."
            if issues else "Nothing measurable is wrong. Watch it with look_motion before you call it good."}
