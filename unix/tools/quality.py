# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Scene audit: the mistakes a builder makes without noticing, measured.

A model that places many things by script does not see that two walls share a plane (the
surface flickers: z-fighting), that one house stands inside another, that a chimney ended up
hidden inside its roof, that something hangs in the air, or that it built eleven of what was
asked ten times. ``audit`` finds those by geometry, and ``inventory`` counts and locates every
asset, so the result can be compared with the request. No tool is registered here:
``check_scene`` reports it and the engine runs it after every turn (see ``core.Engine``).

What counts as a problem is kept narrow on purpose, because a false alarm sends the agent off to
"fix" a correct scene:

* Parts are put together by pushing them into each other (a tree top over its trunk, a post
  into a base, a door into a wall). That is never reported. Parts belong together when they are
  parented, or when their names say so: ``House_0_Walls`` and ``House_0_Roof`` are house 0.
* Two *different* things of about the same size, one well inside the other, are reported (a
  house standing in a house). A small thing set into a big one (a door frame in a wall, a trunk in
  its canopy) and soft things grown into each other (a hedge) are how scenes are built.
* An object almost wholly inside another solid is reported whoever it belongs to: nobody can see it.
* An object is in the air only when neither it nor anything it touches reaches the ground.
  Touching is measured on the triangles, with a few centimetres of tolerance.

On a big scene this is seconds of work, so it is written as steps (``audit_steps``,
``separate_steps``) that the dispatcher runs in slices while the window keeps answering, and
what was measured of each object (its surface in world space, its faces, which pairs are
inside each other) is kept and reused until that object's evaluated mesh or placement changes:
after a turn that moved ten roofs, ten roofs are measured again. Between two slices only names
and plain numbers are held, never Blender objects (the user may delete one meanwhile), and the
work stops when Blender's data is replaced (an undo).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Generator, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Vector
from mathutils.bvhtree import BVHTree

from . import _util
from ._util import r

MAX_OBJECTS = 600
MAX_POLYGONS = 80_000
BODY_POLYGONS = 60_000          # a denser mesh is compared by its box only
PAIR_BUDGET = 400               # pairs measured by sampling, per audit
BURIED = 0.9                    # this much of an object's volume inside another solid, and ...
SHOWING = 0.1                   # ... less than this much of its surface outside it: it cannot be seen
COLLIDING = 0.5                 # this much of a thing inside a different thing of about its size: they collide
SIMILAR = 0.3                   # 'about its size': the smaller box is at least this much of the bigger one
FLICKER_REACH = 0.006           # parallel faces of big things closer than this flicker when seen from a distance
COPLANAR_POLYGONS = 20_000      # a denser mesh is not where shared planes come from
_TOKEN = re.compile(r"[A-Za-z]+|-?\d+")
_DIRECTIONS = [Vector(d).normalized() for d in ((0.31, 0.17, 1.0), (-1.0, 0.23, -0.29), (0.19, -1.0, 0.37))]
_bodies: Dict[str, "_Body"] = {}          # object name -> what was measured of it, while it stays the same
_measured: Dict[tuple, Any] = {}          # (signatures of the objects involved, question) -> answer


class SceneReplaced(Exception):
    """Blender's data was replaced (an undo, a file opened) while the scene was being measured."""


def guarded(steps: Generator) -> Generator:
    """Steps that stop with SceneReplaced when Blender's data is replaced between two slices."""
    epoch = _util.data_epoch()
    try:
        while True:
            try:
                signal = next(steps)
            except StopIteration as done:
                return done.value
            yield signal
            if _util.data_epoch() != epoch:
                raise SceneReplaced("the scene was undone or another file was opened while it was being measured")
    finally:
        steps.close()


def _run(steps: Generator) -> Any:
    while True:
        try:
            next(steps)
        except StopIteration as done:
            return done.value


def _remember(key: tuple, value: Any) -> Any:
    if len(_measured) > 20_000:
        _measured.clear()
    _measured[key] = value
    return value


def _root(obj: bpy.types.Object) -> bpy.types.Object:
    while obj.parent is not None:
        obj = obj.parent
    return obj


def _meshes(scene: bpy.types.Scene) -> List[bpy.types.Object]:
    out = []
    for obj in scene.objects:
        if obj.type != 'MESH' or obj.hide_render or not obj.data.polygons:
            continue
        root = _root(obj)
        if "unix_effect" in root or obj.display_type in {'BOUNDS', 'WIRE'}:
            continue          # effects are emitters and simulation boxes, not solid things
        out.append(obj)
    return out[:MAX_OBJECTS]


def _bounds(objects: List[bpy.types.Object]) -> Tuple[Vector, Vector]:
    points = [o.matrix_world @ Vector(c) for o in objects for c in o.bound_box]
    return (Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points))),
            Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points))))


def identity(name: str) -> Tuple[str, Any]:
    """(kind, instance) read from a name. ``House_0_Walls`` is ('house', 0); ``House.001`` is
    ('house', 1); a bare ``House`` is ('house', 'bare'), its own instance; ``Fence_Rail`` and
    ``Well_Post-1`` are ('fence', None) and ('well', None): a part of that kind, not one of them."""
    tokens = _TOKEN.findall(name)
    if not tokens or not tokens[0][0].isalpha():
        return name.lower(), "bare"
    kind = tokens[0].lower()
    if len(tokens) == 1:
        return kind, "bare"
    if tokens[1].lstrip("-").isdigit():
        return kind, int(tokens[1])
    return kind, None


def same_asset(first: bpy.types.Object, second: bpy.types.Object) -> bool:
    """Do two objects belong to one thing (and so may be pushed into each other freely)?"""
    a, b = _root(first), _root(second)
    if a is b:
        return True
    (kind_a, instance_a), (kind_b, instance_b) = identity(a.name), identity(b.name)
    return kind_a == kind_b and (instance_a == instance_b or instance_a is None or instance_b is None)


