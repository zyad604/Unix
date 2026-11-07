# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Limb tools: measure any creature's limbs and lay bone chains along them.

Placing small bones by reading vertex coordinates and guessing is how finger bones end up
outside the fingers. ``find_limbs`` measures the model instead (see ``_shape.analyse``):
arms, legs, necks and tails, fingers, toes and claws, wing spars and fin rays. ``rig_chain``
builds bones on the measured centre lines, or through explicit points moved to the middle of
the mesh (thin sheets included), so the model never has to copy coordinates around.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

from . import _shape, _turn
from ._util import ensure_object_mode, get_object, r, ui_context, vec
from .registry import ToolError, tool

# Joint positions along a finger: 0 = where it separates from its neighbours (the far, palm-side
# edge of the web), 1 = its tip. The first knuckle sits inside the palm, behind that point.
FINGER_AT = [-0.28, 0.33, 0.69, 1.0]
# A thumb's free length depends on how far it is spread, so its joints are measured from the
# wrist instead: fractions of the path wrist-side root -> where it leaves the hand -> tip.
THUMB_SPLIT = [0.0, 0.44, 0.74, 1.0]

_cache: Dict[str, Dict[str, Any]] = {}
_last: Optional[str] = None


def _stamp(obj: bpy.types.Object) -> Tuple:
    return (len(obj.data.vertices), tuple(round(v, 6) for row in obj.matrix_world for v in row))


def _analysis(mesh_name: Optional[str]) -> Tuple[bpy.types.Object, Dict[str, Any]]:
    name = mesh_name or _last
    if not name or name not in _cache:
        raise ToolError("Run find_limbs on the mesh first; rig_chain builds on what it measured.")
    obj = get_object(name)
    if _cache[name]["stamp"] != _stamp(obj):
        raise ToolError(f"'{name}' changed since find_limbs measured it. Run find_limbs again.")
    return obj, _cache[name]


def _find_segment(analysis: Dict[str, Any], ref: str) -> Dict[str, Any]:
    letter, _dot, tail = ref.partition(".")
    part = next((p for p in analysis["parts"] if p["part"] == letter.upper()), None)
    if part is None or not tail:
        raise ToolError(f"No limb '{ref}'. Ids look like 'A.trunk', 'A.s1', 'A.3' or 'A.index'.")
    wanted = f"{part['part']}.{tail}"
    for seg in [part["trunk"]] + part["stems"] + part["limbs"]:
        if seg["id"] == wanted:
            return seg
    named = [seg for seg in part["limbs"] if seg.get("guess") == tail.lower()]
    if len(named) == 1:
        return named[0]
    if named:
        raise ToolError(f"Part {part['part']} has {len(named)} hands; use the numbered id instead of '{ref}'.")
    known = [part["trunk"]["id"]] + [s["id"] for s in part["stems"]] \
        + [s["id"] + (f" ({s['guess']})" if s.get("guess") else "") for s in part["limbs"]]
    raise ToolError(f"No limb '{ref}' in part {part['part']}. It has: {', '.join(known)}")


def _profile(seg: Dict[str, Any], samples: int) -> List[Dict[str, Any]]:
    centres, radii = seg["centres"], seg["radii"]
    return [{"t": r(i / (samples - 1), 3), "p": vec(_shape.point_at(centres, i / (samples - 1)), 3),
             "radius": r(_shape._value_at(centres, radii, i / (samples - 1)), 3)} for i in range(samples)]


def _narrow(seg: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"t": r(t, 3), "p": vec(point, 3), "radius": r(radius, 3)} for t, point, radius in _shape.narrow_points(seg)][:6]


def _run(seg: Dict[str, Any], samples: int) -> Dict[str, Any]:
    return {"id": seg["id"], "length": r(seg["length"], 3), "from": vec(seg["line"][0], 3), "to": vec(seg["line"][-1], 3),
            "along": _profile(seg, samples), "narrow_points": _narrow(seg)}


