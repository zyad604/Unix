# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Organic modelling: turn a blockout of separate parts into one continuous body.

A model builds a character out of spheres, capsules and boxes. Left like that it reads as a
toy: every joint is a visible seam between two shapes, and smoothing modifiers shatter the
pieces instead of blending them. ``fuse_meshes`` does what a sculptor does after blocking
out: it merges the parts into a single watertight skin (voxel remesh) and relaxes the
creases, so the result can be detailed, textured and rigged as one body.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import bmesh
import bpy
from mathutils import Vector

from . import _turn
from ._util import ensure_object_mode, get_object, r, vec
from .registry import ToolError, tool


@tool(
    "fuse_meshes",
    """Fuse separate mesh parts (a blockout of spheres, capsules, boxes, limbs) into ONE
    continuous, watertight mesh with the joints blended. Use it for every character, creature
    or other organic shape before detailing: separate primitives never look real, and Smooth or
    Subdivision modifiers on separate pieces pull them apart. Parts must overlap where they
    join. Modifiers on the parts are applied in the copy. The parts are kept, hidden, in a
    '<name>_parts' collection. The fused mesh has no UVs: use a procedural or box-projected
    material, or unwrap it afterwards. Then rig it with find_limbs, rig_chain and rig_bind.""",
    {
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Mesh objects to fuse (at least one)."},
        "name": {"type": "string", "description": "Name of the fused object."},
        "voxel_m": {"type": "number", "description": "Detail size in metres. Default: the model's size / 220. Smaller keeps finer "
                                                     "shapes (fingers, nose) and costs more triangles; fingers need about a "
                                                     "third of their thickness."},
        "smooth": {"type": "integer", "description": "Relaxing passes that blend the creases between parts, 0-12 (default 4)."},
        "max_triangles": {"type": "integer", "description": "Triangle budget for the result (default 60000). The fused mesh is "
                                                              "reduced to it while keeping its shape. Raise it only for a close-up hero."},
    },
    ["objects", "name"],
    timeout=900.0,
    mutates=True,
    label="Fused parts into one mesh",
)
def fuse_meshes(objects: List[str], name: str, voxel_m: Optional[float] = None, smooth: int = 4,
                max_triangles: int = 60_000) -> Dict[str, Any]:
    ensure_object_mode()
    parts = [get_object(n) for n in objects]
    bad = [o.name for o in parts if o.type != 'MESH']
    if bad or not parts:
        raise ToolError(f"These are not mesh objects: {', '.join(bad)}" if bad else "Name at least one mesh.")
    name = name.strip()
    if not name:
        raise ToolError("Give the fused object a name.")
    existing = bpy.data.objects.get(name)
    if existing is not None and existing not in parts:
        raise ToolError(f"An object named '{name}' already exists. Choose another name, or include it in `objects`.")
    smooth = max(0, min(int(smooth), 12))
    _turn.mark_mutation()

    depsgraph = bpy.context.evaluated_depsgraph_get()
    merged = bmesh.new()
    for part in parts:
        evaluated = part.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        mesh.transform(evaluated.matrix_world)
        merged.from_mesh(mesh)
        evaluated.to_mesh_clear()
    if not merged.faces:
        merged.free()
        raise ToolError("The parts have no faces.")
    lo = Vector((min(v.co.x for v in merged.verts), min(v.co.y for v in merged.verts), min(v.co.z for v in merged.verts)))
    hi = Vector((max(v.co.x for v in merged.verts), max(v.co.y for v in merged.verts), max(v.co.z for v in merged.verts)))
    size = max((hi - lo).length, 1e-6)
    voxel = float(voxel_m) if voxel_m else size / 220.0
    voxel = max(voxel, size / 700.0)          # a finer grid than this takes minutes and millions of triangles
    if voxel > size / 12.0:
        merged.free()
        raise ToolError(f"voxel_m {voxel} is too coarse for a model {r(size, 3)} m across; try {r(size / 220.0, 4)}.")
    source = bpy.data.meshes.new(name + "_source")
    merged.to_mesh(source)
    merged.free()

    materials = [slot.material for part in parts for slot in part.material_slots if slot.material]
    home = parts[0].users_collection[0] if parts[0].users_collection else bpy.context.scene.collection
    temp = bpy.data.objects.new(name + "_fusing", source)
    home.objects.link(temp)
    remesh = temp.modifiers.new("Remesh", 'REMESH')
    remesh.mode = 'VOXEL'
    remesh.voxel_size = voxel
    remesh.adaptivity = 0.0
    remesh.use_smooth_shade = True
    bpy.context.view_layer.update()
    fused = bpy.data.meshes.new_from_object(temp.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    bpy.data.objects.remove(temp, do_unlink=True)
    bpy.data.meshes.remove(source)
    if not fused.polygons:
        bpy.data.meshes.remove(fused)
        raise ToolError("Nothing was left after fusing: voxel_m is larger than the parts. Use a smaller voxel_m.")

    if smooth:
        bm = bmesh.new()
        bm.from_mesh(fused)
        for _pass in range(smooth):
            bmesh.ops.smooth_vert(bm, verts=bm.verts[:], factor=0.5, use_axis_x=True, use_axis_y=True, use_axis_z=True)
        bm.to_mesh(fused)
        bm.free()
    for poly in fused.polygons:
        poly.use_smooth = True
    fused.name = name
    if materials:
        fused.materials.append(materials[0])

    # keep the blockout, out of the way
    stash = bpy.data.collections.get(name + "_parts") or bpy.data.collections.new(name + "_parts")
    if stash.name not in {c.name for c in bpy.context.scene.collection.children_recursive}:
        home.children.link(stash)
    for part in parts:
        if part.name == name:
            part.name = name + "_blockout"
        for coll in list(part.users_collection):
            coll.objects.unlink(part)
        stash.objects.link(part)
        part.hide_viewport = part.hide_render = True
    result = bpy.data.objects.new(name, fused)
    home.objects.link(result)
    bpy.context.view_layer.update()
    triangles = sum(len(p.vertices) - 2 for p in fused.polygons)
    budget = max(2_000, int(max_triangles))
    reduced_from = None
    if triangles > budget:              # the voxel grid is even everywhere; most of it is flat and can go
        reduce = result.modifiers.new("Budget", 'DECIMATE')
        reduce.ratio = budget / triangles
        bpy.context.view_layer.update()
        lighter = bpy.data.meshes.new_from_object(result.evaluated_get(bpy.context.evaluated_depsgraph_get()))
        result.modifiers.remove(reduce)
        result.data = lighter
        bpy.data.meshes.remove(fused)
        fused, reduced_from = lighter, triangles
        fused.name = name
        for poly in fused.polygons:
            poly.use_smooth = True
        bpy.context.view_layer.update()
    return {"object": result.name, "vertices": len(fused.vertices), "triangles": sum(len(p.vertices) - 2 for p in fused.polygons),
            **({"reduced_from_triangles": reduced_from} if reduced_from else {}),
            "voxel_m": r(voxel, 5), "dimensions_m": vec(result.dimensions, 3), "parts_hidden_in": stash.name,
            "material": materials[0].name if materials else None,
            "next": "look at it from several sides. Thin parts that vanished or merged (fingers, ears) need a smaller voxel_m; "
                    "lumpy joins need more overlap in the blockout or more smooth passes. It has no UVs: use box-projected "
                    "or procedural textures. To change the shape, unhide and edit the parts, then fuse again."}
