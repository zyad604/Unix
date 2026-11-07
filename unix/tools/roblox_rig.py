# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Roblox Studio: rigs and their animation (the part of ``roblox.Sender`` that deals with bones).

A mesh that an armature deforms travels as a skinned mesh, and the armature's actions travel
with it. How, and why this way (each point was tried in Roblox Studio 0.742 before it was built):

* ``EditableMesh`` takes bones and weights (``AddBone``, ``SetVertexBones``,
  ``SetVertexBoneWeights``; four bones per vertex at most, weights kept as bytes). The MeshPart
  made from it is skinned, but Roblox makes no ``Bone`` instances for it: the plugin makes them,
  and a mesh finds its bones by name. So each armature becomes one invisible Part named after it
  that holds the ``Bone`` hierarchy, and every piece of every mesh of that rig is welded to it
  (a ``WeldConstraint`` is what lets a MeshPart use bones that live in another part). A mesh cut
  into pieces is therefore still one body on one skeleton.
* Roblox places a skinned vertex with ``bone's place now x inverse of the bone's place in the
  mesh``, both absolute. So a piece carries each bone's rest place in its own space, the skeleton
  carries the rest places in the world, and nothing depends on where a piece's centre happens to be.
* An action is sampled here (so IK, constraints and drivers arrive baked) and travels as poses:
  per key and bone a rotation (four 16-bit numbers) and, only for bones that really shift, an
  offset. That is ``Bone.Transform``, the same thing a Roblox animation sets. The poses are stored
  in the model as text (base64 in StringValues, which hold just under 200,000 characters each),
  and two fixed pieces of code play them: the plugin while editing, and a client-side script in
  the model in the game. Nothing has to be uploaded for an animation to play.
* Each action is also written as a ``KeyframeSequence`` under ``AnimSaves``, the form Roblox's
  own Animation Editor reads and publishes, for whoever wants real Animation assets.

What Roblox's bones cannot do is said in the report: they do not stretch, a vertex follows four
bones at most, and shape keys and "preserve volume" have no counterpart.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import math
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

import bpy
import numpy as np

from . import _util, motion
from .roblox import MOTION_SAMPLES, _TO_ROBLOX, _may_move, studs_per_unit

RIG_PLUGIN = 13                 # the first version of the bridge plugin that builds skinned meshes
INFLUENCES = 4                  # Roblox: a vertex follows at most this many bones
TEXT_PER_VALUE = 180_000        # a StringValue refuses 200,000 characters and more
VALUES_PER_JOB = 8              # ... and one job stays well under the link's two megabytes
SAMPLE_SECONDS = 0.06           # sampling actions, per call of Rigs.sample: Blender stays responsive
SHIFT_STUDS = 1e-3              # a bone whose offset never exceeds this is sent as rotation only
STRETCH = 0.02                  # a bone scaled by more than this is reported: Roblox bones are rigid
POSE_BUDGET = 40_000            # Pose instances per rig in its KeyframeSequences; longer actions get fewer keys


def plays_rigs(session: Dict[str, Any]) -> bool:
    version = str(session.get("version") or "")
    return version == "test" or (version.isdigit() and int(version) >= RIG_PLUGIN)


def deformer(obj: bpy.types.Object) -> Optional[Tuple[bpy.types.Object, str]]:
    """(armature, bone) for a mesh that bones move, else None. The bone is '' when vertex groups
    say which bones move which vertex (an Armature modifier), or the one bone the object rides on
    (it, or something it is parented to, is parented to that bone)."""
    for modifier in obj.modifiers:
        if modifier.type == 'ARMATURE' and modifier.object is not None and modifier.show_viewport \
                and modifier.use_vertex_groups:
            return modifier.object, ""
    if obj.parent is not None and obj.parent.type == 'ARMATURE' and obj.parent_type == 'ARMATURE':
        return obj.parent, ""
    rider = obj
    while rider.parent is not None:
        if rider.parent.type == 'ARMATURE' and rider.parent_type == 'BONE' and rider.parent_bone in rider.parent.data.bones:
            return rider.parent, rider.parent_bone
        rider = rider.parent
    return None


