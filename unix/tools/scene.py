# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Read-only scene inspection: the model's eyes on structure and numbers."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import bpy
from mathutils import Vector

from ._util import degrees, get_object, r, vec
from .registry import tool

_GEOMETRY_TYPES = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME', 'GREASEPENCIL'}


def world_bounds(obj: bpy.types.Object) -> Optional[Dict[str, List[float]]]:
    if not obj.bound_box or obj.type not in _GEOMETRY_TYPES | {'ARMATURE', 'LATTICE'}:
        return None
    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    lo = Vector((min(c.x for c in corners), min(c.y for c in corners), min(c.z for c in corners)))
    hi = Vector((max(c.x for c in corners), max(c.y for c in corners), max(c.z for c in corners)))
    return {"min": vec(lo, 3), "max": vec(hi, 3)}


def _brief(obj: bpy.types.Object, selected: set) -> Dict[str, Any]:
    row: Dict[str, Any] = {"name": obj.name, "type": obj.type, "loc": vec(obj.matrix_world.translation, 3)}
    if obj.type in _GEOMETRY_TYPES:
        row["dims"] = vec(obj.dimensions, 3)
    if obj.type == 'MESH':
        row["verts"], row["faces"] = len(obj.data.vertices), len(obj.data.polygons)
    if obj.parent:
        row["parent"] = obj.parent.name
    if obj.users_collection:
        row["collection"] = obj.users_collection[0].name
    mats = [s.material.name for s in obj.material_slots if s.material]
    if mats:
        row["materials"] = mats
    if obj.modifiers:
        row["modifiers"] = [m.type for m in obj.modifiers]
    flags = [f for f, on in (("hidden_viewport", obj.hide_get() if obj.name in bpy.context.view_layer.objects else False),
                              ("hidden_render", obj.hide_render), ("selected", obj.name in selected)) if on]
    if flags:
        row["flags"] = flags
    return row


def _collection_tree(col: bpy.types.Collection, depth: int = 0) -> Dict[str, Any]:
    node: Dict[str, Any] = {"name": col.name, "objects": len(col.objects)}
    if col.children and depth < 6:
        node["children"] = [_collection_tree(c, depth + 1) for c in col.children]
    return node


@tool(
    "scene_info",
    """Read-only overview of the current Blender scene: units, frame range, render settings,
    collection tree, and a table of objects (name, type, world location, dimensions in meters,
    vertex/face counts, materials, modifiers). Call this first on every task and again after
    large changes. Use the filters on big scenes instead of asking for everything.""",
    {
        "name_contains": {"type": "string", "description": "Only objects whose name contains this text (case-insensitive)."},
        "type": {"type": "string", "description": "Only this object type, e.g. MESH, LIGHT, CAMERA, EMPTY, ARMATURE, CURVE."},
        "collection": {"type": "string", "description": "Only objects in this collection (direct members)."},
        "limit": {"type": "integer", "description": "Max objects to list (default 80)."},
    },
    label="Read the scene",
)
def scene_info(name_contains: str = "", type: str = "", collection: str = "", limit: int = 80) -> Dict[str, Any]:
    scene = bpy.context.scene
    selected = {o.name for o in bpy.context.view_layer.objects if o.select_get()}
    objs = list(scene.objects)
    counts: Dict[str, int] = {}
    for o in objs:
        counts[o.type] = counts.get(o.type, 0) + 1
    if name_contains:
        objs = [o for o in objs if name_contains.lower() in o.name.lower()]
    if type:
        objs = [o for o in objs if o.type == type.upper()]
    if collection:
        objs = [o for o in objs if any(c.name == collection for c in o.users_collection)]
    objs.sort(key=lambda o: o.name)
    limit = max(1, min(int(limit), 400))
    tris = sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in scene.objects if o.type == 'MESH')
    active = bpy.context.view_layer.objects.active
    info: Dict[str, Any] = {
        "blender": bpy.app.version_string,
        "file": bpy.data.filepath or "(unsaved)",
        "scene": scene.name,
        "units": {"system": scene.unit_settings.system, "scale_length": r(scene.unit_settings.scale_length),
                  "note": "1 Blender unit = 1 meter unless scale_length differs"},
        "frames": {"start": scene.frame_start, "end": scene.frame_end, "current": scene.frame_current,
                   "fps": r(scene.render.fps / scene.render.fps_base, 3)},
        "render": {"engine": scene.render.engine, "resolution": [scene.render.resolution_x, scene.render.resolution_y],
                   "percentage": scene.render.resolution_percentage,
                   "camera": scene.camera.name if scene.camera else None},
        "world": scene.world.name if scene.world else None,
        "counts": counts,
        "total_triangles": tris,
        "active": active.name if active else None,
        "mode": active.mode if active else "OBJECT",
        "selected": sorted(selected)[:50],
        "collections": _collection_tree(scene.collection),
        "objects": [_brief(o, selected) for o in objs[:limit]],
    }
    if len(objs) > limit:
        info["objects_truncated"] = f"showing {limit} of {len(objs)}; filter with name_contains/type/collection"
    return info