# ---------------------------------------------------------------------------------------------
# surfaces in the same plane


def _clip(subject: List[Tuple[float, float]], clip: List[Tuple[float, float]]) -> Tuple[float, Tuple[float, float]]:
    """(area, middle) of what two convex polygons share (Sutherland-Hodgman), both counter-clockwise."""
    output = subject
    for i in range(len(clip)):
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % len(clip)]
        ex, ey = bx - ax, by - ay
        inside = lambda p: ex * (p[1] - ay) - ey * (p[0] - ax) >= -1e-12      # noqa: E731
        points, output = output, []
        for j in range(len(points)):
            current, previous = points[j], points[j - 1]
            if inside(current) != inside(previous):
                dx, dy = current[0] - previous[0], current[1] - previous[1]
                denom = ex * dy - ey * dx
                if abs(denom) > 1e-18:
                    t = (ex * (ay - previous[1]) - ey * (ax - previous[0])) / denom
                    output.append((previous[0] + dx * t, previous[1] + dy * t))
            if inside(current):
                output.append(current)
        if len(output) < 3:
            return 0.0, (0.0, 0.0)
    area = 0.0
    for j in range(len(output)):
        x0, y0 = output[j - 1]
        x1, y1 = output[j]
        area += x0 * y1 - x1 * y0
    return abs(area) / 2.0, (sum(q[0] for q in output) / len(output), sum(q[1] for q in output) / len(output))


def _too_close(first_size: float, second_size: float) -> float:
    """How far apart two parallel overlapping faces of objects this big (their largest dimensions) must be not
    to flicker. A depth buffer tells surfaces apart to a fraction of the viewing distance, and big things are
    looked at from far away: two walls 4 mm apart flicker from across the street, two plates of a pistol 0.3 mm
    apart do not from arm's length."""
    size = max(first_size, second_size)
    return min(max(6e-4 * size, 1e-4), FLICKER_REACH)


_frames: Dict[tuple, Tuple[Vector, Vector, Vector]] = {}


def _frame(key_normal: tuple) -> Tuple[Vector, Vector, Vector]:
    """(normal, u, v): a frame in the plane, the same for every face whose rounded normal is ``key_normal``."""
    frame = _frames.get(key_normal)
    if frame is None:
        reference = Vector(key_normal).normalized()
        helper = Vector((1.0, 0.0, 0.0)) if abs(reference.x) < 0.9 else Vector((0.0, 1.0, 0.0))
        u = reference.cross(helper).normalized()
        frame = _frames[key_normal] = (reference, u, reference.cross(u))
    return frame


