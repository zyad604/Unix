# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Vision: render quick views of the scene and return them as images to the model.

``look`` builds a temporary camera per view, frames the target objects with
``Object.camera_fit_coords`` and renders at low cost (Workbench for shape, EEVEE for
materials and light). Every render setting it touches is restored afterwards, and the
temporary cameras are removed, so the user's scene is left untouched.

The looks are steps (see ``registry``): in a window each picture is rendered by Blender's own
render job, the way F12 renders with "Keep User Interface", and the tool waits for it between
slices. A render of a big scene (and the shaders EEVEE compiles for it the first time) takes
seconds; the window keeps drawing and answering through it.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Vector

from . import _util
from ..bridge.main_thread import drain, in_slices
from ._util import data_dir, eevee_engine_id, ensure_object_mode, resolve_objects
from .registry import WAIT, ToolError, ToolOutput, tool

_VIEWS = {
    "persp": (Vector((1.0, -1.25, 0.8)), False),
    "persp_back": (Vector((-1.0, 1.25, 0.8)), False),
    "front": (Vector((0.0, -1.0, 0.0)), True),
    "back": (Vector((0.0, 1.0, 0.0)), True),
    "right": (Vector((1.0, 0.0, 0.0)), True),
    "left": (Vector((-1.0, 0.0, 0.0)), True),
    "top": (Vector((0.0, 0.0, 1.0)), True),
    "bottom": (Vector((0.0, 0.0, -1.0)), True),
}
_RENDERABLE = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME', 'GREASEPENCIL'}


_BLOCKS = (("scenes", bpy.types.Scene), ("objects", bpy.types.Object), ("materials", bpy.types.Material),
           ("worlds", bpy.types.World), ("cameras", bpy.types.Camera))


class _Restore:
    """Record attribute values and put them back in reverse order.

    What was changed is remembered by the name of the datablock it belongs to and its path in it, not
    only by reference: a look waits for its renders while the window answers the user, who may delete
    an object meanwhile, and touching a deleted one through an old reference can crash Blender. After
    an undo or a file opened nothing is put back: Blender has put its own state back already."""

    def __init__(self) -> None:
        self._saved: List[tuple] = []
        self._epoch = _util.data_epoch()

    def set(self, owner: Any, attr: str, value: Any) -> None:
        try:
            old = getattr(owner, attr)
            setattr(owner, attr, value)
        except Exception:
            return  # property missing on this Blender version
        if isinstance(old, bpy.types.ID):           # a camera, a world: found again by name as well
            old = _Named(_address(old), old)
        self._saved.append((_address(owner), owner, attr, old))

    def restore(self) -> None:
        saved, self._saved = self._saved, []
        if _util.data_epoch() != self._epoch:
            return
        found: Dict[str, Dict[str, Any]] = {}
        for address, owner, attr, old in reversed(saved):
            target = owner
            if address is not None:
                collection, name, path = address
                if collection not in found:
                    found[collection] = {block.name: block for block in getattr(bpy.data, collection)}
                block = found[collection].get(name)
                if block is None:
                    continue                    # deleted meanwhile: nothing to put back
                try:
                    target = block.path_resolve(path) if path else block
                except Exception:
                    continue
            if isinstance(old, _Named):
                if old.address is None:
                    old = old.block
                else:
                    collection, name, _path = old.address
                    if collection not in found:
                        found[collection] = {block.name: block for block in getattr(bpy.data, collection)}
                    old = found[collection].get(name)
                    if old is None:
                        continue
            try:
                setattr(target, attr, old)
            except Exception:
                pass


class _Named:
    """A datablock that was the value of a setting, kept by name."""
    __slots__ = ("address", "block")

    def __init__(self, address: Optional[tuple], block: Any) -> None:
        self.address, self.block = address, block


def _address(owner: Any) -> Optional[tuple]:
    """(collection in bpy.data, datablock name, path inside it) for a setting, or None when it cannot be
    found again by name (then it is put back through the reference, as before)."""
    try:
        block = owner.id_data
        path = "" if owner == block else owner.path_from_id()
    except Exception:
        return None
    for collection, kind in _BLOCKS:
        if isinstance(block, kind):
            return collection, block.name, path
    return None


def _window():
    """The window renders can run behind, or None (background mode)."""
    if bpy.app.background:
        return None
    wm = bpy.context.window_manager
    return bpy.context.window or (wm.windows[0] if wm and wm.windows else None)


def _second_eevee() -> bool:
    """Would a render job run EEVEE beside an EEVEE viewport (or under one the user may switch to)? Twice Unix
    died that way while a look rendered behind the window (31 Oct, 2 Nov: access violation, the user flipping the
    viewport between Material Preview and Rendered and clicking in the outliner meanwhile). Such a render is done
    on the main thread instead: the window waits a moment, nothing runs beside it."""
    if bpy.context.scene.render.engine != 'BLENDER_WORKBENCH':
        return True
    wm = bpy.context.window_manager
    return any(space.type == 'VIEW_3D' and space.shading.type in {'MATERIAL', 'RENDERED'}
               for window in (wm.windows if wm else []) for area in window.screen.areas if area.type == 'VIEW_3D'
               for space in area.spaces)


def _render_still(path: str):
    """Steps: render the scene's active camera to ``path`` (already set as the output). Run in slices
    with a window, Blender renders on its own render job and the steps wait for it with WAIT, so the
    window keeps drawing and answering through the render; otherwise (background mode, run to the end at
    once, or EEVEE in the picture: ``_second_eevee``) it is a plain render that holds the main thread."""
    window = _window()
    if window is None or not in_slices() or _second_eevee():
        bpy.ops.render.render(write_still=True)
        return
    view = bpy.context.preferences.view
    shown = view.render_display_type
    area = next((a for a in window.screen.areas if a.type == 'VIEW_3D'), None) or window.screen.areas[0]
    try:
        view.render_display_type = 'NONE'           # no render window: the picture only goes to the file
        with _util.window_context(window=window, area=area):       # no override inside the window pump
            started = bpy.ops.render.render('INVOKE_DEFAULT', write_still=True)
    finally:
        view.render_display_type = shown
    if 'RUNNING_MODAL' not in started:
        bpy.ops.render.render(write_still=True)
        return
    while bpy.app.is_job_running('RENDER'):
        yield WAIT
    for _ in range(100):                            # the file is written as the job ends
        if os.path.exists(path):
            break
        yield WAIT


