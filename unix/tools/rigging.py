# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Rigging tools: bind a mesh to an armature safely, and check a rig.

Imported models (Sketchfab, FBX) cause two failures that a model working by script walks
straight into:

* They sit under rotated or scaled parents, so local coordinates are not world coordinates.
  Re-parenting the mesh to the armature by hand drops the old parent's rotation and the mesh
  jumps away from bones that were placed correctly. ``rig_bind`` bakes the mesh's world
  rotation and scale into its data, then binds while keeping the mesh exactly where it is.
* They are split into pieces whose border vertices only touch (texture seams, separate arm
  segments, fingernails). Weighting each piece on its own gives the two sides of a seam
  different weights, and the surface tears open as soon as a bone moves. ``rig_bind`` computes
  weights on a welded copy, gives touching vertices identical weights, and makes small loose
  pieces follow the surface they sit on.

``check_rig`` measures what a render cannot show: bones outside the surface (judged per bone,
so finger bones count), seams that open, weights that bleed onto a neighbouring finger.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

from . import _shape, _turn
from ._util import ensure_object_mode, get_object, r, ui_context, vec
from .registry import ToolError, tool


def freeze(obj: bpy.types.Object) -> bool:
    """Bake a mesh object's world rotation and scale into its data and detach it from its
    parent, leaving it exactly where it is in the world. Returns True if anything changed."""
    world = obj.matrix_world.copy()
    loc, rot, scale = world.decompose()
    plain = obj.parent is None and rot.angle < 1e-6 and all(abs(s - 1.0) < 1e-6 for s in scale)
    if plain:
        return False
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    children = [(c, c.matrix_world.copy()) for c in obj.children]
    obj.data.transform(Matrix.LocRotScale(None, rot, scale))
    obj.data.update()
    obj.parent = None
    obj.matrix_world = Matrix.Translation(loc)
    bpy.context.view_layer.update()
    for child, matrix in children:  # children stay where they were
        child.matrix_world = matrix
    return True


def _bone_segments(arm: bpy.types.Object, deform_only: bool = True) -> List[Tuple[str, Vector, Vector]]:
    world = arm.matrix_world
    return [(b.name, world @ b.head_local, world @ b.tail_local) for b in arm.data.bones
            if b.use_deform or not deform_only]


def _closest_on_segment(p: Vector, a: Vector, b: Vector) -> Vector:
    ab = b - a
    denom = ab.length_squared
    t = 0.0 if denom < 1e-12 else max(0.0, min(1.0, (p - a).dot(ab) / denom))
    return a + ab * t


# ---------------------------------------------------------------------------------------------
# weights


def _bone_weights(obj: bpy.types.Object, index: int, names: Dict[int, str]) -> Dict[str, float]:
    return {names[g.group]: g.weight for g in obj.data.vertices[index].groups if g.group in names and g.weight > 1e-5}


def _set_weights(obj: bpy.types.Object, indices: Iterable[int], weights: Dict[str, float], bone_names: Set[str]) -> None:
    indices = list(indices)
    for group in obj.vertex_groups:
        if group.name in bone_names and group.name not in weights:
            group.remove(indices)
    for name, weight in weights.items():
        (obj.vertex_groups.get(name) or obj.vertex_groups.new(name=name)).add(indices, weight, 'REPLACE')


def _clear_bone_groups(obj: bpy.types.Object, bone_names: Set[str]) -> None:
    for group in [g for g in obj.vertex_groups if g.name in bone_names]:
        obj.vertex_groups.remove(group)


