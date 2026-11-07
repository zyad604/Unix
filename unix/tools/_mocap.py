# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Captured motion as plain data. No tools are registered here.

Hand-typed gait maths gives a character that leans the wrong way and holds its arms like wings;
motion recorded from a real person does not. Three pieces make recorded motion usable on any rig:

* ``read_acclaim`` reads an Acclaim skeleton (.asf) and motion (.amc), the format of the CMU
  Graphics Lab Motion Capture Database, into a ``Clip``.
* ``Clip`` and ``RigMotion`` (an armature in the scene that carries an action: an imported BVH
  or FBX) answer the same question for any moment: how far has every bone turned from its rest
  pose, and where are its joints, in Blender's world axes and in metres.
* ``roles`` names the bones of a humanoid skeleton by what they are (hips, spine, thigh...), so
  two skeletons with different names, proportions and rest poses can be matched bone for bone.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from mathutils import Matrix, Quaternion, Vector

Sample = Dict[str, Tuple[Quaternion, Vector, Vector]]     # bone -> (turn from rest, head, tail), world

# ---------------------------------------------------------------------------------------------
# Acclaim ASF / AMC


def _turns(angles: List[float], order: str) -> Matrix:
    """Rotation for angles (degrees) applied one after the other, in the listed order, about fixed axes."""
    out = Matrix.Identity(3)
    for letter, angle in zip(order.upper(), angles):
        out = Matrix.Rotation(math.radians(angle), 3, letter) @ out
    return out


def slerp(a: Quaternion, b: Quaternion, t: float) -> Quaternion:
    """The short way from a to b. mathutils' own slerp goes the long way round when the two have opposite signs,
    and q and -q are the same turn, so that happens whenever a turn passes half a revolution."""
    return a.slerp(-b if a.dot(b) < 0.0 else b, t)


def _to_blender(v: Vector) -> Vector:
    """Acclaim is Y up, facing +Z; Blender is Z up, facing -Y."""
    return Vector((v.x, -v.z, v.y))


def _quat_to_blender(m: Matrix) -> Quaternion:
    q = m.to_quaternion()
    return Quaternion((q.w, q.x, -q.z, q.y))


class Clip:
    """One captured motion: a skeleton and its channel values frame by frame."""

    def __init__(self) -> None:
        self.names: List[str] = ["root"]
        self.parent: Dict[str, Optional[str]] = {"root": None}
        self.direction: Dict[str, Vector] = {}
        self.length: Dict[str, float] = {}
        self.frame_of: Dict[str, Matrix] = {"root": Matrix.Identity(3)}     # each bone's own axes, in the rest pose
        self.dof: Dict[str, List[str]] = {}
        self.root_order: List[str] = ["tx", "ty", "tz", "rx", "ry", "rz"]
        self.root_position = Vector((0.0, 0.0, 0.0))
        self.scale = 0.0254 / 0.45          # file units to metres
        self.fps = 120.0
        self.frames: List[Dict[str, List[float]]] = []
        self._cache: Dict[int, Sample] = {}
        self._rest: Optional[Sample] = None

    @property
    def seconds(self) -> float:
        return max(len(self.frames) - 1, 0) / self.fps

    def _pose(self, values: Optional[Dict[str, List[float]]]) -> Sample:
        turn: Dict[str, Matrix] = {}
        end: Dict[str, Vector] = {}
        out: Sample = {}
        for name in self.names:
            given = (values or {}).get(name, [])
            if name == "root":
                channels = dict(zip(self.root_order, given))
                moved = Vector((channels.get("tx", 0.0), channels.get("ty", 0.0), channels.get("tz", 0.0)))
                order = [c[1] for c in self.root_order if c[0] == "r"]
                local = _turns([channels.get("r" + axis, 0.0) for axis in order], "".join(order))
                axes = self.frame_of["root"]
                turn[name] = axes @ local @ axes.transposed()
                end[name] = (self.root_position + moved) * self.scale
                head = end[name]
            else:
                dof = self.dof.get(name, [])
                local = _turns(list(given[:len(dof)]), "".join(d[1] for d in dof)) if dof else Matrix.Identity(3)
                axes = self.frame_of[name]
                turn[name] = turn[self.parent[name]] @ axes @ local @ axes.transposed()
                head = end[self.parent[name]]
                end[name] = head + turn[name] @ self.direction[name] * (self.length[name] * self.scale)
            out[name] = (_quat_to_blender(turn[name]), _to_blender(head), _to_blender(end[name]))
        return out

    def rest(self) -> Sample:
        if self._rest is None:
            self._rest = self._pose(None)
        return self._rest

    def pose(self, index: int) -> Sample:
        index = max(0, min(index, len(self.frames) - 1))
        if index not in self._cache:
            if len(self._cache) > 4000:
                self._cache.clear()
            self._cache[index] = self._pose(self.frames[index])
        return self._cache[index]

    def sample(self, seconds: float) -> Sample:
        at = max(0.0, min(seconds * self.fps, len(self.frames) - 1.0))
        low = int(math.floor(at))
        part = at - low
        first = self.pose(low)
        if part < 1e-4:
            return first
        second = self.pose(low + 1)
        return {name: (slerp(first[name][0], second[name][0], part), first[name][1].lerp(second[name][1], part),
                       first[name][2].lerp(second[name][2], part)) for name in self.names}