_leftover: List[Any] = []       # clean-ups of a look stopped while its render job still ran


def _settle() -> None:
    """Before a render: refuse while one is running, and finish the clean-up of a look that was stopped
    while its render ran (its settings must be back before the next look records them)."""
    if bpy.app.is_job_running('RENDER'):
        raise ToolError("A render is already running; try again when it finishes.")
    while _leftover:
        _leftover.pop(0)()


def _after_render(cleanup) -> None:
    """Run ``cleanup`` now, or, when a render job is still running (the look was stopped half-way), as
    soon as it ends: settings and cameras are not pulled from under a render."""
    if not bpy.app.is_job_running('RENDER'):
        cleanup()
        return
    _leftover.append(cleanup)

    def later():
        if bpy.app.is_job_running('RENDER'):
            return 0.2
        while _leftover:
            _leftover.pop(0)()
        return None
    bpy.app.timers.register(later, first_interval=0.2)


# the axes and the box diagonals, both ways: the vertex furthest along each is kept when a mesh is thinned
_REACH = np.array([(x, y, z) for x in (-1, 0, 1) for y in (-1, 0, 1) for z in (-1, 0, 1)
                   if (x, y, z) != (0, 0, 0) and (abs(x) + abs(y) + abs(z)) in (1, 3)], dtype=float)


def _framing(names: List[str]):
    """Steps; returns ``_bbox_coords`` of the named objects, measured a few hundred objects per slice, and the
    centre to frame them around."""
    coords: List[float] = []
    sums = [Vector(), 0]
    found = {obj.name: obj for obj in bpy.data.objects}
    total = sum(len(found[n].data.vertices) for n in names if n in found and found[n].type == 'MESH')
    for start in range(0, len(names), 250):
        if start:
            yield
            found = {obj.name: obj for obj in bpy.data.objects}
        chunk = [found[n] for n in names[start:start + 250] if n in found]
        coords.extend(_bbox_coords(chunk, bpy.context.evaluated_depsgraph_get(), total, sums))
    return coords, (sums[0] / sums[1] if sums[1] else None)


