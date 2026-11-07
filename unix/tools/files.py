# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Import and export with verification, plus a safety snapshot of the .blend."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import struct
import threading
import time
from typing import Any, Dict, List, Optional

import bpy

from . import _turn
from ._util import data_dir, ensure_object_mode, get_object, ui_context
from .registry import ToolError, tool
from .roblox_surface import _showing

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".exr", ".hdr", ".tif", ".tiff", ".webp", ".bmp", ".tga"}


def _op(path: str):
    """Resolve 'wm.obj_import' to an operator, or None when this build lacks it."""
    module, name = path.split(".")
    try:
        op = getattr(getattr(bpy.ops, module), name)
        op.get_rna_type()
        return op
    except Exception:
        return None


@tool(
    "import_file",
    """Import a 3D or image file into the scene: .glb/.gltf, .fbx, .obj, .stl, .ply,
    .usd/.usdc/.usda/.usdz, .abc, .bvh (motion capture: an armature with its motion, which
    retarget_motion can put on a rig), .blend (appends all its objects) or an image (loaded as
    an image datablock). Returns the names of the new objects with their dimensions so you can
    scale and place them.""",
    {"path": {"type": "string", "description": "Absolute file path."}},
    ["path"],
    mutates=True,
    label="Imported a file",
)
def import_file(path: str) -> Dict[str, Any]:
    path = bpy.path.abspath(path)
    if not os.path.isfile(path):
        raise ToolError(f"File not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext in _IMAGE_EXTS:
        img = bpy.data.images.load(path, check_existing=True)
        return {"image": img.name, "size": list(img.size),
                "note": "Loaded as an image datablock; wire it with set_material."}
    ensure_object_mode()
    _turn.mark_mutation()
    before = set(bpy.data.objects.keys())
    with ui_context():
        if ext in {".glb", ".gltf"}:
            bpy.ops.import_scene.gltf(filepath=path)
        elif ext == ".fbx":
            bpy.ops.import_scene.fbx(filepath=path)
        elif ext == ".obj":
            (_op("wm.obj_import") or bpy.ops.import_scene.obj)(filepath=path)
        elif ext == ".stl":
            (_op("wm.stl_import") or bpy.ops.import_mesh.stl)(filepath=path)
        elif ext == ".ply":
            (_op("wm.ply_import") or bpy.ops.import_mesh.ply)(filepath=path)
        elif ext in {".usd", ".usdc", ".usda", ".usdz"}:
            bpy.ops.wm.usd_import(filepath=path)
        elif ext == ".abc":
            bpy.ops.wm.alembic_import(filepath=path)
        elif ext == ".bvh":
            if _op("import_anim.bvh") is None:
                raise ToolError("This Blender has its BVH importer switched off (Preferences > Add-ons > BVH).")
            bpy.ops.import_anim.bvh(filepath=path, rotate_mode='QUATERNION', update_scene_fps=False,
                                    update_scene_duration=False)
        elif ext == ".blend":
            with bpy.data.libraries.load(path, link=False) as (src, dst):
                dst.objects = list(src.objects)
            for obj in dst.objects:
                if obj is not None:
                    bpy.context.scene.collection.objects.link(obj)
        else:
            raise ToolError(f"Unsupported file type '{ext}'.")
    new = [bpy.data.objects[n] for n in bpy.data.objects.keys() if n not in before]
    rows = [{"name": o.name, "type": o.type, "dimensions": [round(d, 4) for d in o.dimensions]} for o in new]
    return {"imported": rows[:80], "count": len(new)}


def _glb_summary(path: str) -> Optional[Dict[str, int]]:
    try:
        with open(path, "rb") as fh:
            magic, _version, _length = struct.unpack("<4sII", fh.read(12))
            if magic != b"glTF":
                return None
            chunk_len, _chunk_type = struct.unpack("<II", fh.read(8))
            doc = json.loads(fh.read(chunk_len))
        return {k: len(doc.get(k, [])) for k in ("nodes", "meshes", "materials", "textures", "animations", "skins")}
    except Exception:
        return None


def _needed(targets: List[bpy.types.Object]) -> List[bpy.types.Object]:
    """The objects and everything they need to look and move as they do: parents, rigs, other
    objects their modifiers and constraints use."""
    found: Dict[str, bpy.types.Object] = {}
    todo = list(targets)
    while todo:
        obj = todo.pop()
        if obj is None or obj.name in found:
            continue
        found[obj.name] = obj
        todo.append(obj.parent)
        todo += [getattr(m, "object", None) for m in obj.modifiers]
        todo += [getattr(c, "target", None) for c in obj.constraints]
    return list(found.values())


@contextlib.contextmanager
def _alone(targets: List[bpy.types.Object]):
    """A scene of their own for the export, with the scene's frame range and speed: an exporter that
    plays the animation evaluates every object of the scene on every frame. In a world of 3,500 objects
    one creature took 4.5 minutes to export and the window froze meanwhile; on its own it takes seconds.
    Yields the scene's view layer, the targets selected in it."""
    source = bpy.context.scene
    scene = bpy.data.scenes.new("_unix_export")
    try:
        for key in ("fps", "fps_base"):
            setattr(scene.render, key, getattr(source.render, key))
        scene.frame_start, scene.frame_end, scene.frame_current = source.frame_start, source.frame_end, source.frame_current
        scene.unit_settings.system = source.unit_settings.system
        scene.unit_settings.scale_length = source.unit_settings.scale_length
        for obj in _needed(targets):
            scene.collection.objects.link(obj)
        with _showing(scene):
            layer = scene.view_layers[0]
            layer.update()
            for obj in targets:
                obj.select_set(True, view_layer=layer)
            layer.objects.active = targets[0]
            yield layer
    finally:
        bpy.data.scenes.remove(scene)


@contextlib.contextmanager
def _their_actions(targets: List[bpy.types.Object]):
    """glTF: only the actions made for these objects. An action keyed on an object has a slot named for
    it ("OB" + its name); one whose slots all name other objects (another creature's clips, on a rig with
    the same bones) is left out through the exporter's own action filter. Yields whether the filter is on."""
    names = {o.name for o in bpy.data.objects}
    mine = {"OB" + o.name for o in targets}

    def theirs(action) -> bool:
        owners = [s.identifier for s in getattr(action, "slots", ())]
        return bool(owners) and all(o.startswith("OB") and o[2:] in names and o not in mine for o in owners)
    left_out = [a for a in bpy.data.actions if theirs(a)]
    if not left_out:
        yield False
        return
    from io_scene_gltf2 import GLTF2_filter_action
    registered = hasattr(bpy.types.Scene, "gltf_action_filter")
    if not registered:
        bpy.types.Scene.gltf_action_filter = bpy.props.CollectionProperty(type=GLTF2_filter_action)
        bpy.types.Scene.gltf_action_filter_active = bpy.props.IntProperty()
    items = bpy.data.scenes[0].gltf_action_filter        # where the exporter reads it
    before = [(item.action, item.keep) for item in items]
    items.clear()
    for action in bpy.data.actions:
        item = items.add()
        item.action, item.keep = action, action not in left_out
    try:
        yield True
    finally:
        items.clear()
        for action, keep in before:
            item = items.add()
            item.action, item.keep = action, keep
        if not registered:
            del bpy.types.Scene.gltf_action_filter
            del bpy.types.Scene.gltf_action_filter_active


@tool(
    "export_file",
    """Export the scene or chosen objects to .glb/.gltf, .fbx, .obj, .stl, .ply or
    .usd/.usdc/.usda and verify the file. Modifiers are applied in the export only. For .glb
    the reply lists what the file actually contains (nodes, meshes, materials, textures). Run
    check_mesh and check_materials first.""",
    {
        "path": {"type": "string", "description": "Output file path; the extension picks the format. Relative paths go to the Unix exports folder."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Objects to export, from any scene or collection: they are exported from a scene of their own (fast), and a .glb takes only the actions made for them. Never switch or build scenes for an export. Omit to export everything."},
    },
    ["path"],
    timeout=900.0,
    label="Exported a file",
)
def export_file(path: str, objects: Optional[List[str]] = None) -> Dict[str, Any]:
    if not os.path.isabs(path):
        path = os.path.join(data_dir("exports"), path)
    ext = os.path.splitext(path)[1].lower()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ensure_object_mode()
    targets = [get_object(n) for n in objects] if objects else []     # anywhere in the file: they export from a scene of their own
    use_selection = bool(targets)
    with contextlib.ExitStack() as stack:
        if use_selection:
            stack.enter_context(_alone(targets))
        filtered = stack.enter_context(_their_actions(targets)) if use_selection and ext in {".glb", ".gltf"} else False
        with ui_context():
            if ext in {".glb", ".gltf"}:
                # use_active_scene: the exporter otherwise goes through every scene of the file, and
                # took what was selected in the others too
                bpy.ops.export_scene.gltf(filepath=path, use_selection=use_selection, export_apply=True,
                                          export_format='GLB' if ext == ".glb" else 'GLTF_SEPARATE',
                                          use_active_scene=use_selection, **({"export_action_filter": True} if filtered else {}))
            elif ext == ".fbx":
                bpy.ops.export_scene.fbx(filepath=path, use_selection=use_selection, use_mesh_modifiers=True)
            elif ext == ".obj":
                bpy.ops.wm.obj_export(filepath=path, export_selected_objects=use_selection, apply_modifiers=True)
            elif ext == ".stl":
                bpy.ops.wm.stl_export(filepath=path, export_selected_objects=use_selection, apply_modifiers=True)
            elif ext == ".ply":
                bpy.ops.wm.ply_export(filepath=path, export_selected_objects=use_selection, apply_modifiers=True)
            elif ext in {".usd", ".usdc", ".usda"}:
                bpy.ops.wm.usd_export(filepath=path, selected_objects_only=use_selection)
            else:
                raise ToolError(f"Unsupported export type '{ext}'. Use .glb, .gltf, .fbx, .obj, .stl, .ply or .usd.")
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ToolError("The exporter ran but wrote no file. Check the object selection and path.")
    out: Dict[str, Any] = {"saved": path, "bytes": os.path.getsize(path),
                           "exported": [o.name for o in targets] if targets else "entire scene"}
    if ext == ".glb":
        out["file_contains"] = _glb_summary(path)
    return out


def _to_disk(path: str) -> None:
    """Have Windows write the copy out now. Twice (4 and 5 Nov) the PC lost power seconds after a safety save:
    the file had its full size but its end was still only in memory and came back as zeros, so the newest
    save could not be opened. Off the main thread: a 200 MB scene takes a second or two."""
    try:
        with open(path, "rb+") as fh:
            os.fsync(fh.fileno())
    except OSError:
        pass


KEEP_SNAPSHOTS = 20         # copies kept at most
SNAPSHOTS_GB = 12.0         # and all of them together at most this big
FREE_GB = 4.0               # and the disk keeps this much free after a copy
GB = 1024 ** 3


def _make_room(folder: str, coming: int) -> bool:
    """Drop the oldest copies until the next one (``coming`` bytes) fits the limits; the newest three always stay.
    False when even then the disk could not take it. On 5 Nov the copies of a scene that had grown to 2 GB, twenty
    of them kept by count alone, filled the disk (25 GB) and the next saves failed."""
    blends = sorted(f for f in os.listdir(folder) if f.endswith(".blend"))
    sizes = {}
    for name in blends:
        try:
            sizes[name] = os.path.getsize(os.path.join(folder, name))
        except OSError:
            sizes[name] = 0
    while len(blends) > 3:
        free = shutil.disk_usage(folder).free
        if len(blends) < KEEP_SNAPSHOTS and sum(sizes[b] for b in blends) + coming <= SNAPSHOTS_GB * GB                 and free - coming >= FREE_GB * GB:
            break
        old = blends.pop(0)
        for suffix in ("", "1"):
            try:
                os.remove(os.path.join(folder, old + suffix))
            except OSError:
                pass
    return shutil.disk_usage(folder).free - coming >= GB


def snapshot(label: str = "") -> Optional[str]:
    """Save a copy of the current .blend (the open file's path is unchanged). Main thread. None when it could not
    be saved, also when the disk has no room for it even after the oldest copies are dropped."""
    folder = data_dir("snapshots")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:32]
    path = os.path.join(folder, time.strftime("%Y%m%d_%H%M%S") + (f"_{safe}" if safe else "") + ".blend")
    newest = max((os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".blend")),
                 key=os.path.getmtime, default=None)
    try:
        coming = os.path.getsize(newest) if newest else 0      # the scene's last copy: about what this one takes
    except OSError:
        coming = 0
    if not _make_room(folder, int(coming * 1.1)):
        print(f"[Unix] no room on the disk for a {coming / GB:.1f} GB copy of the scene: not saved")
        return None
    try:
        with ui_context():
            bpy.ops.wm.save_as_mainfile(filepath=path, copy=True, check_existing=False)
    except Exception:
        return None
    threading.Thread(target=_to_disk, args=(path,), daemon=True, name="UnixFlush").start()
    from .. import recovery
    recovery.saved_now()                    # a copy of the scene was just made: no safety save of the same scene follows it
    _make_room(folder, 0)
    return path


@tool(
    "save_snapshot",
    """Save a safety copy of the current .blend to the Unix snapshots folder without changing
    the open file. One is taken automatically before each turn; call this before a risky or
    destructive step. Returns the snapshot path to mention to the user.""",
    {"label": {"type": "string", "description": "Short label for the snapshot file name."}},
    label="Saved a snapshot",
)
def save_snapshot(label: str = "") -> Dict[str, Any]:
    path = snapshot(label)
    if path is None:
        raise ToolError("Could not save a snapshot.")
    return {"snapshot": path}