def read_acclaim(skeleton: str, motion: str) -> Clip:
    """Parse the text of an .asf and an .amc file. Raises ValueError when they are not that."""
    clip = Clip()
    section = ""
    bone: Dict[str, Any] = {}
    length_unit = 0.45
    in_hierarchy = False
    for raw in skeleton.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(":"):
            section = line[1:].split()[0].lower()
            continue
        words = line.split()
        key = words[0].lower()
        if section == "units" and key == "length":
            length_unit = float(words[1]) or 0.45
        elif section == "root":
            if key == "order":
                clip.root_order = [w.lower() for w in words[1:]]
            elif key == "position":
                clip.root_position = Vector([float(w) for w in words[1:4]])
            elif key == "orientation":
                clip.frame_of["root"] = _turns([float(w) for w in words[1:4]], "XYZ")
        elif section == "bonedata":
            if key == "begin":
                bone = {"dof": []}
            elif key == "end":
                name = bone.get("name")
                if not name or "direction" not in bone or "length" not in bone:
                    raise ValueError("The skeleton file has a bone without a name, direction or length.")
                clip.names.append(name)
                clip.direction[name] = bone["direction"]
                clip.length[name] = bone["length"]
                clip.frame_of[name] = bone.get("axis", Matrix.Identity(3))
                clip.dof[name] = bone["dof"]
            elif key == "name":
                bone["name"] = words[1]
            elif key == "direction":
                bone["direction"] = Vector([float(w) for w in words[1:4]])
            elif key == "length":
                bone["length"] = float(words[1])
            elif key == "axis":
                bone["axis"] = _turns([float(w) for w in words[1:4]], words[4] if len(words) > 4 else "XYZ")
            elif key == "dof":
                bone["dof"] = [w.lower() for w in words[1:] if w.lower() in ("rx", "ry", "rz")]
        elif section == "hierarchy":
            if key == "begin":
                in_hierarchy = True
            elif key == "end":
                in_hierarchy = False
            elif in_hierarchy:
                for child in words[1:]:
                    clip.parent[child] = words[0]
    clip.scale = 0.0254 / length_unit
    if len(clip.names) < 2 or any(name not in clip.parent for name in clip.names):
        raise ValueError("That is not an Acclaim skeleton (.asf) file.")
    depth = {name: 0 for name in clip.names}
    for name in clip.names:                      # parents before children, whatever order the file lists them in
        steps, at = 0, name
        while clip.parent[at] is not None:
            at = clip.parent[at]
            steps += 1
            if steps > len(clip.names):
                raise ValueError("The skeleton's hierarchy loops.")
        depth[name] = steps
    clip.names.sort(key=lambda name: depth[name])

    frame: Optional[Dict[str, List[float]]] = None
    for raw in motion.splitlines():
        line = raw.strip()
        if not line or line[0] in "#:":
            continue
        words = line.split()
        if len(words) == 1 and words[0].isdigit():
            frame = {}
            clip.frames.append(frame)
        elif frame is not None:
            try:
                frame[words[0]] = [float(w) for w in words[1:]]
            except ValueError:
                raise ValueError("The motion file has a line that is not numbers.")
    if not clip.frames:
        raise ValueError("That is not an Acclaim motion (.amc) file.")
    return clip