def _bbox_coords(objects: List[bpy.types.Object], depsgraph, total: Optional[int] = None,
                 sums: Optional[list] = None) -> List[float]:
    """Points to frame: a sample of each mesh's own vertices (a bounding box's empty corners
    push the camera back and leave the subject small), box corners for everything else. Each
    mesh's outermost vertex along every axis is always kept; on a big scene the rest are
    thinned to about 20,000 points in all, which frame the same and cost far less to fit."""
    import numpy as np
    coords: List[float] = []
    meshes = [obj.evaluated_get(depsgraph) for obj in objects]
    if total is None:
        total = sum(len(ev.data.vertices) for ev in meshes if ev.type == 'MESH' and ev.data is not None)
    thin = max(1, -(-total // 20_000))
    for ev in meshes:
        mesh = ev.data if ev.type == 'MESH' else None
        if mesh is not None and len(mesh.vertices):
            points = np.empty(len(mesh.vertices) * 3, np.float32)
            mesh.vertices.foreach_get("co", points)
            points = points.reshape(-1, 3).astype(np.float64)
            matrix = np.array(ev.matrix_world)
            points = points @ matrix[:3, :3].T + matrix[:3, 3]
            if sums is not None:        # the centre is the mean of the full sample, whatever is thinned below
                kept = points[::max(1, len(points) // 1500)]
                sums[0] += Vector(kept.sum(0).tolist())
                sums[1] += len(kept)
            step = max(1, len(points) // 1500, thin if len(points) > 64 else 1)
            if step > 1:        # keep the outermost vertex towards each axis and each box corner: the frame stays put
                outer = np.unique((points @ _REACH.T).argmax(0))
                points = np.concatenate((points[::step], points[outer]))
            coords.extend(points.ravel().tolist())
        else:
            for corner in ev.bound_box:
                point = ev.matrix_world @ Vector(corner)
                coords.extend(point)
                if sums is not None:
                    sums[0] += point
                    sums[1] += 1
    return coords


def _without_grounds(objects: List[bpy.types.Object]) -> List[bpy.types.Object]:
    """What to frame when nobody says: everything except ground. A lawn or a floor many times wider than
    what stands on it would fill the picture and leave the subject a speck in the middle; it is still
    rendered, the camera just does not back off to take all of it in."""
    def box(obj):
        points = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
        hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
        return lo, hi

    boxes = {obj: box(obj) for obj in objects}
    flat = [o for o, (lo, hi) in boxes.items() if (hi.z - lo.z) < 0.03 * max(hi.x - lo.x, hi.y - lo.y, 1e-9)]
    rest = [o for o in objects if o not in flat]
    if not flat or not rest:
        return objects
    lo = Vector((min(boxes[o][0].x for o in rest), min(boxes[o][0].y for o in rest), 0.0))
    hi = Vector((max(boxes[o][1].x for o in rest), max(boxes[o][1].y for o in rest), 0.0))
    area = max((hi.x - lo.x) * (hi.y - lo.y), 1e-9)
    grounds = [o for o in flat if (boxes[o][1].x - boxes[o][0].x) * (boxes[o][1].y - boxes[o][0].y) > 4.0 * area]
    return [o for o in objects if o not in grounds] or objects


def memory_left_gb() -> Optional[float]:
    """Memory the computer can still hand out (the smaller of free RAM and free commit), or None off Windows."""
    if os.name != "nt":
        return None
    import ctypes

    class _Status(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + \
                   [(name, ctypes.c_ulonglong) for name in ("total_phys", "avail_phys", "total_commit", "avail_commit",
                                                            "total_virtual", "avail_virtual", "avail_extended")]
    status = _Status()
    status.length = ctypes.sizeof(_Status)
    kernel32 = ctypes.WinDLL("kernel32")               # its own handle: the shared windll's types belong to others
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_Status)]
    kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return min(status.avail_phys, status.avail_commit) / 1024 ** 3


def _rendered_need_gb(objects: List[bpy.types.Object]) -> Tuple[float, float]:
    """A rough guess at what an EEVEE render of these objects takes on top of what Blender holds, and the part of
    it that is textures: textures unpacked (about four times their files) and a compiled shader per material."""
    mats = {s.material for o in objects for s in getattr(o, "material_slots", ()) if s.material}
    images, size = set(), 0
    for mat in mats:
        for node in (mat.node_tree.nodes if mat.use_nodes and mat.node_tree else ()):
            image = getattr(node, "image", None) if node.type == 'TEX_IMAGE' else None
            if image is not None and image not in images:
                images.add(image)
                if image.packed_file is not None:
                    size += image.packed_file.size
                else:
                    try:
                        size += os.path.getsize(bpy.path.abspath(image.filepath))
                    except OSError:
                        pass
    textures = size * 4.0 / 1024 ** 3
    return 1.0 + len(mats) * 0.006 + textures, textures


def _configure_engine(scene: bpy.types.Scene, shading: str, restore: _Restore) -> None:
    if shading == "solid":
        restore.set(scene.render, "engine", 'BLENDER_WORKBENCH')
        display = scene.display
        restore.set(display.shading, "light", 'STUDIO')
        restore.set(display.shading, "color_type", 'MATERIAL')
        restore.set(display.shading, "show_cavity", True)
        restore.set(display.shading, "show_object_outline", True)
        restore.set(display.shading, "show_shadows", False)
        restore.set(display, "render_aa", '5')
    else:
        restore.set(scene.render, "engine", eevee_engine_id())
        restore.set(scene.eevee, "taa_render_samples", 16)


def _render_viewport(render: bpy.types.RenderSettings, size: int) -> None:
    window = bpy.context.window or bpy.context.window_manager.windows[0]
    area = next((a for a in window.screen.areas if a.type == 'VIEW_3D'), None)
    if area is None:
        raise ToolError("No 3D viewport is open.")
    region = next(r for r in area.regions if r.type == 'WINDOW')
    scale = size / max(region.width, region.height, 1)
    old = (render.resolution_x, render.resolution_y)
    render.resolution_x = max(64, int(region.width * scale))
    render.resolution_y = max(64, int(region.height * scale))
    try:
        with _util.window_context(window=window, screen=window.screen, area=area, region=region):
            bpy.ops.render.opengl(write_still=True, view_context=True)
    finally:
        render.resolution_x, render.resolution_y = old


def _bone_proxies(armatures: List[bpy.types.Object], scene: bpy.types.Scene) -> List[bpy.types.Object]:
    """Temporary meshes that stand in for bones (armatures do not render): one octahedron per bone."""
    proxies = []
    mat = bpy.data.materials.new("_unix_bones")
    mat.diffuse_color = (1.0, 0.72, 0.05, 1.0)
    for arm in armatures:
        verts, faces = [], []
        world = arm.matrix_world
        for bone in arm.pose.bones:
            head, tail = world @ bone.head, world @ bone.tail
            axis = tail - head
            length = axis.length
            if length < 1e-7:
                continue
            unit = axis / length
            side = unit.orthogonal().normalized()
            other = unit.cross(side)
            radius = max(length * 0.1, 0.002)
            mid = head + axis * 0.12
            base = len(verts)
            verts += [head, mid + side * radius, mid + other * radius, mid - side * radius, mid - other * radius, tail]
            for i in range(4):
                a, b = base + 1 + i, base + 1 + (i + 1) % 4
                faces += [(base, b, a), (base + 5, a, b)]
        if not verts:
            continue
        mesh = bpy.data.meshes.new("_unix_bones")
        mesh.from_pydata([tuple(v) for v in verts], [], faces)
        mesh.materials.append(mat)
        obj = bpy.data.objects.new("_unix_bones", mesh)
        scene.collection.objects.link(obj)
        proxies.append(obj)
    if not proxies:
        bpy.data.materials.remove(mat)
    return proxies


def _focus_coords(armatures: List[bpy.types.Object], bones: Optional[List[str]], point: Optional[List[float]],
                  radius: float) -> List[float]:
    """Corners of the box to frame for a close-up: around the named bones, or around a point."""
    if bones:
        found = [(arm, arm.pose.bones[name]) for arm in armatures for name in bones if name in arm.pose.bones]
        missing = sorted(set(bones) - {bone.name for _arm, bone in found})
        if missing:
            known = [b.name for arm in armatures for b in arm.pose.bones][:60]
            raise ToolError(f"No bone named {', '.join(missing)}. Bones: {', '.join(known) or 'none (name the armature in targets)'}")
        points = [arm.matrix_world @ end for arm, bone in found for end in (bone.head, bone.tail)]
        lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
        hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
        pad = max((hi - lo).length * 0.15, 1e-3)
    else:
        if len(point) != 3:
            raise ToolError("focus_point must be [x, y, z].")
        lo = hi = Vector(point)
        pad = max(float(radius), 1e-3)
    lo, hi = lo - Vector((pad, pad, pad)), hi + Vector((pad, pad, pad))
    return [c for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z) for c in (x, y, z)]


@tool(
    "look",
    """Render quick preview images and SEE them. Use after every meaningful build step and
    before reporting done: numbers do not reveal ugly proportions, floating parts, flipped
    normals or bad lighting. Frames the target objects automatically from each requested view.
    shading 'solid' is fast and shows form (no lights needed); 'rendered' uses EEVEE with the
    scene's lights, materials and world. Set use_scene_camera to check the real shot. In solid
    shading, armatures are drawn as solid orange bones inside a see-through mesh, so you can see whether
    a skeleton really sits inside its model: name the armature (or a mesh bound to one). Small
    parts are invisible when the whole model is framed: use focus_bones (or focus_point with
    focus_radius) for a close-up of fingers, a face or any detail.""",
    {
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "Object names to frame. Omit to frame everything renderable."},
        "views": {"type": "array", "items": {"type": "string", "enum": sorted(_VIEWS) + ["viewport"]},
                  "description": "Up to 4 of: persp, persp_back, front, back, left, right, top, bottom, or 'viewport' "
                                 "(exactly what the user currently sees in their 3D view, overlays included). Default ['persp']."},
        "shading": {"type": "string", "enum": ["solid", "rendered"], "description": "Default 'solid'."},
        "use_scene_camera": {"type": "boolean", "description": "Render through the scene's active camera instead of auto views."},
        "isolate": {"type": "boolean", "description": "Hide everything except the targets in these renders. Use it when other objects stand in the way."},
        "size": {"type": "integer", "description": "Longest image side in pixels (default 768, max 1280)."},
        "show_bones": {"type": "boolean", "description": "Draw armature bones (default: on in solid shading when a target is, or is bound to, an armature)."},
        "focus_bones": {"type": "array", "items": {"type": "string"},
                        "description": "Close-up: frame only these bones (e.g. one finger's three bones, or all bones of a hand)."},
        "focus_point": {"type": "array", "items": {"type": "number"}, "description": "Close-up: world point [x,y,z] to frame, with focus_radius."},
        "focus_radius": {"type": "number", "description": "Radius in metres around focus_point (default 0.15)."},
    },
    timeout=420.0,
    label="Looked at the scene",
)
def look_steps(targets: Optional[List[str]] = None, views: Optional[List[str]] = None, shading: str = "solid",
               use_scene_camera: bool = False, isolate: bool = False, size: int = 768,
               show_bones: Optional[bool] = None, focus_bones: Optional[List[str]] = None,
               focus_point: Optional[List[float]] = None, focus_radius: float = 0.15,
               _coords: Optional[List[float]] = None):
    # Steps (see registry): one picture per slice, each rendered on Blender's render job. Between slices only
    # names are held for what this made (cameras, bone stand-ins); the settings it changed come back by name.
    # _coords (not in the schema, so only other tools can pass it): frame these world points instead of
    # the targets as they are now. look_motion uses it to hold one framing across every frame of a move.
    _settle()
    ensure_object_mode()
    scene = bpy.context.scene
    in_scene = {o.name for o in scene.objects}       # a set: asking the scene's list is slow on a big scene
    if targets:  # a root empty stands for the whole asset under it
        family = []
        for obj in resolve_objects(targets, None):
            family.extend([obj] + list(obj.children_recursive))
        objects = [o for o in dict.fromkeys(family) if o.type in _RENDERABLE and o.name in in_scene]
    else:
        family = []
        objects = [o for o in resolve_objects(None, _RENDERABLE) if o.name in in_scene]
    armatures: List[bpy.types.Object] = []
    if show_bones or (show_bones is None and shading == "solid"):
        armatures = [o for o in (dict.fromkeys(family) if targets else scene.objects) if o.type == 'ARMATURE']
        for obj in objects:  # a mesh brings its rig along
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE' and mod.object is not None and mod.object not in armatures:
                    armatures.append(mod.object)
        if targets:          # and a rig brings the meshes bound to it
            for obj in scene.objects:
                if obj.type == 'MESH' and obj not in objects and any(
                        m.type == 'ARMATURE' and m.object in armatures for m in obj.modifiers):
                    objects.append(obj)
        if show_bones and shading != "solid":
            shading = "solid"  # bones are only visible through an X-rayed solid view
    too_heavy = ""
    if shading == "rendered":
        # 5 Nov: two rendered looks of a 3,000-object imported game scene took the computer's last memory (0.2 GB of
        # 57 GB left) and Windows itself went down with it, again and again. A look that would not fit is solid.
        shown = objects if isolate and targets else [o for o in scene.objects if o.type in _RENDERABLE and not o.hide_render]
        left, (need, textures) = memory_left_gb(), _rendered_need_gb(shown)
        if left is not None and need > left - 4.0:
            # Solid views with textures load every texture too (on 5 Nov that alone took 11 GB): only when they fit.
            textured = textures * 2.5 < left - 4.0
            shading, too_heavy = "solid", (
                f"Rendered shading was not used: it would need about {need:.0f} GB and only {left:.0f} GB is free, "
                "and running out takes the whole computer down. These are solid views"
                + (" with textures" if textured else " in plain colours (the textures alone would not fit)")
                + ". To see materials and light, look at a few targets with isolate=True.")
    views = (views or ["persp"])[:4]
    if "viewport" in views and bpy.app.background:
        raise ToolError("There is no viewport in background mode; use the other views.")
    if not objects and not armatures and not use_scene_camera and views != ["viewport"]:
        raise ToolError("Nothing renderable to look at. Create geometry first, or pass use_scene_camera.")
    if use_scene_camera and scene.camera is None:
        raise ToolError("The scene has no active camera. Create one or omit use_scene_camera.")
    size = max(256, min(int(size), 1280))
    out_dir = data_dir("looks")
    stamp = time.strftime("%H%M%S")

    restore = _Restore()
    temp_cams: List[str] = []
    proxies: List[bpy.types.Object] = []
    made: List[str] = []            # names of the bone stand-ins, for the clean-up
    images: List[str] = []
    meta: List[Dict[str, Any]] = []
    render = scene.render
    epoch = _util.data_epoch()
    try:
        _configure_engine(scene, shading, restore)
        if too_heavy and "with textures" in too_heavy:
            restore.set(scene.display.shading, "color_type", 'TEXTURE')
        restore.set(render, "resolution_percentage", 100)
        restore.set(render, "film_transparent", False)
        restore.set(render, "use_file_extension", True)
        restore.set(render.image_settings, "color_mode", 'RGB')  # before the format: JPEG forces RGB
        restore.set(render.image_settings, "file_format", 'JPEG')
        restore.set(render.image_settings, "quality", 88)
        if shading == "solid":
            # Workbench shows Material.diffuse_color, not the node colour: mirror it for the render.
            for mat in {s.material for o in objects for s in o.material_slots if s.material}:
                bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None) \
                    if mat.use_nodes and mat.node_tree else None
                base = bsdf.inputs.get("Base Color") if bsdf else None
                if base is not None and not base.is_linked:
                    restore.set(mat, "diffuse_color", tuple(base.default_value))
        if armatures:
            proxies = _bone_proxies(armatures, scene)
            made = [p.name for p in proxies]
            if proxies:
                # solid bones inside a glass model: X-ray would fade the bones as much as the mesh
                restore.set(scene.display.shading, "color_type", 'OBJECT')
                for obj in objects:
                    restore.set(obj, "color", (0.62, 0.66, 0.74, 0.28))
                for proxy in proxies:
                    proxy.color = (1.0, 0.66, 0.0, 1.0)
                bpy.context.view_layer.update()
        if isolate and targets:
            keep = {o.name for o in objects} | {o.name for o in proxies}
            for obj in scene.objects:
                if obj.name not in keep and obj.type in _RENDERABLE and not obj.hide_render:
                    restore.set(obj, "hide_render", True)

        names = [o.name for o in objects][:40]
        rig_names = [a.name for a in armatures]
        if use_scene_camera:
            aspect = render.resolution_x / max(1, render.resolution_y)
            restore.set(render, "resolution_x", size if aspect >= 1 else int(size * aspect))
            restore.set(render, "resolution_y", int(size / aspect) if aspect >= 1 else size)
            jobs = [("camera:" + scene.camera.name, scene.camera.name)]
        else:
            restore.set(render, "resolution_x", size)
            restore.set(render, "resolution_y", size)
            framed = objects if targets else _without_grounds(objects)
            framing = [o.name for o in framed + proxies]
            close = middle = None
            if focus_bones or focus_point:
                close = _focus_coords(armatures or [o for o in scene.objects if o.type == 'ARMATURE'],
                                      focus_bones, focus_point, focus_radius)
            objects = armatures = family = proxies = framed = None     # not held across the pauses
            if close is not None:
                coords = close
            elif _coords:
                coords = list(_coords)
            else:
                yield
                coords, middle = (yield from _framing(framing)) if framing else ([], None)
            yield                   # what to frame is known; the camera comes in the next slice
            scene = bpy.context.scene
            depsgraph = bpy.context.evaluated_depsgraph_get()
            jobs = []
            # One temporary camera, moved for each view: linking a new object (or switching the scene's
            # camera) rebuilds the scene's dependency graph, which takes a good part of a second on a big
            # scene; moving a camera does not.
            cam_data = bpy.data.cameras.new("_unix_look")
            cam_data.lens = 50.0
            cam = bpy.data.objects.new("_unix_look", cam_data)
            scene.collection.objects.link(cam)
            temp_cams.append(cam.name)
            restore.set(scene, "camera", cam)       # now: both changes cost one rebuild of the scene graph, not two
            import numpy as np
            points = np.array(coords, dtype=np.float64).reshape(-1, 3)
            centre = middle if middle is not None else Vector(points.mean(0).tolist())
            centre_np = np.array(tuple(centre))
            radius = float(np.sqrt(((points - centre_np) ** 2).sum(1)).max())
            for view in views:
                if view == "viewport":
                    jobs.append((view, None))
                    continue
                direction, ortho = _VIEWS[view]
                unit = direction.normalized()
                cam_data.type = 'ORTHO' if ortho else 'PERSP'
                rotation = unit.to_track_quat('Z', 'Y').to_euler()
                cam.rotation_euler = rotation
                bpy.context.view_layer.update()
                location, ortho_scale = cam.camera_fit_coords(depsgraph, coords)
                setting = {"type": cam_data.type, "rotation": tuple(rotation), "clip_start": 0.01, "clip_end": 100000.0,
                           "ortho_scale": cam_data.ortho_scale}
                if ortho:
                    # Sit just outside the targets along the view axis, so objects that are
                    # further out in front of them do not block the view.
                    reach = float(((points - centre_np) @ np.array(tuple(unit))).max())
                    lateral = Vector(location) - unit * (Vector(location) - centre).dot(unit)
                    setting["location"] = tuple(lateral + unit * (reach + 0.05 * radius + 0.05))
                    setting["ortho_scale"] = ortho_scale * 1.12
                    setting["clip_start"] = 0.001
                else:
                    setting["location"] = tuple(centre + (Vector(location) - centre) * 1.12)  # breathing room
                if focus_bones or focus_point:
                    # cutaway: render only the slab that holds the close-up, so nothing in front of
                    # it (the rest of the body, the other hand) hides it and nothing behind clutters it
                    depths = (np.array(setting["location"]) - points) @ np.array(tuple(unit))
                    setting["clip_start"] = max(float(depths.min()) * 0.98, 0.0005)
                    setting["clip_end"] = float(depths.max()) * 1.02 + 1e-3
                jobs.append((view, setting))

        restore.set(scene, "camera", scene.camera)
        objects = armatures = family = proxies = None      # Blender objects are not held across the pauses below
        for index, (view, setting) in enumerate(jobs):
            yield
            scene = bpy.context.scene
            render = scene.render
            path = os.path.join(out_dir, f"look_{stamp}_{index}_{view.replace(':', '_')}.jpg")
            restore.set(render, "filepath", path)
            if setting is None:  # the user's live viewport, as they see it
                _render_viewport(render, size)
            else:
                cam = bpy.data.objects.get(setting) if isinstance(setting, str) else bpy.data.objects.get(temp_cams[0])
                if cam is None:
                    continue
                if isinstance(setting, dict):
                    cam.data.type, cam.rotation_euler, cam.location = setting["type"], setting["rotation"], setting["location"]
                    cam.data.ortho_scale = setting["ortho_scale"]
                    cam.data.clip_start, cam.data.clip_end = setting["clip_start"], setting["clip_end"]
                if scene.camera != cam:
                    scene.camera = cam
                del cam
                yield from _render_still(path)
            if os.path.exists(path):
                images.append(path)
                meta.append({"view": view, "image": len(images)})
    finally:
        def cleanup() -> None:
            if _util.data_epoch() != epoch:
                return          # undone or another file opened: Blender has put its own state back
            restore.restore()
            for name in temp_cams:
                cam = bpy.data.objects.get(name)
                if cam is not None:
                    data = cam.data
                    bpy.data.objects.remove(cam, do_unlink=True)
                    if data is not None and data.users == 0:
                        bpy.data.cameras.remove(data)
            bone_mats = set()
            for name in made:
                proxy = bpy.data.objects.get(name)
                if proxy is None:
                    continue
                data = proxy.data
                bone_mats |= {m.name for m in data.materials if m is not None}
                bpy.data.objects.remove(proxy, do_unlink=True)
                bpy.data.meshes.remove(data)
            for name in bone_mats:
                mat = bpy.data.materials.get(name)
                if mat is not None:
                    bpy.data.materials.remove(mat)
        _after_render(cleanup)

    if not images:
        raise ToolError("The render produced no image. Check that a GPU/display is available for the render engine.")
    data: Dict[str, Any] = {"shading": shading,
                            "framed": names if not use_scene_camera else "scene camera",
                            "views": meta, "note": "Images follow in order. Judge silhouette, proportions, contact "
                            "between parts, shading artefacts and (rendered) light and materials."}
    if too_heavy:
        data["rendered_skipped"] = too_heavy
    if focus_bones or focus_point:
        data["close_up"] = focus_bones or {"point": focus_point, "radius": focus_radius}
    if made:
        data["bones"] = ("Orange shapes are the bones of " + ", ".join(rig_names) + "; the mesh is see-through. "
                         "Every bone should run inside its part of the model. If the skeleton sits beside the model, at a "
                         "right angle to it or at a different size, the rig is wrong.")
    return ToolOutput(data, images)


def look(*args: Any, **kwargs: Any) -> ToolOutput:
    """``look`` run to the end at once, for tools that need the pictures before they can go on."""
    return drain(look_steps(*args, **kwargs))


@tool(
    "look_around",
    """See the WHOLE thing in one picture: all six sides (front, right, back / left, top, bottom),
    each framed square-on. Use it after building or changing any object, and before you report:
    something that is right from the side you built it from can be a stack of slabs from its end
    or from above, and you only know if you look. Judge every tile: does each side read as the
    real thing, are the cross-sections rounded where the real ones are, is anything missing on
    the sides you did not work from. Other objects are hidden in these renders.""",
    {
        "targets": {"type": "array", "items": {"type": "string"}, "description": "The objects that make up the thing. Omit for everything except the lighting set."},
        "shading": {"type": "string", "enum": ["solid", "rendered"], "description": "solid shows form (default); rendered shows materials and light."},
        "size": {"type": "integer", "description": "Pixels per tile (default 400, max 560)."},
    },
    timeout=600.0,
    label="Looked at it from every side",
)
def look_around(targets: Optional[List[str]] = None, shading: str = "solid", size: int = 400):
    import numpy as np
    from .reference import _load_pixels

    scene = bpy.context.scene
    if not targets:
        targets = [o.name for o in scene.objects if o.type in _RENDERABLE and not o.hide_render and not o.hide_get()
                   and not o.name.startswith("Unix_")]
    if not targets:
        raise ToolError("Nothing to look at: the scene has no visible objects.")
    size = max(240, min(int(size), 560))
    order = ["front", "right", "back", "left", "top", "bottom"]
    shots = (yield from look_steps(targets=targets, views=order[:4], shading=shading, isolate=True, size=size,
                                   show_bones=False)).images
    shots += (yield from look_steps(targets=targets, views=order[4:], shading=shading, isolate=True, size=size,
                                    show_bones=False)).images
    if len(shots) != 6:
        raise ToolError("Could not render all six sides.")
    tiles = [_load_pixels(path, size)[:, :size] for path in shots]
    gap = 6
    line = np.ones((size, gap, 4), dtype=np.float32)
    rows = [np.concatenate([tiles[k], line, tiles[k + 1], line, tiles[k + 2]], axis=1) for k in (0, 3)]
    sheet = np.concatenate([rows[0], np.ones((gap, rows[0].shape[1], 4), dtype=np.float32), rows[1]], axis=0)[::-1]
    height, width = sheet.shape[:2]
    image = bpy.data.images.new("_unix_around", width, height, alpha=False)
    out = os.path.join(data_dir("looks"), f"around_{time.strftime('%H%M%S')}.jpg")
    try:
        image.pixels.foreach_set(np.ascontiguousarray(sheet).ravel())
        image.filepath_raw, image.file_format = out, 'JPEG'
        image.save()
    finally:
        bpy.data.images.remove(image)
    return ToolOutput({
        "tiles": {"top_row": ["front (seen from -Y)", "right (seen from +X)", "back (seen from +Y)"],
                  "bottom_row": ["left (seen from -X)", "top (seen from above)", "bottom (seen from below)"]},
        "shading": shading, "framed": targets[:40],
        "judge": "Every tile, not only the best one. An end or top view that shows rectangles stacked on each other means the "
                 "parts are slabs: give them rounded cross-sections (profile_solid round_m of a third to half the thickness) and "
                 "their real, different widths. A side the photo did not cover still has to look finished.",
    }, [out])


_COMPASS = ("east (+X)", "north-east", "north (+Y)", "north-west", "west (-X)", "south-west", "south (-Y)", "south-east")


@tool(
    "look_map",
    """A labelled map of the scene, seen from straight above: every asset has a number, and an
    arrow that points the way its front (its local -Y side) faces. The reply lists each number
    with the asset's name, position, size and facing, and counts the assets of each kind. Use it
    whenever several things are placed in relation to each other, to check with your own eyes
    how many there are, how they are spaced, whether anything overlaps, and whether each one
    faces what it should (a door toward the road, seats toward the table). The top of the image
    is +Y.""",
    {"size": {"type": "integer", "description": "Longest image side in pixels (default 1024, max 1280)."}},
    timeout=420.0,
    label="Looked at the map",
)
def look_map(size: int = 1024):
    from . import quality
    _settle()
    scene = bpy.context.scene
    ensure_object_mode()
    groups = quality._assets(quality._meshes(scene))
    if not groups:
        raise ToolError("Nothing to map: the scene has no mesh objects.")
    boxes = [quality._bounds(asset["members"]) for asset in groups]
    low = Vector((min(b[0].x for b in boxes), min(b[0].y for b in boxes), 0.0))
    high = Vector((max(b[1].x for b in boxes), max(b[1].y for b in boxes), 0.0))
    extent = max((high - low).length, 1e-3)
    text_size = extent * 0.03
    paint = {name: bpy.data.materials.new(name) for name in ("_unix_map_arrow", "_unix_map_text")}
    paint["_unix_map_arrow"].diffuse_color = (1.0, 0.25, 0.0, 1.0)
    paint["_unix_map_text"].diffuse_color = (1.0, 0.95, 0.1, 1.0)
    temporary: List[Tuple[str, str]] = []           # (object, its data) by name: held across the render's pauses
    legend = []
    ordered = sorted(range(len(groups)), key=lambda i: (-(boxes[i][0].y + boxes[i][1].y), boxes[i][0].x + boxes[i][1].x))[:80]
    epoch = _util.data_epoch()
    paints = [material.name for material in paint.values()]
    more = max(0, len(groups) - len(ordered))
    # what each label needs, as plain values: the labels are made over several slices
    marks = [(groups[i]["name"], boxes[i][0].copy(), boxes[i][1].copy(),
              groups[i]["main"].matrix_world.to_3x3() @ Vector((0.0, -1.0, 0.0))) for i in ordered]
    del groups, boxes, paint, scene
    try:
        for number, (name, lo, hi, front) in enumerate(marks, start=1):
            if number % 20 == 1:
                yield
                scene = bpy.context.scene
                paint = {"_unix_map_arrow": bpy.data.materials.get(paints[0]), "_unix_map_text": bpy.data.materials.get(paints[1])}
            centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z + extent * 0.004))
            width, depth, height = hi.x - lo.x, hi.y - lo.y, hi.z - lo.z
            front.z = 0.0
            flat = height < 0.05 * max(width, depth)             # ground, roads, water: no front to speak of
            entry = {"n": number, "name": name, "at": [round(centre.x, 2), round(centre.y, 2)],
                     "size": [round(width, 2), round(depth, 2), round(height, 2)]}
            if front.length > 1e-6 and not flat:
                front.normalize()
                entry["faces"] = _COMPASS[round(math.atan2(front.y, front.x) / (math.pi / 4)) % 8]
                reach = max(min(width, depth) * 0.5, text_size * 1.5)
                side = Vector((-front.y, front.x, 0.0)) * (text_size * 0.35)
                tip = centre + front * (reach + text_size * 1.4)
                neck = centre + front * reach
                mesh = bpy.data.meshes.new("_unix_map_arrow")
                mesh.from_pydata([tuple(centre + side * 0.4), tuple(centre - side * 0.4), tuple(neck - side * 0.4),
                                  tuple(neck + side * 0.4), tuple(neck + side * 1.6), tuple(neck - side * 1.6), tuple(tip)],
                                 [], [(0, 1, 2, 3), (4, 5, 6)])
                mesh.materials.append(paint["_unix_map_arrow"])
                arrow = bpy.data.objects.new("_unix_map_arrow", mesh)
                scene.collection.objects.link(arrow)
                temporary.append((arrow.name, mesh.name))
            curve = bpy.data.curves.new("_unix_map_text", 'FONT')
            curve.body = str(number)
            curve.size = text_size
            curve.align_x, curve.align_y = 'CENTER', 'CENTER'
            curve.materials.append(paint["_unix_map_text"])
            label = bpy.data.objects.new("_unix_map_text", curve)
            label.location = centre + Vector((0.0, 0.0, extent * 0.004))
            scene.collection.objects.link(label)
            temporary.append((label.name, curve.name))
            legend.append(entry)
        paint = scene = None
        yield
        bpy.context.view_layer.update()
        shot = yield from look_steps(views=["top"], size=max(512, min(int(size), 1280)), show_bones=False)
    finally:
        def cleanup() -> None:
            if _util.data_epoch() != epoch:
                return
            # all in one batch: removing objects one by one from a big scene costs a scene update each
            objects = {obj.name: obj for obj in bpy.data.objects}
            gone = [objects[name] for name, _data in temporary if name in objects]
            if gone:
                bpy.data.batch_remove(gone)
            datas = {block.name: block for block in list(bpy.data.curves) + list(bpy.data.meshes)}
            spare = [datas[data] for _name, data in temporary if data in datas and datas[data].users == 0]
            spare += [m for m in (bpy.data.materials.get(name) for name in paints) if m is not None]
            if spare:
                bpy.data.batch_remove(spare)
        _after_render(cleanup)
    counted = quality.inventory(bpy.context.scene)
    data = {"assets": counted["assets"], "by_kind": counted["by_kind"], "legend": legend,
            "note": "Top of the image is +Y (north), right is +X. Yellow numbers match `legend`; each orange arrow points "
                    "where that asset's front faces. Compare the counts and the facing with what was asked."}
    if more:
        data["not_labelled"] = more
    return ToolOutput(data, shot.images)


