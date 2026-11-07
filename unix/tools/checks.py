# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Measured quality checks. Each returns numbers plus a plain `issues` list.

These are the objective half of verification (``look`` is the visual half). They read
the scene and never change it.
"""

from __future__ import annotations

import math
import os
import re
import statistics
from typing import Any, Dict, List, Optional

import bmesh
import bpy
from bpy_extras import mesh_utils

from ._util import ensure_object_mode, r, resolve_objects
from .registry import tool

_MAX_OBJECTS = 40
_DEFAULT_NAME = re.compile(
    r"^(Cube|Sphere|Icosphere|Cylinder|Cone|Torus|Plane|Circle|Grid|Suzanne|Empty|Light|Point|Sun|Spot|Area|"
    r"Camera|BezierCurve|NurbsPath|Text|Armature|Mball)(\.\d{3})?$")


def _mesh_objects(names: Optional[List[str]]) -> List[bpy.types.Object]:
    return resolve_objects(names, {'MESH'}, default_visible=False)[:_MAX_OBJECTS]


@tool(
    "check_mesh",
    """Topology report for mesh objects: non-manifold and boundary edges, loose vertices/edges,
    triangles / quads / n-gons, zero-area faces, duplicate vertices, inward-facing normals,
    high-valence poles and unapplied scale. Run after modeling and before export or 3D print.""",
    {"objects": {"type": "array", "items": {"type": "string"},
                 "description": "Mesh object names. Omit to check every mesh in the scene (max 40)."}},
    label="Checked topology",
)
def check_mesh(objects: Optional[List[str]] = None) -> Dict[str, Any]:
    ensure_object_mode()
    reports = []
    for obj in _mesh_objects(objects):
        bm = bmesh.new()
        bm.from_mesh(obj.data)
        bm.normal_update()
        sizes = [len(f.verts) for f in bm.faces]
        non_manifold = sum(1 for e in bm.edges if not e.is_manifold and not e.is_boundary and not e.is_wire)
        boundary = sum(1 for e in bm.edges if e.is_boundary)
        wire = sum(1 for e in bm.edges if e.is_wire)
        loose_verts = sum(1 for v in bm.verts if not v.link_edges)
        zero_area = sum(1 for f in bm.faces if f.calc_area() < 1e-10)
        doubles = len(bmesh.ops.find_doubles(bm, verts=bm.verts, dist=1e-5)["targetmap"]) if len(bm.verts) < 400_000 else -1
        poles = sum(1 for v in bm.verts if len(v.link_edges) >= 6)
        closed = bool(bm.faces) and boundary == 0 and non_manifold == 0 and wire == 0
        inward = closed and bm.calc_volume(signed=True) < 0
        rep: Dict[str, Any] = {
            "object": obj.name, "verts": len(bm.verts), "edges": len(bm.edges), "faces": len(bm.faces),
            "tris": sizes.count(3), "quads": sizes.count(4), "ngons": sum(1 for s in sizes if s > 4),
            "non_manifold_edges": non_manifold, "boundary_edges": boundary, "wire_edges": wire,
            "loose_verts": loose_verts, "zero_area_faces": zero_area, "duplicate_verts": doubles,
            "poles_6plus": poles, "watertight": closed, "scale": [r(s, 4) for s in obj.scale],
        }
        if closed:
            rep["volume_m3"] = r(abs(bm.calc_volume(signed=True)) * abs(obj.scale.x * obj.scale.y * obj.scale.z), 6)
        issues = []
        if not bm.faces:
            issues.append("mesh has no faces")
        if non_manifold:
            issues.append(f"{non_manifold} non-manifold edges (shared by 3+ faces or interior faces)")
        if loose_verts or wire:
            issues.append(f"{loose_verts} loose verts, {wire} wire edges")
        if zero_area:
            issues.append(f"{zero_area} zero-area faces")
        if doubles > 0:
            issues.append(f"{doubles} duplicate verts within 0.01 mm (merge by distance)")
        if inward:
            issues.append("normals point inward (recalculate outside)")
        if any(abs(s - 1.0) > 1e-4 for s in obj.scale):
            issues.append("unapplied scale (modifiers, bevels and UV density will be skewed)")
        if any(s < 0 for s in obj.scale):
            issues.append("negative scale (normals flip on export)")
        rep["issues"] = issues
        bm.free()
        reports.append(rep)
    return {"checked": len(reports), "with_issues": sum(1 for x in reports if x["issues"]), "objects": reports}


def _uv_area(coords: List) -> float:
    return abs(sum(coords[i][0] * coords[(i + 1) % len(coords)][1] - coords[(i + 1) % len(coords)][0] * coords[i][1]
                   for i in range(len(coords)))) / 2.0


@tool(
    "check_uv",
    """UV report for mesh objects: UV layers, island count, share of UVs outside the 0-1 tile,
    zero-area UV faces, used UV space, texel-density spread across faces (stretch indicator) and,
    when texture_size is given, texel density in pixels per meter. Island overlap is estimated
    from island bounding boxes, so treat `overlap_candidates` as places to look, not proof.""",
    {
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Mesh names. Omit for all meshes (max 40)."},
        "texture_size": {"type": "integer", "description": "Texture edge in pixels (e.g. 2048) to report px/m density."},
    },
    label="Checked UVs",
)
def check_uv(objects: Optional[List[str]] = None, texture_size: int = 0) -> Dict[str, Any]:
    ensure_object_mode()
    reports = []
    for obj in _mesh_objects(objects):
        me = obj.data
        rep: Dict[str, Any] = {"object": obj.name, "uv_layers": [u.name for u in me.uv_layers]}
        issues: List[str] = []
        layer = me.uv_layers.active
        if layer is None or not me.polygons:
            issues.append("no UV layer" if layer is None else "no faces")
            rep["issues"] = issues
            reports.append(rep)
            continue
        uvs = [d.uv for d in layer.data]
        outside = sum(1 for uv in uvs if uv.x < -1e-4 or uv.x > 1.0001 or uv.y < -1e-4 or uv.y > 1.0001)
        sx, sy, sz = (abs(s) for s in obj.scale)
        ratios, zero, used = [], 0, 0.0
        for poly in me.polygons:
            a_uv = _uv_area([uvs[i] for i in poly.loop_indices])
            used += a_uv
            a_3d = poly.area * ((sx * sy + sy * sz + sx * sz) / 3.0)
            if a_uv < 1e-12:
                zero += 1
            elif a_3d > 1e-12:
                ratios.append(math.sqrt(a_uv / a_3d))
        islands = mesh_utils.mesh_linked_uv_islands(me) if len(me.polygons) < 300_000 else []
        boxes = []
        for island in islands[:600]:
            pts = [uvs[i] for p in island for i in me.polygons[p].loop_indices]
            boxes.append((min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts)))
        overlaps = 0
        if len(boxes) <= 300:
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    a, b = boxes[i], boxes[j]
                    if min(a[2], b[2]) - max(a[0], b[0]) > 1e-4 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-4:
                        overlaps += 1
        rep.update({
            "active_layer": layer.name, "islands": len(islands),
            "outside_0_1_pct": r(100.0 * outside / max(1, len(uvs)), 2),
            "zero_area_uv_faces": zero, "uv_space_used_pct": r(min(used, 9.99) * 100.0, 1),
            "overlap_candidates": overlaps,
        })
        if ratios:
            med = statistics.median(ratios)
            spread = (max(ratios) / max(min(ratios), 1e-9))
            rep["density_spread_max_over_min"] = r(spread, 2)
            if texture_size:
                rep["texel_density_px_per_m"] = r(med * texture_size, 1)
            if spread > 4.0:
                issues.append(f"texel density varies {spread:.1f}x across faces (stretching or uneven island scale)")
        if zero:
            issues.append(f"{zero} faces have zero UV area (unwrapped to a point or line)")
        if outside:
            issues.append(f"{rep['outside_0_1_pct']}% of UVs lie outside the 0-1 tile")
        if overlaps:
            issues.append(f"{overlaps} island bounding boxes overlap (verify; intended for mirrored/stacked islands)")
        rep["issues"] = issues
        reports.append(rep)
    return {"checked": len(reports), "with_issues": sum(1 for x in reports if x["issues"]), "objects": reports}


_NON_COLOR_INPUTS = {"Roughness", "Metallic", "Normal", "Alpha", "Specular IOR Level", "Specular", "Height",
                     "Transmission Weight", "Coat Weight", "Sheen Weight"}


def _feeds(node: bpy.types.Node, depth: int = 0) -> List[str]:
    """Names of shader inputs this node's outputs reach (through helper nodes)."""
    found: List[str] = []
    if depth > 4:
        return found
    for output in node.outputs:
        for link in output.links:
            target = link.to_node
            if target.type in {'BSDF_PRINCIPLED', 'DISPLACEMENT', 'EMISSION', 'BSDF_DIFFUSE', 'BSDF_GLOSSY'}:
                found.append(link.to_socket.name)
            elif target.type == 'NORMAL_MAP' or target.type == 'BUMP':
                found.append("Normal")
            else:
                found.extend(_feeds(target, depth + 1))
    return found