# ---------------------------------------------------------------------------------------------
# an armature in the scene as a motion source


class RigMotion:
    """An armature object whose action is the motion (an imported BVH or FBX, or another rig)."""

    def __init__(self, obj, fps: float, first: float, last: float) -> None:
        import bpy
        self._bpy = bpy
        self.obj = obj
        self.fps = fps
        self.first = first
        self.seconds = max(last - first, 0.0) / fps
        world = obj.matrix_world
        order: List[Any] = []
        stack = [b for b in obj.data.bones if b.parent is None][::-1]
        while stack:
            b = stack.pop()
            order.append(b)
            stack.extend(list(b.children)[::-1])
        self.names = [b.name for b in order]
        self.parent = {b.name: (b.parent.name if b.parent else None) for b in order}
        self._rest_turn = {b.name: (world @ b.matrix_local).to_quaternion() for b in order}
        self._rest: Sample = {b.name: (Quaternion(), world @ b.head_local, world @ b.tail_local) for b in order}

    def rest(self) -> Sample:
        return self._rest

    def sample(self, seconds: float) -> Sample:
        bpy = self._bpy
        at = self.first + max(0.0, min(seconds, self.seconds)) * self.fps
        whole = int(math.floor(at + 1e-6))
        scene = bpy.context.scene
        scene.frame_set(whole, subframe=max(0.0, min(at - whole, 0.999)))
        posed = self.obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
        world = posed.matrix_world
        out: Sample = {}
        for name in self.names:
            pbone = posed.pose.bones[name]
            turn = (world @ pbone.matrix).to_quaternion() @ self._rest_turn[name].inverted()
            out[name] = (turn, world @ pbone.head, world @ pbone.tail)
        return out


# ---------------------------------------------------------------------------------------------
# which bone is what

SIDED = ("clavicle", "upper_arm", "forearm", "hand", "thigh", "shin", "foot", "toe")
_WORDS = {
    "hips": ("hips", "hip", "pelvis"),
    "spine": ("spine", "lowerback", "upperback", "thorax", "chest", "torso", "abdomen", "waist", "back", "belly", "stomach"),
    "neck": ("neck", "lowerneck", "upperneck"),
    "head": ("head",),
    "clavicle": ("clavicle", "shoulder", "collar", "collarbone"),
    "upper_arm": ("upperarm", "arm", "humerus", "uparm", "bicep"),
    "forearm": ("forearm", "lowerarm", "radius", "lowarm", "elbow"),
    "hand": ("hand", "wrist"),
    "thigh": ("thigh", "upleg", "upperleg", "femur"),
    "shin": ("shin", "calf", "lowerleg", "leg", "tibia", "knee", "lowleg"),
    "foot": ("foot", "ankle"),
    "toe": ("toe", "toes", "toebase", "ball"),
}
_ROLE_OF = {word: role for role, words in _WORDS.items() for word in words}
_LEFT, _RIGHT = {"l", "left", "lft"}, {"r", "right", "rgt"}
_NOISE = {"mixamorig", "bip", "def", "org", "bone", "jnt", "joint", "rig", "valvebiped", "character", "b", "j", "fk", "skel"}
_CONTROL = {"ik", "pole", "target", "ctrl", "control", "twist", "roll", "mch", "helper", "end", "nub", "tweak"}
_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _read_name(name: str) -> Tuple[str, str, bool]:
    """A bone name as (what it is called without its side, its side 'L'/'R'/'', is it a control)."""
    tokens = [t.lower() for t in _TOKEN.findall(name)]
    side = ""
    kept = []
    for token in tokens:
        if token in _LEFT:
            side = "L"
        elif token in _RIGHT:
            side = "R"
        elif token.isdigit() or token in _NOISE:
            continue
        else:
            kept.append(token)
    control = any(t in _CONTROL for t in kept)
    kept = [t for t in kept if t not in _CONTROL]
    key = "".join(kept)
    if not side and len(kept) == 1 and key[:1] in "lr" and key[1:] in _ROLE_OF:      # 'lfemur', 'rhand'
        side, key = key[0].upper(), key[1:]
    return key, side, control


