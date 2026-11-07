# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Real motion on any rig.

A walk typed as sine waves reads as a machine. People have been recorded doing nearly
everything a character is asked to do, and that data is free: ``motion_search`` looks up the
CMU Graphics Lab Motion Capture Database (about 2,600 recorded moves, free for any use, no key
and no account), and ``retarget_motion`` puts a recorded move on the user's rig, whatever its
bone names, proportions and rest pose.

How the transfer works, so that it holds for any humanoid rig:

* Bones are matched by what they are (``_mocap.roles``), not by name.
* Each bone takes the *turn from the rest pose* of its counterpart, measured in world space, after
  being aimed the way the counterpart's rest pose points. A rig resting in an A-pose and a
  recording that rests in a T-pose therefore end up in the same pose.
* The hips and the ankles are carried over as positions, scaled by leg length about the floor,
  and each leg is then solved to reach its ankle. A foot that was planted in the recording is
  planted on the rig: no sliding, whatever the proportions.
* A loop is found by comparing poses across the clip, cut there, and its small mismatch is spread
  over the cycle so the last frame leads into the first.

The same transfer takes any armature in the scene as its source (an imported BVH or FBX).
"""

from __future__ import annotations

import html
import json
import math
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Matrix, Quaternion, Vector

from . import _mocap, _turn, assets
from ._util import data_dir, ensure_object_mode, get_object, r
from .motion import Showing, action_named, assign, rig_of
from .registry import ToolError, registry, tool

CMU = "http://mocap.cs.cmu.edu"
ALLOWED_HOSTS = {"mocap.cs.cmu.edu"}
USER_AGENT = "ChinaUnix/0.1 (Blender add-on; motion capture lookup)"
MAX_BYTES = 40 * 1024 * 1024
SEARCH_TTL_S = 30 * 24 * 3600
MAX_FRAMES = 2400
UP = Vector((0.0, 0.0, 1.0))
_SKIP_WORDS = {"a", "an", "the", "to", "and", "of", "in", "on", "with", "then", "from", "into", "motion", "animation", "cycle", "loop"}
_ROW = re.compile(r"<tr[^>]*>\s*<td>\s*</td>\s*<td>\s*(\d+)\s*</td>\s*<td>(.*?)</td>(.*?)</tr>", re.S | re.I)
_HEAD = re.compile(r"Subject #(\d+)\s*\(([^)]*)\)")
_AMC = re.compile(r"/subjects/(\d+)/(\d+_\d+)\.amc", re.I)


# ---------------------------------------------------------------------------------------------
# the library


def _fetch(url: str, timeout: float = 45.0) -> bytes:
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ToolError(f"Download host not allowed: {host}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ToolError("That recording is too large.")
    return data


def _require_online() -> None:
    if not assets.settings["enabled"]:
        raise ToolError("Online lookups are turned off in the add-on preferences (Online assets). Recorded motion cannot be "
                        "fetched; key the motion by hand, or retarget from an armature the user imports.")


def parse_search(page: str) -> List[Dict[str, Any]]:
    """The trials listed on a CMU search page: clip id, what it is, who did it, frames per second."""
    out = []
    marks = [(m.start(), m.group(2)) for m in _HEAD.finditer(page)]
    for index, (at, subject) in enumerate(marks):
        chunk = page[at:marks[index + 1][0] if index + 1 < len(marks) else len(page)]
        for row in _ROW.finditer(chunk):
            link = _AMC.search(row.group(3))
            if link is None:
                continue
            rates = re.findall(r"<td>\s*(\d+)\s*</td>", row.group(3))
            out.append({"clip": link.group(2), "motion": " ".join(html.unescape(row.group(2)).split()),
                        "recorded": " ".join(html.unescape(subject).split()), "fps": int(rates[-1]) if rates else 120})
    return out


def _search_word(word: str) -> List[Dict[str, Any]]:
    path = os.path.join(data_dir("motions"), f"search_{re.sub(r'[^a-z0-9]+', '_', word)}.json")
    if os.path.isfile(path) and time.time() - os.path.getmtime(path) < SEARCH_TTL_S:
        try:
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            pass
    url = f"{CMU}/search.php?" + urllib.parse.urlencode({"subjectnumber": "%", "motion": word})
    found = parse_search(_fetch(url).decode("utf-8", "replace"))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(found, handle)
    return found


@tool(
    "motion_search",
    """Find real recorded human motion for a character: walking, running, turning, stopping,
    sidestepping, walking backwards, jumping, sitting, climbing, fighting, dancing, sports,
    everyday actions. Searches the CMU Graphics Lab Motion Capture Database (about 2,600
    recordings of real people, free for any use, no key). Use it before animating any human or
    humanoid move: recorded motion carries the weight, balance and timing that typed-in curves do
    not. Search with one to three plain words ('run', 'walk backward', 'run stop', 'jump',
    'sidestep'); the fewer the words, the more it finds. Returns clip ids with what each one is.
    Pass a clip id to retarget_motion to put it on the rig; if the first clip is not what you
    need, try the next one or other words.""",
    {
        "query": {"type": "string", "description": "What the motion is, in one to three plain words."},
        "count": {"type": "integer", "description": "How many clips to list (default 12, max 30)."},
    },
    ["query"],
    main_thread=False,
    timeout=180.0,
    label="Looked up recorded motion",
)
def motion_search(query: str, count: int = 12) -> Dict[str, Any]:
    _require_online()
    words = [w for w in re.findall(r"[a-z]+", query.lower()) if w not in _SKIP_WORDS][:4]
    if not words:
        raise ToolError("Say what the motion is: 'run', 'walk backward', 'jump'.")
    scores: Dict[str, Tuple[int, Dict[str, Any]]] = {}
    empty = []
    for word in words:
        try:
            found = _search_word(word)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"Could not reach the motion library ({type(exc).__name__}: {exc}). Key the motion by hand, and say "
                            f"that no recorded motion could be fetched.")
        if not found:
            empty.append(word)
        for item in found:
            points = 2 if word in item["motion"].lower() else 1
            before = scores.get(item["clip"], (0, item))[0]
            scores[item["clip"]] = (before + points, item)
    if not scores:
        raise ToolError(f"No recorded motion matches '{query}'. Use plainer words: walk, run, jog, jump, turn, stop, backward, "
                        f"sideways, sit, climb, punch, kick, dance, throw, swim, crawl.")
    count = max(1, min(int(count), 30))
    ranked = sorted(scores.values(), key=lambda pair: (-pair[0], len(pair[1]["motion"]), pair[1]["clip"]))
    out: Dict[str, Any] = {"query": query, "found": len(ranked), "clips": [item for _points, item in ranked[:count]],
                           "use": "retarget_motion(target=<the rig>, clip=<id>). A clip is often longer than the move you want and "
                                  "holds several things in a row: for a game cycle pass loop and in_place, and use start_s/end_s to "
                                  "take one part. Then look_motion and check_motion."}
    if empty:
        out["no_match_for"] = empty
    return out


def _clip_id(clip: str) -> Tuple[str, str]:
    match = re.fullmatch(r"\s*(\d{1,3})_(\d{1,3})\s*", clip)
    if match is None:
        raise ToolError(f"'{clip}' is not a clip id. Ids look like '127_03' and come from motion_search.")
    subject = f"{int(match.group(1)):02d}"
    return subject, f"{subject}_{int(match.group(2)):02d}"


def fetch_clip(clip: str) -> _mocap.Clip:
    """Download (once) and read one recording."""
    subject, ident = _clip_id(clip)
    folder = data_dir("motions")
    texts = []
    for name, url in ((f"{subject}.asf", f"{CMU}/subjects/{subject}/{subject}.asf"),
                      (f"{ident}.amc", f"{CMU}/subjects/{subject}/{ident}.amc")):
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            _require_online()
            try:
                data = _fetch(url, 120.0)
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(f"Could not fetch clip {ident} ({type(exc).__name__}: {exc}). Check the id with motion_search.")
            with open(path + ".part", "wb") as handle:
                handle.write(data)
            os.replace(path + ".part", path)
        with open(path, encoding="utf-8", errors="replace") as handle:
            texts.append(handle.read())
    try:
        return _mocap.read_acclaim(texts[0], texts[1])
    except ValueError as exc:
        for name in (f"{subject}.asf", f"{ident}.amc"):       # a bad download must not be kept
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
        raise ToolError(f"Clip {ident} could not be read: {exc}")


# ---------------------------------------------------------------------------------------------
# the transfer


def _flat(v: Vector) -> Vector:
    return Vector((v.x, v.y, 0.0))


def _facing(left: Vector, right: Vector) -> Vector:
    """Which way a body faces, from where its left and right hips are."""
    out = _flat(left - right).cross(UP)
    return out.normalized() if out.length > 1e-9 else Vector((0.0, -1.0, 0.0))


def _yaw(a: Vector, b: Vector) -> Quaternion:
    """The turn about the vertical that takes direction a to direction b."""
    return Quaternion(UP, math.atan2(b.y, b.x) - math.atan2(a.y, a.x))


def _mirror_turn(q: Quaternion, normal: Vector) -> Quaternion:
    axis = Vector((q.x, q.y, q.z))
    axis = axis - 2.0 * axis.dot(normal) * normal
    return Quaternion((q.w, -axis.x, -axis.y, -axis.z))


def _mirror_point(p: Vector, origin: Vector, normal: Vector) -> Vector:
    return p - 2.0 * (p - origin).dot(normal) * normal


def _ease(x: float) -> float:
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _turned(a: Quaternion, b: Quaternion) -> float:
    """Angle between two turns, in degrees, whichever sign each quaternion has."""
    return math.degrees(2.0 * math.acos(min(1.0, abs(a.dot(b)))))


def _despike(turns: List[Dict[str, Quaternion]], bones: List[str], fps: float) -> int:
    """Recordings have glitches: a marker lost for a few frames flips a foot round and back. No joint turns faster
    than about 1,500 degrees a second, so a turn faster than that which comes back is bridged by the short way
    from the last good frame to the next. Returns how many frames were repaired."""
    limit = 1500.0 / fps
    mended = 0
    for bone in bones:
        index = 1
        while index < len(turns):
            before = turns[index - 1][bone]
            if _turned(turns[index][bone], before) <= limit:
                index += 1
                continue
            back = next((j for j in range(index + 1, min(index + 8, len(turns)))
                         if _turned(turns[j][bone], before) <= limit * (j - index + 1)), None)
            if back is None:
                if index >= len(turns) - 8:     # the last frames of a take, where the actor leaves the cameras: hold
                    for j in range(index, len(turns)):
                        turns[j][bone] = before
                    mended += len(turns) - index
                    break
                index += 1                      # it does not come back: a real fast turn
                continue
            after = turns[back][bone]
            for j in range(index, back):
                turns[j][bone] = _mocap.slerp(before, after, (j - index + 1) / (back - index + 1))
            mended += back - index
            index = back + 1
    return mended


def _find_cycle(features, poses, fps: float, least_s: Optional[float] = None) -> Tuple[int, int, float]:
    """The two frames one cycle apart whose poses and speeds match best: (first, last, mismatch).
    Only stretches where the body really moves count: standing still matches itself perfectly."""
    import numpy as np
    count = len(features)
    step = np.linalg.norm(np.diff(poses, axis=0), axis=1)          # how much the pose changes frame to frame
    busy = float(np.percentile(step, 90))
    if least_s is None:            # a stride repeats within a second; a slow move (an idle, breathing) needs a long
        least_s = 0.4 if busy * fps > 1.2 else 2.0        # loop, or it plays as a twitch
    shortest = max(3, int(round(least_s * fps)))
    longest = min(count - 1, max(int(round(4.0 * fps)), shortest + int(round(2.0 * fps))))
    if longest < shortest:
        raise ToolError(f"That stretch is shorter than one cycle of the move ({least_s:.1f} s or more). Use a longer "
                        f"start_s/end_s window, a shorter min_cycle_s, or loop=false.")
    squares = (features * features).sum(axis=1)
    cost = squares[:, None] + squares[None, :] - 2.0 * features @ features.T
    first, last = np.indices(cost.shape)
    apart = last - first
    walked = np.concatenate([[0.0], np.cumsum(step)])
    lively = (walked[last] - walked[first]) >= 0.5 * busy * np.maximum(apart, 1)
    cost = np.where((apart >= shortest) & (apart <= longest) & lively, cost, np.inf)
    if not np.isfinite(cost).any():
        raise ToolError("Nothing in that stretch repeats while moving. Use a window where the move is under way (start_s, end_s), "
                        "or loop=false.")
    best = float(cost.min())
    close = np.argwhere(cost <= best * 1.2 + 1e-12)
    i, j = min(((int(a), int(b)) for a, b in close), key=lambda pair: (pair[1] - pair[0], cost[pair[0], pair[1]]))
    return i, j, math.sqrt(max(float(cost[i, j]), 0.0) / max(features.shape[1] / 3, 1))


class _Target:
    """The user's rig as plain matrices, so a pose can be worked out without touching the scene."""

    def __init__(self, rig: bpy.types.Object) -> None:
        self.rig = rig
        self.world = rig.matrix_world.copy()
        order, stack = [], [b for b in rig.data.bones if b.parent is None][::-1]
        while stack:
            bone = stack.pop()
            order.append(bone)
            stack.extend(list(bone.children)[::-1])
        self.names = [b.name for b in order]
        self.parent = {b.name: (b.parent.name if b.parent else None) for b in order}
        self.deform = {b.name: b.use_deform for b in order}
        self.rel = {b.name: (b.parent.matrix_local.inverted() @ b.matrix_local) if b.parent else b.matrix_local.copy()
                    for b in order}
        self.rest = {b.name: self.world @ b.matrix_local for b in order}
        self.turn = {name: m.to_quaternion() for name, m in self.rest.items()}
        self.head = {b.name: self.world @ b.head_local for b in order}
        self.tail = {b.name: self.world @ b.tail_local for b in order}

    def pose(self, turns: Dict[str, Quaternion], places: Dict[str, Vector],
             legs: List[Dict[str, Any]], ankles: Dict[str, Vector], forward: Vector):
        """World matrix and basis matrix of every bone, for the wanted world turns and places.
        Legs are solved on the way down so that each ankle lands on its wanted place."""
        turns = dict(turns)
        world: Dict[str, Matrix] = {}
        basis: Dict[str, Matrix] = {}
        by_thigh = {leg["thigh"]: leg for leg in legs}
        for name in self.names:
            base = (world[self.parent[name]] if self.parent[name] else self.world) @ self.rel[name]
            loc, rot, scale = base.decompose()
            leg = by_thigh.get(name)
            if leg is not None and leg["side"] in ankles and name in turns and leg["shin"] in turns:
                hip, ankle = loc, ankles[leg["side"]]
                l1, l2 = leg["l1"], leg["l2"]
                reach = ankle - hip
                far = max(abs(l1 - l2) + 1e-4, min(reach.length, (l1 + l2) * 0.9995))
                aim = reach.normalized() if reach.length > 1e-9 else -UP
                d1 = turns[name] @ leg["e1"]
                bend = d1 * l1 - aim * (d1 * l1).dot(aim)
                if bend.length < 1e-4 * l1:
                    bend = forward - aim * forward.dot(aim)
                bend = bend.normalized() if bend.length > 1e-9 else Vector((0.0, -1.0, 0.0))
                along = (l1 * l1 - l2 * l2 + far * far) / (2.0 * far)
                knee = hip + aim * along + bend * math.sqrt(max(l1 * l1 - along * along, 0.0))
                turns[name] = d1.rotation_difference((knee - hip).normalized()) @ turns[name]
                d2 = turns[leg["shin"]] @ leg["e2"]
                turns[leg["shin"]] = d2.rotation_difference((hip + aim * far - knee).normalized()) @ turns[leg["shin"]]
            world[name] = Matrix.LocRotScale(places.get(name, loc), turns.get(name, rot), scale)
            basis[name] = base.inverted() @ world[name]
        return world, basis