@tool(
    "find_limbs",
    """Measure a mesh's limbs so bones can be placed exactly: fingers, toes, arms, legs, tails,
    tentacles, horns, wing spars, fin rays, branches. Works for any creature or object and on
    meshes split into touching pieces. ALWAYS call this before placing bones in fingers or other
    thin parts, then build the bones with rig_chain, which follows these measured centre lines.
    It walks the surface from the thick end (or from `root`) and returns, per connected body
    ('part'): `trunk` (centre line from the root to the first fork, with its thickness along the
    way and its `narrow_points`, which is where elbows, wrists, knees, ankles and necks are),
    `stems` (long runs between forks: arms, legs, neck, wing arms), `limbs` (each branch that
    ends in a tip: where it leaves the body, its tip, length, thickness) and `fans` (three to
    eight short limbs spreading from one stem, with the point they spread from: a hand, a foot,
    claws, a wing's spars; a fan of five is named thumb, index, middle, ring, pinky).
    Coordinates are world space.""",
    {
        "mesh": {"type": "string", "description": "Mesh object name."},
        "root": {"type": "array", "items": {"type": "number"},
                 "description": "Optional world point [x,y,z] where the part attaches to the rest of the body "
                                "(pelvis or chest for a whole character). Default: its thick end."},
        "detail": {"type": "integer", "description": "Slices along the longest path (default 120; raise to 240 for very short limbs)."},
        "smallest": {"type": "number", "description": "Shortest branch that counts as a limb, as a fraction of the part's length "
                                                       "(default 0.03). Use 0.01 to find fingers and toes on a whole character."},
    },
    ["mesh"],
    timeout=600.0,
    label="Measured the limbs",
)
def find_limbs(mesh: str, root: Optional[List[float]] = None, detail: int = 120, smallest: float = 0.03) -> Dict[str, Any]:
    global _last
    ensure_object_mode()
    obj = get_object(mesh)
    if obj.type != 'MESH' or not obj.data.polygons:
        raise ToolError(f"'{mesh}' is not a mesh with faces.")
    if len(obj.data.vertices) > 400_000:
        raise ToolError("This mesh has more than 400,000 vertices; measure a decimated copy instead.")
    if root is not None and len(root) != 3:
        raise ToolError("root must be [x, y, z].")
    analysis = _shape.analyse(obj, Vector(root) if root else None, max(24, min(int(detail), 400)),
                              max(0.002, min(float(smallest), 0.5)))
    analysis["stamp"] = _stamp(obj)
    _cache[obj.name] = analysis
    _last = obj.name
    parts = []
    for part in analysis["parts"]:
        entry: Dict[str, Any] = {"part": part["part"], "root": vec(part["root"], 3), "surface_length": r(part["span"], 3),
                                 "trunk": _run(part["trunk"], 11)}
        if part["stems"]:
            entry["stems"] = [_run(seg, 5) for seg in part["stems"][:12]]
        entry["limbs"] = [{"id": s["id"], **({"guess": s["guess"]} if s.get("guess") else {}),
                           "base": vec(s["line"][0], 3), "tip": vec(s["tip"], 3), "length": r(s["length"], 3),
                           "radius": r(sum(s["radii"]) / len(s["radii"]), 3)} for s in part["limbs"][:60]]
        if part["fans"]:
            entry["fans"] = []
            for fan in part["fans"]:
                item: Dict[str, Any] = {"kind": "hand" if fan["hand"] else "fan", "stem": fan["stem"].get("id"),
                                        "origin": vec(fan["origin"], 3), "limbs": [m["id"] for m in fan["limbs"]]}
                if fan["hand"]:
                    item["fingers"] = {m["guess"]: m["id"] for m in fan["limbs"]}
                    item["palm_faces"] = vec(fan["normal"], 2)
                    item["palm_side_known"] = fan["side_known"]
                else:
                    item["plane_normal"] = vec(fan["normal"], 2)
                entry["fans"].append(item)
        parts.append(entry)
    out: Dict[str, Any] = {"mesh": obj.name, "parts": parts}
    if analysis["small_pieces"]:
        out["small_loose_pieces"] = analysis["small_pieces"]
        out["note"] = "Small loose pieces (nails, buttons) are not limbs; rig_bind makes them follow the surface they sit on."
    if not any(p["limbs"] for p in parts):
        out["hint"] = "No branches found: each part is a single tube. Use its trunk id in rig_chain."
    out["next"] = ("Build bones with rig_chain. Fingers of a hand: one chain per finger with three names and no `at`, e.g. "
                   "{limb:'A.2', names:['Index.01.L','Index.02.L','Index.03.L'], parent:'Hand.L'}. Arm, leg, neck or tail: the "
                   "trunk or stem with `at` fractions taken from narrow_points (near the middle = elbow or knee, the last one = "
                   "wrist or ankle), or just names for evenly spaced bones. Wing spars, toes and claws: a chain per fan limb with "
                   "start:'origin' so it begins where the fan spreads from.")
    return out