def _modifier_props(mod: bpy.types.Modifier) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": mod.name, "type": mod.type, "show_viewport": mod.show_viewport}
    for prop in mod.bl_rna.properties:
        ident = prop.identifier
        if ident in {"rna_type", "name", "type", "show_viewport", "show_render", "show_in_editmode",
                     "show_on_cage", "show_expanded", "is_active", "use_pin_to_last", "persistent_uid",
                     "execution_time", "is_override_data", "use_apply_on_spline"}:
            continue
        try:
            value = getattr(mod, ident)
        except Exception:
            continue
        if prop.type in {'BOOLEAN', 'INT', 'FLOAT', 'ENUM', 'STRING'} and not getattr(prop, "is_array", False):
            out[ident] = r(value) if prop.type == 'FLOAT' else value
        elif prop.type == 'POINTER' and value is not None and hasattr(value, "name"):
            out[ident] = value.name
        if len(out) > 22:
            break
    return out


def _material_summary(mat: bpy.types.Material) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": mat.name, "users": mat.users}
    if not mat.use_nodes or mat.node_tree is None:
        out["nodes"] = False
        return out
    bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is not None:
        values = {}
        for key in ("Base Color", "Metallic", "Roughness", "Alpha", "Emission Strength", "Transmission Weight", "IOR"):
            sock = bsdf.inputs.get(key)
            if sock is None:
                continue
            if sock.is_linked:
                src = sock.links[0].from_node
                values[key] = f"<{src.type}:{getattr(getattr(src, 'image', None), 'name', src.name)}>"
            else:
                dv = sock.default_value
                values[key] = vec(dv, 3) if hasattr(dv, "__len__") else r(dv, 3)
        out["principled"] = values
    images = [{"image": n.image.name, "colorspace": n.image.colorspace_settings.name,
               "size": list(n.image.size)} for n in mat.node_tree.nodes if n.type == 'TEX_IMAGE' and n.image]
    if images:
        out["images"] = images
    out["node_count"] = len(mat.node_tree.nodes)
    return out