@tool(
    "check_materials",
    """Material report: objects with no material or empty slots, materials without a Principled
    BSDF, image textures that are missing on disk, colour-space mistakes (data maps such as
    normal/roughness/metallic must be Non-Color, base colour must be sRGB) and unused materials.""",
    {"objects": {"type": "array", "items": {"type": "string"}, "description": "Object names. Omit for all meshes (max 40)."}},
    label="Checked materials",
)
def check_materials(objects: Optional[List[str]] = None) -> Dict[str, Any]:
    issues: List[str] = []
    rows = []
    seen: Dict[str, Dict[str, Any]] = {}
    for obj in resolve_objects(objects, {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT'}, default_visible=False)[:_MAX_OBJECTS]:
        slots = [s.material.name if s.material else None for s in obj.material_slots]
        rows.append({"object": obj.name, "slots": slots})
        if not slots:
            issues.append(f"{obj.name}: no material")
        elif None in slots:
            issues.append(f"{obj.name}: {slots.count(None)} empty material slot(s)")
        for slot in obj.material_slots:
            mat = slot.material
            if mat is None or mat.name in seen:
                continue
            info: Dict[str, Any] = {"name": mat.name, "textures": []}
            seen[mat.name] = info
            if not mat.use_nodes or mat.node_tree is None:
                issues.append(f"material '{mat.name}': not node-based")
                continue
            if not any(n.type == 'BSDF_PRINCIPLED' for n in mat.node_tree.nodes):
                issues.append(f"material '{mat.name}': no Principled BSDF (exports poorly to glTF/FBX)")
            for node in mat.node_tree.nodes:
                if node.type != 'TEX_IMAGE':
                    continue
                img = node.image
                if img is None:
                    issues.append(f"material '{mat.name}': image node '{node.name}' has no image")
                    continue
                space = img.colorspace_settings.name
                feeds = _feeds(node)
                info["textures"].append({"image": img.name, "colorspace": space, "feeds": sorted(set(feeds))})
                path = bpy.path.abspath(img.filepath) if img.filepath else ""
                if img.source == 'FILE' and not img.packed_file and (not path or not os.path.exists(path)):
                    issues.append(f"material '{mat.name}': texture '{img.name}' is missing on disk ({img.filepath or 'no path'})")
                if any(f in _NON_COLOR_INPUTS for f in feeds) and space not in {"Non-Color", "Raw", "Linear Rec.709", "Linear"}:
                    issues.append(f"material '{mat.name}': '{img.name}' drives {sorted(set(feeds))} but is {space}; set Non-Color")
                if "Base Color" in feeds and space in {"Non-Color", "Raw"}:
                    issues.append(f"material '{mat.name}': base colour map '{img.name}' is {space}; set sRGB")
    unused = sorted(m.name for m in bpy.data.materials if m.users == 0)
    return {"objects": rows, "materials": list(seen.values()), "unused_materials": unused[:40], "issues": issues}


@tool(
    "check_scene",
    """Scene report. Geometry problems, measured: surfaces of different objects lying in the same
    plane (they flicker: z-fighting), assets standing inside each other, assets hanging in the
    air. An inventory: how many assets of each kind there are, to compare with what was asked.
    Hygiene: default names, unapplied scale, absurd sizes, missing camera or lights, identical
    objects stacked in one place, visibility mismatches, orphan data. Run it before reporting
    any build as finished and fix every geometry problem it lists; the same check runs by itself
    after your turn and sends you back if something is left.""",
    label="Checked the scene",
)
def check_scene():
    # Steps (see registry): the geometry is measured in slices, so the window keeps answering on a big
    # scene. Everything that needs Blender's objects is read before the first pause; afterwards only
    # names and numbers are held.
    scene = bpy.context.scene
    issues: List[str] = []
    objs = list(scene.objects)
    default_named = [o.name for o in objs if _DEFAULT_NAME.match(o.name)]
    if default_named:
        issues.append(f"{len(default_named)} default-named objects: {', '.join(default_named[:10])}")
    scaled = [o.name for o in objs if o.type == 'MESH' and any(abs(s - 1.0) > 1e-4 for s in o.scale)]
    if scaled:
        issues.append(f"{len(scaled)} meshes with unapplied scale: {', '.join(scaled[:10])}")
    far = [o.name for o in objs if o.matrix_world.translation.length > 1000.0]
    if far:
        issues.append(f"{len(far)} objects more than 1 km from the origin: {', '.join(far[:6])}")
    for o in objs:
        if o.type == 'MESH' and o.data.polygons:
            biggest = max(o.dimensions)
            if biggest > 2000.0 or 0 < biggest < 0.0005:
                issues.append(f"{o.name}: largest dimension {biggest:.4g} m looks wrong for real-world scale")
    for o in objs:                       # a triangle budget: detail nobody can see costs every frame and every export
        if o.type == 'MESH' and len(o.data.polygons) > 40_000:
            triangles = _triangles(o.data)
            if triangles > 100_000:
                ratio = round(50_000 / triangles, 2)
                how = (f"reduce it with a Decimate modifier (ratio about {ratio}) and look again" if ratio >= 0.1 else
                       "thinning it that far would wreck its shape (leaves and thin parts turn to shreds), so rebuild it with "
                       "fewer subdivisions and segments, or use a lighter model in its place")
                issues.append(f"{o.name} has {triangles:,} triangles. A detailed prop needs 10,000 to 50,000 and a hero "
                              f"character under 100,000: {how}, unless the user asked for film detail.")
    stacked: Dict[tuple, List[str]] = {}
    for o in objs:
        if o.type == 'MESH':
            key = (o.data.name if o.data.users > 1 else len(o.data.vertices),
                   tuple(round(v, 3) for v in o.matrix_world.translation), tuple(round(v, 3) for v in o.dimensions))
            stacked.setdefault(key, []).append(o.name)
    dupes = [names for names in stacked.values() if len(names) > 1]
    if dupes:
        issues.append(f"{len(dupes)} groups of identical objects in the same place, e.g. {dupes[0][:4]}")
    if scene.camera is None:
        issues.append("no active camera (needed for render_image and shot checks)")
    lights = [o for o in objs if o.type == 'LIGHT']
    world_lit = bool(scene.world and scene.world.use_nodes)
    if not lights and not world_lit:
        issues.append("no lights and no world lighting (rendered views will be black)")
    mismatch = [o.name for o in objs if o.hide_render != o.hide_viewport]
    if mismatch:
        issues.append(f"{len(mismatch)} objects visible in only one of viewport/render: {', '.join(mismatch[:8])}")
    orphans = {kind: sum(1 for d in getattr(bpy.data, kind) if d.users == 0)
               for kind in ("meshes", "materials", "images", "curves", "node_groups", "cameras", "lights")}
    orphans = {k: v for k, v in orphans.items() if v}
    camera = scene.camera.name if scene.camera else None
    total_triangles = sum(_triangles(o.data) for o in objs if o.type == 'MESH')
    lights_count = len(lights)
    object_count = len(objs)
    del objs, lights, scene             # Blender objects are not held across the pauses below
    from . import _turn, quality
    touched = _turn.touched()
    yield
    apart = yield from quality.separate_steps(None, touched)      # surfaces sharing a plane are taken off it here and now
    if apart:
        _turn.mark_mutation()
    geometry = yield from quality.audit_steps(None)
    yield
    counted = quality.inventory(None)
    issues = geometry["problems"] + issues
    return {
        **({"separated": apart[:30], "separated_note": "These shared a plane with something (they would have flickered) "
            "and were moved a few millimetres off it. Nothing for you to do; do not move them back."} if apart else {}),
        "inventory": {"assets": counted["assets"], "by_kind": counted["by_kind"]},
        "geometry": {k: geometry[k] for k in ("collapsed", "flickering_surfaces", "buried", "overlapping_assets", "floating")
                     if geometry[k]},
        "how_measured": "Parts of one thing (parented, or named alike: House_0_Walls, House_0_Roof) may be pushed into each "
                        "other and are never reported. Touching is tested on the triangles with a few centimetres of "
                        "tolerance, not on bounding boxes: what `geometry` lists is real.",
        "objects": object_count,
        "total_triangles": total_triangles,
        "lights": lights_count, "camera": camera,
        "orphan_data": orphans, "issues": issues,
    }


def _triangles(mesh: bpy.types.Mesh) -> int:
    """Triangles the mesh's faces make (a face of n corners makes n - 2), counted without visiting each face."""
    return len(mesh.loops) - 2 * len(mesh.polygons)