@tool(
    "render_image",
    """Final-quality still through the scene's active camera with the scene's own engine and
    settings, saved to disk and returned as a preview. Slow: use `look` while iterating and
    this once at the end when the user wants a render. Samples are capped to keep it bounded.""",
    {
        "path": {"type": "string", "description": "Output file (.png/.jpg). Default: a dated file in the Unix renders folder."},
        "max_samples": {"type": "integer", "description": "Sample cap for Cycles/EEVEE (default 128)."},
        "resolution_percentage": {"type": "integer", "description": "Scale of the scene resolution, 10-100 (default 100)."},
    },
    timeout=1800.0,
    label="Rendered an image",
)
def render_image(path: str = "", max_samples: int = 128, resolution_percentage: int = 100):
    # Steps: a final render takes minutes; it runs on Blender's render job while the window keeps answering.
    scene = bpy.context.scene
    if scene.camera is None:
        raise ToolError("The scene has no active camera. Add a camera and set scene.camera first.")
    _settle()
    ensure_object_mode()
    if not path:
        path = os.path.join(data_dir("renders"), time.strftime("render_%Y%m%d_%H%M%S.png"))
    path = bpy.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = 'JPEG' if path.lower().endswith((".jpg", ".jpeg")) else 'PNG'
    restore = _Restore()
    started = time.time()
    try:
        restore.set(scene.render, "filepath", path)
        restore.set(scene.render, "resolution_percentage", max(10, min(int(resolution_percentage), 100)))
        settings = scene.render.image_settings
        restore.set(settings, "color_mode", settings.color_mode)  # JPEG would otherwise leave it at RGB
        restore.set(settings, "file_format", fmt)
        cap = max(1, int(max_samples))
        if scene.render.engine == 'CYCLES':
            restore.set(scene.cycles, "samples", min(scene.cycles.samples, cap))
        elif hasattr(scene, "eevee"):
            restore.set(scene.eevee, "taa_render_samples", min(scene.eevee.taa_render_samples, cap))
        del scene, settings
        yield from _render_still(path)
    finally:
        _after_render(restore.restore)
    scene = bpy.context.scene
    if not os.path.exists(path):
        raise ToolError("Render finished but no file was written.")

    # Attach a small copy so a 4K render does not flood the model.
    preview = path
    try:
        img = bpy.data.images.load(path)
        w, h = img.size
        if max(w, h) > 1280:
            scale = 1280 / max(w, h)
            img.scale(max(1, int(w * scale)), max(1, int(h * scale)))
            preview = os.path.join(data_dir("looks"), "render_preview.png")
            img.filepath_raw, img.file_format = preview, 'PNG'
            img.save()
        bpy.data.images.remove(img)
    except Exception:
        preview = path
    return ToolOutput({"saved": path, "engine": scene.render.engine, "seconds": round(time.time() - started, 1),
                       "resolution": [scene.render.resolution_x, scene.render.resolution_y]}, [preview])


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".exr", ".hdr", ".tif", ".tiff", ".bmp", ".tga"}


