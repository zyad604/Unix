# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Roblox Studio: turn Blender objects into jobs for the Unix Bridge plugin.

Studio cannot import a file on command, so nothing is exported to disk. The plugin builds
everything from data with Studio's own APIs, and each kind of thing is sent the way Roblox
keeps it best:

* a box, cylinder, ball or wedge-shaped prism becomes native ``Part``s. They are exact, cheap,
  and permanent: they survive Play, saving and publishing with nothing uploaded;
* any other mesh becomes a ``MeshPart``. Roblox only keeps mesh geometry that is an asset on the
  user's account: a mesh built in the session looks right in Edit mode and turns into a
  checkered box in Play. So meshes are uploaded when the user agrees (asked once, see
  ``Sender``), and otherwise the report says plainly that they are a preview;
* the same shape used many times (forty trees) travels once and is uploaded once: later copies
  name it and carry only their place, turn and size;
* Roblox allows 20,000 triangles per mesh; a detailed model is never simplified to fit, it is
  cut into pieces of 10,000 triangles (one run of pieces per material) so full detail arrives;
* small things travel together in one job, a heavy piece travels alone, and every job is
  confirmed by Studio before the next one leaves;
* objects that move in Blender's timeline carry their motion: the transform is sampled over the
  frame range and played back by the plugin (in Edit mode) and by a script in the game;
* a mesh that an armature deforms travels skinned to the armature's bones, and the armature's
  actions travel as poses that the plugin and a script in the model play (see ``roblox_rig``);
* a Unix effect that plays on the spot (a fire, a column, a plume) travels as what Blender
  renders: its frames are filmed into one sheet of pictures that a particle emitter plays as a
  flipbook, so what stands in Roblox is what was made here. Roblox keeps a picture only as an
  image asset on the user's account, so this needs the same yes as meshes; without it, and for
  bursts and weather, the effect is rebuilt from Roblox's stock particles and a light;
* a surface travels whole (``roblox_surface.py``): its colour, normal, roughness and metal
  pictures, what its alpha cuts out and the light it gives off become a ``SurfaceAppearance``,
  in the pictures' own size up to Roblox's 1024 px. What shader nodes make (noise, gradients,
  mixes: nothing Roblox can run) is baked into such pictures on a stand-in copy of the object.
  Roblox keeps a picture, like a mesh, only as an asset on the user's account.

Blender is Z-up in metres; Roblox is Y-up in studs (1 stud = 0.28 m): (x, y, z) -> (x, z, -y).
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import itertools
import math
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Matrix, Vector

from ..bridge.roblox_link import link
from . import _util, interact, roblox_surface
from .registry import ToolError
from .roblox_surface import BAKE_PX, PICTURE_PX, Baked as _Baked, made_by_nodes as _made_by_nodes, surface as _surface

STUDS_PER_METER = 1.0 / 0.28


def studs_per_unit(scene: Optional[bpy.types.Scene] = None) -> float:
    """Studs for one Blender unit. A scene whose units are None is laid out in studs (Roblox makers set
    it up that way: 1 unit = 1 stud); a metric or imperial one in metres, scale_length to the unit,
    and a stud is 0.28 m. Before this the sender always took metres, an agent building to a studs spec
    shrank the whole world under a 0.28 parent to make it arrive the right size."""
    units = (scene or bpy.context.scene).unit_settings
    return 1.0 if units.system == 'NONE' else STUDS_PER_METER * (units.scale_length or 1.0)
TRIANGLES_PER_PIECE = 10_000
MAX_PART_STUDS = 2048           # the widest a MeshPart may be, along any of its axes
CELL_STUDS = 1500               # a wider mesh is cut in cells this wide: room for the triangles that cross a cell's edge
BATCH_TRIANGLES = 10_000        # small things share a job up to this weight; a heavier piece goes alone
BATCH_ITEMS = 80
UPLOADS_PER_JOB = 16            # Studio uploads several at a time: keep each job well inside JOB_SECONDS
SLICE_SECONDS = 0.03            # converting objects, per call of Sender.step: Blender stays responsive
MOTION_SAMPLES = 240            # at most this many keys per object; longer ranges are sampled more coarsely
JOB_SECONDS = 180.0             # how long Studio may take over one job
ASK_SECONDS = 840.0             # how long the upload question waits for the user
_TO_ROBLOX = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, -1.0, 0.0]])    # Blender vector -> Roblox vector
_UP = np.array([0.0, 1.0, 0.0])
_RIGHT = np.array([1.0, 0.0, 0.0])
PICTURES_WAITING = 8            # this many picture jobs queued: they leave before more objects are converted
CYLINDER_SIDES = 12             # fewer sides than this is a deliberate low-poly shape: it stays a mesh
BALL_VERTICES = 100

# Roblox plays flipbooks of 2x2, 4x4 or 8x8 square cells from one square picture of at most 1024 px.
# A big effect fills the screen, so it gets fewer, sharper frames; a small one gets more, smaller ones.
FILM_PX = 1024
FILM_BIG_M = 3.0                # from this size on: 4 x 4 cells of 256 px; below it: 8 x 8 cells of 128 px
FILM_FPS = {4: 12.0, 8: 15.0}
FILM_BLEND = {4: 3, 8: 8}       # frames at the seam that are cross-faded, so the loop has no jump
FILM_SQUASH = 1.5               # a ParticleEmitter with Squash s draws its picture (1 + s) times as tall and 1 / (1 + s) as wide
                                # (measured in Studio at 0.5, 1, 1.5 and -1); beyond 1.5 it no longer holds
FILM_BAND_ROWS = 256            # the sheet travels in bands of this many rows: one job stays about a megabyte
FILM_PLUGIN = 12                # the first version of the bridge plugin that plays films
KNOWN_PLUGIN = 15               # the first that says which shapes and pictures the place already holds
FILMED = ("fire", "smoke")      # presets that play on the spot and loop (bursts and weather stay native)

KEEP_ALWAYS = "Upload, and always do"
KEEP_ONCE = "Upload this time"
KEEP_NO = "No, preview only"


def _axes(points: np.ndarray) -> np.ndarray:
    """Blender (x, y, z) rows to Roblox (x, z, -y) rows."""
    return np.stack([points[:, 0], points[:, 2], -points[:, 1]], axis=1)