def _faces(body: "_Body", fine: float, tolerance: float) -> Dict[str, Any]:
    """The faces of one object worth comparing for shared planes, as arrays in polygon order: polygon, facing,
    distance along the rounded normal, outline box in the plane, area, rounded normal, material, bucket code,
    and where each outline's corners are in ``fx``/``fy`` (reversed where the outline runs clockwise). Worked
    out on whole arrays and kept with the object until it changes: no Python object per face, so a big scene
    kept between audits does not slow Python's memory sweeps."""
    cached = body.faces
    if cached is not None and cached[0] == (fine, tolerance):
        return cached[1]
    faces: Dict[str, Any] = {"poly": np.zeros(0, np.int64)}
    if body.start is not None and len(body.start):
        world, start, total, loops = body.world, body.start, body.total, body.loop_vert
        count = len(start)
        length = np.sqrt((body.normals * body.normals).sum(1))
        valid = length >= 1e-12
        unit = body.normals / np.where(valid, length, 1.0)[:, None]
        axis = np.abs(unit).argmax(1)
        facing = np.where(unit[np.arange(count), axis] < 0, -1, 1)
        key = np.round(unit * facing[:, None], 2) + 0.0            # + 0.0: no negative zeros in the keys
        reference = key / np.maximum(np.sqrt((key * key).sum(1)), 1e-12)[:, None]
        helper = np.where((np.abs(reference[:, 0]) < 0.9)[:, None], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        u = np.cross(reference, helper)
        u /= np.maximum(np.sqrt((u * u).sum(1)), 1e-12)[:, None]
        v = np.cross(reference, u)
        owner = np.repeat(np.arange(count), total)
        points = world[loops]
        fx = (points * u[owner]).sum(1)
        fy = (points * v[owner]).sum(1)
        distance = (world[loops[start]] * reference).sum(1)
        index = np.arange(len(loops))
        first = start[owner]
        previous = np.where(index == first, first + total[owner] - 1, index - 1)
        twice = np.add.reduceat(fx[previous] * fy - fx * fy[previous], start)
        area = np.abs(twice) / 2.0
        box = np.stack((np.minimum.reduceat(fx, start), np.minimum.reduceat(fy, start),
                        np.maximum.reduceat(fx, start), np.maximum.reduceat(fy, start)), 1)
        keep = np.nonzero(valid & (area >= fine * fine * 4))[0]
        slot = np.floor(distance[keep] / tolerance).astype(np.int64)
        whole = np.rint(key[keep] * 100).astype(np.int64) + 100
        faces = {"poly": keep, "facing": facing[keep], "depth": distance[keep], "box": box[keep], "area": area[keep],
                 "key": key[keep], "material": body.material[keep].astype(np.int64), "start": start[keep],
                 "total": total[keep], "flip": twice[keep] < 0, "fx": fx, "fy": fy,
                 "code": (((whole[:, 0] * 201 + whole[:, 1]) * 201 + whole[:, 2]) << 34) + slot + (1 << 33)}
    body.faces = ((fine, tolerance), faces)
    return faces


def _outline(faces: Dict[str, Any], k: int) -> List[Tuple[float, float]]:
    """Face ``k``'s outline in its plane, counter-clockwise."""
    s0, n = int(faces["start"][k]), int(faces["total"][k])
    flat = list(zip(faces["fx"][s0:s0 + n].tolist(), faces["fy"][s0:s0 + n].tolist()))
    if faces["flip"][k]:
        flat.reverse()
    return flat


def _coplanar(bodies: List["_Body"], tolerance: float, covered=None) -> Generator:
    """Steps; returns pairs of objects with faces in the same plane (or nearly: see ``_too_close``), facing the
    same way, overlapping: the renderer cannot tell which is in front, so the surface flickers. ``covered(point,
    normal, first, second)`` says whether something else hides that spot (the underside of two parts
    standing on the ground): what cannot be seen cannot flicker. Each pair comes with the way the shared
    faces point and how far apart they would have to be.

    Faces are put in buckets by rounded normal and by distance (each also in the next bucket, so a plane on
    a bucket edge is found from both sides), and only faces in one bucket are compared, in the order a
    double loop over each bucket would meet them. The cheap tests run on all those pairs at once."""
    budget = MAX_POLYGONS
    fine, tolerance = tolerance, max(tolerance, FLICKER_REACH)     # fine: the smallest area worth a look
    parts: List[Tuple[int, Dict[str, Any], int]] = []
    for index, body in enumerate(bodies):
        if budget <= 0:
            break
        if body.start is None:
            continue            # a dense organic mesh: not where shared planes come from
        faces = _faces(body, fine, tolerance)
        take = min(len(body.start), budget)
        budget -= take
        cut = int(np.searchsorted(faces["poly"], take))
        if cut:
            parts.append((index, faces, cut))
        yield
    if not parts:
        return []
    owner = np.concatenate([np.full(cut, index) for index, _faces_, cut in parts])
    part = np.concatenate([np.full(cut, n) for n, (_index, _faces_, cut) in enumerate(parts)])
    local = np.concatenate([np.arange(cut) for _index, _faces_, cut in parts])

    def joined(name: str) -> Any:
        return np.concatenate([faces[name][:cut] for _index, faces, cut in parts])

    poly, facing, depth, box = joined("poly"), joined("facing"), joined("depth"), joined("box")
    material, code = joined("material"), joined("code")
    size = np.array([body.size for body in bodies])[owner]
    yield

    # buckets with two faces or more, in the order a dict filled face by face would hold them
    codes = np.stack((code, code + 1), 1).ravel()
    _groups, first, inverse, counts = np.unique(codes, return_index=True, return_inverse=True, return_counts=True)
    crowded = np.nonzero(counts[inverse] >= 2)[0]
    if not len(crowded):
        return []
    crowded = crowded[np.lexsort((crowded, inverse[crowded]))]
    ids = np.unique(inverse[crowded])
    split = np.split(crowded // 2, np.cumsum(counts[ids])[:-1])
    groups = [split[k] for k in np.argsort(first[ids], kind="stable").tolist()]
    yield

    # every pair in every bucket, bucket by bucket, row by row
    a_list, b_list = [], []
    by_size: Dict[int, Tuple[Any, Any]] = {}
    for members in groups:
        n = len(members)
        if n not in by_size:
            by_size[n] = np.triu_indices(n, 1)
        i, j = by_size[n]
        a_list.append(members[i])
        b_list.append(members[j])
    a, b = np.concatenate(a_list), np.concatenate(b_list)
    yield
    keep = facing[a] == facing[b]
    one = owner[a] == owner[b]
    keep &= ~(one & ((poly[a] == poly[b]) | (material[a] == material[b])))   # the same face, or one object in one material
    reach = np.where(one, fine, np.minimum(np.maximum(6e-4 * np.maximum(size[a], size[b]), 1e-4), FLICKER_REACH))
    keep &= np.abs(depth[a] - depth[b]) <= reach
    keep &= (box[a, 0] < box[b, 2]) & (box[b, 0] < box[a, 2]) & (box[a, 1] < box[b, 3]) & (box[b, 1] < box[a, 3])
    survivors = np.nonzero(keep)[0]
    yield

    shared: Dict[tuple, float] = {}                  # (first, second, which way the faces point) -> shared area
    limits: Dict[Tuple[int, int], float] = {}
    seen = set()
    for count, k in enumerate(survivors.tolist()):
        if count and count % 200 == 0:
            yield
        fa, fb = int(a[k]), int(b[k])
        ia, ib = int(owner[fa]), int(owner[fb])
        pa, pb = int(poly[fa]), int(poly[fb])
        pair = (min(ia, ib), max(ia, ib))
        if pair not in limits:
            limits[pair] = _too_close(bodies[pair[0]].size, bodies[pair[1]].size)
        tag = (ia, pa, ib, pb) if (ia, pa) < (ib, pb) else (ib, pb, ia, pa)
        if tag in seen:
            continue
        seen.add(tag)
        faces_a, faces_b = parts[int(part[fa])][1], parts[int(part[fb])][1]
        la, lb = int(local[fa]), int(local[fb])
        overlap, middle = _clip(_outline(faces_a, la), _outline(faces_b, lb))
        if overlap > max(fine * fine * 4, 0.01 * min(float(faces_a["area"][la]), float(faces_b["area"][lb]))):
            reference, u, v = _frame(tuple(faces_a["key"][la].tolist()))
            spot = u * middle[0] + v * middle[1] + reference * float(depth[fa])
            side = int(facing[fa])
            if covered is not None and covered(spot, reference * side, ia, ib):
                continue
            way = reference * side
            key = (pair[0], pair[1], (round(way.x, 2), round(way.y, 2), round(way.z, 2)))
            shared[key] = shared.get(key, 0.0) + overlap
    out = []
    for (first_index, second_index, way), area in sorted(shared.items(), key=lambda item: -item[1]):
        size_max = max(bodies[first_index].size, bodies[second_index].size)
        if area < 0.01 and area < 0.02 * size_max * size_max:
            continue                   # a speck (a door knob against its door): nobody will see it flicker
        out.append({"a": bodies[first_index].name, "b": bodies[second_index].name, "area_m2": r(area, 4),
                    "faces": list(Vector(way).normalized()), "apart_m": limits[(first_index, second_index)]})
    return out


def separate_steps(scene: Optional[bpy.types.Scene] = None, movable: Optional[set] = None) -> Generator:
    """Steps; take surfaces that share a plane off it, without asking anybody. The smaller thing of each pair
    moves by a centimetre or so (twice what its size needs): a sheet lying on a surface (glass, trim, a path, a
    sign) comes out to sit on it, anything bulkier goes in, so its face ends up inside the other solid where it
    cannot be seen. ``movable`` names the objects that may be moved (what this turn made or changed); when the
    smaller one is not among them the other goes the opposite way instead. Returns what was moved."""
    scene_name = (scene or bpy.context.scene).name
    moved: List[Dict[str, Any]] = []
    done = set()
    for _round in range(3):
        scene = bpy.data.scenes.get(scene_name) or bpy.context.scene
        names = [obj.name for obj in _meshes(scene)]
        if not names:
            break
        bodies = yield from _collect(names)
        pairs = [p for p in (yield from _flicker(bodies)) if p["a"] != p["b"]]
        changed = False
        for pair in pairs:          # in one slice: the objects are looked up by name and moved at once
            first, second = bpy.data.objects.get(pair["a"]), bpy.data.objects.get(pair["b"])
            if first is None or second is None:
                continue
            small, big = sorted((first, second), key=lambda o: _box_volume(*_bounds([o])))
            way = Vector(pair["faces"])
            corners = [small.matrix_world @ Vector(c) for c in small.bound_box]
            thick = max(p.dot(way) for p in corners) - min(p.dot(way) for p in corners)
            sheet = thick <= max(0.05, 0.2 * max(small.dimensions)) or _see_through(small)
            step = way * (2.0 * pair["apart_m"] if sheet else -2.0 * pair["apart_m"])
            mover = small
            if movable is not None and small.name not in movable:
                if big.name not in movable:
                    continue                        # neither is ours to move
                mover, step = big, way * (-2.0 * pair["apart_m"])     # the big one draws back; what is on it stands proud
            # One move along a line clears both faces a thing shares with another on that line (flush front and
            # back: it ends up proud on one side and sunk on the other), so the pair is not moved twice.
            line = (frozenset((first.name, second.name)), tuple(round(abs(c), 2) for c in way))
            if line in done:
                continue
            done.add(line)
            place = mover.matrix_world.copy()
            place.translation = place.translation + step
            mover.matrix_world = place
            moved.append({"moved": mover.name, "by_mm": r(step.length * 1000, 1), "off": (big if mover is small else small).name})
            changed = True
        if not changed:
            break
        bpy.context.view_layer.update()
    return moved


def separate(scene: Optional[bpy.types.Scene] = None, movable: Optional[set] = None) -> List[Dict[str, Any]]:
    """``separate_steps`` run to the end at once."""
    return _run(separate_steps(scene, movable))


# ---------------------------------------------------------------------------------------------
# assets: what belongs together


def _box_volume(lo: Vector, hi: Vector) -> float:
    return max(hi.x - lo.x, 1e-3) * max(hi.y - lo.y, 1e-3) * max(hi.z - lo.z, 1e-3)


def _assets(objects: List[bpy.types.Object]) -> List[Dict[str, Any]]:
    """The things in the scene: one entry per instance, with every object that belongs to it.
    ``main`` is its biggest object (the one whose turn says which way the thing faces)."""
    by_root: Dict[bpy.types.Object, List[bpy.types.Object]] = {}
    for obj in objects:
        by_root.setdefault(_root(obj), []).append(obj)
    instances: Dict[Tuple[str, Any], Dict[str, Any]] = {}
    shared: Dict[str, Dict[str, Any]] = {}
    for root, members in by_root.items():
        kind, instance = identity(root.name)
        label = _TOKEN.findall(root.name)[0] if _TOKEN.findall(root.name) and _TOKEN.findall(root.name)[0][0].isalpha() else root.name
        target = shared.setdefault(kind, {"kind": label, "roots": [], "members": []}) if instance is None \
            else instances.setdefault((kind, instance), {"kind": label, "roots": [], "members": [], "instance": instance})
        target["roots"].append(root)
        target["members"].extend(members)
    kinds_with_instances = {kind for kind, _instance in instances}
    assets = list(instances.values())
    for kind, entry in shared.items():
        if kind in kinds_with_instances:
            continue                 # a fence rail next to numbered fence posts: part of the fence, not one more fence
        assets.append(entry)
    for entry in assets:
        entry["main"] = max(entry["members"], key=lambda o: _box_volume(*_bounds([o])))
        if len(entry["roots"]) == 1:
            entry["name"] = entry["roots"][0].name
        elif isinstance(entry.get("instance"), int):
            entry["name"] = f"{entry['kind']}_{entry['instance']}"
        else:
            entry["name"] = entry["kind"]
    return assets


# ---------------------------------------------------------------------------------------------
# bodies: touching, inside, buried


class _Body:
    """One object's surface in world space, with the questions the audit asks of it. Built from the
    evaluated mesh and kept (``_bodies``) while ``sig`` (its evaluated geometry and placement) stays the
    same; ``see_through``, ``size`` and the box of its corners are refreshed on every audit. Holds no
    Blender data: it stays valid while the window answers the user between two slices."""

    def __init__(self, name: str, sig: tuple, evaluated, arrays: Dict[str, Any]) -> None:
        mesh = evaluated.data
        matrix = np.array(evaluated.matrix_world, dtype=np.float64)
        co = arrays["co"].reshape(-1, 3).astype(np.float64)
        world = co @ matrix[:3, :3].T + matrix[:3, 3]
        loop_vert, total = arrays["loop_vert"], arrays["total"]
        start = np.empty(len(total), np.int32)
        mesh.polygons.foreach_get("loop_start", start)
        loop_edge = np.empty(len(loop_vert), np.int32)
        mesh.loops.foreach_get("edge_index", loop_edge)
        uses = np.bincount(loop_edge, minlength=len(mesh.edges)) if len(mesh.edges) else np.zeros(0)
        self.closed = bool(len(uses)) and bool(np.all(uses == 2))     # a solid, not a sheet
        self.name, self.sig = name, sig
        low, high = world.min(0), world.max(0)
        self.lo, self.hi = Vector(low.tolist()), Vector(high.tolist())
        size = self.hi - self.lo
        self.margin = max(0.02, min(0.05, 0.02 * max(size)))                   # how near counts as touching
        self.flat = min(size) < max(0.05, 0.03 * max(size))                    # a path, a rug, a pond: dressing on a surface
        self.vertex = Vector(world[0].tolist())
        self.dense = len(total) > BODY_POLYGONS
        self.verts: List[Vector] = []
        self.polys: List[tuple] = []
        self.tree = self.fat = None
        points = world.tolist()
        if not self.dense:
            self.verts = [Vector(p) for p in points]
            corners = loop_vert.tolist()
            self.polys = [tuple(corners[s:s + n]) for s, n in zip(start.tolist(), total.tolist())]
            self.tree = BVHTree.FromPolygons(points, self.polys)
            centre = (low + high) / 2
            spread = high - low
            grow = np.where(spread > 1e-9, (spread + 2 * self.margin) / np.where(spread > 1e-9, spread, 1.0), 1.0)
            self.fat = BVHTree.FromPolygons((centre + (world - centre) * grow).tolist(), self.polys)
        # the faces, for shared planes: only a mesh light enough to be where shared planes come from
        self.start = self.total = self.loop_vert = self.normals = self.material = self.world = None
        if len(total) <= COPLANAR_POLYGONS:
            normals = np.empty(len(total) * 3, np.float32)
            mesh.polygons.foreach_get("normal", normals)
            turn = matrix[:3, :3]
            inverse = np.linalg.inv(turn) if abs(np.linalg.det(turn)) > 1e-30 else np.linalg.pinv(turn)
            self.normals = normals.reshape(-1, 3).astype(np.float64) @ inverse
            self.start, self.total, self.loop_vert, self.world = start, total, loop_vert, world
            self.material = arrays["material"]
        self.faces = None
        # refreshed by every audit (see _collect)
        self.see_through = False
        self.size = 0.0
        self.blo = self.bhi = None

    def inside(self, point: Vector) -> bool:
        """Is the point in this solid? Rays from it cross the surface an odd number of times.
        Counting crossings does not depend on which way the faces point, so a mesh built
        inside out is still a solid here."""
        if self.tree is None or not self.closed:
            return False
        if any(point[k] < self.lo[k] or point[k] > self.hi[k] for k in range(3)):
            return False
        votes = 0
        for direction in _DIRECTIONS:
            origin, crossings = point, 0
            for _hit in range(64):
                hit = self.tree.ray_cast(origin, direction)
                if hit[0] is None:
                    break
                crossings += 1
                origin = hit[0] + direction * 1e-5
            votes += crossings % 2
        return votes >= 2


def _signature(evaluated) -> Tuple[Optional[tuple], Optional[Dict[str, Any]]]:
    """What an object's measurements depend on (its evaluated geometry, materials by face, and placement),
    read from whole arrays at once; (None, None) for an empty mesh."""
    mesh = evaluated.data
    if mesh is None or not len(mesh.vertices) or not len(mesh.polygons):
        return None, None
    co = np.empty(len(mesh.vertices) * 3, np.float32)
    mesh.vertices.foreach_get("co", co)
    loop_vert = np.empty(len(mesh.loops), np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vert)
    total = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("loop_total", total)
    material = np.empty(len(mesh.polygons), np.int32)
    mesh.polygons.foreach_get("material_index", material)
    placement = tuple(v for row in evaluated.matrix_world for v in row)
    sig = (placement, len(co), len(loop_vert), len(mesh.edges), hash(co.tobytes()), hash(loop_vert.tobytes()),
           hash(total.tobytes()), hash(material.tobytes()))
    return sig, {"co": co, "loop_vert": loop_vert, "total": total, "material": material}


def _collect(names: List[str]) -> Generator:
    """Steps; returns the bodies of these objects, in order. An object is measured again only when its
    evaluated mesh or its placement changed since the last time; gone or empty objects are left out."""
    bodies: List[_Body] = []
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for count, name in enumerate(names):
        if count:
            yield
            depsgraph = bpy.context.evaluated_depsgraph_get()     # the user may have changed something meanwhile
        obj = bpy.data.objects.get(name)
        if obj is None or obj.type != 'MESH':
            continue
        evaluated = obj.evaluated_get(depsgraph)
        sig, arrays = _signature(evaluated)
        if sig is None:
            continue
        body = _bodies.get(name)
        if body is None or body.sig != sig:
            body = _bodies[name] = _Body(name, sig, evaluated, arrays)
        body.see_through = _see_through(obj)
        body.size = max(obj.dimensions)
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        body.blo = Vector((min(p.x for p in corners), min(p.y for p in corners), min(p.z for p in corners)))
        body.bhi = Vector((max(p.x for p in corners), max(p.y for p in corners), max(p.z for p in corners)))
        bodies.append(body)
    if len(_bodies) > 2 * len(names) + 2000:
        keep = set(names)
        for name in [n for n in _bodies if n not in keep]:
            del _bodies[name]
    return bodies


def _see_through(obj: bpy.types.Object) -> bool:
    if obj.display_type in {'WIRE', 'BOUNDS'}:
        return True
    for slot in obj.material_slots:
        material = slot.material
        if material is None:
            continue
        if material.diffuse_color[3] < 0.95:
            return True
        if material.use_nodes and material.node_tree:
            for node in material.node_tree.nodes:
                if node.type != 'BSDF_PRINCIPLED':
                    continue
                alpha, glass = node.inputs.get("Alpha"), node.inputs.get("Transmission Weight")
                if (alpha is not None and (alpha.is_linked or alpha.default_value < 0.95)) \
                        or (glass is not None and (glass.is_linked or glass.default_value > 0.3)):
                    return True
    return False


def _glows(obj: bpy.types.Object) -> bool:
    """Does the object give off light (a bulb, a flame, a screen)? A light inside its housing is where it belongs."""
    for slot in obj.material_slots:
        material = slot.material
        if material is None or not material.use_nodes or not material.node_tree:
            continue
        for node in material.node_tree.nodes:
            if node.type == 'EMISSION':
                return True
            if node.type == 'BSDF_PRINCIPLED':
                strength, colour = node.inputs.get("Emission Strength"), node.inputs.get("Emission Color")
                lit = strength is not None and (strength.is_linked or strength.default_value > 0.0)
                dark = colour is not None and not colour.is_linked and max(colour.default_value[:3]) <= 0.0
                if lit and not dark:
                    return True
    return False


def _touching(a: _Body, b: _Body) -> bool:
    reach = a.margin + b.margin
    if any(a.lo[k] - reach > b.hi[k] or b.lo[k] - reach > a.hi[k] for k in range(3)):
        return False
    if a.dense or b.dense:
        return True                    # too heavy to compare triangle by triangle: their boxes meet
    if a.fat.overlap(b.tree) or b.fat.overlap(a.tree):
        return True
    return b.inside(a.vertex) or a.inside(b.vertex)       # one wholly inside the other: no surfaces cross


def _share(small: _Body, big: _Body, cells: int = 6) -> Tuple[float, int]:
    """(how much of ``small``'s solid is inside ``big``'s solid, sample points that were in ``small``)."""
    lo, hi = small.lo, small.hi
    own = both = 0
    for i in range(cells):
        for j in range(cells):
            for k in range(cells):
                point = Vector((lo.x + (hi.x - lo.x) * (i + 0.5) / cells, lo.y + (hi.y - lo.y) * (j + 0.5) / cells,
                                lo.z + (hi.z - lo.z) * (k + 0.5) / cells))
                if small.inside(point):
                    own += 1
                    if big.inside(point):
                        both += 1
    return (both / own if own else 0.0), own


def _showing(small: _Body, around: List[_Body]) -> float:
    """How much of ``small``'s surface can be seen past the solids ``around`` it (0..1). A pond in
    a basin shows its whole top; a chimney sunk into a roof shows two slivers. Each face is
    sampled at its middle and towards each corner, a millimetre off the surface on the side that
    is outside ``small``, weighted by the face's area."""
    total = outside = 0.0
    for poly in small.polys[:4000]:
        corners = [small.verts[index] for index in poly]
        middle = sum(corners, Vector()) / len(corners)
        normal = Vector()
        for k in range(len(corners)):
            normal += (corners[k] - middle).cross(corners[(k + 1) % len(corners)] - middle)
        area = normal.length / 2
        if area <= 1e-12:
            continue
        normal.normalize()
        if small.inside(middle + normal * 1e-3):
            normal = -normal                                   # the face was built pointing inward
        samples = [middle] + [middle + (corner - middle) * 0.8 for corner in corners]
        seen = sum(1 for point in samples if not any(body.inside(point + normal * 1e-3) for body in around))
        total += area
        outside += area * seen / len(samples)
    return outside / total if total else 1.0


def _contacts(bodies: List[_Body]) -> Generator:
    """Steps; returns the pairs of bodies that touch. Boxes far apart are ruled out on whole arrays first;
    what touching takes is kept for pairs that have not changed."""
    pairs: List[Tuple[int, int]] = []
    if len(bodies) < 2:
        return pairs
    lo = np.array([tuple(b.lo) for b in bodies])
    hi = np.array([tuple(b.hi) for b in bodies])
    margin = np.array([b.margin for b in bodies])
    for i in range(len(bodies) - 1):
        reach = (margin[i] + margin[i + 1:])[:, None]
        near = np.all(lo[i] - reach <= hi[i + 1:], 1) & np.all(lo[i + 1:] - reach <= hi[i], 1)
        for j in (np.nonzero(near)[0] + i + 1).tolist():
            a, b = bodies[i], bodies[j]
            key = (a.sig, b.sig, "touching")
            touching = _measured.get(key)
            if touching is None:
                touching = _remember(key, _touching(a, b))
            if touching:
                pairs.append((i, j))
        yield
    return pairs


def _named(name: str) -> Optional[bpy.types.Object]:
    return bpy.data.objects.get(name)


def _glows_named(name: str) -> bool:
    obj = _named(name)              # looked up afresh: a slice may have ended since the last lookup
    return obj is not None and _glows(obj)


def _inside_each_other(bodies: List[_Body], pairs: List[Tuple[int, int]]) -> Generator:
    """Steps; returns (hidden objects, different things that collide) among the pairs that touch."""
    linked: Dict[int, List[int]] = {}
    for i, j in pairs:
        linked.setdefault(i, []).append(j)
        linked.setdefault(j, []).append(i)
    buried, colliding, measured, judged = [], [], 0, set()
    for i, j in pairs:
        a, b = bodies[i], bodies[j]
        small, big, small_index = (a, b, i) if _box_volume(a.lo, a.hi) <= _box_volume(b.lo, b.hi) else (b, a, j)
        if not (small.closed and big.closed) or small.dense or big.dense:
            continue
        lo = Vector([max(small.lo[k], big.lo[k]) for k in range(3)])
        hi = Vector([min(small.hi[k], big.hi[k]) for k in range(3)])
        if min(hi - lo) <= 0:
            continue
        boxed = _box_volume(lo, hi) / _box_volume(small.lo, small.hi)
        first, second = _named(small.name), _named(big.name)
        together = first is not None and second is not None and same_asset(first, second)
        if boxed < (0.6 if together else 0.08):
            continue                   # not enough of it is even near the other one
        measured += 1
        if measured > PAIR_BUDGET:
            break
        key = (small.sig, big.sig, "share")
        found = _measured.get(key)
        if found is None:
            found = _remember(key, _share(small, big))
            yield
        share, points = found
        if points < 6:
            continue
        if share >= BURIED and not big.see_through and small_index not in judged and not _glows_named(small.name):
            judged.add(small_index)
            around = [bodies[k] for k in linked.get(small_index, []) if bodies[k].closed and not bodies[k].see_through]
            key = (small.sig, tuple(body.sig for body in around), "showing")
            showing = _measured.get(key)
            if showing is None:
                showing = _remember(key, _showing(small, around))
                yield
            if showing < SHOWING:
                buried.append({"object": small.name, "inside": big.name, "showing": f"{round(100 * showing)}%"})
                continue
        alike = _box_volume(small.lo, small.hi) >= SIMILAR * _box_volume(big.lo, big.hi)
        if not together and alike and share >= COLLIDING and not small.flat and not big.flat:
            colliding.append({"a": a.name, "b": b.name, "share": f"{round(100 * share)}%", "of": small.name})
    return buried, colliding


def _floating(bodies: List[_Body], pairs: List[Tuple[int, int]]) -> List[Dict[str, Any]]:
    """Objects that neither reach the ground nor touch anything that does, as groups that hang together."""
    if not bodies:
        return []
    ground = max(0.0, min(body.lo.z for body in bodies))       # a scene built above zero stands on its lowest things
    linked: Dict[int, List[int]] = {index: [] for index in range(len(bodies))}
    for i, j in pairs:
        linked[i].append(j)
        linked[j].append(i)
    supported = {index for index, body in enumerate(bodies) if body.lo.z <= ground + body.margin}
    frontier = list(supported)
    while frontier:
        for other in linked[frontier.pop()]:
            if other not in supported:
                supported.add(other)
                frontier.append(other)
    out, grouped = [], set()
    down = Vector((0.0, 0.0, -1.0))
    for index in range(len(bodies)):
        if index in supported or index in grouped:
            continue
        cluster, frontier = [index], [index]
        grouped.add(index)
        while frontier:
            for other in linked[frontier.pop()]:
                if other not in grouped and other not in supported:
                    grouped.add(other)
                    cluster.append(other)
                    frontier.append(other)
        lowest = min((bodies[k] for k in cluster), key=lambda body: body.lo.z)
        gap, under = lowest.lo.z - ground, "the ground level"
        if lowest.tree is not None:
            middle = Vector(((lowest.lo.x + lowest.hi.x) / 2, (lowest.lo.y + lowest.hi.y) / 2, lowest.lo.z - 1e-4))
            drops = [middle] + [v - Vector((0.0, 0.0, 1e-4)) for v in lowest.verts[:400] if v.z <= lowest.lo.z + lowest.margin]
            for k, body in enumerate(bodies):
                if k in cluster or body.tree is None or body.lo.z > lowest.hi.z \
                        or body.hi.x < lowest.lo.x or body.lo.x > lowest.hi.x or body.hi.y < lowest.lo.y or body.lo.y > lowest.hi.y:
                    continue
                for origin in drops:                           # down from its underside ...
                    hit = body.tree.ray_cast(origin, down)
                    if hit[0] is not None and hit[3] < gap:
                        gap, under = hit[3], f"'{body.name}'"
                for vertex in body.verts[:400]:                # ... and up from what is below it: a thin post is found
                    hit = lowest.tree.ray_cast(vertex + Vector((0.0, 0.0, 1e-4)), -down)
                    if hit[0] is not None and hit[3] < gap:
                        gap, under = hit[3], f"'{body.name}'"
        entry = {"asset": lowest.name, "gap_m": r(gap, 3), "above": under}
        if len(cluster) > 1:
            entry["with"] = sorted(bodies[k].name for k in cluster if bodies[k] is not lowest)[:8]
        out.append(entry)
    return out


# ---------------------------------------------------------------------------------------------


def _flicker(bodies: List[_Body]) -> Generator:
    """Steps; returns the shared planes that can be seen (see ``_coplanar``)."""
    if not bodies:
        return []
    lo = Vector((min(b.blo.x for b in bodies), min(b.blo.y for b in bodies), min(b.blo.z for b in bodies)))
    hi = Vector((max(b.bhi.x for b in bodies), max(b.bhi.y for b in bodies), max(b.bhi.z for b in bodies)))
    low = np.array([tuple(b.lo) for b in bodies]) - 0.08
    high = np.array([tuple(b.hi) for b in bodies]) + 0.08
    solid = np.array([b.tree is not None for b in bodies])

    def covered(spot: Vector, normal: Vector, first: int, second: int) -> bool:
        """Is this spot of a shared plane hidden by a third object lying against it or around it? Only a body
        whose box is within a few centimetres of the spot can be (the probes reach 6 cm at most)."""
        point = np.array(tuple(spot))
        near = solid & np.all(low <= point, 1) & np.all(point <= high, 1)
        for index in np.nonzero(near)[0].tolist():
            if index in (first, second):
                continue
            body = bodies[index]
            if body.inside(spot + normal * 0.01):
                return True
            hit = body.tree.ray_cast(spot - normal * 0.02, normal, 0.06)
            if hit[0] is not None:
                return True
        return False

    return (yield from _coplanar(bodies, max(0.001, (hi - lo).length * 1e-5), covered))


def inventory(scene: Optional[bpy.types.Scene] = None) -> Dict[str, Any]:
    """What is in the scene, counted: things by kind, and where each one is and faces."""
    scene = scene or bpy.context.scene
    assets = _assets(_meshes(scene))
    kinds: Dict[str, int] = {}
    rows = []
    for asset in assets:
        kinds[asset["kind"]] = kinds.get(asset["kind"], 0) + 1
        lo, hi = _bounds(asset["members"])
        main = asset["main"]
        yaw = math.degrees(main.matrix_world.to_euler('XYZ').z)
        front = main.matrix_world.to_3x3() @ Vector((0.0, -1.0, 0.0))
        rows.append({"name": asset["name"], "parts": len(asset["members"]), "at": [r((lo.x + hi.x) / 2, 2), r((lo.y + hi.y) / 2, 2)],
                     "yaw_deg": r(yaw, 1), "front": [r(front.x, 2), r(front.y, 2)],
                     "size": [r(hi.x - lo.x, 2), r(hi.y - lo.y, 2), r(hi.z - lo.z, 2)]})
    return {"assets": len(assets), "by_kind": dict(sorted(kinds.items(), key=lambda item: (-item[1], item[0]))),
            "placed": rows[:80]}


def audit_steps(scene: Optional[bpy.types.Scene] = None) -> Generator:
    """Steps; returns the geometric problems in the scene. ``problems`` is a list of plain sentences, worst
    first; ``keys`` names each one, so a problem that was already shown can be told from a new one."""
    scene = scene or bpy.context.scene
    names = [obj.name for obj in _meshes(scene)]
    empty = {"collapsed": [], "flickering_surfaces": [], "buried": [], "overlapping_assets": [], "floating": [], "problems": [], "keys": []}
    if not names:
        return empty
    bodies = yield from _collect(names)
    if not bodies:
        return empty
    pairs = yield from _contacts(bodies)
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}          # one line per pair, however many planes they share
    for item in (yield from _flicker(bodies)):
        entry = merged.setdefault((item["a"], item["b"]), {"a": item["a"], "b": item["b"], "area_m2": 0.0})
        entry["area_m2"] = r(entry["area_m2"] + item["area_m2"], 4)
    flicker = sorted(merged.values(), key=lambda item: -item["area_m2"])
    buried, colliding = yield from _inside_each_other(bodies, pairs)
    yield
    floating = _floating(bodies, pairs)
    problems, keys = [], []

    def report(key: tuple, line: str) -> None:
        if len(problems) < 30:
            problems.append(line)
            keys.append("|".join(str(part) for part in key))

    collapsed = []            # squeezed to a line or a point (a scale of zero on two axes): it is there and cannot be seen
    for body in bodies:
        size = sorted(body.hi - body.lo)
        if size[1] < 1e-5 and size[2] > 1e-4:
            collapsed.append(body.name)
            report(("collapsed", body.name),
                   f"'{body.name}' has no size in two directions: it has been squeezed to a line {r(size[2], 3)} m long and "
                   f"cannot be seen. Its scale or its vertices are wrong (a zero where a size belongs): rebuild it at its real size.")
    floating = [item for item in floating if item["asset"] not in collapsed]
    for item in flicker[:12]:
        where = "its own faces" if item["a"] == item["b"] else f"'{item['b']}'"
        report(("flicker", *sorted((item["a"], item["b"]))),
               f"'{item['a']}' shares a plane with {where} over {item['area_m2']} m2: the surface will flicker "
               f"(z-fighting). Move one of them off the plane by at least 1 cm, shorten it so they only meet, or "
               f"delete the duplicate.")
    for item in buried[:12]:
        report(("buried", item["object"], item["inside"]),
               f"'{item['object']}' is hidden inside '{item['inside']}': only {item['showing']} of its surface is outside "
               f"that solid. Move it until it shows, or remove it.")
    for item in colliding[:12]:
        other = item["b"] if item["of"] == item["a"] else item["a"]
        report(("overlap", *sorted((item["a"], item["b"]))),
               f"'{item['of']}' stands inside '{other}': {item['share']} of it is within that solid, and they are different "
               f"things of about the same size. Move them apart.")
    for item in floating[:12]:
        group = f" (with {len(item['with'])} more that hang on it)" if item.get("with") else ""
        report(("float", item["asset"]),
               f"'{item['asset']}'{group} touches nothing that stands on the ground: its underside is {item['gap_m']} m "
               f"above {item['above']}. Seat it, unless it is meant to float.")
    return {"collapsed": collapsed[:40], "flickering_surfaces": flicker[:40], "buried": buried[:40], "overlapping_assets": colliding[:40],
            "floating": floating[:40], "problems": problems, "keys": keys}


def audit(scene: Optional[bpy.types.Scene] = None) -> Dict[str, Any]:
    """``audit_steps`` run to the end at once."""
    return _run(audit_steps(scene))