@tool(
    "view_image",
    """Open an image file and SEE it: a reference image the user attached, a texture, or a
    render on disk. Use it on every attached image before starting, and note what you take
    from it (shape, proportions, colours, materials, mood).""",
    {"path": {"type": "string", "description": "Absolute path to a .png/.jpg/.webp/.exr/.hdr/.tif/.bmp/.tga file."}},
    ["path"],
    label="Viewed an image",
)
def view_image(path: str) -> ToolOutput:
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        raise ToolError(f"File not found: {path}")
    if os.path.splitext(path)[1].lower() not in _IMAGE_EXTS:
        raise ToolError("Not an image file. Use import_file for 3D files.")
    img = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = img.size
        if width == 0 or height == 0:
            raise ToolError("Blender could not read that image.")
        if max(width, height) > 1280:
            factor = 1280 / max(width, height)
            img.scale(max(1, int(width * factor)), max(1, int(height * factor)))
        out = os.path.join(data_dir("looks"), f"view_{time.strftime('%H%M%S')}.jpg")
        scene = bpy.context.scene
        restore = _Restore()
        try:
            restore.set(scene.render.image_settings, "color_mode", 'RGB')
            restore.set(scene.render.image_settings, "file_format", 'JPEG')
            restore.set(scene.render.image_settings, "quality", 88)
            img.save_render(out, scene=scene)
        finally:
            restore.restore()
    finally:
        bpy.data.images.remove(img)
    return ToolOutput({"file": path, "original_size": [width, height]}, [out])