def _nearest_bone_weights(mesh_obj: bpy.types.Object, arm: bpy.types.Object, only: Optional[Iterable[int]] = None,
                          influences: int = 3) -> None:
    """Distance-based weights: each vertex follows the nearest bones it can reach through the
    inside of the mesh, so a finger does not pick up the bones of the finger next to it."""
    segments = _bone_segments(arm)
    if not segments:
        raise ToolError("The armature has no deform bones.")
    bone_names = {name for name, _a, _b in segments}
    if only is None:
        _clear_bone_groups(mesh_obj, bone_names)
    world = mesh_obj.matrix_world
    turn = world.to_3x3()
    mesh = mesh_obj.data
    verts = [world @ v.co for v in mesh.vertices]
    polys = [tuple(p.vertices) for p in mesh.polygons]
    tree = BVHTree.FromPolygons(verts, polys) if polys else None
    for index in (range(len(verts)) if only is None else only):
        p = verts[index]
        ranked = sorted(((p - _closest_on_segment(p, a, b)).length, name, _closest_on_segment(p, a, b))
                        for name, a, b in segments)[:6]
        chosen = ranked
        if tree is not None:
            start = p - (turn @ mesh.vertices[index].normal).normalized() * max(ranked[0][0] * 0.02, 1e-5)
            seen = []
            for distance, name, target in ranked:
                ray = target - start
                if ray.length < 1e-9 or tree.ray_cast(start, ray.normalized(), ray.length * 0.98)[0] is None:
                    seen.append((distance, name, target))
            chosen = seen or ranked[:1]
        chosen = chosen[:influences]
        closest = max(chosen[0][0], 1e-6)
        raw = [(name, (closest / max(d, 1e-6)) ** 4) for d, name, _t in chosen]
        total = sum(w for _n, w in raw)
        weights = {name: w / total for name, w in raw if w / total > 0.02}
        if only is None:        # groups were just cleared: nothing to remove first
            for name, weight in weights.items():
                (mesh_obj.vertex_groups.get(name) or mesh_obj.vertex_groups.new(name=name)).add([index], weight, 'REPLACE')
        else:
            _set_weights(mesh_obj, [index], weights, bone_names)


def _auto_weights(obj: bpy.types.Object, arm: bpy.types.Object, welded: _shape.Welded, bone_names: Set[str],
                  skip: Set[int]) -> Optional[int]:
    """Blender's automatic weights, computed on a copy with touching vertices welded and copied
    back by position, so the mesh is not changed and seams get one weight. Returns the number of
    vertices it could not weight (they are filled from the nearest bones), or None if it failed."""
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    temp_mesh = obj.data.copy()
    bm = bmesh.new()
    bm.from_mesh(temp_mesh)
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=welded.eps)
    bm.to_mesh(temp_mesh)
    bm.free()
    temp = bpy.data.objects.new("_unix_bind", temp_mesh)
    scene.collection.objects.link(temp)
    temp.matrix_world = obj.matrix_world.copy()
    temp.vertex_groups.clear()
    try:
        for other in view_layer.objects:
            other.select_set(False)
        temp.select_set(True)
        arm.select_set(True)
        view_layer.objects.active = arm
        try:
            with ui_context():
                bpy.ops.object.parent_set(type='ARMATURE_AUTO')
        except RuntimeError:
            return None
        names = {g.index: g.name for g in temp.vertex_groups if g.name in bone_names}
        solved = [{names[g.group]: g.weight for g in v.groups if g.group in names and g.weight > 1e-5}
                  for v in temp_mesh.vertices]
        if not any(solved):
            return None
        tree = KDTree(len(temp_mesh.vertices))
        for vertex in temp_mesh.vertices:
            tree.insert(vertex.co, vertex.index)
        tree.balance()
        _clear_bone_groups(obj, bone_names)
        groups = {name: obj.vertex_groups.new(name=name) for name in sorted(bone_names)}
        missed = []
        for vertex in obj.data.vertices:
            weights = solved[tree.find(vertex.co)[1]]
            if not weights:
                if vertex.index not in skip:
                    missed.append(vertex.index)
                continue
            for name, weight in weights.items():
                groups[name].add([vertex.index], weight, 'REPLACE')
        if len(missed) > 0.25 * len(obj.data.vertices):
            return None
        if missed:
            _nearest_bone_weights(obj, arm, only=missed)
        return len(missed)
    finally:
        bpy.data.objects.remove(temp, do_unlink=True)
        bpy.data.meshes.remove(temp_mesh)