def _through_palm(point: Vector, normal: Vector, volume: _shape.Volume) -> Vector:
    """The middle of the hand's thickness at a point (which may lie just outside the surface)."""
    up = volume.tree.ray_cast(point, normal)
    down = volume.tree.ray_cast(point, -normal)
    if up[0] is not None and down[0] is not None:
        return up[0].lerp(down[0], 0.5)
    if up[0] is None and down[0] is None:
        return point
    way = normal if up[0] is not None else -normal
    first = up[0] if up[0] is not None else down[0]
    second = volume.tree.ray_cast(first + way * 1e-4, way)
    return first.lerp(second[0], 0.5) if second[0] is not None else point


def _joints_on_limb(seg: Dict[str, Any], at: List[float], volume: Optional[_shape.Volume]) -> List[Vector]:
    line = seg["line"]
    length = seg["length"]
    if all(0.0 <= t <= 1.0 for t in at):
        return [_shape.point_at(line, t) for t in at]
    ahead = (_shape.point_at(line, 0.35) - line[0])
    if ahead.length < 1e-9:
        raise ToolError(f"Limb {seg['id']} is too short to extend.")
    direction = ahead.normalized()
    radius = seg["radii"][0] if seg["radii"] else length * 0.1
    normal = seg["fan"]["normal"] if seg.get("fan") else None
    out = []
    for t in at:
        if t >= 0.0:
            out.append(_shape.point_at(line, min(t, 1.0)))
            continue
        point = line[0] + direction * (t * length)
        if volume is not None and volume.tree is not None:
            for _try in range(14):      # stay inside the body, in the middle of the palm's thickness
                candidate = _through_palm(point, normal, volume) if normal is not None else point
                inside, depth = volume.inside(candidate)
                if inside and depth >= radius * 0.2:
                    point = candidate
                    break
                point = point.lerp(line[0], 0.15)
        out.append(point)
    return out


def _thumb_joints(seg: Dict[str, Any], volume: Optional[_shape.Volume]) -> List[Vector]:
    """Thumb root, knuckle, middle joint and tip. The root sits a quarter of the way from the wrist
    to where the thumb leaves the hand; the bones then share the path in anatomical proportion."""
    base = seg["line"][0]
    start = seg["fan"]["origin"].lerp(base, 0.25)
    if volume is not None:
        start = volume.centre(start, base - start, rounds=2)
    path = [start] + list(seg["line"])
    return [_shape.point_at(path, t) for t in THUMB_SPLIT]