def _soles(target: _Target, leg: Dict[str, Any]) -> None:
    """The real underside of the foot: the lowest points of the meshes the foot and toe bones carry,
    each in its own bone's frame. Keeping these on or above the floor keeps the actual shoe out of it,
    where joints alone would let a long toe cap or a thick heel sink in."""
    import numpy as np
    from .motion import _Skin, bound_meshes
    rig = target.rig
    index = {name: i for i, name in enumerate(b.name for b in rig.data.bones)}
    carried = [index[b] for b in (leg["foot"], leg["toe"]) if b]
    skin = _Skin(rig, bound_meshes(rig), index)
    if not len(skin.points):
        return
    pick = np.nonzero(np.isin(skin.lead, carried))[0]
    if len(pick) < 8:
        return
    world = np.array(rig.matrix_world, dtype=np.float64)
    at = (skin.points[pick] @ world.T)[:, :3]
    low = np.argsort(at[:, 2])[:max(8, len(pick) // 4)]
    low = low[::max(1, len(low) // 80)]
    toe = leg["toe"]
    points = []
    for k in low:
        spot = Vector(at[k])
        if toe and skin.lead[pick[k]] == index[toe]:
            points.append(("toe", target.turn[toe].inverted() @ (spot - target.head[toe])))
        else:
            points.append(("foot", target.turn[leg["foot"]].inverted() @ (spot - leg["ankle0"])))
    leg["skin"] = points
    leg["floor"] = float(at[low, 2].min())


def _sole_points(leg: Dict[str, Any], ankle: Vector, want: Dict[str, Quaternion], target: _Target) -> List[Vector]:
    foot, toe = leg["foot"], leg["toe"]
    toe_turn = None
    if toe:
        toe_turn = want.get(toe) or (want[foot] @ target.turn[foot].inverted() @ target.turn[toe])
    if "skin" in leg:
        toe_head = ankle + want[foot] @ leg["toe_at"] if toe else ankle
        return [(toe_head + toe_turn @ local) if kind == "toe" else (ankle + want[foot] @ local) for kind, local in leg["skin"]]
    spots = [ankle + want[foot] @ point for point in leg["sole"]]
    if toe:
        spots.append(ankle + want[foot] @ leg["toe_at"] + toe_turn @ leg["toe_tip"])
    return spots


def _legs(target: _Target, found: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for side in "LR":
        thigh, shin = found.get(f"thigh.{side}"), found.get(f"shin.{side}")
        if not thigh or not shin:
            continue
        foot, toe = found.get(f"foot.{side}"), found.get(f"toe.{side}")
        hip, knee = target.head[thigh], target.head[shin]
        ankle = target.head[foot] if foot else target.tail[shin]
        leg: Dict[str, Any] = {"side": side, "thigh": thigh, "shin": shin, "foot": foot, "toe": toe, "ankle0": ankle,
                               "l1": (knee - hip).length, "l2": (ankle - knee).length,
                               "e1": target.turn[thigh].inverted() @ (knee - hip).normalized(),
                               "e2": target.turn[shin].inverted() @ (ankle - knee).normalized()}
        if foot:                              # the points of the foot that can touch the floor, in the foot's own frame
            back = target.turn[foot].inverted()
            floor = min(target.tail[foot].z, target.tail[toe].z if toe else target.tail[foot].z)
            heel = Vector((ankle.x, ankle.y, floor)) - _flat(target.tail[foot] - ankle) * 0.25
            leg["floor"] = floor
            leg["sole"] = [back @ (target.tail[foot] - ankle), back @ (heel - ankle)]
            if toe:
                leg["toe_at"] = back @ (target.head[toe] - ankle)
                leg["toe_tip"] = target.turn[toe].inverted() @ (target.tail[toe] - target.head[toe])
            _soles(target, leg)
        out.append(leg)
    return out


def _controls(rig: bpy.types.Object, target: _Target) -> List[Dict[str, Any]]:
    """IK constraints whose targets are bones of this rig: those control bones have to be driven,
    or the constraint overrides the pose."""
    out = []
    for pbone in rig.pose.bones:
        for con in pbone.constraints:
            if con.type != 'IK' or con.mute or con.influence < 0.5 or con.target != rig or con.subtarget not in target.rest:
                continue
            chain = [pbone.name]
            while len(chain) < (con.chain_count or 99) and target.parent[chain[-1]]:
                chain.append(target.parent[chain[-1]])
            follower = next((p.name for p in rig.pose.bones for c in p.constraints
                             if c.type in {'COPY_ROTATION', 'COPY_TRANSFORMS'} and not c.mute and c.target == rig
                             and c.subtarget == con.subtarget), None)
            pole = con.pole_subtarget if con.pole_target == rig and con.pole_subtarget in target.rest else None
            entry = {"bone": pbone.name, "goal": con.subtarget, "top": chain[-1], "follower": follower, "pole": pole}
            if pole:
                # Where the pole sits, in the frame of the chain's top bone (the thigh): the bend then follows the
                # thigh's own turn. Taken from the solved bend instead, it is undefined when the leg is straight,
                # and the pole flipping to the other side twists the whole leg round in one frame.
                joint, top = target.head[pbone.name], target.head[chain[-1]]
                line = target.tail[pbone.name] - top
                line = line.normalized() if line.length > 1e-9 else -UP
                away = target.head[pole] - joint
                side = away - line * away.dot(line)
                entry["pole_local"] = target.turn[chain[-1]].inverted() @ (side if side.length > 1e-6 else away).normalized()
                entry["pole_dist"] = away.length
            out.append(entry)
    return out


def _stored_pose(rig: bpy.types.Object, action: bpy.types.Action, target: _Target, legs: List[Dict[str, Any]],
                 found: Dict[str, Any]) -> Dict[str, Any]:
    """The first pose of an action already on the rig, in the same terms the transfer works in."""
    with Showing(rig, action):
        bpy.context.scene.frame_set(int(round(action.frame_range[0])))
        posed = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        world = posed.matrix_world
        turns = {name: (world @ posed.pose.bones[name].matrix).to_quaternion() for name in target.names}
        mid = (world @ posed.pose.bones[found["thigh.L"]].head + world @ posed.pose.bones[found["thigh.R"]].head) / 2
        ankles = {leg["side"]: world @ (posed.pose.bones[leg["foot"]].head if leg["foot"] else posed.pose.bones[leg["shin"]].tail)
                  for leg in legs}
    return {"turns": turns, "mid": mid, "ankles": ankles}


def _transfer(target_name: str, motion: Any, source_label: str, name: str, start_s: Optional[float], end_s: Optional[float],
              loop: bool, in_place: bool, mirror: bool, bone_map: Optional[Dict[str, str]], blend_from: str, blend_to: str,
              arms_out_deg: float, min_cycle_s: Optional[float] = None, turn_deg: float = 0.0) -> Dict[str, Any]:
    import numpy as np

    ensure_object_mode()
    scene = bpy.context.scene
    fps = scene.render.fps / scene.render.fps_base
    rig = rig_of(get_object(target_name))
    if rig is None:
        raise ToolError(f"'{target_name}' is not an armature and is not bound to one.")
    target = _Target(rig)
    found = _mocap.roles(target.names, target.parent, target.deform)
    theirs = _mocap.roles(motion.names, motion.parent)
    needed = ("hips", "thigh.L", "thigh.R", "shin.L", "shin.R")
    for label, got, names in (("The rig", found, target.names), ("The recording", theirs, motion.names)):
        missing = [role for role in needed if role not in got]
        if missing:
            raise ToolError(f"{label} has no bone that reads as: {', '.join(missing)}. Its bones: {', '.join(names[:70])}. "
                            f"Pass bone_map {{rig bone: recording bone}} for the ones the names do not give away"
                            + (", after renaming or adding the rig's hip and leg bones." if label == "The rig" else "."))
    crossed = {(key[:-1] + ("R" if key.endswith("L") else "L")) if key[-2:] in (".L", ".R") else key: bone
               for key, bone in theirs.items()} if mirror else theirs
    mapping = _mocap.pairs(found, crossed)
    for mine, other in (bone_map or {}).items():
        if mine not in target.rest:
            raise ToolError(f"bone_map: the rig has no bone '{mine}'. Its bones: {', '.join(target.names[:70])}")
        if not other:
            mapping.pop(mine, None)
        elif other not in motion.parent:
            raise ToolError(f"bone_map: the recording has no bone '{other}'. Its bones: {', '.join(motion.names[:70])}")
        else:
            mapping[mine] = other

    # -- the two skeletons at rest ---------------------------------------------------------------
    at_rest = motion.rest()
    front = _facing(target.head[found["thigh.L"]], target.head[found["thigh.R"]])
    their_front = _facing(at_rest[theirs["thigh.L"]][1], at_rest[theirs["thigh.R"]][1])
    align = _yaw(their_front, front)
    left = UP.cross(front).normalized()
    legs = _legs(target, found)

    def ankle_bone(side: str) -> Tuple[str, int]:            # the recording's ankle: a foot's head, or a shin's tail
        foot = crossed.get(f"foot.{side}")
        return (foot, 1) if foot else (crossed[f"shin.{side}"], 2)

    def leg_length(sample: Any, roles_: Dict[str, Any], side: str, ankle: Vector) -> float:
        hip, knee = sample[roles_[f"thigh.{side}"]][1], sample[roles_[f"shin.{side}"]][1]
        return (knee - hip).length + (ankle - knee).length

    their_leg = sum(leg_length(at_rest, crossed, s, at_rest[ankle_bone(s)[0]][ankle_bone(s)[1]]) for s in "LR") / 2
    my_leg = sum(leg["l1"] + leg["l2"] for leg in legs) / len(legs)
    if their_leg < 1e-6 or my_leg < 1e-6:
        raise ToolError("A leg has no length: the hip, knee and ankle bones could not be told apart.")
    scale = my_leg / their_leg
    hips = found["hips"]
    # Each limb bone is first aimed the way its counterpart points at rest, so an A-pose rig takes a T-pose
    # recording's pose, and a foot that is flat in the recording is flat on the rig. The body's own column
    # (hips, spine, neck, head) is not: two rigs stand upright the same way, but the bones of the column point
    # along whatever line each rig's maker chose, and re-aiming them tilts the mesh's head or chest.
    role_of = {bone: role.split(".")[0] for role, bones in found.items() for bone in (bones if isinstance(bones, list) else [bones])}
    aim: Dict[str, Quaternion] = {}
    for mine, other in mapping.items():
        fixed = target.turn[mine]
        if role_of.get(mine) in _mocap.SIDED:
            theirs_way = align @ (at_rest[other][2] - at_rest[other][1])
            if mirror:
                theirs_way = theirs_way - 2.0 * theirs_way.dot(left) * left
            my_way = target.tail[mine] - target.head[mine]
            if theirs_way.length > 1e-6 and my_way.length > 1e-6:
                fixed = my_way.rotation_difference(theirs_way) @ fixed
        aim[mine] = fixed

    # -- sample the recording ----------------------------------------------------------------------
    begin = max(0.0, float(start_s or 0.0))
    finish = min(motion.seconds, float(end_s)) if end_s is not None else motion.seconds
    if finish - begin < 2.0 / fps:
        raise ToolError(f"Nothing to take: the recording runs {motion.seconds:.2f} s and the window is {begin:.2f} to {finish:.2f} s.")
    count = int(math.floor((finish - begin) * fps + 1e-6)) + 1
    most = MAX_FRAMES // 2 if loop else MAX_FRAMES
    if count > most:
        raise ToolError(f"That is {count} frames ({finish - begin:.0f} s). Take a part of it with start_s and end_s "
                        f"(at most {most / fps:.0f} s at a time).")
    keep_frame = (scene.frame_current, scene.frame_subframe)
    samples = [motion.sample(begin + index / fps) for index in range(count)]
    scene.frame_set(keep_frame[0], subframe=keep_frame[1])

    def mid_of(sample: Any) -> Vector:
        return (sample[theirs["thigh.L"]][1] + sample[theirs["thigh.R"]][1]) / 2

    heading = _yaw(align @ _facing(samples[0][theirs["thigh.L"]][1], samples[0][theirs["thigh.R"]][1]), front) @ align
    lows = np.array([min(sample[ankle_bone("L")[0]][ankle_bone("L")[1]].z, sample[ankle_bone("R")[0]][ankle_bone("R")[1]].z)
                     for sample in samples])
    their_floor = float(np.percentile(lows, 5))
    # A capture floor is rarely level: across a long take the planted ankles drift by centimetres, and a single
    # floor height leaves the rig hovering or sinking at one end. The floor is taken from the lower edge of the
    # ankle heights over a step or two around each moment (a jump still sees the landings around it). Real
    # changes of level larger than a few centimetres (stairs, a box) are kept.
    reach = max(1, int(round(0.75 * fps)))
    under = np.array([np.percentile(lows[max(0, i - reach):i + reach + 1], 10) for i in range(len(lows))])
    width = max(1, int(round(0.5 * fps)))
    kernel = np.ones(2 * width + 1) / (2 * width + 1)
    local_floor = np.convolve(np.pad(under, width, mode="edge"), kernel, mode="valid")
    local_floor = np.clip(local_floor, their_floor - 0.06, their_floor + 0.06)
    start_mid = mid_of(samples[0])
    my_mid = (target.head[found["thigh.L"]] + target.head[found["thigh.R"]]) / 2
    my_origin = Vector((my_mid.x, my_mid.y, min(leg["ankle0"].z for leg in legs)))
    unalign = align.inverted()

    spin = Quaternion(UP, math.radians(turn_deg or 0.0))     # the whole move turned to face another way

    def place(point: Vector, index: int) -> Vector:
        origin = Vector((start_mid.x, start_mid.y, float(local_floor[index])))
        out = my_origin + scale * (heading @ (point - origin))
        if mirror:
            out = _mirror_point(out, my_origin, left)
        return my_origin + spin @ (out - my_origin)

    turns: List[Dict[str, Quaternion]] = []
    mids: List[Vector] = []
    ankles: List[Dict[str, Vector]] = []
    for index, sample in enumerate(samples):
        frame_turns = {}
        for mine, other in mapping.items():
            moved = heading @ sample[other][0] @ unalign
            if mirror:
                moved = _mirror_turn(moved, left)
            frame_turns[mine] = spin @ moved @ aim[mine]
        turns.append(frame_turns)
        mids.append(place(mid_of(sample), index))
        ankles.append({side: place(sample[ankle_bone(side)[0]][ankle_bone(side)[1]], index) for side in "LR"})

    # -- a loop: find one cycle and close it -------------------------------------------------------
    info: Dict[str, Any] = {}
    mended = _despike(turns, list(mapping), fps)
    if mended:
        info["glitches_repaired"] = f"{mended} bone-frames where the recording flipped a joint and back"
    half = max(1, int(round(0.5 * fps)))                      # how fast the recording moves, every half second:
    info["speed_every_half_second_m_s"] = [               # it shows where a clip stands still and where the move is
        r(_flat(mids[k + half] - mids[k]).length / (half / fps), 1) for k in range(0, len(mids) - half, half)][:40]
    travel = _flat(mids[-1] - mids[0])
    if loop:
        tall = max(max(p[2].z for p in at_rest.values()) - min(p[2].z for p in at_rest.values()), 1e-6)
        used = sorted(set(mapping.values()))
        rows = []
        for sample in samples:
            centre = mid_of(sample)
            turn = _yaw(_facing(sample[theirs["thigh.L"]][1], sample[theirs["thigh.R"]][1]), Vector((0.0, -1.0, 0.0)))
            row = []
            for bone in used:
                row += list(turn @ (sample[bone][1] - centre) / tall)
            rows.append(row + [(centre.z - their_floor) / tall])
        poses = np.array(rows)
        speeds = np.gradient(poses, axis=0) * fps * 0.1
        first, last, mismatch = _find_cycle(np.concatenate([poses, speeds], axis=1), poses, fps, min_cycle_s)
        turns, mids, ankles = turns[first:last + 1], mids[first:last + 1], ankles[first:last + 1]
        span = last - first
        travel = _flat(mids[-1] - mids[0])
        info["cycle"] = {"taken_from_s": [r(begin + first / fps, 2), r(begin + last / fps, 2)], "seconds": r(span / fps, 2),
                         "match": "good" if mismatch < 0.02 else "fair" if mismatch < 0.05 else
                         "poor: this stretch does not really repeat; try another window or clip"}
        ends = {mine: turns[0][mine] @ turns[-1][mine].inverted() for mine in mapping}

        def left_over(a: Vector, b: Vector) -> Vector:      # what the last frame is off by; the distance covered
            off = b - a                                     # is kept unless the move is to stay on the spot
            return off if in_place else Vector((0.0, 0.0, off.z))

        off_mid = left_over(mids[0], mids[-1])
        off_ankle = {side: left_over(ankles[0][side], ankles[-1][side]) for side in "LR"}
        # on the spot means on the rig's own spot: the cycle starts wherever the recording had got to by then
        centre = Vector((mids[0].x - my_origin.x, mids[0].y - my_origin.y, 0.0)) if in_place else Vector()
        for index in range(span + 1):
            part = index / span
            for mine in mapping:
                turns[index][mine] = _mocap.slerp(Quaternion(), ends[mine], part) @ turns[index][mine]
            mids[index] = mids[index] - off_mid * part - centre
            for side in "LR":
                ankles[index][side] = ankles[index][side] - off_ankle[side] * part - centre
    elif in_place:                                  # follow the travel, not the sway of the hips from foot to foot
        flat = np.array([[p.x, p.y] for p in mids])
        width = max(1, int(round(0.6 * fps)))
        padded = np.concatenate([flat[0] + (flat[1] - flat[0]) * np.arange(-width, 0)[:, None], flat,
                                 flat[-1] + (flat[-1] - flat[-2]) * np.arange(1, width + 1)[:, None]])
        kernel = np.ones(2 * width + 1) / (2 * width + 1)
        smooth = np.stack([np.convolve(padded[:, k], kernel, mode="valid") for k in range(2)], axis=1)
        for index in range(len(mids)):
            carried = Vector((smooth[index, 0] - smooth[0, 0], smooth[index, 1] - smooth[0, 1], 0.0))
            mids[index] = mids[index] - carried
            for side in "LR":
                ankles[index][side] = ankles[index][side] - carried
    frames = len(turns)

    # -- lead out of, or into, a pose the rig already has ------------------------------------------
    for label, upto in ((blend_from, True), (blend_to, False)):
        if not label:
            continue
        stored = _stored_pose(rig, action_named(label), target, legs, found)
        width = max(2, min(frames // 3, int(round(0.3 * fps))))
        for step in range(width + 1):
            index = step if upto else frames - 1 - step
            keep = _ease(step / width)                      # 0 at the stored pose, 1 where the recording takes over
            for mine in mapping:
                turns[index][mine] = _mocap.slerp(stored["turns"][mine], turns[index][mine], keep)
            before = mids[index].copy()
            wanted = stored["mid"] if in_place else Vector((before.x, before.y, stored["mid"].z))
            mids[index] = wanted.lerp(before, keep)
            for side in "LR":
                if side in stored["ankles"]:
                    offset = stored["ankles"][side] - stored["mid"]
                    ankles[index][side] = mids[index] + offset.lerp(ankles[index][side] - before, keep)

    # -- write the action ---------------------------------------------------------------------------
    _turn.mark_mutation()
    name = (name or source_label).strip()[:60] or "Motion"
    old = bpy.data.actions.get(name)
    if old is not None:
        bpy.data.actions.remove(old)
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    assign(rig, action)
    for pbone in rig.pose.bones:                              # bones this action does not key rest, as the solve assumes
        pbone.matrix_basis = Matrix.Identity(4)
    controls = _controls(rig, target)
    arms = [(found.get(f"upper_arm.{side}"), 1.0 if side == "L" else -1.0) for side in "LR"]
    hips_off = target.turn[hips].inverted() @ (my_mid - target.head[hips])       # hips bone -> between the hip joints
    for leg in legs:                                                             # between the hip joints -> this hip joint
        leg["hip_off"] = target.turn[hips].inverted() @ (target.head[leg["thigh"]] - my_mid)
    last_turn: Dict[str, Quaternion] = {}
    last_euler: Dict[str, Any] = {}
    wanted_ankles: List[Dict[str, Vector]] = []
    cycle = frames - 1 if loop else 0                         # in a loop the last frame is the first again

    def soften(values: List[float]) -> List[float]:
        """A correction eases in and out over a few frames: lifting an ankle by 3 cm in one frame snaps the knee."""
        out = []
        for i in range(len(values)):
            best = 0.0
            for apart, weight in enumerate((1.0, 0.6, 0.25)):
                for j in (i - apart, i + apart):
                    j = j % cycle if cycle else j
                    if 0 <= j < len(values):
                        best = max(best, values[j] * weight)
            out.append(best)
        return out

    plans = []
    lifts: Dict[str, List[float]] = {leg["side"]: [] for leg in legs}
    for index in range(frames):
        want = dict(turns[index])
        ahead = _flat(want[hips] @ target.turn[hips].inverted() @ front)
        ahead = ahead.normalized() if ahead.length > 1e-6 else front
        if arms_out_deg:
            for bone, sign in arms:
                if bone in want:
                    want[bone] = Quaternion(ahead, math.radians(arms_out_deg) * sign) @ want[bone]
        for leg in legs:                                     # keep heel and toes on or above the floor
            foot, under = leg["foot"], 0.0
            if foot and foot in want:
                under = max(0.0, leg["floor"] - min(p.z for p in _sole_points(leg, ankles[index][leg["side"]], want, target)))
            lifts[leg["side"]].append(under)
        plans.append((want, ahead))
    lifts = {side: soften(values) for side, values in lifts.items()}
    drops = []
    for index, (want, _ahead) in enumerate(plans):
        drop = 0.0                                           # a leg that would have to stretch past its length: the
        for leg in legs:                                     # hips come down instead, as a real body's would
            ankle = ankles[index][leg["side"]] + Vector((0.0, 0.0, lifts[leg["side"]][index]))
            gap = ankle - (mids[index] + want[hips] @ leg["hip_off"])
            longest = (leg["l1"] + leg["l2"]) * 0.995
            across = Vector((gap.x, gap.y)).length
            if gap.length > longest and across < longest:
                drop = max(drop, -gap.z - math.sqrt(longest * longest - across * across))
        drops.append(drop)
    drops = soften(drops)
    for index in range(frames):
        want, ahead = plans[index]
        reach = {leg["side"]: ankles[index][leg["side"]] + Vector((0.0, 0.0, lifts[leg["side"]][index])) for leg in legs}
        mid = mids[index] - Vector((0.0, 0.0, drops[index]))
        places = {hips: mid - want[hips] @ hips_off}
        wanted_ankles.append(reach)
        world, basis = target.pose(want, places, legs, reach, ahead)
        driven = set(want) | {leg["shin"] for leg in legs} | {leg["thigh"] for leg in legs}
        located = {hips}
        for control in controls:                            # IK targets and poles follow the solved limb
            bone, goal = control["bone"], control["goal"]
            base = (world[target.parent[goal]] if target.parent[goal] else target.world) @ target.rel[goal]
            loc, rot, size = base.decompose()
            tip = world[bone] @ Vector((0.0, rig.data.bones[bone].length, 0.0))
            follower = control["follower"]
            if follower:
                rot = world[follower].to_quaternion() @ target.turn[follower].inverted() @ target.turn[goal]
            world[goal] = Matrix.LocRotScale(tip, rot, size)
            basis[goal] = base.inverted() @ world[goal]
            driven.add(goal)
            located.add(goal)
            pole = control["pole"]
            if pole:
                joint = world[bone].translation
                out = world[control["top"]].decompose()[1] @ control["pole_local"]
                base = (world[target.parent[pole]] if target.parent[pole] else target.world) @ target.rel[pole]
                loc, rot, size = base.decompose()
                world[pole] = Matrix.LocRotScale(joint + out.normalized() * control["pole_dist"], rot, size)
                basis[pole] = base.inverted() @ world[pole]
                driven.add(pole)
                located.add(pole)
        frame = index + 1
        for bone in driven:
            pbone = rig.pose.bones[bone]
            loc, rot, _size = basis[bone].decompose()
            if bone in last_turn and rot.dot(last_turn[bone]) < 0.0:
                rot.negate()
            last_turn[bone] = rot
            if pbone.rotation_mode == 'QUATERNION':
                pbone.rotation_quaternion = rot
                pbone.keyframe_insert("rotation_quaternion", frame=frame, group=bone)
            elif pbone.rotation_mode == 'AXIS_ANGLE':
                axis, angle = rot.to_axis_angle()
                pbone.rotation_axis_angle = (angle, axis.x, axis.y, axis.z)
                pbone.keyframe_insert("rotation_axis_angle", frame=frame, group=bone)
            else:
                euler = rot.to_euler(pbone.rotation_mode, last_euler[bone]) if bone in last_euler else rot.to_euler(pbone.rotation_mode)
                last_euler[bone] = euler
                pbone.rotation_euler = euler
                pbone.keyframe_insert("rotation_euler", frame=frame, group=bone)
            if bone in located:
                pbone.location = loc
                pbone.keyframe_insert("location", frame=frame, group=bone)
    for pbone in rig.pose.bones:     # every other bone is keyed at rest, so no action inherits a pose another one left
        keyed_rot = pbone.rotation_mode
        if pbone.name not in last_turn:
            pbone.matrix_basis = Matrix.Identity(4)
            path = {"QUATERNION": "rotation_quaternion", "AXIS_ANGLE": "rotation_axis_angle"}.get(keyed_rot, "rotation_euler")
            for frame in (1, frames):
                pbone.keyframe_insert("location", frame=frame, group=pbone.name)
                pbone.keyframe_insert(path, frame=frame, group=pbone.name)
        elif pbone.name not in located:
            pbone.location = (0.0, 0.0, 0.0)
            pbone.keyframe_insert("location", frame=1, group=pbone.name)
        pbone.scale = (1.0, 1.0, 1.0)
        pbone.keyframe_insert("scale", frame=1, group=pbone.name)
    action.use_frame_range = True
    action.frame_start, action.frame_end = 1, frames
    action.use_cyclic = bool(loop)

    # -- did the rig follow? ------------------------------------------------------------------------
    worst = 0.0
    for index in sorted({0, frames // 4, frames // 2, (3 * frames) // 4, frames - 1}):
        scene.frame_set(index + 1)
        posed = rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        for leg in legs:
            pbone = posed.pose.bones[leg["foot"]] if leg["foot"] else posed.pose.bones[leg["shin"]]
            at = posed.matrix_world @ (pbone.head if leg["foot"] else pbone.tail)
            worst = max(worst, (at - wanted_ankles[index][leg["side"]]).length)
    scene.frame_set(1)

    seconds = (frames - 1) / fps
    speed = travel.length / seconds if seconds > 0 else 0.0
    way = "on the spot"
    if speed > 0.15:
        ahead_by, left_by = travel.normalized().dot(front), travel.normalized().dot(left)
        way = ("forward" if ahead_by > 0.7 else "backward" if ahead_by < -0.7 else "to its left" if left_by > 0.7
               else "to its right" if left_by < -0.7 else
               ("forward" if ahead_by > 0 else "backward") + " and to its " + ("left" if left_by > 0 else "right"))
    said = set(re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", action.name).lower()))
    named = {"left": "to its left", "right": "to its right", "back": "backward", "backward": "backward",
             "backwards": "backward", "forward": "forward", "fwd": "forward", "front": "forward"}
    claims = {named[w] for w in said if w in named}
    wrong_name = bool(claims) and speed > 0.15 and not any(c in way for c in claims)
    out: Dict[str, Any] = {
        "action": action.name, "on": rig.name, "from": source_label, "frames": [1, frames], "fps": r(fps, 2),
        "seconds": r(seconds, 2), "loops": bool(loop), "in_place": bool(in_place),
        "recorded_move": f"{speed:.2f} m/s {way}" if speed > 0.15 else "stays where it is",
        "size_ratio": r(scale, 3), "bones_driven": len(mapping), "feet_off_target_cm": r(worst * 100, 1), **info,
        "next": "look_motion and check_motion on this action. If the rig's limbs pass through its body (a wide or bulky "
                "character), run this again with arms_out_deg. If it is the wrong part of the recording, change start_s/end_s.",
    }
    if loop:
        out["play_range"] = f"frames 1 to {frames - 1} repeat; frame {frames} is frame 1 again"
    quiet = [n for n in target.names if target.deform[n] and n not in mapping]
    if quiet:
        out["bones_left_at_rest"] = quiet[:30]
    if wrong_name:
        out["name_does_not_match"] = (f"'{action.name}' says {', '.join(sorted(claims))}, but this recording moves {way} (seen from the "
                                      f"character, not the camera). Run it again with mirror toggled, or name it for where it goes.")
    if worst > 0.03:
        out["warning"] = (f"The feet end up to {worst * 100:.0f} cm from where they should be: a constraint on the rig is "
                          f"overriding the pose. Check the leg constraints, or mute them and run this again.")
    return out


@tool(
    "retarget_motion",
    """Put real recorded motion on a rig: the move a real person made, on the user's character,
    whatever its bone names, proportions and rest pose. Give `clip` (an id from motion_search) or
    `source` (an armature in the scene that carries motion, such as an imported BVH or FBX).
    Bones are matched by what they are; feet that were planted in the recording stay planted.
    `loop` finds one cycle in the recording, cuts it there and closes it, for walks, runs and
    anything that repeats; `in_place` keeps the character on the spot (game cycles); `mirror`
    swaps left and right (a step to the left from a step to the right); `start_s`/`end_s` take a
    part of the recording; `blend_from`/`blend_to` name actions already on the rig whose first
    pose this one starts from or ends on, so a start or a stop joins the idle and the cycle;
    `arms_out_deg` holds the arms further from the body on a broad character; `turn_deg` turns the
    whole move to head another way (when no recording goes that way). Writes one action named
    `name` (an action of that name is replaced) that keys every bone, and leaves it on the rig. The rig needs
    hips and two legs; a rig built with find_limbs and rig_chain and bound with rig_bind works.
    Afterwards: look_motion and check_motion.""",
    {
        "target": {"type": "string", "description": "The rig to animate (the armature, or a mesh bound to it)."},
        "clip": {"type": "string", "description": "Clip id from motion_search, like '127_03'."},
        "source": {"type": "string", "description": "Instead of clip: an armature object in the scene whose action is the motion."},
        "name": {"type": "string", "description": "Name for the new action, for what it is: 'Run_Forward'."},
        "loop": {"type": "boolean", "description": "Find one cycle and make it repeat seamlessly."},
        "in_place": {"type": "boolean", "description": "Keep the character on the spot."},
        "mirror": {"type": "boolean", "description": "Swap left and right."},
        "start_s": {"type": "number", "description": "Take the recording from this many seconds in."},
        "end_s": {"type": "number", "description": "Take the recording up to this many seconds in."},
        "blend_from": {"type": "string", "description": "An action on the rig: start from its first pose."},
        "blend_to": {"type": "string", "description": "An action on the rig: end on its first pose."},
        "arms_out_deg": {"type": "number", "description": "Hold the upper arms this many degrees further out from the body."},
        "min_cycle_s": {"type": "number", "description": "With loop: the shortest cycle to accept. Default 0.4 s for steps and strides, "
                                                         "2 s for slow moves; give 3-4 for an idle so it does not twitch."},
        "turn_deg": {"type": "number", "description": "Turn the whole move about the vertical: the body then faces and travels that "
                                                      "many degrees to its left (90 = a forward run heading to its left, 180 = "
                                                      "running back facing the way it goes; negative turns right)."},
        "bone_map": {"type": "object", "description": "Only for bones the names do not give away: {rig bone: recording bone}."},
    },
    ["target"],
    main_thread=False,
    timeout=900.0,
    mutates=True,
    label="Put recorded motion on the rig",
)
def retarget_motion(target: str, clip: str = "", source: str = "", name: str = "", loop: bool = False, in_place: bool = False,
                    mirror: bool = False, start_s: Optional[float] = None, end_s: Optional[float] = None, blend_from: str = "",
                    blend_to: str = "", arms_out_deg: float = 0.0, min_cycle_s: Optional[float] = None,
                    turn_deg: float = 0.0, bone_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    if bool(clip) == bool(source):
        raise ToolError("Give either `clip` (an id from motion_search) or `source` (an armature in the scene).")
    recorded = fetch_clip(clip) if clip else None            # the download stays off Blender's main thread

    def work() -> Dict[str, Any]:
        if recorded is not None:
            motion, label = recorded, f"clip {_clip_id(clip)[1]}"
        else:
            obj = get_object(source)
            if obj.type != 'ARMATURE' or obj.animation_data is None or obj.animation_data.action is None:
                raise ToolError(f"'{source}' is not an armature with an action on it.")
            scene = bpy.context.scene
            low, high = obj.animation_data.action.frame_range
            rate = float(obj.get("unix_fps") or scene.render.fps / scene.render.fps_base)
            motion, label = _mocap.RigMotion(obj, rate, low, high), obj.animation_data.action.name
        return _transfer(target, motion, label, name, start_s, end_s, bool(loop), bool(in_place), bool(mirror), bone_map,
                         blend_from, blend_to, float(arms_out_deg or 0.0), min_cycle_s, float(turn_deg or 0.0))

    return registry.run_on_main(work, 840.0)