@tool(
    "look_uv",
    """Render the UV layout of a mesh as an image and SEE it: islands, wasted space, overlaps
    and stretching that check_uv can only hint at. Use after unwrapping.""",
    {
        "object": {"type": "string", "description": "Mesh object name."},
        "size": {"type": "integer", "description": "Image size in pixels (default 1024)."},
    },
    ["object"],
    label="Looked at the UV layout",
)
def look_uv(object: str, size: int = 1024) -> ToolOutput:
    import numpy as np
    from ._util import get_object, ui_context

    obj = get_object(object)
    if obj.type != 'MESH' or not obj.data.uv_layers:
        raise ToolError(f"'{object}' is not a mesh with a UV layer. Unwrap it first.")
    ensure_object_mode()
    size = max(256, min(int(size), 2048))
    raw = os.path.join(data_dir("looks"), f"uv_{time.strftime('%H%M%S')}_raw.png")
    view_layer = bpy.context.view_layer
    previous = view_layer.objects.active
    try:
        view_layer.objects.active = obj
        with ui_context():
            bpy.ops.uv.export_layout(filepath=raw, export_all=True, mode='PNG', size=(size, size),
                                     opacity=0.25, check_existing=False)
    finally:
        view_layer.objects.active = previous
    if not os.path.isfile(raw):
        raise ToolError("The UV layout could not be exported.")
    img = bpy.data.images.load(raw)
    try:  # flatten the transparent export onto white so the black edges are visible
        pixels = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        rgba = pixels.reshape(-1, 4)
        alpha = rgba[:, 3:4].copy()
        rgba[:, :3] = rgba[:, :3] * alpha + (1.0 - alpha)
        rgba[:, 3] = 1.0
        img.pixels.foreach_set(pixels)
        out = raw.replace("_raw.png", ".png")
        img.filepath_raw, img.file_format = out, 'PNG'
        img.save()
    finally:
        bpy.data.images.remove(img)
        try:
            os.remove(raw)
        except OSError:
            pass
    return ToolOutput({"object": obj.name, "uv_layer": obj.data.uv_layers.active.name,
                       "note": "White is empty UV space, grey shapes are islands, the frame is the 0-1 tile."}, [out])