@tool(
    "rig_chain",
    """Create bone chains in an armature (made if it does not exist). Each chain is a run of
    connected bones through its joints. Two ways to give the joints: (1) `limb` (an id from
    find_limbs such as 'A.2', 'A.index', 'A.s1' or 'A.trunk') with `at`, fractions along that
    limb's measured centre line: 0 = where it starts, 1 = its tip or end, below 0 = further back
    into the body. Omit `at` for the fingers and thumb of a hand with three names each: the
    joints are placed anatomically (the first knuckle inside the palm). (2) `points`, explicit
    world positions, with `centre: true` to move each joint to the middle of the mesh around it
    (the middle of the thickness in a thin sheet such as a wing membrane, ear or fin). With
    neither `at` nor a hand, the bones are spaced evenly: right for tails, necks, tentacles and
    spines. For a limb of a fan, `start: 'origin'` begins the chain at the point the fan spreads
    from (a wing spar from the wing's wrist, a toe from the ankle side of the foot). `names` has
    one name per bone (joints minus one). Bones that already exist are moved, so a chain can be
    rebuilt with different `at` values. Bones of one fan share a roll: local X bends a limb out
    of the fan's plane (in a hand, positive X curls a finger toward the palm), local Z swings it
    within the plane (spreading fingers, folding a wing).""",
    {
        "armature": {"type": "string", "description": "Armature object name; created at the world origin if missing."},
        "chains": {"type": "array", "description": "Chains to build, in order (a later chain may use an earlier bone as parent).",
                   "items": {"type": "object", "properties": {
                       "names": {"type": "array", "items": {"type": "string"}, "description": "Bone names from the body outward."},
                       "limb": {"type": "string", "description": "Limb id from find_limbs."},
                       "at": {"type": "array", "items": {"type": "number"}, "description": "Joint fractions along the limb, one more than names."},
                       "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                                  "description": "World joints [[x,y,z],...] instead of limb/at."},
                       "centre": {"type": "boolean", "description": "With points: move each joint to the middle of the mesh."},
                       "start": {"type": "string", "enum": ["origin"],
                                 "description": "With a fan limb: begin at the point the fan spreads from; `at` then runs origin (0) to tip (1)."},
                       "parent": {"type": "string", "description": "Bone the first bone of this chain hangs from."},
                   }, "required": ["names"]}},
        "mesh": {"type": "string", "description": "The mesh find_limbs measured (default: the last one)."},
    },
    ["armature", "chains"],
    timeout=300.0,
    mutates=True,
    label="Built bones",
)
def rig_chain(armature: str, chains: List[Dict[str, Any]], mesh: Optional[str] = None) -> Dict[str, Any]:
    ensure_object_mode()
    if not chains:
        raise ToolError("Give at least one chain.")
    obj = analysis = None
    if any(c.get("limb") for c in chains):
        obj, analysis = _analysis(mesh)
    elif mesh or _last:
        obj = bpy.data.objects.get(mesh or _last)
    volume = _shape.Volume(obj) if obj is not None and obj.type == 'MESH' else None

    plans = []
    for index, chain in enumerate(chains):
        names = chain.get("names") or []
        if not names or len(set(names)) != len(names):
            raise ToolError(f"Chain {index + 1}: give one unique name per bone.")
        roll = curl = None
        if chain.get("limb"):
            seg = _find_segment(analysis, chain["limb"])
            at = chain.get("at")
            fan = seg.get("fan")
            guess = seg.get("guess")
            if chain.get("start") == "origin":
                if fan is None:
                    raise ToolError(f"Chain {index + 1}: start 'origin' needs a limb that belongs to a fan; {seg['id']} does not.")
                at = at or [i / len(names) for i in range(len(names) + 1)]
                if len(at) != len(names) + 1 or min(at) < 0.0 or max(at) > 1.0001 or any(b <= a for a, b in zip(at, at[1:])):
                    raise ToolError(f"Chain {index + 1}: with start 'origin', `at` needs {len(names) + 1} increasing values from 0 to 1.")
                joints = [_shape.point_at([fan["origin"]] + list(seg["line"]), float(t)) for t in at]
            elif at is None and guess == "thumb" and len(names) == 3:
                joints = _thumb_joints(seg, volume)
            else:
                if at is None:
                    at = FINGER_AT if guess and guess != "thumb" and len(names) == 3 \
                        else [i / len(names) for i in range(len(names) + 1)]
                if len(at) != len(names) + 1:
                    raise ToolError(f"Chain {index + 1}: {len(names)} bones need {len(names) + 1} `at` values, got {len(at)}.")
                if any(b <= a for a, b in zip(at, at[1:])) or at[-1] > 1.0001:
                    raise ToolError(f"Chain {index + 1}: `at` must increase and end at 1 or less.")
                joints = _joints_on_limb(seg, [float(t) for t in at], volume)
            if fan is not None:
                roll = fan["normal"]
                curl = ("rotate on local X: positive curls toward the palm" if fan["hand"] and fan["side_known"]
                        else "local X bends out of the fan's plane, local Z swings within it")
        elif chain.get("points"):
            joints = [Vector(p) for p in chain["points"]]
            if len(joints) != len(names) + 1:
                raise ToolError(f"Chain {index + 1}: {len(names)} bones need {len(names) + 1} points, got {len(joints)}.")
            if chain.get("centre"):
                if volume is None:
                    raise ToolError("`centre` needs a mesh: pass `mesh`.")
                joints = [volume.centre(p, joints[min(i + 1, len(joints) - 1)] - joints[max(i - 1, 0)])
                          for i, p in enumerate(joints)]
        else:
            raise ToolError(f"Chain {index + 1}: give `limb` or `points`.")
        short = [names[i] for i in range(len(names)) if (joints[i + 1] - joints[i]).length < 1e-5]
        if short:
            raise ToolError(f"Chain {index + 1}: zero-length bones {', '.join(short)}; spread the joints apart.")
        plans.append((names, joints, chain.get("parent"), roll, curl))

    arm_obj = bpy.data.objects.get(armature)
    if arm_obj is not None and arm_obj.type != 'ARMATURE':
        raise ToolError(f"'{armature}' exists and is not an armature.")
    known = set(arm_obj.data.bones.keys()) if arm_obj else set()
    for names, _joints, parent, _roll, _curl in plans:
        if parent and parent not in known:
            raise ToolError(f"Parent bone '{parent}' does not exist yet. Build it first (chains run in order).")
        known.update(names)

    _turn.mark_mutation()
    created = arm_obj is None
    if created:
        arm_obj = bpy.data.objects.new(armature, bpy.data.armatures.new(armature))
        bpy.context.scene.collection.objects.link(arm_obj)
        arm_obj.show_in_front = True      # bones stay visible through the mesh
    view_layer = bpy.context.view_layer
    for other in view_layer.objects:
        other.select_set(False)
    arm_obj.hide_set(False)
    arm_obj.select_set(True)
    view_layer.objects.active = arm_obj
    to_local = arm_obj.matrix_world.inverted()
    turn = to_local.to_3x3()
    with ui_context():
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            bones = arm_obj.data.edit_bones
            for names, joints, parent, roll, _curl in plans:
                previous = bones[parent] if parent else None
                for i, name in enumerate(names):
                    bone = bones.get(name) or bones.new(name)
                    bone.use_connect = False
                    bone.head = to_local @ joints[i]
                    bone.tail = to_local @ joints[i + 1]
                    bone.parent = previous
                    if previous is not None and (previous.tail - bone.head).length < 1e-5:
                        bone.use_connect = True
                    axis = (joints[i + 1] - joints[i]).normalized()
                    toward = roll
                    if toward is None:
                        toward = Vector((0.0, 0.0, 1.0)) if abs(axis.z) < 0.9 else Vector((0.0, -1.0, 0.0))
                    bone.align_roll(turn @ toward)
                    previous = bone
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
    report = []
    for names, joints, parent, _roll, curl in plans:
        entry: Dict[str, Any] = {"bones": names, "parent": parent, "joints": [vec(p, 3) for p in joints],
                                 "lengths": [r((joints[i + 1] - joints[i]).length, 3) for i in range(len(names))]}
        if volume is not None and volume.tree is not None:
            samples = [joints[i].lerp(joints[i + 1], f) for i in range(len(names)) for f in (0.15, 0.5, 0.85)]
            entry["inside_mesh"] = f"{round(100 * sum(1 for p in samples if volume.inside(p)[0]) / len(samples))}%"
        if curl:
            entry["bend"] = curl
        report.append(entry)
    return {"armature": arm_obj.name, "created_armature": created, "bones_total": len(arm_obj.data.bones), "chains": report,
            "next": "When the skeleton is complete: rig_bind, then check_rig, then look with focus_bones for close-ups of small bones."}