def _seal_seams(obj: bpy.types.Object, welded: _shape.Welded, bone_names: Set[str]) -> int:
    """Give vertices that share a position the same weights. Returns how many were changed."""
    names = {g.index: g.name for g in obj.vertex_groups if g.name in bone_names}
    changed = 0
    for group in welded.twin_groups():
        each = [_bone_weights(obj, index, names) for index in group]
        keys = set().union(*each)
        if all(abs(w.get(k, 0.0) - each[0].get(k, 0.0)) < 1e-4 for w in each[1:] for k in keys):
            continue
        mean = {k: sum(w.get(k, 0.0) for w in each) / len(each) for k in keys}
        total = sum(mean.values())
        if total < 1e-6:
            continue
        _set_weights(obj, group, {k: v / total for k, v in mean.items() if v / total > 1e-4}, bone_names)
        changed += len(group)
    return changed


def _attach_riders(obj: bpy.types.Object, welded: _shape.Welded, bone_names: Set[str]) -> int:
    """Small loose pieces (nails, buttons, eyes) take the weights of the surface under them, so
    they move rigidly with it instead of drifting or stretching."""
    names = {g.index: g.name for g in obj.vertex_groups if g.name in bone_names}
    done = 0
    for indices, anchor in welded.riders():
        weights = _bone_weights(obj, anchor, names)
        if weights:
            _set_weights(obj, indices, weights, bone_names)
            done += 1
    return done


def _unweighted(mesh_obj: bpy.types.Object, bone_names: set) -> int:
    index_ok = {g.index for g in mesh_obj.vertex_groups if g.name in bone_names}
    return sum(1 for v in mesh_obj.data.vertices
               if not any(g.group in index_ok and g.weight > 1e-5 for g in v.groups))