def _srgb(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return round(value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055, 3)


def _pose(position: np.ndarray, rotation: np.ndarray, offset: np.ndarray, base: Optional[np.ndarray] = None) -> List[float]:
    """The twelve CFrame numbers for a rigid pose given in Blender world space. ``base`` is the
    part's own resting rotation, already in Roblox space."""
    p = _TO_ROBLOX @ position * studs_per_unit() - offset
    r = _TO_ROBLOX @ rotation @ _TO_ROBLOX.T
    if base is not None:
        r = r @ base
    return [round(float(v), 4) for v in (*p, *r.ravel())]


def _unit(basis: np.ndarray) -> np.ndarray:
    lengths = np.maximum(np.linalg.norm(basis, axis=0), 1e-12)
    return basis / lengths


def _to_blender(point: np.ndarray, offset: np.ndarray) -> np.ndarray:
    """A Roblox point of this send (studs, offset removed) back in Blender world space."""
    return _TO_ROBLOX.T @ ((point + offset) / studs_per_unit())


def _may_move(obj: bpy.types.Object) -> bool:
    while obj is not None:
        data = obj.animation_data
        if (data is not None and (data.action is not None or len(data.drivers) or len(data.nla_tracks))) \
                or len(obj.constraints) or obj.rigid_body is not None:
            return True
        obj = obj.parent
    return False


def _sample_motion(objects: List[bpy.types.Object]):
    """World matrices of the objects over the scene's frame range: ({name: [matrix, ...]} for
    those that really move, keys per second)."""
    scene = bpy.context.scene
    start, end = (scene.frame_preview_start, scene.frame_preview_end) if scene.use_preview_range \
        else (scene.frame_start, scene.frame_end)
    watched = [o for o in objects if _may_move(o)]
    if not watched or end <= start:
        return {}, 0.0
    step = max(1, math.ceil((end - start + 1) / MOTION_SAMPLES))
    current = scene.frame_current
    samples: Dict[str, List[np.ndarray]] = {o.name: [] for o in watched}
    try:
        for frame in range(start, end + 1, step):
            scene.frame_set(frame)
            for obj in watched:
                samples[obj.name].append(np.array(obj.matrix_world, dtype=np.float64))
    finally:
        scene.frame_set(current)
    moving = {name: mats for name, mats in samples.items() if any(np.abs(m - mats[0]).max() > 1e-5 for m in mats[1:])}
    return moving, scene.render.fps / scene.render.fps_base / step


# ---------------------------------------------------------------------------------------------
# native shapes: a mesh that IS a box, cylinder, ball or wedge-shaped prism becomes Parts.
# Everything is measured on the world-space vertices, so it does not matter whether the shape's
# size and turn live in the object's transform or were written straight into its vertices.


def _upright(axes: List[np.ndarray], sizes: List[float]) -> Tuple[np.ndarray, np.ndarray, np.ndarray, List[float]]:
    """Name three perpendicular directions X, Y, Z the way a person would: Y is the one closest
    to up, X the one closest to world X, both pointing the positive way."""
    order = [0, 1, 2]
    up = max(order, key=lambda i: abs(axes[i] @ _UP))
    order.remove(up)
    side = max(order, key=lambda i: abs(axes[i] @ _RIGHT))
    order.remove(side)
    y = axes[up] * (1.0 if axes[up] @ _UP >= 0 else -1.0)
    x = axes[side] * (1.0 if axes[side] @ _RIGHT >= 0 else -1.0)
    return x, y, np.cross(x, y), [sizes[side], sizes[up], sizes[order[0]]]


def _as_box(points: np.ndarray, area: float) -> Optional[List[Dict[str, Any]]]:
    origin, others = points[0], points[1:] - points[0]
    lengths = np.linalg.norm(others, axis=1)
    if lengths.min() <= 1e-6:
        return None
    tolerance = 1e-4 * lengths.max() + 1e-6
    for i, j, k in itertools.combinations(range(7), 3):
        a, b, c = others[i], others[j], others[k]
        la, lb, lc = lengths[i], lengths[j], lengths[k]
        if abs(a @ b) > 1e-4 * la * lb or abs(a @ c) > 1e-4 * la * lc or abs(b @ c) > 1e-4 * lb * lc:
            continue
        corners = np.array([origin + a * u + b * v + c * w for u in (0, 1) for v in (0, 1) for w in (0, 1)])
        nearest = np.linalg.norm(corners[:, None, :] - points[None, :, :], axis=2)
        if nearest.min(axis=1).max() > tolerance or len(set(nearest.argmin(axis=1).tolist())) != 8:
            continue
        if abs(2.0 * (la * lb + lb * lc + la * lc) - area) > 0.01 * area:
            return None                                        # the right corners, but faces are missing or doubled
        x, y, z, size = _upright([a / la, b / lb, c / lc], [la, lb, lc])
        return [{"shape": "Block", "centre": origin + (a + b + c) / 2, "axes": (x, y, z), "size": size}]
    return None


def _wedge(corner: np.ndarray, top: np.ndarray, front: np.ndarray, depth: np.ndarray) -> Dict[str, Any]:
    """A WedgePart for the right triangle (right angle at ``corner``) pushed along ``depth``. Roblox's
    wedge has its full faces at the bottom and the back, and slopes from the bottom front edge up
    to the top back edge."""
    if abs((front - corner) @ _UP) > abs((top - corner) @ _UP) * 1.0001:
        top, front = front, top                                # the leg that stands more upright is the height
    rise, run = top - corner, corner - front
    y, z = rise / np.linalg.norm(rise), run / np.linalg.norm(run)
    return {"shape": "Wedge", "centre": (top + front) / 2 + depth / 2, "axes": (np.cross(y, z), y, z),
            "size": [float(np.linalg.norm(depth)), float(np.linalg.norm(rise)), float(np.linalg.norm(run))]}


def _as_prism(points: np.ndarray, area: float) -> Optional[List[Dict[str, Any]]]:
    """Six corners that are a triangle pushed straight along its normal: a ramp, or a gable roof.
    A right triangle is one wedge; any other is two, split along a height of the triangle."""
    scale = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
    tolerance = 1e-4 * scale + 1e-6
    for j in range(1, 6):
        depth = points[j] - points[0]
        shifted = points + depth
        nearest = np.linalg.norm(shifted[:, None, :] - points[None, :, :], axis=2)
        base = [i for i in range(6) if nearest[i].min() <= tolerance]
        if len(base) != 3 or len({int(nearest[i].argmin()) for i in base} | set(base)) != 6:
            continue
        a, b, c = points[base[0]], points[base[1]], points[base[2]]
        normal = np.cross(b - a, c - a)
        twice = float(np.linalg.norm(normal))
        length = float(np.linalg.norm(depth))
        if twice <= 1e-9 or length <= 1e-6 or np.linalg.norm(np.cross(depth, normal / twice)) > 1e-4 * length:
            continue                                           # flat, or pushed at a slant
        sides = [np.linalg.norm(b - a), np.linalg.norm(c - b), np.linalg.norm(a - c)]
        if abs(twice + sum(sides) * length - area) > 0.01 * area:
            return None
        corners = [a, b, c]
        for index in range(3):                                 # a right angle: one wedge
            p, u, v = corners[index], corners[(index + 1) % 3], corners[(index + 2) % 3]
            if abs((u - p) @ (v - p)) <= 1e-4 * np.linalg.norm(u - p) * np.linalg.norm(v - p):
                return [_wedge(p, u, v, depth)]
        best = None
        for index in range(3):                                 # else two, split along the most upright height
            apex, u, v = corners[index], corners[(index + 1) % 3], corners[(index + 2) % 3]
            along = (v - u) / np.linalg.norm(v - u)
            reach = (apex - u) @ along
            if reach <= 1e-6 or reach >= np.linalg.norm(v - u) - 1e-6:
                continue                                       # the height falls outside this side
            foot = u + along * reach
            height = apex - foot
            upright = abs(height @ _UP) / np.linalg.norm(height)
            if best is None or upright > best[0] + 1e-6:
                best = (upright, foot, apex, u, v)
        if best is None:
            return None
        _upr, foot, apex, u, v = best
        return [_wedge(foot, apex, u, depth), _wedge(foot, apex, v, depth)]
    return None


def _as_cylinder(points: np.ndarray, area: float, spread: np.ndarray) -> Optional[List[Dict[str, Any]]]:
    """Two equal rings (with or without a centre vertex) closed by caps. The axis is looked for
    among the principal directions of the points and of the face normals: one of the two always
    singles it out."""
    centred = points - points.mean(axis=0)
    candidates = list(np.linalg.eigh(centred.T @ centred)[1].T) + list(np.linalg.eigh(spread)[1].T)
    scale = float(np.linalg.norm(points.max(axis=0) - points.min(axis=0)))
    for axis in candidates:
        along = points @ axis
        low, high = float(along.min()), float(along.max())
        length = high - low
        if length <= 1e-6:
            continue
        at_low, at_high = np.abs(along - low) < 1e-4 * scale, np.abs(along - high) < 1e-4 * scale
        if not (at_low | at_high).all():
            continue
        flat = points - np.outer(along, axis)
        middle = flat.mean(axis=0)
        reach = np.linalg.norm(flat - middle, axis=1)
        radius = float(reach.max())
        rim = reach > 0.5 * radius
        sides = int((rim & at_low).sum())
        if radius <= 1e-6 or sides < CYLINDER_SIDES or sides != int((rim & at_high).sum()) or (~rim).sum() > 2 \
                or np.abs(reach[rim] - radius).max() > 0.005 * radius or (reach[~rim] > 1e-3 * radius).any():
            continue
        expected = sides * radius * radius * math.sin(2 * math.pi / sides) + sides * length * 2 * radius * math.sin(math.pi / sides)
        if abs(expected - area) > 0.015 * area:
            return None                                        # a pipe, a cup, a cone frustum
        strongest = int(np.abs(axis).argmax())
        x = axis * (1.0 if axis[strongest] >= 0 else -1.0)
        reference = _UP if abs(x @ _UP) < 0.9 else -_RIGHT
        y = reference - (reference @ x) * x
        y /= np.linalg.norm(y)
        return [{"shape": "Cylinder", "centre": middle + axis * (low + high) / 2, "axes": (x, y, np.cross(x, y)),
                 "size": [length, 2 * radius, 2 * radius]}]
    return None


def _as_ball(points: np.ndarray, area: float) -> Optional[List[Dict[str, Any]]]:
    low, high = points.min(axis=0), points.max(axis=0)
    centre = (low + high) / 2
    reach = np.linalg.norm(points - centre, axis=1)
    radius = float(reach.mean())
    if radius <= 1e-6 or np.abs(reach - radius).max() > 0.01 * radius or np.abs((high - low) - 2 * radius).max() > 0.06 * radius:
        return None
    if not 0.88 <= area / (4 * math.pi * radius * radius) <= 1.01:
        return None                                            # a dome or a shell with a hole
    return [{"shape": "Ball", "centre": centre, "axes": (_RIGHT, _UP, np.cross(_RIGHT, _UP)), "size": [2 * radius] * 3}]


class _Instance:
    """One object of a collection instance, where the instance puts it: it travels like an object of its own."""
    __slots__ = ("source", "matrix", "name")

    def __init__(self, source: bpy.types.Object, matrix: Matrix, name: str) -> None:
        self.source, self.matrix, self.name = source, matrix, name


def _instances(family: List[bpy.types.Object]) -> List[_Instance]:
    """The meshes that the collection instances among ``family`` show (nested ones too), each where it stands.
    Without them a farm placed eight times as an instance of one farm collection arrived as eight empty points."""
    owners = {o.name for o in family if o.type == 'EMPTY' and o.instance_type == 'COLLECTION' and o.instance_collection}
    if not owners:
        return []
    out: List[_Instance] = []
    named: Dict[str, int] = {}
    for instance in bpy.context.evaluated_depsgraph_get().object_instances:
        if not instance.is_instance or instance.parent is None:
            continue
        owner, source = instance.parent.original, instance.object.original
        if owner.name not in owners or source == owner or source.type != 'MESH' or "unix_effect" in source:
            continue
        name = f"{owner.name}_{source.name}"
        named[name] = named.get(name, 0) + 1
        if named[name] > 1:                     # the same object twice in one instance (nested collections)
            name += f"_{named[name]}"
        out.append(_Instance(source, instance.matrix_world.copy(), name))
    return out


def _native(evaluated: bpy.types.Object, mesh: bpy.types.Mesh, offset: np.ndarray,
            images: Dict[str, Any], world: Optional[np.ndarray] = None, called: str = "") -> Optional[List[Dict[str, Any]]]:
    """Parts for a mesh that is exactly a box, a cylinder, a ball or a triangular prism, else None. ``world``
    and ``called`` place and name it (an object of a collection instance); else it is the object's own."""
    count = len(mesh.vertices)
    if (count not in (6, 8) and count < 2 * CYLINDER_SIDES) or count > 20_000 or len(mesh.materials) > 1 \
            or not len(mesh.polygons):
        return None
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    world = np.array(evaluated.matrix_world, dtype=np.float64) if world is None else world
    points = _axes(co.reshape(-1, 3).astype(np.float64) @ world[:3, :3].T + world[:3, 3]) * studs_per_unit() - offset
    mesh.calc_loop_triangles()
    corners = np.empty(len(mesh.loop_triangles) * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("vertices", corners)
    corners = corners.reshape(-1, 3)
    cross = np.cross(points[corners[:, 1]] - points[corners[:, 0]], points[corners[:, 2]] - points[corners[:, 0]])
    doubled = np.linalg.norm(cross, axis=1)
    area = float(doubled.sum()) / 2
    if area <= 1e-9:
        return None
    if count == 8:
        shapes = _as_box(points, area)
    elif count == 6:
        shapes = _as_prism(points, area)
    else:
        facing = cross[doubled > 1e-12] / doubled[doubled > 1e-12][:, None]
        spread = (facing * doubled[doubled > 1e-12][:, None]).T @ facing      # sum of area * normal normal^T
        shapes = _as_cylinder(points, area, spread)
        if shapes is None and count >= BALL_VERTICES:
            shapes = _as_ball(points, area)
    if not shapes:
        return None
    surface = _surface(mesh.materials[0] if mesh.materials else None, images, part=True)
    if surface.pop("_pictured", False):
        return None                    # pictures mapped by UVs need the mesh: a Part cannot carry them
    surface.pop("reflectance", None)
    note = surface.pop("_procedural", None) or surface.pop("_unreadable", None)
    surface.pop("_uv", None)
    parts = []
    for index, shape in enumerate(shapes):
        x, y, z = shape["axes"]
        centre = shape["centre"]
        turn = np.stack([x, y, z], axis=1)
        parts.append({"name": (called or evaluated.name) + ("" if index == 0 else f"_{index + 1}"), "shape": shape["shape"],
                      "size": [round(max(float(s), 0.001), 3) for s in shape["size"]],
                      "cframe": [round(float(v), 4) for v in (*centre, *turn.ravel())],
                      "_centre": _to_blender(centre, offset), "_turn": turn, **surface})
    if note:
        parts[0]["_plain"] = note
    return parts


# ---------------------------------------------------------------------------------------------
# meshes


def _frame(world: np.ndarray) -> np.ndarray:
    """The object's own rotation (Blender space), or none when it is flattened or sheared. Vertices
    are sent in this frame so that two copies of a mesh turned differently are the same shape."""
    basis = world[:3, :3]
    lengths = np.linalg.norm(basis, axis=0)
    if lengths.min() <= 1e-9:
        return np.eye(3)
    unit = basis / lengths
    if np.abs(unit.T @ unit - np.eye(3)).max() > 1e-3:
        return np.eye(3)
    if np.linalg.det(unit) < 0:
        unit = unit * np.array([-1.0, 1.0, 1.0])               # mirrored: the flip stays in the vertices
    return unit


def _facing(corners: np.ndarray, points: np.ndarray) -> str:
    """Which way a mesh's faces point, from its triangles (vertex indices) alone: 'out' or 'in'
    for a closed surface whose faces all agree, 'open' for anything else (a sheet, a leaf, a
    mesh with holes, or faces that disagree). Roblox draws one side of a face only."""
    count = int(corners.max()) + 1
    start = corners.ravel().astype(np.int64)
    end = corners[:, [1, 2, 0]].ravel().astype(np.int64)
    forward, backward = np.sort(start * count + end), np.sort(end * count + start)
    if len(np.unique(forward)) != len(forward) or not np.array_equal(forward, backward):
        return "open"
    a, b, c = points[corners[:, 0]], points[corners[:, 1]], points[corners[:, 2]]
    volume = float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum())
    return "out" if volume >= 0 else "in"


def _ordered(triangles: np.ndarray) -> np.ndarray:
    """The same triangles in one fixed order (each starting at its lowest corner, then sorted), so
    two meshes with the same faces listed differently are recognised as the same shape."""
    first = triangles.argmin(axis=1)
    rows = np.arange(len(triangles))
    turned = np.stack([triangles[rows, first], triangles[rows, (first + 1) % 3], triangles[rows, (first + 2) % 3]], axis=1)
    return turned[np.lexsort((turned[:, 2], turned[:, 1], turned[:, 0]))].ravel().astype(np.int32)


def _pieces(faces: np.ndarray, loop_vertex: np.ndarray, positions: np.ndarray) -> List[np.ndarray]:
    """The triangles of one material in pieces Roblox can hold: at most TRIANGLES_PER_PIECE each, and no wider
    than a MeshPart may be. A mesh wider than that (a world's ground edge, 4,600 studs across) is cut into cells
    first; Roblox refused it whole and it was missing from the place."""
    if not len(faces):
        return []
    at = positions[loop_vertex[faces]]                       # (triangles, 3 corners, xyz) in the part's frame
    low = at.reshape(-1, 3).min(axis=0)
    if float((at.reshape(-1, 3).max(axis=0) - low).max()) <= MAX_PART_STUDS:
        return [faces[i:i + TRIANGLES_PER_PIECE] for i in range(0, len(faces), TRIANGLES_PER_PIECE)]
    cells = np.floor((at.mean(axis=1) - low) / CELL_STUDS).astype(np.int64)
    order = np.lexsort((cells[:, 2], cells[:, 1], cells[:, 0]))
    _cells, starts = np.unique(cells[order], axis=0, return_index=True)
    ends = list(starts[1:]) + [len(order)]
    out = []
    for start, end in zip(starts, ends):
        cell = faces[order[start:end]]
        out.extend(cell[i:i + TRIANGLES_PER_PIECE] for i in range(0, len(cell), TRIANGLES_PER_PIECE))
    return out


def _meshes(evaluated: bpy.types.Object, mesh: bpy.types.Mesh, offset: np.ndarray,
            images: Dict[str, Any], called: str = "", world: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
    mesh.calc_loop_triangles()
    triangles = len(mesh.loop_triangles)
    if not triangles:
        return []
    count, loops = len(mesh.vertices), len(mesh.loops)
    co = np.empty(count * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    loop_vertex = np.empty(loops, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", loop_vertex)
    normals = np.empty(loops * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    tri_loops = np.empty(triangles * 3, dtype=np.int32)
    mesh.loop_triangles.foreach_get("loops", tri_loops)
    tri_material = np.empty(triangles, dtype=np.int32)
    mesh.loop_triangles.foreach_get("material_index", tri_material)
    uv = None
    if mesh.uv_layers.active is not None:
        uv = np.empty(loops * 2, dtype=np.float32)
        mesh.uv_layers.active.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2).astype(np.float64)
    laid = roblox_surface.projected_uvs(mesh, list(mesh.materials), roblox_surface.texspace(evaluated, mesh))
    if laid is not None:                         # pictures projected from the object's coordinates: UVs that repeat them
        uv = laid

    world = np.array(evaluated.matrix_world, dtype=np.float64) if world is None else world
    basis = world[:3, :3]
    frame = _frame(world)
    turn = _TO_ROBLOX @ frame @ _TO_ROBLOX.T                    # the part's rotation in Roblox
    home = _TO_ROBLOX @ world[:3, 3] * studs_per_unit() - offset
    positions = _axes((co.reshape(-1, 3).astype(np.float64) @ basis.T) @ frame) * studs_per_unit()      # in the part's frame
    try:
        normal_matrix = np.linalg.inv(basis).T
    except np.linalg.LinAlgError:
        normal_matrix = basis
    normals = _axes((normals.reshape(-1, 3).astype(np.float64) @ normal_matrix.T) @ frame)
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-9)
    tri_loops = tri_loops.reshape(-1, 3)
    if np.linalg.det(basis) < 0:                 # a mirrored object: keep the faces pointing outward
        tri_loops = tri_loops[:, ::-1]
    facing = _facing(loop_vertex[tri_loops], positions)
    if facing == "in":                           # built inside out: Roblox would show its far side. Turn it.
        tri_loops = tri_loops[:, ::-1]
        normals = -normals

    turned = np.abs(turn - np.eye(3)).max() > 1e-6
    used = sorted(set(tri_material.tolist()))
    items = []
    for index in used:
        material = mesh.materials[index] if index < len(mesh.materials) else None
        surface = _surface(material, images)
        if uv is None:
            roblox_surface.without_uvs(surface)
        mapping = surface.pop("_uv", None)
        plain = surface.pop("_procedural", None) or surface.pop("_unreadable", None)
        placed = None
        if uv is not None:
            placed = uv
            if mapping and ("texture" in surface or "_look" in surface):      # what the Mapping node did: tile, turn, shift
                scaled = uv * np.array(mapping["scale"])
                cos, sin = math.cos(mapping["turn"]), math.sin(mapping["turn"])
                placed = np.stack([scaled[:, 0] * cos - scaled[:, 1] * sin, scaled[:, 0] * sin + scaled[:, 1] * cos], axis=1) \
                    + np.array(mapping["shift"])
            placed = np.stack([placed[:, 0], 1.0 - placed[:, 1]], axis=1)      # Roblox images start at the top-left
        groups = _pieces(tri_loops[tri_material == index], loop_vertex, positions)
        pieces = len(groups)
        for piece, faces in enumerate(groups):
            corner = faces.ravel()
            at = positions[loop_vertex[corner]]
            low, high = at.min(axis=0), at.max(axis=0)
            middle, extent = (low + high) / 2, max(float((high - low).max()), 1e-9)
            # centred and scaled to size 1: the same shape at another size or place gives the same numbers
            columns = [np.round((at - middle) / extent, 5), np.round(normals[corner], 2)]
            if placed is not None:
                columns.append(np.round(placed[corner], 4))
            unique, first, inverse = np.unique(np.concatenate(columns, axis=1), axis=0, return_index=True, return_inverse=True)
            name = called or evaluated.name
            if len(used) > 1 and material is not None:
                name += "_" + (material.get("unix_named") or material.name)
            if pieces > 1:
                name += f"_{piece + 1}"
            centre = turn @ middle + home
            item = {"name": name, "center": [round(float(c), 3) for c in centre],
                    "size": [round(float(s), 3) for s in (high - low)],
                    "_centre": _to_blender(centre, offset), "_turn": turn,
                    "_shape": {"points": unique[:, :3], "normals": unique[:, 3:6], "extent": extent,
                               "uv": unique[:, 6:8] if uv is not None else None,
                               "triangles": _ordered(np.asarray(inverse).reshape(-1, 3)),
                               "vertices": loop_vertex[corner[first]]}, **surface}     # which Blender vertex each one is
            if turned:
                item["rot"] = [round(float(v), 5) for v in turn.ravel()]
            if plain:
                item["_plain"] = plain
            if facing == "open":
                item["double_sided"] = True      # no inside to hide: drawn from both sides, so no face goes missing
            elif facing == "in":
                item["_turned"] = True
            items.append(item)
    return items


def _effect(root: bpy.types.Object, offset: np.ndarray) -> Dict[str, Any]:
    size = float(root.get("unix_size", root.empty_display_size * 2.0))
    position = _axes(np.array([root.matrix_world.translation[:]], dtype=np.float64))[0] * studs_per_unit() - offset
    item: Dict[str, Any] = {"name": root.name, "effect": str(root["unix_effect"]), "size": round(size * studs_per_unit(), 3),
                            "position": [round(float(c), 3) for c in position]}
    tint = root.get("unix_color")
    if tint is not None:
        item["color"] = [_srgb(c) for c in list(tint)[:3]]
    return item


def _filmed(root: bpy.types.Object) -> bool:
    """Whether this effect travels as pictures of itself: it stays in one place and repeats."""
    return "unix_column" in root or (str(root.get("unix_effect", "")) in FILMED and bool(root.get("unix_loop", True)))


def _plays_films(session: Dict[str, Any]) -> bool:
    version = str(session.get("version") or "")
    return version == "test" or (version.isdigit() and int(version) >= FILM_PLUGIN)


def _film_grid(root: bpy.types.Object) -> int:
    return 4 if float(root.get("unix_size", 1.0)) >= FILM_BIG_M else 8


def _film_frames(root: bpy.types.Object, grid: int) -> Tuple[List[int], float]:
    """The frames to film (one flipbook's worth plus the ones blended into the seam) and the speed
    the flipbook then plays at. A short effect is filmed more finely instead of running past its end."""
    scene = bpy.context.scene
    rate = scene.render.fps / (scene.render.fps_base or 1.0)
    start = int(root.get("unix_start", scene.frame_start))
    end = max(int(root.get("unix_end", scene.frame_end)), start + 1)
    count = grid * grid + FILM_BLEND[grid]
    step = min(rate / FILM_FPS[grid], (end - start) / (count - 1))
    return [int(round(start + step * i)) for i in range(count)], rate / step


def _film_sheet(cells: List[np.ndarray], grid: int) -> np.ndarray:
    """The filmed frames as one square sheet of bytes, first frame top-left. The first frames are
    cross-faded from the ones filmed after the last, so the last frame runs on into the first."""
    count, blend = grid * grid, FILM_BLEND[grid]
    frames = [np.asarray(cell, dtype=np.float32) for cell in cells]
    for index in range(min(blend, len(frames) - count)):
        mix = (index + 1) / (blend + 1)
        late, early = frames[count + index], frames[index]
        alpha = late[..., 3:] * (1.0 - mix) + early[..., 3:] * mix
        colour = late[..., :3] * late[..., 3:] * (1.0 - mix) + early[..., :3] * early[..., 3:] * mix
        frames[index] = np.concatenate([colour / np.maximum(alpha, 1e-6), alpha], axis=2)
    size = frames[0].shape[0]
    sheet = np.zeros((grid * size, grid * size, 4), dtype=np.float32)
    for index, frame in enumerate(frames[:count]):
        row, column = divmod(index, grid)
        sheet[row * size:(row + 1) * size, column * size:(column + 1) * size] = frame
    return (np.clip(sheet, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


def pick_session(project: str) -> Dict[str, Any]:
    """The Studio window to send to. Raises a ToolError that tells the model what to ask."""
    sessions = link.sessions()
    if not sessions:
        raise ToolError("no_link")
    if project:
        wanted = project.strip().lower()
        match = [s for s in sessions if s["session"] == project or s["place"].lower() == wanted] \
            or [s for s in sessions if wanted in s["place"].lower()]
        if len(match) == 1:
            return match[0]
        raise ToolError(f"No single open Roblox place matches '{project}'. Open places: "
                        + ", ".join(s["place"] for s in sessions) + ".")
    if len(sessions) > 1:
        raise ToolError("More than one Roblox Studio window is open: " + ", ".join(f"'{s['place']}'" for s in sessions)
                        + ". Ask the user which place to send to (ask_user, with these names as the options), then call "
                          "send_to_engine again with `project` set to the place they chose.")
    return sessions[0]


class Sender:
    """Sends an asset to one Studio window.

    Built on the main thread, then ``step`` is called again and again (also on the main
    thread): each call converts objects for a moment, posts the next job, or checks on the job
    Studio is building. Nothing waits inside a call, so Blender stays responsive however big the
    asset is, and each job is confirmed before the next one leaves.

    ``keep`` says what happens to meshes, which Roblox only keeps once they are assets on the
    user's account: 'upload', 'preview' (Edit mode only, nothing uploaded) or 'ask'. With 'ask'
    the user is asked when the first real mesh turns up, and never if everything is Parts and
    effects; ``remember`` is called if they answer "always".
    """

    def __init__(self, family: List[bpy.types.Object], asset: str, session: Dict[str, Any], at_origin: bool,
                 keep: str = "preview", remember: Optional[Callable[[], None]] = None) -> None:
        from . import roblox_fx
        effects = [o for o in family if "unix_effect" in o]
        inside_effects = {child for root in effects for child in root.children_recursive}
        self.todo: List[Any] = [o for o in family if o.type == 'MESH' and o not in inside_effects and o not in effects]
        instanced = _instances([o for o in family if o not in inside_effects])
        if not self.todo and not effects and not instanced:
            raise ToolError("Nothing to send: no meshes and no Unix effects among these objects.")
        self.asset, self.session = asset, session
        self.keep, self.remember = keep, remember
        self.why_preview = "" if keep != "preview" else "off"      # off | declined | unanswered | no user
        self.total = len(self.todo) + len(instanced)
        self.instanced = len(instanced)

        self.offset = np.zeros(3)
        if at_origin:      # the asset's base centre becomes its origin; the plugin stands it in front of the camera
            points = [o.matrix_world @ Vector(corner) for o in self.todo for corner in o.bound_box]
            points += [item.matrix @ Vector(corner) for item in instanced for corner in item.source.bound_box]
            points += [o.matrix_world.translation.copy() for o in effects]
            lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
            hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
            self.offset = _axes(np.array([[(lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z]]))[0] * studs_per_unit()

        from . import roblox_rig
        self.rigs = roblox_rig.Rigs(self.todo, self.offset, session)       # meshes that bones move go skinned (see roblox_rig)
        self.motion, self.fps = _sample_motion(self.rigs.others(self.todo))
        self.images = roblox_surface.Pictures(roblox_surface.plugin_shows_surfaces(session),
                                              self.todo + list({item.source: None for item in instanced}))
        self.todo = self.todo + instanced       # a new list: rigs and motion keep the objects' own; instances stand still
        self.sent_images: set = set()
        self.shapes: Dict[Tuple, List[Tuple[str, np.ndarray, np.ndarray, Optional[np.ndarray]]]] = {}
        effect_items = [_effect(root, self.offset) for root in effects]
        # effects that travel as pictures of themselves: filmed before anything is sent (see _film)
        self.films = [(root, item) for root, item in zip(effects, effect_items) if _filmed(root)]
        self.film: Optional[Tuple[Any, float, int]] = None
        self.bands: List[Dict[str, Any]] = []
        # What the place already holds as assets (asked first, plugin 15 on): those shapes go without their
        # geometry and those pictures do not travel at all. A re-send of a world carried every picture again,
        # a megabyte a job: 290 of its 343 jobs, minutes of a send that changed nothing.
        version = str(self.session.get("version", ""))
        self.studio_has: Optional[Dict[str, set]] = None if version.isdigit() and int(version) >= KNOWN_PLUGIN else {}
        self.held_back = {"shapes": 0, "pictures": 0}
        self.filmed: List[str] = []
        self.not_filmed: List[str] = []
        self.film_seconds, self.film_frames = 0.0, 0
        layered = [item for item in effect_items if item["effect"] in roblox_fx.ORB_EFFECTS]
        self.plain = [item for item in effect_items if item not in layered]
        self.effects = [item["effect"] for item in effect_items]
        self.effects_named = [item["name"] for item in effect_items]
        self.base = {"asset": asset, "place": "camera" if at_origin else "world"}
        self.ready: List[Tuple[str, Dict[str, Any], int]] = []      # (parts | meshes, item, triangles)
        self.model_job = bool(self.todo or self.plain)              # jobs that build the asset's own Model
        self.opened = False
        self.closed = not self.model_job
        self.wrap_up: List[Dict[str, Any]] = []
        if layered:        # an orb, shield or pulse: core, shells, plasma, arcs, sparks, animated
            name = asset + "_Energy" if self.model_job else asset
            children = [{"class": "Model", "name": item["name"],
                         "children": roblox_fx.energy_orb(radius=max(item["size"] * 0.5, 1.0), color=item.get("color"),
                                                          base=item["position"])} for item in layered]
            self.wrap_up.append({"kind": "tree", "asset": name, "parent": "Workspace", "place": self.base["place"],
                                 "root": {"class": "Model", "name": name, "children": children}})
        self.question = None
        self.asked = 0.0
        self.can_ask: Optional[bool] = None     # None: follow the chat's setting (questions are off in auto mode)
        self.flight: Optional[str] = None
        self.flight_since = 0.0
        self.replies: List[Dict[str, Any]] = []
        self.counts = {"parts": 0, "mesh_pieces": 0, "shapes": 0, "triangles": 0, "moving": 0, "cut": 0,
                       "turned": 0, "two_sided": 0, "baked": 0, "bake_reused": 0, "bake_kept": 0}
        self.bakes: Dict[Tuple, Any] = {}       # baked stand-ins kept for the copies still to come (see _bake_key)
        self.waited = 0.0                       # seconds spent waiting for the user's answer: not sending time
        self.plain_materials: set = set()       # materials whose look could not be carried: shader nodes, missing images
        self.posted = 0
        self.began = time.monotonic()

    # -- conversion --------------------------------------------------------------------

    def _track(self, item: Dict[str, Any], obj: bpy.types.Object, undo: Optional[np.ndarray]) -> None:
        """Keep the object's sampled motion with a part: the part moves rigidly with its object."""
        centre, turn = item.pop("_centre"), item.pop("_turn")
        matrices = self.motion.get(obj.name)
        if not matrices or undo is None:
            return
        frames = []
        for matrix in matrices:
            move = matrix @ undo
            frames.append(_pose(move[:3, :3] @ centre + move[:3, 3], _unit(move[:3, :3]), self.offset, turn))
        item["track"] = {"fps": round(self.fps, 3), "frames": frames}
        self.counts["moving"] += 1

    def _shape(self, item: Dict[str, Any]) -> int:
        """Give a mesh piece its geometry, or only the name of a shape this send already carried.
        Returns its triangle count."""
        shape = item.pop("_shape")
        points, normals, uv, triangles = shape["points"], shape["normals"], shape["uv"], shape["triangles"]
        bucket = (len(points), len(triangles), uv is not None, hashlib.sha1(triangles.tobytes()).hexdigest())
        for key, known_points, known_normals, known_uv in self.shapes.get(bucket, ()):
            if np.abs(known_points - points).max() < 2e-3 and np.abs(known_normals - normals).max() < 0.06 \
                    and (uv is None or np.abs(known_uv - uv).max() < 2e-3):
                item["key"] = key                               # a copy: Studio already has this shape
                return len(triangles) // 3
        digest = hashlib.sha1(points.tobytes() + normals.tobytes() + triangles.tobytes())
        if uv is not None:
            digest.update(uv.tobytes())
        item["key"] = digest.hexdigest()[:16]
        self.shapes.setdefault(bucket, []).append((item["key"], points, normals, uv))
        self.counts["shapes"] += 1
        item["v"] = np.round(points * shape["extent"], 3).ravel().tolist()
        item["n"] = normals.ravel().tolist()
        item["t"] = triangles.tolist()
        if uv is not None:
            item["uv"] = uv.ravel().tolist()
        return len(triangles) // 3

    def _bake_key(self, obj: bpy.types.Object) -> Optional[Tuple]:
        """What makes two objects' baked pictures the same: one mesh, the same materials, the same modifiers.
        A room of twenty-four copies of one chair is baked once."""
        if obj.type != 'MESH' or any(m.type == 'ARMATURE' for m in obj.modifiers):
            return None
        mesh = obj.data           # copies made with Shift+D have meshes of their own: compare the shapes, not the names
        points = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", points)
        shape = hashlib.sha1(np.round(points, 4).tobytes() + len(mesh.polygons).to_bytes(4, "little")).hexdigest()
        return (shape, tuple(m.name if m is not None else "" for m in mesh.materials),
                tuple((m.type, m.name) for m in obj.modifiers if m.show_render))

    def _convert(self, item: Any) -> None:
        placed = item if isinstance(item, _Instance) else None
        obj = placed.source if placed else item
        stand_in, key = None, None
        if _made_by_nodes(obj, self.images.full):       # shader nodes do not travel: bake what they make into pictures
            key = self._bake_key(obj)
            stand_in = self.bakes.get(key) if key is not None else None
            if stand_in is not None:
                stand_in.object.matrix_world = (placed.matrix if placed else obj.matrix_world).copy()
                self.counts["bake_reused"] += 1
            else:
                stand_in = _Baked(obj)
                if placed and stand_in.ok:
                    stand_in.object.matrix_world = placed.matrix.copy()
                if stand_in.ok:
                    self.counts["baked"] += 1
                    self.counts["bake_kept"] += 1 if stand_in.from_cache else 0
                    for original, copy in zip(obj.data.materials, stand_in.materials):
                        if original is not None:
                            copy["unix_named"] = original.name
                    if key is not None:
                        self.bakes[key] = stand_in
                else:
                    stand_in.remove()
                    stand_in = None
        try:
            self._convert_from(obj, stand_in.object if stand_in is not None else obj, baked=stand_in is not None,
                               placed=placed)
        finally:
            if stand_in is not None and key is None:
                stand_in.remove()

    def _drop_bakes(self) -> None:
        bakes, self.bakes = list(self.bakes.values()), {}
        for stand_in in bakes:
            try:
                stand_in.remove()
            except ReferenceError:
                pass

    def _convert_from(self, obj: bpy.types.Object, source: bpy.types.Object, baked: bool,
                      placed: Optional[_Instance] = None) -> None:
        evaluated = source.evaluated_get(bpy.context.evaluated_depsgraph_get())
        mesh = evaluated.to_mesh()
        # an instance's object stands where the instance puts it (a baked stand-in was put there already)
        world = np.array(placed.matrix, dtype=np.float64) if placed and not baked else None
        try:
            try:
                undo = np.linalg.inv(np.array(evaluated.matrix_world, dtype=np.float64))
            except np.linalg.LinAlgError:
                undo = None
            if placed:
                undo = None                             # instances stand still: _track only tidies the piece
            parts = None if baked or (not placed and self.rigs.of(obj)) else _native(
                evaluated, mesh, self.offset, self.images, world, placed.name if placed else "")
            if parts is not None:
                for part in parts:
                    self.plain_materials.update(filter(None, [part.pop("_plain", None)]))
                    self._track(part, obj, undo)
                    self.counts["parts"] += 1
                    self.ready.append(("parts", part, 0))
                return
            pieces = _meshes(evaluated, mesh, self.offset, self.images, placed.name if placed else obj.name, world)
            if not placed and self.rigs.skin(obj, mesh, pieces):       # bones move it: the pieces are skinned, the parts stay put
                self.motion.pop(obj.name, None)
                self.counts["shapes"] += len(pieces)
            if len(pieces) > len({m.name for m in mesh.materials if m is not None} or {""}):
                self.counts["cut"] += 1
            self.counts["turned"] += 1 if any([piece.pop("_turned", False) for piece in pieces]) else 0
            self.counts["two_sided"] += 1 if any(piece.get("double_sided") for piece in pieces) else 0
            for piece in pieces:
                self.plain_materials.update(filter(None, [piece.pop("_plain", None)]))
                self._track(piece, obj, undo)
                roblox_surface.carry(piece, self.images, self.sent_images, self.bands)
                texture = piece.get("texture")
                if texture:                 # the pixels travel once; later pieces name the image
                    if texture["key"] in self.sent_images:
                        piece["texture"] = {"key": texture["key"]}
                    self.sent_images.add(texture["key"])
                triangles = self._shape(piece) if "_shape" in piece else len(piece["t"]) // 3
                self.counts["mesh_pieces"] += 1
                self.counts["triangles"] += triangles
                self.ready.append(("meshes", piece, triangles))
        finally:
            evaluated.to_mesh_clear()

    # -- keeping meshes ------------------------------------------------------------------

    def _ask(self, pictures: bool = False) -> None:
        """Roblox keeps a mesh, and an effect's pictures, only as assets on the user's account. That is
        theirs to decide."""
        allowed = (interact.interactive["enabled"] and not bpy.app.background) if self.can_ask is None else self.can_ask
        if not allowed:
            self.keep, self.why_preview = "preview", "no user"
            return
        if pictures:
            text = (f"'{self.asset}' is an effect made in Unix. To look in Roblox the way it looks here it travels as "
                    f"pictures of itself, and Roblox only keeps a picture as an image asset on your Roblox account. Without "
                    f"the upload it is rebuilt from Roblox's stock particles and will not look the same. Upload it?")
        else:
            expected = self.images.expected()
            pictures = sum(expected.values())
            surfaces = (f", and their surfaces need up to {pictures} pictures ({self.images.in_words(expected)}) that it only "
                        f"keeps as image assets there" if pictures else "")
            text = (f"'{self.asset}' has shapes that Roblox only keeps as mesh assets on your Roblox account{surfaces}. Without "
                    f"the upload they look right while you edit, but turn into checkered boxes when you press Play or reopen "
                    f"the place. Upload them?")
        self.question = interact.begin_question(text, [KEEP_ALWAYS, KEEP_ONCE, KEEP_NO])
        if self.question is None:           # another question is on screen
            self.keep, self.why_preview = "preview", "no user"
            return
        self.asked = time.monotonic()
        _util.status["text"] = "Waiting for your answer"

    def _decide(self, answer: Optional[str]) -> None:
        text = (answer or "").strip().lower()
        if not text:
            self.keep, self.why_preview = "preview", "unanswered"
        elif text.startswith(("no", "n ", "preview", "don", "skip")) or text == "n":
            self.keep, self.why_preview = "preview", "declined"
        else:
            self.keep = "upload"
            if "always" in text and self.remember is not None:
                try:
                    self.remember()
                except Exception:
                    pass

    # -- effects as pictures -------------------------------------------------------------

    def _film(self) -> bool:
        """Film the next frame of the next effect. True when there is nothing left to film."""
        from . import vfx
        if not self.films:
            return True
        root, item = self.films[0]
        began = time.monotonic()
        try:
            if self.film is None:
                grid = _film_grid(root)
                frames, speed = _film_frames(root, grid)
                self.film = (vfx.Film(root, frames, FILM_PX // grid, fit=(1.0 + FILM_SQUASH) ** 2), speed, grid)
            film, speed, grid = self.film
            done = film.step()
        except ReferenceError:              # the effect was deleted while we were filming it
            self.close()
            self.films.pop(0)
            return not self.films
        except Exception as exc:            # a render that fails must not lose the send: it goes as stock particles
            self.close()
            self.films.pop(0)
            self.not_filmed.append(f"{item['name']} (could not be filmed: {str(exc).strip()[:120]})")
            return not self.films
        self.film_seconds += time.monotonic() - began
        if not done:
            _util.status["text"] = f"Filming {item['name']} for Roblox  {round(100 * len(film.cells) / len(film.frames))}%"
            return False
        film.close()
        self.film = None
        self.films.pop(0)
        sheet = _film_sheet(film.cells, grid)
        key = hashlib.sha1(sheet.tobytes()).hexdigest()[:16]
        height, width = sheet.shape[:2]
        for top in range(0, height, FILM_BAND_ROWS):
            band = sheet[top:top + FILM_BAND_ROWS]
            self.bands.append({"kind": "picture", "key": key, "w": width, "h": height, "y": top, "rows": int(band.shape[0]),
                               "data": base64.b64encode(band.tobytes()).decode("ascii")})
        centre = _axes(np.array([film.centre[:]], dtype=np.float64))[0] * studs_per_unit() - self.offset
        glows = str(root.get("unix_column", root.get("unix_effect", ""))) in ("fire", "energy")
        # the cell is square and what it shows is not: the emitter's Squash stretches it back
        stretch = math.sqrt(film.height / film.width)
        item["film"] = {"key": key, "grid": grid, "fps": round(speed, 2),
                        "size": round(math.sqrt(film.width * film.height) * studs_per_unit(), 3),
                        "squash": round(stretch - 1.0 if stretch >= 1.0 else 1.0 - 1.0 / stretch, 4),
                        "centre": [round(float(c), 3) for c in centre], "glows": glows}
        self.film_frames = grid * grid
        self.filmed.append(item["name"])
        return not self.films

    def close(self) -> None:
        """Put the scene back if a send ends while an effect is being filmed or objects are kept baked."""
        self._drop_bakes()
        self.rigs.close()
        film, self.film = self.film, None
        if film is not None:
            try:
                film[0].close()
            except Exception:
                pass

    # -- the loop ----------------------------------------------------------------------

    def _weight(self, kind: str, item: Dict[str, Any], triangles: int) -> int:
        if kind == "parts":
            return 20
        upload = BATCH_TRIANGLES // UPLOADS_PER_JOB if self.keep == "upload" else 0
        pictures = (item.get("surface") or {}).get("first", 0) * upload      # each new picture is an upload of its own
        if "v" not in item:
            return 60 + pictures            # a copy of a shape Studio already has
        return max(triangles, upload or 200) + pictures

    def _full(self) -> bool:
        return len(self.ready) >= BATCH_ITEMS or sum(self._weight(*entry) for entry in self.ready) >= BATCH_TRIANGLES \
            or len(self.bands) >= PICTURES_WAITING

    def _held(self, job: Dict[str, Any]) -> Dict[str, Any]:
        """Leave out what Studio already holds: the geometry of its shapes (the plugin builds those from its asset)."""
        for item in job.get("meshes") or ():
            if "v" in item and item.get("key") in self.studio_has.get("meshes", ()):
                for field in ("v", "n", "t", "uv"):
                    item.pop(field, None)
                self.held_back["shapes"] += 1
        return job

    def _next_job(self) -> Optional[Dict[str, Any]]:
        while self.bands and self.bands[0]["key"] in self.studio_has.get("pictures", ()):
            band = self.bands.pop(0)        # Studio holds this picture as an asset already: its rows stay here
            if band["y"] == 0:
                self.held_back["pictures"] += 1
        if self.bands:
            return self.bands.pop(0)
        if self.ready or (self.model_job and not self.opened):
            job: Dict[str, Any] = dict(self.base, upload=self.keep == "upload", parts=[], meshes=[])
            weight = 0
            while self.ready and (weight == 0 or (weight + self._weight(*self.ready[0]) <= BATCH_TRIANGLES
                                                  and len(job["parts"]) + len(job["meshes"]) < BATCH_ITEMS)):
                kind, item, triangles = self.ready.pop(0)
                weight += self._weight(kind, item, triangles)
                job[kind].append(item)
            if not self.opened:
                job.update(first=True, effects=self.plain, **self.rigs.opening())
                self.opened = True
            if not self.ready and not self.todo:
                job["last"] = True
                self.closed = True
            return self._held(job)
        if not self.closed:
            self.closed = True
            return dict(self.base, last=True)
        if self.wrap_up:
            return self.wrap_up.pop(0)
        return None

    def step(self) -> Optional[Dict[str, Any]]:
        """None while there is more to do; the report when everything has been built. ``waiting``
        says the last call had nothing to do but wait (for Studio or the user)."""
        if self.question is not None:
            answer = interact.collect_answer(self.question)
            if answer is None and time.monotonic() - self.asked < ASK_SECONDS:
                self.waiting = True
                return None
            if answer is None:
                interact.withdraw_question(self.question)
            self.question = None
            self.waited += time.monotonic() - self.asked
            self._decide(answer)
        self.waiting = False
        if self.flight is not None:              # Blender goes on converting while Studio builds the last job
            reply = link.result(self.flight)
            if reply is None and time.monotonic() - self.flight_since > JOB_SECONDS:
                _util.status["text"] = ""
                raise ToolError(f"Roblox Studio ('{self.session['place']}') stopped answering after {self.posted} jobs. "
                                f"Was the place closed, or is Studio showing a dialog?")
            if reply is not None and self.studio_has is None:       # the answer to "what do you hold?"
                report = reply.get("report") or {}
                self.studio_has = {"meshes": set(report.get("meshes") or ()), "pictures": set(report.get("pictures") or ())}
                self.flight = None
            elif reply is not None:
                self.replies.append(reply)
                self.flight = None
        if self.studio_has is None:
            if self.flight is None:
                self.flight = link.post(self.session["session"], {"kind": "known"})
                self.flight_since = time.monotonic()
            self.waiting = True
            return None
        if self.films:
            if self.keep == "ask":
                self._ask(pictures=True)
                if self.question is not None:
                    return None
            if self.keep != "upload" or not _plays_films(self.session):
                old = self.keep == "upload"
                self.not_filmed += [item["name"] + (" (Roblox Studio is running an older Unix Bridge: restart Studio "
                                                    "once and send again)" if old else "") for _root, item in self.films]
                self.films = []
            elif not self._film():
                return None
        if not self.rigs.sample(self.asset, self.wrap_up):      # the rigs' actions, a few keys per call
            return None
        until = time.monotonic() + SLICE_SECONDS
        while self.todo and not self._full():
            obj = self.todo.pop(0)
            baked_before = self.counts["baked"]
            try:
                with (contextlib.nullcontext() if isinstance(obj, _Instance) else self.rigs.resting(obj)):
                    self._convert(obj)
            except ReferenceError:
                pass                        # the object was deleted while we were sending
            if self.counts["baked"] != baked_before:
                until = 0.0                 # a bake takes a moment: let Blender breathe before the next one
            if self.keep == "ask" and self.counts["mesh_pieces"]:
                self._ask()
                if self.question is not None:
                    return None
            if time.monotonic() > until:
                break
        if self.todo and not self._full():
            return None                     # more to convert before this job is worth sending
        if self.flight is not None:         # the next job is ready: it goes when Studio answers the last one
            self.waiting = True
            return None
        job = self._next_job()
        if job is not None:
            try:
                self.flight = link.post(self.session["session"], job)
            except KeyError:
                _util.status["text"] = ""
                raise ToolError(f"Roblox Studio ('{self.session['place']}') was closed while sending.")
            self.flight_since = time.monotonic()
            self.posted += 1
            done = self.total - len(self.todo)
            _util.status["text"] = f"Sending to Roblox  {done} of {self.total}" if self.total else "Sending to Roblox"
            return None
        _util.status["text"] = ""
        return self._report()

    def _report(self) -> Dict[str, Any]:
        self._drop_bakes()
        built = {"parts": 0, "meshes": 0, "effects": 0}
        tally = {"uploaded": 0, "reused": 0, "kept": 0, "preview": 0, "pictures_uploaded": 0, "pictures_reused": 0,
                 "adopting": 0}
        studio_time: Dict[str, float] = {}
        problems, model = [], ""
        for reply in self.replies:
            if not reply["ok"]:
                problems.append(reply["message"] or "a step failed in Studio")
                continue
            report = reply["report"]
            for key in built:
                built[key] += int(report.get(key) or 0)
            if report.get("created"):          # a layered native effect (see roblox_fx)
                built["effects"] += 1
            for key in tally:
                tally[key] += int(report.get(key) or 0)
            for phase, seconds in (report.get("timing") or {}).items():
                studio_time[phase] = studio_time.get(phase, 0.0) + float(seconds)
            problems += [str(n) for n in (report.get("notes") or [])]
            model = model or report.get("model") or ""
        out: Dict[str, Any] = {"engine": "Roblox Studio", "place": self.session["place"], "asset": self.asset,
                               "scale": ("1 Blender unit = 1 stud (the scene's units are None: it is laid out in studs)"
                                         if studs_per_unit() == 1.0 else
                                         f"1 Blender unit = {studs_per_unit():.2f} studs (metres; a stud is 0.28 m). For a "
                                         "layout made in studs set Scene > Units to None; never scale the scene to fit")}
        if self.session.get("testing"):
            out["tell_the_user_first"] = ("Studio is running a play test, and what was sent went into the place being edited: "
                                          "it appears when the test is stopped, not in the running game. Say this first.")
        out.update({
            "built": built,
            "in_studio": model or f"Workspace.{self.asset}", "triangles": self.counts["triangles"],
            "jobs": self.posted, "seconds": round(time.monotonic() - self.began - self.waited, 1),
            "next": "It stands on the ground in front of the Studio camera and is selected. Sending the same asset again "
                    "replaces it where it stands."})
        if studio_time:
            out["studio_seconds"] = {phase: round(seconds, 1) for phase, seconds in studio_time.items() if seconds >= 0.05}
        if any(self.held_back.values()):
            out["not_sent_again"] = (f"Studio already held {self.held_back['shapes']} of the shapes and "
                                     f"{self.held_back['pictures']} of the pictures as assets: they were not sent again.")
        if self.counts["turned"] or self.counts["two_sided"]:
            out["faces"] = (f"Faces were made right for Roblox on the way: {self.counts['turned']} meshes built inside out were "
                            f"turned, {self.counts['two_sided']} open meshes are drawn from both sides. The Blender scene "
                            f"was not changed.")
        whole = self.images.full
        if self.sent_images and whole:
            kept = tally["pictures_uploaded"] + tally["pictures_reused"]
            out["textures"] = (
                f"{len(self.sent_images)} pictures went along for the surfaces of {self.images.surfaces} meshes "
                f"({self.images.in_words()} maps; in their own size up to {PICTURE_PX} px, with the material's tint and tiling): "
                f"each surface is a SurfaceAppearance, so gloss, metal, bumps, cut-outs and glow arrive, not only colour. "
                + (f"Image assets on the user's Roblox account hold {kept} of them ({tally['pictures_uploaded']} uploaded now, "
                   f"{tally['pictures_reused']} already there)." if kept else
                   "They are shown from memory while the place is edited, like meshes that are not uploaded: Roblox keeps a "
                   "picture only as an image asset on the user's account."))
        elif self.sent_images:
            out["textures"] = (f"{len(self.sent_images)} image textures went along (at up to {roblox_surface.LEGACY_PX} px, with "
                               f"the material's tint and tiling). Only colour arrived: Roblox Studio is running an older Unix "
                               f"Bridge that cannot show normal, roughness, metal or glow pictures. Restart Studio once and "
                               f"send again.")
        if self.counts["baked"]:
            out["baked"] = (f"{self.counts['baked']} objects had a look made by shader nodes, which Roblox cannot run: "
                            + (f"every part of it that varies over the surface (colour, gloss, metal, bumps, glow, cut-outs) "
                               f"was baked into pictures on fresh UVs on the way (up to {BAKE_PX} px, by the object's size), so "
                               f"it arrives."
                               if whole else f"their colour was baked into image textures on the way, so it arrives.")
                            + (f" {self.counts['bake_kept']} of them were unchanged since an earlier send and used its bake."
                               if self.counts["bake_kept"] else "")
                            + " The Blender scene was not changed.")
        if self.images.notes:
            out["approximated"] = sorted(self.images.notes)
        if self.plain_materials:
            names = sorted(self.plain_materials)
            out["plain_colours"] = (f"{len(names)} materials arrived as a plain colour, because their look is made by shader nodes "
                                    f"with no image behind it (or the image file is missing): {', '.join(names[:8])}. Roblox "
                                    f"cannot run Blender's shader nodes. Tell the user; an image texture on the material is what "
                                    f"travels.")
        if built["parts"]:
            out["parts_note"] = (f"{built['parts']} shapes are native Roblox Parts (boxes, cylinders, balls, wedges): exact, "
                                 f"and they stay in Play and after saving with nothing uploaded.")
        if self.instanced:
            out["instances"] = (f"{self.instanced} objects shown by collection instances travelled as objects of their own, "
                                f"each where its instance puts it and named after it. Nothing needs making real for a send.")
        if self.counts["cut"]:
            out["detail"] = (f"Full detail was kept: heavy meshes were cut into pieces of at most {TRIANGLES_PER_PIECE:,} "
                             f"triangles instead of being simplified.")
        if self.counts["mesh_pieces"] > self.counts["shapes"]:
            out["copies"] = (f"{self.counts['mesh_pieces']} meshes are {self.counts['shapes']} different shapes: each shape was "
                             f"sent once and its copies reuse it.")
        if self.counts["moving"]:
            out["animation"] = (f"{self.counts['moving']} pieces carry their Blender motion ({round(self.fps, 1)} keys per second, "
                                f"looping). It plays in Edit mode now and in Play through the UnixAnimator script; the model's "
                                f"Enabled attribute stops and starts it.")
        out.update(self.rigs.report(self.replies, uploaded=self.keep == "upload"))
        if self.filmed:
            out["effects_as_made"] = (f"{', '.join(self.filmed[:6])}: what stands in Roblox is the effect made here, filmed "
                                      f"here (filming its {self.film_frames} frames took {round(self.film_seconds)} s) and played by a particle "
                                      f"emitter as a looping flipbook from an image asset on the user's Roblox account. It "
                                      f"faces the camera from every side, plays in Edit mode and in Play, and its light is a "
                                      f"PointLight. To change how it looks, change it here and send again.")
        if self.not_filmed:
            reason = {"declined": "The user chose not to upload it. ",
                      "unanswered": "The user was asked whether to upload it and did not answer. ",
                      "no user": "The user could not be asked whether to upload it. ",
                      "off": "Uploading is switched off in the preferences (Meshes sent to Roblox). "
                      }.get(self.why_preview, "")
            out["effects_not_as_made"] = (
                f"{'; '.join(self.not_filmed[:6])}: in Roblox this is a stand-in built from Roblox's stock particles, NOT the "
                f"effect made here, and it looks different. The real one travels as pictures that Roblox keeps only as an "
                f"image asset on the user's account. {reason}Tell the user this plainly; if they want the effect as made, "
                f"send again with upload=true. Do not rebuild it by hand with roblox_build.")
        others = [name for name in self.effects_named
                  if name not in self.filmed and not any(note.startswith(name) for note in self.not_filmed)]
        if others:
            out["effects_note"] = (f"{', '.join(others[:6])}: bursts, weather and orbs are rebuilt with Roblox's own particle "
                                   f"emitters (they are not the Blender effect): tune them in the Properties panel.")
        if built["meshes"]:
            kept = tally["kept"] if (tally["kept"] or tally["preview"]) else (built["meshes"] if tally["uploaded"] else 0)
            preview = built["meshes"] - kept - tally["adopting"]
            if tally["adopting"]:
                out["meshes_processing"] = (
                    f"{tally['adopting']} meshes were uploaded a moment ago and Roblox is still processing them. Studio shows "
                    f"them from memory now and swaps each to its uploaded asset by itself as soon as Roblox has it ready (a few "
                    f"minutes, while Studio stays open): then they stay in Play, after saving and when published. Do not send "
                    f"again for them.")
            if kept:
                out["meshes_kept"] = (f"{kept} meshes use mesh assets on the user's Roblox account ({tally['uploaded']} uploaded "
                                      f"now, {tally['reused']} already there): they stay in Play, after saving and when published.")
            if preview > 0:
                reason = {"declined": "The user chose not to upload them.",
                          "unanswered": "The user was asked whether to upload them and did not answer.",
                          "no user": "The user could not be asked whether to upload them.",
                          "off": "Uploading is switched off in the preferences (Meshes sent to Roblox)."
                          }.get(self.why_preview, "Their upload did not go through (see problems).")
                pictures = (f" The same goes for their {len(self.sent_images)} pictures: an upload makes each an image asset."
                            if self.sent_images and whole and not tally["pictures_uploaded"] + tally["pictures_reused"] else "")
                out["preview_only"] = (
                    f"{preview} meshes are a Studio preview, NOT part of the game yet. They look right in Edit mode, but Roblox "
                    f"keeps mesh geometry only as an asset on the user's account: in Play, and after the place is saved or "
                    f"reopened, each of these shows as a checkered box.{pictures} {reason} Tell the user this plainly, do not "
                    f"call the send finished, and if they want it in the game send again with upload=true.")
        if problems:
            out["problems"] = problems[:12]
        return out