def roles(names: List[str], parent: Dict[str, Optional[str]], deform: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """Name the bones of a humanoid skeleton: {'hips': bone, 'spine': [bones], 'neck': [bones],
    'head': bone, 'thigh.L': bone, ...}. Only what was found is in the result."""
    def depth(name: str) -> int:
        steps = 0
        while parent.get(name):
            name = parent[name]
            steps += 1
        return steps

    def ancestors(name: str) -> List[str]:
        out = []
        while parent.get(name):
            name = parent[name]
            out.append(name)
        return out

    found: Dict[str, List[Tuple[int, int, str]]] = {}
    for name in names:
        key, side, control = _read_name(name)
        role = _ROLE_OF.get(key)
        if role is None or (role in SIDED) != bool(side):
            continue
        rank = (1 if control else 0) + (0 if deform is None or deform.get(name, True) else 2)
        found.setdefault(f"{role}.{side}" if side else role, []).append((rank, depth(name), name))
    best = {role: min(options)[2] for role, options in found.items()}

    out: Dict[str, Any] = {}
    for side in "LR":
        for upper, lower in (("thigh", "shin"), ("shin", "foot"), ("foot", "toe"), ("clavicle", "upper_arm"),
                             ("upper_arm", "forearm"), ("forearm", "hand")):
            a, b = best.get(f"{upper}.{side}"), best.get(f"{lower}.{side}")
            if a and b and a not in ancestors(b):                 # the lower bone must hang from the upper one
                options = [n for _r, _d, n in sorted(found[f"{lower}.{side}"]) if a in ancestors(n)]
                if options:
                    best[f"{lower}.{side}"] = options[0]
                else:
                    best.pop(f"{lower}.{side}")
        for role in SIDED:
            if f"{role}.{side}" in best:
                out[f"{role}.{side}"] = best[f"{role}.{side}"]
    thighs = [out.get("thigh.L"), out.get("thigh.R")]
    hips = best.get("hips")
    if all(thighs):
        shared = [a for a in ancestors(thighs[0]) if a in ancestors(thighs[1])]
        if hips is None or hips not in shared:
            hips = shared[0] if shared else None
    if hips:
        out["hips"] = hips
    head = best.get("head")
    if head and hips and hips in ancestors(head):
        out["head"] = head
        between = [a for a in ancestors(head) if a != hips and hips in ancestors(a)][::-1]
        neck = [n for n in between if _ROLE_OF.get(_read_name(n)[0]) == "neck"]
        spine = [n for n in between if n not in neck]
        if spine:
            out["spine"] = spine
        if neck:
            out["neck"] = neck
    elif hips:
        spine = sorted((n for options in (found.get("spine", []),) for _r, _d, n in options if hips in ancestors(n)), key=depth)
        if spine:
            out["spine"] = spine
        if head:
            out["head"] = head
    return out


def pairs(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, str]:
    """Target bone -> source bone, for every role both skeletons have. Chains of different
    lengths (three spine bones against five) are matched by how far along the chain each sits."""
    out: Dict[str, str] = {}
    for role, bone in target.items():
        other = source.get(role)
        if other is None:
            continue
        if isinstance(bone, list):
            for index, name in enumerate(bone):
                along = index / (len(bone) - 1) if len(bone) > 1 else 0.5
                out[name] = other[min(len(other) - 1, int(round(along * (len(other) - 1))))]
        else:
            out[bone] = other
    return out