@tool(
    "object_info",
    """Full detail for one object: transforms, world bounding box, mesh statistics (verts, edges,
    faces, triangles, n-gons, UV layers, vertex groups, shape keys), modifier settings, material
    values and textures, constraints, animation, children and custom properties.""",
    {"name": {"type": "string", "description": "Exact object name."}},
    ["name"],
    label="Inspected an object",
)
def object_info(name: str) -> Dict[str, Any]:
    obj = get_object(name)
    info: Dict[str, Any] = {
        "name": obj.name, "type": obj.type,
        "data": obj.data.name if obj.data else None,
        "data_users": obj.data.users if obj.data else 0,
        "location": vec(obj.location), "rotation_deg": degrees(obj.rotation_euler), "scale": vec(obj.scale),
        "world_location": vec(obj.matrix_world.translation),
        "world_rotation_deg": degrees(obj.matrix_world.to_euler()),
        "world_scale": vec(obj.matrix_world.to_scale()),
        "dimensions": vec(obj.dimensions),
        "world_bounds": world_bounds(obj),
        "parent": obj.parent.name if obj.parent else None,
        "children": [c.name for c in obj.children][:60],
        "collections": [c.name for c in obj.users_collection],
        "visible": {"viewport": not obj.hide_viewport, "render": not obj.hide_render},
    }
    chain, ancestor = [], obj.parent
    while ancestor is not None and len(chain) < 12:
        chain.append(ancestor.name)
        ancestor = ancestor.parent
    if chain:
        info["parent_chain"] = chain
    world_rot, world_scale = obj.matrix_world.to_quaternion(), obj.matrix_world.to_scale()
    if world_rot.angle > 1e-4 or any(abs(s - 1.0) > 1e-4 for s in world_scale):
        info["transform_warning"] = (
            "This object is rotated or scaled in the world (by its own transform or by its parents), so its vertex "
            "coordinates are NOT world coordinates. Use obj.matrix_world @ v.co for world positions, and bind rigs with "
            "rig_bind. Never re-parent it by script without restoring matrix_world afterwards.")
    if obj.type == 'MESH':
        me = obj.data
        sizes = [len(p.vertices) for p in me.polygons]
        info["mesh"] = {
            "verts": len(me.vertices), "edges": len(me.edges), "faces": len(me.polygons),
            "triangles": sum(s - 2 for s in sizes), "tris": sizes.count(3), "quads": sizes.count(4),
            "ngons": sum(1 for s in sizes if s > 4),
            "uv_layers": [u.name for u in me.uv_layers],
            "color_attributes": [a.name for a in me.color_attributes],
            "vertex_groups": [g.name for g in obj.vertex_groups][:60],
            "shape_keys": [k.name for k in me.shape_keys.key_blocks] if me.shape_keys else [],
            "smooth_faces": sum(1 for p in me.polygons if p.use_smooth),
        }
    elif obj.type == 'ARMATURE':
        info["armature"] = {"bones": len(obj.data.bones), "names": [b.name for b in obj.data.bones][:80]}
    elif obj.type == 'LIGHT':
        li = obj.data
        info["light"] = {"type": li.type, "energy": r(li.energy), "color": vec(li.color, 3),
                         "size": r(getattr(li, "shadow_soft_size", 0.0))}
    elif obj.type == 'CAMERA':
        cam = obj.data
        info["camera"] = {"type": cam.type, "lens_mm": r(cam.lens), "ortho_scale": r(cam.ortho_scale),
                          "clip": [r(cam.clip_start), r(cam.clip_end)], "sensor_width": r(cam.sensor_width),
                          "dof": cam.dof.use_dof}
    if obj.modifiers:
        info["modifiers"] = [_modifier_props(m) for m in obj.modifiers]
    if obj.material_slots:
        info["materials"] = [(_material_summary(s.material) if s.material else {"name": None, "empty_slot": True})
                             for s in obj.material_slots]
    if obj.constraints:
        info["constraints"] = [{"name": c.name, "type": c.type,
                                "target": getattr(getattr(c, "target", None), "name", None)} for c in obj.constraints]
    anim = obj.animation_data
    if anim and anim.action:
        info["animation"] = {"action": anim.action.name, "frame_range": vec(anim.action.frame_range, 1)}
    custom = {k: (v if isinstance(v, (int, float, str, bool)) else str(v)) for k, v in obj.items()
              if not k.startswith("_")}
    if custom:
        info["custom_properties"] = dict(list(custom.items())[:30])
    return info