@tool(
    "rig_bind",
    """Bind mesh objects to an armature so the bones deform them. ALWAYS use this instead of
    parenting by script. It keeps every mesh exactly where it is in the world (models imported
    from Sketchfab or FBX sit under rotated parents, and re-parenting by hand makes the mesh jump
    away from the bones), bakes the mesh's world rotation and scale into its data, adds the
    Armature modifier and computes weights. It also handles meshes split into touching pieces:
    vertices that share a position get identical weights (otherwise the surface tears open at
    the seam when a bone moves), and small loose pieces such as nails follow the surface under
    them. method 'auto' = Blender's automatic weights with nearest-bone weights where they fail;
    'nearest' = nearest-bone weights everywhere; 'keep' = keep the current weights and only
    repair seams and loose pieces (run it after editing or smoothing weights by script). Place the
    bones first, then call this, then check_rig.""",
    {
        "armature": {"type": "string", "description": "Armature object name."},
        "meshes": {"type": "array", "items": {"type": "string"}, "description": "Mesh objects to bind."},
        "method": {"type": "string", "enum": ["auto", "nearest", "keep"], "description": "Default 'auto'."},
    },
    ["armature", "meshes"],
    timeout=900.0,
    mutates=True,
    label="Bound the mesh to the rig",
)
def rig_bind(armature: str, meshes: List[str], method: str = "auto") -> Dict[str, Any]:
    ensure_object_mode()
    arm = get_object(armature)
    if arm.type != 'ARMATURE':
        raise ToolError(f"'{armature}' is not an armature.")
    targets = [get_object(n) for n in meshes]
    bad = [o.name for o in targets if o.type != 'MESH']
    if bad or not targets:
        raise ToolError(f"These are not mesh objects: {', '.join(bad)}" if bad else "Name at least one mesh.")
    bone_names = {b.name for b in arm.data.bones if b.use_deform}
    if not bone_names:
        raise ToolError("The armature has no deform bones.")
    if method == "keep":
        bare = [o.name for o in targets if not any(g.name in bone_names for g in o.vertex_groups)]
        if bare:
            raise ToolError(f"No bone weights to keep on: {', '.join(bare)}. Use method 'auto'.")
    _turn.mark_mutation()
    for pbone in arm.pose.bones:  # bind in the rest pose
        pbone.matrix_basis = Matrix.Identity(4)
    view_layer = bpy.context.view_layer
    report = []
    for obj in targets:
        step = max(1, len(obj.data.vertices) // 200)
        sample = range(0, len(obj.data.vertices), step)
        before = [obj.matrix_world @ obj.data.vertices[i].co for i in sample]
        frozen = freeze(obj)
        for mod in [m for m in obj.modifiers if m.type == 'ARMATURE']:
            obj.modifiers.remove(mod)
        welded = _shape.Welded(obj)
        rider_verts = {index for indices, _anchor in welded.riders() for index in indices}
        used, filled = method, 0
        if method == "auto":
            missed = _auto_weights(obj, arm, welded, bone_names, rider_verts)
            if missed is None:
                used = "nearest (automatic weights failed on this mesh)"
            else:
                filled = missed
        if used.startswith("nearest"):
            _nearest_bone_weights(obj, arm)
        sealed = _seal_seams(obj, welded, bone_names)
        riders = _attach_riders(obj, welded, bone_names)
        # parent to the armature without moving, and make sure exactly one Armature modifier points at it
        world = obj.matrix_world.copy()
        obj.parent = arm
        obj.parent_type = 'OBJECT'
        obj.matrix_world = world
        mods = [m for m in obj.modifiers if m.type == 'ARMATURE']
        for extra in mods[1:]:
            obj.modifiers.remove(extra)
        mod = mods[0] if mods else obj.modifiers.new("Armature", 'ARMATURE')
        mod.object = arm
        if obj.modifiers[0] != mod:
            obj.modifiers.move(list(obj.modifiers).index(mod), 0)
        view_layer.update()
        after = [obj.matrix_world @ obj.data.vertices[i].co for i in sample]
        moved = max(((a - b).length for a, b in zip(before, after)), default=0.0)
        entry = {"mesh": obj.name, "weights": used, "frozen_transforms": frozen,
                 "vertex_groups": sum(1 for g in obj.vertex_groups if g.name in bone_names),
                 "unweighted_verts": _unweighted(obj, bone_names), "moved_m": r(moved, 5)}
        seam_verts = sum(len(g) for g in welded.twin_groups())
        if seam_verts:
            entry["seam_verts"] = seam_verts
            entry["seam_verts_repaired"] = sealed
        if riders:
            entry["loose_pieces_attached"] = riders
        if filled:
            entry["filled_from_nearest_bone"] = filled
        report.append(entry)
    for o in view_layer.objects:
        o.select_set(False)
    return {"armature": arm.name, "bound": report,
            "next": "Run check_rig, then look at it: bones are drawn in yellow through the mesh. Use focus_bones for close-ups."}


# ---------------------------------------------------------------------------------------------
# checking


def _posed_coords(obj: bpy.types.Object) -> np.ndarray:
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    mesh = evaluated.data
    flat = np.empty(len(mesh.vertices) * 3, dtype=np.float64)
    mesh.vertices.foreach_get("co", flat)
    world = np.array(evaluated.matrix_world, dtype=np.float64)
    return flat.reshape(-1, 3) @ world[:3, :3].T + world[:3, 3]


def _bone_distance(points: np.ndarray, head: Vector, tail: Vector) -> np.ndarray:
    a = np.array(head, dtype=np.float64)
    ab = np.array(tail, dtype=np.float64) - a
    denom = float(ab @ ab)
    t = np.clip(((points - a) @ ab) / denom, 0.0, 1.0) if denom > 1e-12 else np.zeros(len(points))
    return np.linalg.norm(points - (a + np.outer(t, ab)), axis=1)


def _owners(points: np.ndarray, segments: List[Tuple[str, Vector, Vector]]) -> Tuple[np.ndarray, np.ndarray]:
    """For every point: the index of its nearest bone, and the distance to it."""
    best = np.full(len(points), np.inf)
    owner = np.zeros(len(points), dtype=np.int32)
    for index, (_name, head, tail) in enumerate(segments):
        distance = _bone_distance(points, head, tail)
        closer = distance < best
        best[closer] = distance[closer]
        owner[closer] = index
    return owner, best


@tool(
    "check_rig",
    """Measure a rig after binding. Reports: whether the skeleton occupies the same space as the
    mesh; every bone that runs outside the surface or just under it (judged against each bone's
    own length, so finger bones count); unweighted vertices; the Armature modifier; and a pose
    test that bends every bone and reports bones that move nothing, seams that open (touching
    vertices that come apart: the surface tears), and weights that bleed (bending one finger,
    toe or horn drags its neighbour). Works for any rig. Run it after every rig_bind and fix
    what it reports before saying done.""",
    {
        "armature": {"type": "string", "description": "Armature object name."},
        "meshes": {"type": "array", "items": {"type": "string"},
                   "description": "Meshes to check. Omit to check every mesh with an Armature modifier for this rig."},
    },
    ["armature"],
    timeout=600.0,
    label="Checked the rig",
)
def check_rig(armature: str, meshes: Optional[List[str]] = None) -> Dict[str, Any]:
    ensure_object_mode()
    arm = get_object(armature)
    if arm.type != 'ARMATURE':
        raise ToolError(f"'{armature}' is not an armature.")
    if meshes:
        targets = [get_object(n) for n in meshes]
    else:
        targets = [o for o in bpy.context.scene.objects if o.type == 'MESH'
                   and any(m.type == 'ARMATURE' and m.object == arm for m in o.modifiers)]
    issues: List[str] = []
    segments = _bone_segments(arm)
    out: Dict[str, Any] = {"armature": arm.name, "bones": len(arm.data.bones), "deform_bones": len(segments)}
    if not targets:
        issues.append("no mesh is bound to this armature (no Armature modifier points at it): use rig_bind")
        out["issues"] = issues
        return out
    saved_pose = {b.name: b.matrix_basis.copy() for b in arm.pose.bones}
    saved_modes = {b.name: b.rotation_mode for b in arm.pose.bones}
    for pbone in arm.pose.bones:
        pbone.matrix_basis = Matrix.Identity(4)
    bpy.context.view_layer.update()
    bone_names = {name for name, _a, _b in segments}
    bone_index = {name: i for i, (name, _a, _b) in enumerate(segments)}
    reports = []
    followed: Set[str] = set()           # bones that move at least one vertex of some mesh
    muted: List[bpy.types.Modifier] = []
    try:
        for obj in targets:
            mesh = obj.data
            if not mesh.vertices or not mesh.polygons:
                issues.append(f"{obj.name}: mesh has no faces")
                continue
            for mod in obj.modifiers:       # compare like with like: only the armature deforms during the test
                if mod.type != 'ARMATURE' and mod.show_viewport:
                    mod.show_viewport = False
                    muted.append(mod)
            bpy.context.view_layer.update()
            world = obj.matrix_world
            welded = _shape.Welded(obj)
            volume = _shape.Volume(obj)
            lo, hi, size = welded.lo, welded.hi, welded.size
            names = {g.index: g.name for g in obj.vertex_groups if g.name in bone_names}
            used = {names[g.group] for v in mesh.vertices for g in v.groups if g.group in names and g.weight > 0.05}
            followed |= used

            in_box = 0
            outside: List[Dict[str, Any]] = []
            shallow: List[str] = []
            grazing: List[str] = []
            for name, head, tail in segments:
                mid = (head + tail) / 2
                margin = size * 0.05
                if all(lo[i] - margin <= mid[i] <= hi[i] + margin for i in range(3)):
                    in_box += 1
                if name not in used:
                    continue
                length = (tail - head).length
                worst, count = 0.0, 0
                for f in (0.1, 0.3, 0.5, 0.7, 0.9):
                    inside, distance = volume.inside(head.lerp(tail, f))
                    if not inside and distance > length * 0.08:
                        count += 1
                        worst = max(worst, distance)
                if count >= 2 or (count and not volume.inside(mid)[0]):
                    outside.append({"bone": name, "outside_by_m": r(worst, 4), "bone_length_m": r(length, 4)})
                    continue
                centred = 0.0           # 1 = in the middle, 0 = on the skin; the best of three points along the bone,
                for f in (0.25, 0.5, 0.75):   # because a bone passing a fork (the web between fingers) is near skin there
                    near, far = volume.depth(head.lerp(tail, f))
                    if near > 0.0:
                        centred = max(centred, near / max((near + far) / 2, 1e-9))
                if centred < 0.3:
                    shallow.append(name)
                if centred < 0.12:
                    grazing.append(name)
            unweighted = _unweighted(obj, bone_names)
            mods = [m for m in obj.modifiers if m.type == 'ARMATURE']
            rep: Dict[str, Any] = {
                "mesh": obj.name, "bones_in_mesh_bounds": f"{in_box}/{len(segments)}",
                "bones_outside_surface": len(outside), "unweighted_verts": unweighted,
                "bones_without_group": len(bone_names - {g.name for g in obj.vertex_groups}),
                "world_rotation_deg": [r(math.degrees(a), 1) for a in world.to_euler()],
            }
            if outside:
                rep["outside"] = outside[:24]
            if shallow:
                rep["just_under_the_surface"] = shallow[:24]
            if in_box < len(segments) * 0.5:
                issues.append(f"{obj.name}: only {in_box} of {len(segments)} bones are within the mesh's bounds. The skeleton "
                              f"and the mesh do not occupy the same space (mesh world bounds {vec(lo, 2)} to {vec(hi, 2)}). "
                              f"Rebuild the bones in world space, or the mesh was moved when it was bound.")
            elif outside:
                issues.append(f"{obj.name}: {len(outside)} bones run outside the surface: "
                              f"{', '.join(o['bone'] for o in outside[:10])}. Rebuild them on the measured centre lines "
                              f"(find_limbs, then rig_chain) and bind again. Only a bone with no mesh around it may stay outside.")
            if grazing:
                issues.append(f"{obj.name}: {len(grazing)} bones run right under the skin instead of through the middle: "
                              f"{', '.join(grazing[:10])}. Weights are better with centred bones (rig_chain).")
            if unweighted:
                issues.append(f"{obj.name}: {unweighted} vertices have no bone weight and will stay behind when posed")
            if len(mods) != 1 or mods[0].object != arm:
                issues.append(f"{obj.name}: expected exactly one Armature modifier pointing at {arm.name}, found {len(mods)}")

            # pose test: bend every bone in turn and measure what the mesh does
            rest = _posed_coords(obj)
            if len(rest) != len(mesh.vertices):
                issues.append(f"{obj.name}: could not run the pose test (a modifier changes the vertex count)")
                reports.append(rep)
                continue
            owner, owner_distance = _owners(rest, segments)
            twins = [(group[0], other) for group in welded.twin_groups() for other in group[1:]]
            twin_a = np.array([a for a, _b in twins], dtype=np.int64)
            twin_b = np.array([b for _a, b in twins], dtype=np.int64)
            gap_limit = max(size * 2e-4, welded.eps * 4)
            candidates = [b for b in sorted(arm.pose.bones, key=lambda b: -b.bone.length) if b.bone.use_deform]
            if len(candidates) > 120:
                candidates = candidates[::math.ceil(len(candidates) / 120)]
            dead, thrown, seams, bleed = [], [], [], []
            for pbone in candidates:
                pbone.rotation_mode = 'XYZ'
                pbone.rotation_euler = (math.radians(35.0), 0.0, 0.0)
                bpy.context.view_layer.update()
                posed = _posed_coords(obj)
                pbone.rotation_euler = (0.0, 0.0, 0.0)
                move = np.linalg.norm(posed - rest, axis=1)
                biggest = float(move.max())
                if biggest < size * 1e-5:
                    if pbone.name in used:
                        dead.append(pbone.name)
                    continue
                if biggest > max(pbone.bone.length * 6.0, size * 0.9):
                    thrown.append(pbone.name)
                if len(twins):
                    gap = float(np.linalg.norm(posed[twin_a] - posed[twin_b], axis=1).max())
                    if gap > gap_limit:
                        seams.append({"bone": pbone.name, "gap_m": r(gap, 4)})
                family = {pbone.name} | {c.name for c in pbone.children_recursive}
                if pbone.parent is not None:
                    family.add(pbone.parent.name)
                allowed = np.array([name in family for name, _a, _b in segments])
                moving = move > biggest * 0.25
                # bleed = vertices that sit on another bone's own part (much nearer to it than to the bent
                # bone) and still follow; skin stretched between two bones, like a wing membrane, is meant to
                head, tail = segments[bone_index[pbone.name]][1:]
                stray = moving & ~allowed[owner] & (owner_distance < 0.5 * _bone_distance(rest, head, tail))
                if int(stray.sum()) >= max(3, 0.08 * int(moving.sum())):
                    others = np.bincount(owner[stray], minlength=len(segments))
                    top = [segments[i][0] for i in np.argsort(-others)[:2] if others[i]]
                    bleed.append({"bone": pbone.name, "also_moves": top, "verts": int(stray.sum())})
            bpy.context.view_layer.update()
            rep["pose_test"] = {"bones_tested": len(candidates), "no_effect": dead, "overshoot": thrown,
                                "seams_open": seams[:16], "bleed": bleed[:16]}
            if dead:
                issues.append(f"{obj.name}: bending {', '.join(dead[:5])} does not move the mesh (missing or empty weights)")
            if thrown:
                issues.append(f"{obj.name}: bending {', '.join(thrown[:5])} throws vertices far away (bad weights)")
            if seams:
                widest = max(s["gap_m"] for s in seams)
                issues.append(f"{obj.name}: the surface tears open when {', '.join(s['bone'] for s in seams[:6])} bend "
                              f"(gap up to {widest} m). Touching vertices have different weights: run rig_bind again "
                              f"(method 'keep' repairs seams without recomputing weights).")
            if bleed:
                issues.append(f"{obj.name}: bending " + "; ".join(f"{b['bone']} also drags {', '.join(b['also_moves'])}"
                                                                   for b in bleed[:6])
                              + ". Weights bleed between neighbouring parts: bind with method 'nearest', or check that "
                                "each bone runs through the middle of its own part.")
            reports.append(rep)
        idle = [name for name, _a, _b in segments if name not in followed]
        if idle and reports:
            issues.append(f"{len(idle)} deform bones have no vertices following them: {', '.join(idle[:10])}. A bone outside the "
                          f"mesh gets no weights. Move it inside its part (find_limbs, rig_chain) and bind again, or set "
                          f"use_deform False if it is only a control.")
            out["bones_no_vertex_follows"] = idle[:40]
    finally:
        for mod in muted:
            mod.show_viewport = True
        for pbone in arm.pose.bones:
            pbone.rotation_mode = saved_modes[pbone.name]
            pbone.matrix_basis = saved_pose[pbone.name]
        bpy.context.view_layer.update()
    out.update(meshes=reports, issues=issues)
    return out