def _rigid(matrices: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """Blender world matrices (..., 4, 4) as rigid Roblox ones: studs, Y up, this send's offset
    removed, and no scale (a CFrame has none)."""
    out = np.zeros(matrices.shape)
    basis = matrices[..., :3, :3]
    basis = basis / np.maximum(np.linalg.norm(basis, axis=-2, keepdims=True), 1e-12)
    turn = _TO_ROBLOX @ basis @ _TO_ROBLOX.T
    mirrored = np.linalg.det(turn) < 0          # a mirrored armature: flipped the same way at rest and
    turn[..., 0] = np.where(mirrored[..., None], -turn[..., 0], turn[..., 0])       # in every pose, so the motion between is unchanged
    out[..., :3, :3] = turn
    out[..., :3, 3] = (matrices[..., :3, 3] @ _TO_ROBLOX.T) * studs_per_unit() - offset
    out[..., 3, 3] = 1.0
    return out


def _numbers(matrix: np.ndarray) -> List[float]:
    """The twelve numbers of a CFrame."""
    return [round(float(v), 5) for v in (*matrix[:3, 3], *matrix[:3, :3].ravel())]


def _quaternions(turns: np.ndarray) -> np.ndarray:
    """Rotation matrices (..., 3, 3) as quaternions (..., 4) in Roblox's order x, y, z, w."""
    m = turns
    w = np.sqrt(np.maximum(0.0, 1.0 + m[..., 0, 0] + m[..., 1, 1] + m[..., 2, 2])) / 2
    x = np.sqrt(np.maximum(0.0, 1.0 + m[..., 0, 0] - m[..., 1, 1] - m[..., 2, 2])) / 2
    y = np.sqrt(np.maximum(0.0, 1.0 - m[..., 0, 0] + m[..., 1, 1] - m[..., 2, 2])) / 2
    z = np.sqrt(np.maximum(0.0, 1.0 - m[..., 0, 0] - m[..., 1, 1] + m[..., 2, 2])) / 2
    # each is right up to its sign; the largest fixes the others' signs through the off-diagonal sums
    q = np.stack([x, y, z, w], axis=-1)
    lead = q.argmax(axis=-1)
    sums = {(0, 1): m[..., 1, 0] + m[..., 0, 1], (0, 2): m[..., 0, 2] + m[..., 2, 0], (1, 2): m[..., 2, 1] + m[..., 1, 2],
            (0, 3): m[..., 2, 1] - m[..., 1, 2], (1, 3): m[..., 0, 2] - m[..., 2, 0], (2, 3): m[..., 1, 0] - m[..., 0, 1]}
    for a in range(4):
        for b in range(4):
            if a != b:
                sign = np.where(sums[(min(a, b), max(a, b))] < 0, -1.0, 1.0)
                q[..., b] = np.where(lead == a, q[..., b] * sign, q[..., b])
    return q / np.maximum(np.linalg.norm(q, axis=-1, keepdims=True), 1e-12)


class _Rig:
    """One armature on its way to Roblox: which bones travel, where they rest, and its actions as poses."""

    def __init__(self, armature: bpy.types.Object, meshes: List[Tuple[bpy.types.Object, str]], offset: np.ndarray) -> None:
        self.armature, self.name, self.offset = armature, armature.name, offset
        bones = armature.data.bones
        wanted, self.loose = set(), False
        for obj, ridden in meshes:
            if ridden:
                wanted.add(ridden)
                continue
            groups = {g.index: g.name for g in obj.vertex_groups if g.name in bones and bones[g.name].use_deform}
            wanted.update(groups.values())
            # a vertex no bone holds stays where its object is: it needs something to hold on to
            self.loose = self.loose or any(not any(g.group in groups and g.weight > 0.0 for g in v.groups)
                                           for v in obj.data.vertices)
        for name in list(wanted):                   # the chain up to the root, so the hierarchy is the armature's
            parent = bones[name].parent
            while parent is not None and parent.name not in wanted:
                wanted.add(parent.name)
                parent = parent.parent
        self.bones: List[str] = []

        def walk(bone: bpy.types.Bone) -> None:     # parents before children
            if bone.name in wanted:
                self.bones.append(bone.name)
            for child in bone.children:
                walk(child)

        for bone in bones:
            if bone.parent is None:
                walk(bone)
        self.index = {name: at for at, name in enumerate(self.bones)}
        self.parents = [self.index[bones[name].parent.name] if bones[name].parent is not None else -1 for name in self.bones]
        self.still = -1
        if self.loose or not self.bones:
            name = self.name + "_Still"
            while name in bones:
                name += "_"
            self.still = len(self.bones)            # a bone that never moves on its own: it follows the armature object
            self.bones.append(name)
            self.parents.append(-1)
        place = np.array(armature.matrix_world, dtype=np.float64)
        self.rest_world = np.array([place @ np.array(bones[name].matrix_local, dtype=np.float64) if at != self.still else place
                                    for at, name in enumerate(self.bones)])
        self.rest_scale = np.linalg.norm(self.rest_world[:, :3, :3], axis=1)
        self.rest = _rigid(self.rest_world, offset)
        self.home = _rigid(place, offset)
        self.bendy = sorted(name for name in self.bones if name in bones and bones[name].bbone_segments > 1)

        current = armature.animation_data.action if armature.animation_data else None
        names = motion.actions_for(armature)
        self.todo: List[Optional[str]] = list(names) if names else [None]
        self.default = current.name if current is not None else (names[0] if names else "")
        self.tracks: List[Dict[str, Any]] = []
        self.frames: List[np.ndarray] = []          # world matrices of the action being sampled, key by key
        self.plan: Optional[Tuple[str, List[float], float, bool]] = None
        self.stretched: set = set()
        self.meshes: List[str] = []
        self.trimmed = 0

    # -- the skeleton --------------------------------------------------------------------

    def skeleton(self) -> Dict[str, Any]:
        return {"cframe": _numbers(self.home),
                "bones": [{"name": name, "parent": self.parents[at] + 1, "cframe": _numbers(self.rest[at])}
                          for at, name in enumerate(self.bones)]}

    def now(self, depsgraph=None) -> np.ndarray:
        """World matrices of the travelling bones in the pose the armature is in at this moment."""
        posed = self.armature.evaluated_get(depsgraph or bpy.context.evaluated_depsgraph_get())
        place = np.array(posed.matrix_world, dtype=np.float64)
        pose = posed.pose.bones
        return np.array([place @ np.array(pose[name].matrix, dtype=np.float64) if at != self.still else place
                         for at, name in enumerate(self.bones)])

    # -- actions -------------------------------------------------------------------------

    def _plan(self, name: Optional[str]) -> Tuple[str, List[float], float, bool]:
        """(track name, the frames to sample, keys per second, whether it is marked as a loop)."""
        scene = bpy.context.scene
        rate = scene.render.fps / (scene.render.fps_base or 1.0)
        if name is not None:
            action = bpy.data.actions[name]
            first, last = (float(v) for v in action.frame_range)
            loop = bool(getattr(action, "use_cyclic", False))
        elif _may_move(self.armature) or any(len(bone.constraints) for bone in self.armature.pose.bones):
            first, last = (scene.frame_preview_start, scene.frame_preview_end) if scene.use_preview_range \
                else (scene.frame_start, scene.frame_end)
            name, loop = "Timeline", False          # no action, but something moves it: what the timeline shows
        else:
            return "Pose", [float(scene.frame_current) + scene.frame_subframe], rate, False     # it just stands in a pose
        count = int(min(MOTION_SAMPLES, max(2, round(last - first) + 1)))
        if last <= first:
            return name, [first], rate, loop
        return name, [first + (last - first) * at / (count - 1) for at in range(count)], rate * (count - 1) / (last - first), loop

    def sample(self, until: float, stage: Optional[bpy.types.Scene] = None) -> bool:
        """Sample a few keys of the next action, played in ``stage`` (a scene of the rigs alone) when
        given. True when every action has been sampled. The armature has its own action again, and the
        scene its own frame, whenever this returns."""
        if not self.todo:
            return True
        name = self.todo[0]
        if self.plan is None:
            self.plan, self.frames = self._plan(name), []
        _called, frames, _rate, _loop = self.plan
        scene = stage or bpy.context.scene
        action = bpy.data.actions.get(name) if name is not None else None
        with motion.Showing(self.armature, action, scene):
            while len(self.frames) < len(frames):
                frame = frames[len(self.frames)]
                scene.frame_set(int(math.floor(frame)), subframe=frame - math.floor(frame))
                # the scene's own evaluation, which its first frame_set makes (before it there is none)
                self.frames.append(self.now(stage.view_layers[0].depsgraph if stage is not None else None))
                if time.monotonic() > until:
                    break
        if len(self.frames) == len(frames):
            self._finish()
            self.todo.pop(0)
            self.plan = None
        return not self.todo

    def _finish(self) -> None:
        """Turn the sampled world matrices into what Roblox plays: each bone's pose relative to its
        rest, in its parent's space (``Bone.Transform``)."""
        called, _frames, rate, loop = self.plan
        world = np.array(self.frames)                                   # (keys, bones, 4, 4)
        scale = np.linalg.norm(world[:, :, :3, :3], axis=2)
        off = np.abs(scale / np.maximum(self.rest_scale, 1e-12) - 1.0).max(axis=(0, 2)) > STRETCH
        self.stretched.update(name for name, is_off in zip(self.bones, off) if is_off)
        now = _rigid(world, self.offset)
        parents = np.array(self.parents)
        rooted = parents < 0
        above_now = now[:, np.where(rooted, 0, parents)]
        above_rest = self.rest[np.where(rooted, 0, parents)]
        above_now[:, rooted] = np.eye(4)                                # a root bone hangs on the part, which stays put
        above_rest[rooted] = np.eye(4)
        # now = (parent now) x (parent rest)^-1 x (rest) x Transform
        pose = np.linalg.inv(self.rest) @ above_rest @ np.linalg.inv(above_now) @ now
        turns = _quaternions(pose[..., :3, :3])
        for key in range(len(turns)):                                   # the short way round from each key to the next
            before = turns[key - 1] if key else np.array([0.0, 0.0, 0.0, 1.0])
            flip = (turns[key] * before).sum(axis=1) < 0
            turns[key, flip] *= -1.0
        shifts = pose[..., :3, 3]
        moving = [int(at) for at in np.where(np.abs(shifts).max(axis=(0, 2)) > SHIFT_STUDS)[0]]
        keys = len(turns)
        data = np.concatenate([np.round(turns * 32767.0).astype("<i2").reshape(keys, -1).view(np.uint8),
                               shifts[:, moving].astype("<f4").reshape(keys, -1).view(np.uint8)], axis=1)
        self.tracks.append({"name": called, "fps": round(float(rate), 4), "frames": keys, "moving": moving, "loop": loop,
                            "text": base64.b64encode(data.tobytes()).decode("ascii")})
        self.frames = []

    # -- skinning ------------------------------------------------------------------------

    def weights(self, obj: bpy.types.Object, mesh: bpy.types.Mesh, ridden: str) -> Tuple[np.ndarray, np.ndarray]:
        """For every vertex of the evaluated mesh: its (up to four) bones and their weights as
        bytes that add up to 255. Blender divides by the sum of the bone weights; so is done here."""
        count = len(mesh.vertices)
        bones = np.zeros((count, INFLUENCES), dtype=np.int64)
        shares = np.zeros((count, INFLUENCES), dtype=np.float64)
        if ridden:
            bones[:, 0], shares[:, 0] = self.index[ridden], 1.0
        else:
            groups = {g.index: self.index[g.name] for g in obj.vertex_groups if g.name in self.index
                      and self.armature.data.bones[g.name].use_deform}
            rows, columns, values = [], [], []
            for vertex in mesh.vertices:
                for held in vertex.groups:
                    bone = groups.get(held.group)
                    if bone is not None and held.weight > 0.0:
                        rows.append(vertex.index)
                        columns.append(bone)
                        values.append(held.weight)
            rows, columns, values = np.array(rows, dtype=np.int64), np.array(columns, dtype=np.int64), np.array(values)
            order = np.lexsort((-values, rows))                         # per vertex, the strongest first
            rows, columns, values = rows[order], columns[order], values[order]
            rank = np.arange(len(rows)) - np.searchsorted(rows, rows, side="left")
            kept = rank < INFLUENCES
            self.trimmed += len(set(rows[~kept].tolist()))
            bones[rows[kept], rank[kept]] = columns[kept]
            shares[rows[kept], rank[kept]] = values[kept]
        total = shares.sum(axis=1)
        held = total > 0.0
        shares[held] /= total[held][:, None]
        if (~held).any():                                               # no bone holds it: it stays with the armature object
            bones[~held, 0] = self.still if self.still >= 0 else 0
            shares[~held, 0] = 1.0
        exact = shares * 255.0
        whole = np.floor(exact)
        short = (255 - whole.sum(axis=1)).astype(np.int64)              # the bytes must add up to 255 exactly
        by_rest = np.argsort(-(exact - whole), axis=1, kind="stable")
        for extra in range(INFLUENCES - 1):
            rows = np.where(short > extra)[0]
            whole[rows, by_rest[rows, extra]] += 1
        return bones, whole.astype(np.uint8)

    def skin(self, obj: bpy.types.Object, mesh: bpy.types.Mesh, ridden: str, pieces: List[Dict[str, Any]]) -> None:
        """Give each piece its geometry and its skin: the bones it uses, where each rests in the
        piece's own space, and every vertex's bones and weights."""
        bones, shares = self.weights(obj, mesh, ridden)
        # a rider is bound as it stands now, on the bone as it stands now; a deformed mesh in its rest shape, at rest
        places = _rigid(self.now(), self.offset) if ridden else self.rest
        for piece in pieces:
            shape = piece.pop("_shape")
            points, normals, uv, triangles = shape["points"], shape["normals"], shape["uv"], shape["triangles"]
            own_bones, own_shares = bones[shape["vertices"]], shares[shape["vertices"]]
            used = set(own_bones[own_shares > 0].tolist())
            for at in list(used):                                       # with the chain above each, parents first
                above = self.parents[at]
                while above >= 0 and above not in used:
                    used.add(above)
                    above = self.parents[above]
            used = sorted(used)
            local = np.zeros(len(self.bones), dtype=np.int64)
            local[used] = np.arange(len(used))
            part = np.eye(4)                                            # the part as the plugin will place it
            part[:3, :3] = np.array(piece["rot"]).reshape(3, 3) if "rot" in piece else np.eye(3)
            part[:3, 3] = piece["center"]
            bind = np.linalg.inv(part) @ places[used]
            wide = len(used) > 256
            packed = np.where(own_shares > 0, local[own_bones], 0).astype("<u2" if wide else np.uint8)
            skin = {"rig": self.name, "names": [self.bones[at] for at in used],
                    "parents": [int(local[self.parents[at]]) + 1 if self.parents[at] >= 0 else 0 for at in used],
                    "bind": [_numbers(matrix) for matrix in bind],
                    "vb": base64.b64encode(packed.tobytes()).decode("ascii"),
                    "vw": base64.b64encode(own_shares.tobytes()).decode("ascii")}
            if wide:
                skin["wide"] = True
            digest = hashlib.sha1(points.tobytes() + normals.tobytes() + triangles.tobytes())
            if uv is not None:
                digest.update(uv.tobytes())
            digest.update(packed.tobytes() + own_shares.tobytes() + np.round(bind, 4).tobytes() + "|".join(skin["names"]).encode())
            piece.update(key=digest.hexdigest()[:16], v=np.round(points * shape["extent"], 3).ravel().tolist(),
                         n=normals.ravel().tolist(), t=triangles.tolist(), skin=skin)
            if uv is not None:
                piece["uv"] = uv.ravel().tolist()
        self.meshes.append(obj.name)


class Rigs:
    """The armatures behind the meshes of one send."""

    def __init__(self, meshes: List[bpy.types.Object], offset: np.ndarray, session: Dict[str, Any]) -> None:
        found = {obj.name: deformer(obj) for obj in meshes}
        found = {name: hit for name, hit in found.items() if hit is not None}
        self.live = plays_rigs(session)
        self.old = sorted(found) if not self.live else []        # an older plugin: these arrive posed and rigid, as before
        self.by_mesh: Dict[str, Tuple[_Rig, str]] = {}
        self.rigs: List[_Rig] = []
        self.unkept: Dict[str, List[str]] = {}
        self.began = 0.0
        self.seconds = 0.0
        self.queued = False
        self.stage: Optional[bpy.types.Scene] = None
        if not self.live:
            return
        by_armature: Dict[str, List[Tuple[bpy.types.Object, str]]] = {}
        for obj in meshes:
            if obj.name in found:
                by_armature.setdefault(found[obj.name][0].name, []).append((obj, found[obj.name][1]))
        for name, bound in by_armature.items():
            rig = _Rig(bpy.data.objects[name], bound, offset)
            self.rigs.append(rig)
            for obj, ridden in bound:
                self.by_mesh[obj.name] = (rig, ridden)
            for obj, ridden in bound:
                self._notes(obj, ridden)

    def _notes(self, obj: bpy.types.Object, ridden: str) -> None:
        """What this mesh has that Roblox's skinning has no counterpart for."""
        def note(what: str) -> None:
            self.unkept.setdefault(what, []).append(obj.name)

        if obj.data.shape_keys is not None and len(obj.data.shape_keys.key_blocks) > 1:
            note("shape keys (the mesh arrives with their current values; they do not animate)")
        for modifier in obj.modifiers:
            if modifier.type == 'ARMATURE' and modifier.show_viewport and not ridden:
                if modifier.use_deform_preserve_volume:
                    note("'Preserve Volume' (Roblox blends bones linearly, so sharp bends pinch a little more)")
                if modifier.use_bone_envelopes:
                    note("bone envelopes (only vertex-group weights travel)")
            elif modifier.type in {'CLOTH', 'SOFT_BODY', 'MESH_DEFORM', 'LATTICE', 'HOOK', 'SURFACE_DEFORM', 'MESH_CACHE',
                                   'MESH_SEQUENCE_CACHE'} and modifier.show_viewport:
                note(f"the {modifier.name} modifier's own motion (its shape at the current frame is what travels)")

    # -- used by the Sender ----------------------------------------------------------------

    def of(self, obj: bpy.types.Object) -> Optional[_Rig]:
        hit = self.by_mesh.get(obj.name)
        return hit[0] if hit else None

    def others(self, objects: List[bpy.types.Object]) -> List[bpy.types.Object]:
        """The objects whose motion is their own transform: bones move the rest."""
        return [obj for obj in objects if obj.name not in self.by_mesh]

    def opening(self) -> Dict[str, Any]:
        """What the job that opens the model carries: every skeleton, so pieces can be hung on them."""
        return {"rigs": {rig.name: rig.skeleton() for rig in self.rigs}} if self.rigs else {}

    @contextlib.contextmanager
    def resting(self, obj: bpy.types.Object) -> Iterator[None]:
        """While a deformed mesh is converted its Armature modifiers are off, so what is read is its
        rest shape with every other modifier applied. They are switched back on afterwards."""
        hit = self.by_mesh.get(obj.name)
        off = [m for m in obj.modifiers if m.type == 'ARMATURE' and m.show_viewport] if hit and not hit[1] else []
        for modifier in off:
            modifier.show_viewport = False
        try:
            yield
        finally:
            for modifier in off:
                modifier.show_viewport = True

    def skin(self, obj: bpy.types.Object, mesh: bpy.types.Mesh, pieces: List[Dict[str, Any]]) -> int:
        """Skin the pieces of a mesh that bones move. Returns how many pieces that was (0: not such a mesh)."""
        hit = self.by_mesh.get(obj.name)
        if hit is None or not pieces:
            return 0
        hit[0].skin(obj, mesh, hit[1], pieces)
        return len(pieces)

    def sample(self, asset: str, jobs: List[Dict[str, Any]]) -> bool:
        """Sample the rigs' actions, a few keys per call. True when there is nothing left to
        sample; the jobs that carry the poses have then been added to ``jobs``."""
        if self.queued or not self.rigs:
            return True
        began = time.monotonic()
        waiting = [rig for rig in self.rigs if rig.todo]
        if waiting:
            rig = waiting[0]
            total = sum(len(r.tracks) + len(r.todo) for r in self.rigs)
            done = sum(len(r.tracks) for r in self.rigs)
            _util.status["text"] = f"Reading animation for Roblox  {done + 1} of {total}"
            try:
                finished = rig.sample(began + SAMPLE_SECONDS, self._stage())
            except ReferenceError:                  # the armature was deleted while we were sending
                rig.todo, finished = [], True
            self.seconds += time.monotonic() - began
            if not finished or len(waiting) > 1:
                return False
        self.close()
        for rig in self.rigs:
            jobs.extend(self._jobs(asset, rig))
        self.queued = True
        return True

    def _stage(self) -> bpy.types.Scene:
        """A scene of the rigs alone (and what they need), the frames are played in. Played in the user's
        scene every frame evaluated the whole world: 130 actions of 13 creatures in a scene of 3,500 objects
        took more than half an hour."""
        if self.stage is None:
            from .files import _needed
            source = bpy.context.scene
            stage = bpy.data.scenes.new("_unix_motion")
            stage.render.fps, stage.render.fps_base = source.render.fps, source.render.fps_base
            stage.frame_start, stage.frame_end = source.frame_start, source.frame_end
            for obj in _needed([rig.armature for rig in self.rigs]):
                stage.collection.objects.link(obj)
            self.stage = stage
        return self.stage

    def close(self) -> None:
        """Remove the rigs' scene (a send that ends early calls this too)."""
        stage, self.stage = self.stage, None
        if stage is not None:
            try:
                bpy.data.scenes.remove(stage)
            except ReferenceError:
                pass

    def _jobs(self, asset: str, rig: _Rig) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        poses = sum(track["frames"] for track in rig.tracks) * len(rig.bones)
        for track in rig.tracks:
            text = track["text"]
            values = [text[at:at + TEXT_PER_VALUE] for at in range(0, len(text), TEXT_PER_VALUE)]
            for first in range(0, len(values), VALUES_PER_JOB):
                job = {"kind": "motion", "asset": asset, "rig": rig.name, "name": track["name"], "at": first,
                       "values": values[first:first + VALUES_PER_JOB]}
                if first == 0:
                    job.update(fps=track["fps"], frames=track["frames"], moving=track["moving"], loop=track["loop"])
                out.append(job)
        names = [track["name"] for track in rig.tracks]
        out.append({"kind": "motion", "asset": asset, "rig": rig.name, "done": True,
                    "default": rig.default if rig.default in names else (names[0] if names else ""),
                    "thin": max(1, math.ceil(poses / POSE_BUDGET))})
        return out

    # -- the report --------------------------------------------------------------------

    def report(self, replies: List[Dict[str, Any]], uploaded: bool) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        if self.old:
            out["not_transferred"] = (f"Bones and animation of {', '.join(self.old[:5])}: the Roblox Studio that is open still "
                                      f"runs an older Unix Bridge, which cannot build skinned meshes. The mesh arrived in its "
                                      f"current pose and moves only as a whole. Tell the user to restart Roblox Studio once and "
                                      f"send again.")
        if not self.rigs:
            return out
        built = {"skinned": 0, "unskinned": [], "playing": [], "keyframes": 0, "script": True}
        for reply in replies:
            report = reply.get("report") or {}
            built["skinned"] += int(report.get("skinned") or 0)
            built["unskinned"] += [str(name) for name in (report.get("unskinned") or [])]
            if report.get("rig_ready"):
                built["playing"].append(str(report["rig_ready"]))
                built["keyframes"] += int(report.get("keyframes") or 0)
                built["script"] = built["script"] and report.get("script") is not False
        bones = sum(len(rig.bones) for rig in self.rigs)
        meshes = sum(len(rig.meshes) for rig in self.rigs)
        names = ", ".join(rig.name for rig in self.rigs[:4])
        out["rig"] = (f"{meshes} meshes arrived skinned ({built['skinned']} mesh pieces) to {bones} bones of {names}: the "
                      f"bones stand in the armature's hierarchy as Bone instances under an invisible part named after the "
                      f"armature, every piece is welded to it, and each vertex follows up to {INFLUENCES} bones with its "
                      f"Blender weights.")
        tracks = [track for rig in self.rigs for track in rig.tracks]
        played = [track for track in tracks if track["frames"] > 1]
        if played:
            listed = ", ".join(f"{t['name']} ({t['frames']} keys)" for t in played[:12]) + (" ..." if len(played) > 12 else "")
            default = next((rig.default for rig in self.rigs if rig.default), played[0]["name"])
            out["animations"] = (
                f"{len(played)} actions travelled as sampled poses (IK, constraints and drivers baked in), reading them took "
                f"{round(self.seconds, 1)} s: {listed}. They loop. In Edit mode the plugin plays them now; in Play the "
                f"UnixRigAnimator script in the model plays them on each player's screen, with no animation asset. The "
                f"model's attributes steer it: Animation = the action's name (now '{default}'), Enabled = false stops it "
                f"and true starts it, AnimationSpeed = 1 is normal speed.")
            if built["keyframes"]:
                out["animations"] += (f" Each action is also a KeyframeSequence in the model's AnimSaves, the form Roblox's "
                                      f"Animation Editor reads: from there the user can publish it as an Animation asset "
                                      f"himself, if he wants one.")
            if not built["script"]:
                out["animations"] += (" Studio blocked writing the script, so they play in Edit mode only: allow script "
                                      "injection for Unix Bridge (Plugins > Manage Plugins) and send again.")
        elif tracks:
            out["pose"] = ("The rig has no action, so it stands in Roblox in the pose it has here; its bones are there to be "
                           "animated.")
        missing = [rig.name for rig in self.rigs if rig.name not in built["playing"]]
        if missing:
            out["rig_problem"] = (f"Studio did not confirm the animation of {', '.join(missing)}: see problems. Say so.")
        if built["unskinned"]:
            out["rig_problem"] = (out.get("rig_problem", "") + f" {len(built['unskinned'])} mesh pieces came back from "
                                  f"Roblox without their skinning ({', '.join(built['unskinned'][:5])}): they stand still in "
                                  f"the rest pose. Say so.").strip()
        lost = []
        trimmed = sum(rig.trimmed for rig in self.rigs)
        if trimmed:
            lost.append(f"{trimmed} vertices were held by more than {INFLUENCES} bones: the {INFLUENCES} strongest were kept "
                        f"and made to add up to 1")
        stretched = sorted(name for rig in self.rigs for name in rig.stretched)
        if stretched:
            lost.append(f"bone scaling (stretch) on {', '.join(stretched[:6])}: Roblox bones turn and shift but do not scale")
        bendy = sorted(name for rig in self.rigs for name in rig.bendy)
        if bendy:
            lost.append(f"bendy-bone curves of {', '.join(bendy[:6])}: each travels as one straight bone")
        for what, objects in self.unkept.items():
            lost.append(f"{what} on {', '.join(objects[:5])}")
        if lost:
            out["rig_not_carried"] = "; ".join(lost) + ". Tell the user what matters of this."
        if not uploaded:
            out["rig_in_play"] = ("Like every mesh, a skinned mesh is part of the game only once it is uploaded (see "
                                  "preview_only): until then the character is a set of checkered boxes in Play, whatever "
                                  "its bones do.")
        return out
