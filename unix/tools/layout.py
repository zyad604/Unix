# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Placement and lighting rigs: the two things that most often make a scene look wrong.

``place_object`` sizes an asset in meters, seats it on the ground or on another object
and nudges it clear of its neighbours. ``light_rig`` builds a lighting setup scaled to
the subject. Both are deterministic maths the model would otherwise re-derive (and get
slightly wrong) in every script.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

from . import _turn
from ._util import ensure_object_mode, get_object, r, resolve_objects, vec
from .registry import ToolError, tool

_GEOMETRY = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME'}
RIG_COLLECTION = "Unix_Lighting"


def _family(obj: bpy.types.Object) -> List[bpy.types.Object]:
    return [obj] + list(obj.children_recursive)


def _bounds(objects: List[bpy.types.Object]) -> Optional[Tuple[Vector, Vector]]:
    depsgraph = bpy.context.evaluated_depsgraph_get()
    points: List[Vector] = []
    for obj in objects:
        if obj.type not in _GEOMETRY:
            continue
        ev = obj.evaluated_get(depsgraph)
        points.extend(ev.matrix_world @ Vector(c) for c in ev.bound_box)
    if not points:
        return None
    lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    return lo, hi


def _footprint(objects: List[bpy.types.Object]) -> Optional[Tuple[Vector, float]]:
    box = _bounds(objects)
    if box is None:
        return None
    lo, hi = box
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2))
    return centre, max(hi.x - lo.x, hi.y - lo.y) / 2


def _surface_height(support: bpy.types.Object, x: float, y: float) -> Optional[float]:
    """Top surface of ``support`` under (x, y), by a downward ray in its local space."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    ev = support.evaluated_get(depsgraph)
    inverse = ev.matrix_world.inverted()
    box = _bounds([support])
    top = (box[1].z if box else 0.0) + 10.0
    origin = inverse @ Vector((x, y, top))
    direction = (inverse.to_3x3() @ Vector((0, 0, -1))).normalized()
    hit, location, _normal, _index = ev.ray_cast(origin, direction)
    return (ev.matrix_world @ location).z if hit else None


@tool(
    "place_object",
    """Size, orient and seat an object (with its children) in one step: scale it so its longest
    side is `size_m` meters, rotate it about Z, put its base on the ground (Z=0) or on top of
    another object, centre it at an XY position and nudge it sideways until it no longer
    overlaps its neighbours. Use it after import_file / asset_model and whenever you arrange
    furniture or props, instead of computing offsets by hand.""",
    {
        "object": {"type": "string", "description": "Object to place (its children move with it)."},
        "xy": {"type": "array", "items": {"type": "number"}, "description": "[x, y] for the centre of its footprint. Omit to keep its position."},
        "on": {"type": "string", "description": "Name of an object to rest on (table, shelf, floor mesh). Omit for the ground plane Z=0."},
        "size_m": {"type": "number", "description": "Target length of the longest bounding-box side, in meters."},
        "yaw_deg": {"type": "number", "description": "Rotation about Z in degrees."},
        "avoid_overlap": {"type": "boolean", "description": "Nudge sideways to clear other objects' footprints (default true)."},
    },
    ["object"],
    mutates=True,
    label="Placed an object",
)
def place_object(object: str, xy: Optional[List[float]] = None, on: str = "", size_m: float = 0.0,
                 yaw_deg: Optional[float] = None, avoid_overlap: bool = True) -> Dict[str, Any]:
    ensure_object_mode()
    obj = get_object(object)
    support = get_object(on) if on else None
    family = _family(obj)
    if _bounds(family) is None:
        raise ToolError(f"'{object}' has no geometry to place.")
    if xy is not None and len(xy) != 2:
        raise ToolError("xy must be [x, y].")
    if size_m and not (0.001 <= size_m <= 1000.0):
        raise ToolError("size_m must be between 0.001 and 1000 meters.")
    _turn.mark_mutation()
    view_layer = bpy.context.view_layer

    if size_m:
        lo, hi = _bounds(family)
        longest = max(hi - lo)
        if longest > 1e-9:
            obj.scale = obj.scale * (size_m / longest)
            view_layer.update()
    if yaw_deg is not None:
        obj.rotation_mode = 'XYZ'
        obj.rotation_euler.z = math.radians(yaw_deg)
        view_layer.update()

    def move(delta: Vector) -> None:
        obj.matrix_world.translation = obj.matrix_world.translation + delta
        view_layer.update()

    lo, hi = _bounds(family)
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2))
    target = Vector(xy) if xy is not None else centre
    move(Vector((target.x - centre.x, target.y - centre.y, 0.0)))

    nudged = 0.0
    if avoid_overlap:
        family_names = {o.name for o in family}
        skip = family_names | ({support.name} | {c.name for c in support.children_recursive} if support else set())
        others = []
        for other in bpy.context.scene.objects:
            if other.name in skip or other.parent is not None or other.type not in _GEOMETRY:
                continue
            fp = _footprint(_family(other))
            if fp and fp[1] < 50.0:  # ignore floors and huge backdrops
                others.append(fp)
        mine = _footprint(family)
        radius = mine[1]

        def clear(point: Vector) -> bool:
            return all((point - c).length >= radius + rad for c, rad in others)

        if not clear(mine[0]):
            step = max(radius * 0.5, 0.05)
            found = None
            for ring in range(1, 12):
                for k in range(8):
                    angle = k * math.pi / 4
                    candidate = mine[0] + Vector((math.cos(angle), math.sin(angle))) * step * ring
                    if clear(candidate):
                        found = candidate
                        break
                if found is not None:
                    break
            if found is not None:
                nudged = (found - mine[0]).length
                move(Vector((found.x - mine[0].x, found.y - mine[0].y, 0.0)))

    lo, hi = _bounds(family)
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2))
    base = 0.0
    seated_on = "ground (Z=0)"
    if support is not None:
        height = _surface_height(support, centre.x, centre.y)
        if height is None:
            raise ToolError(f"'{on}' has no surface under ({centre.x:.2f}, {centre.y:.2f}). Choose an xy on top of it.")
        base, seated_on = height, support.name
    move(Vector((0.0, 0.0, base - lo.z)))

    lo, hi = _bounds(family)
    out: Dict[str, Any] = {"object": obj.name, "seated_on": seated_on, "base_z": r(lo.z, 4),
                           "footprint_centre": vec(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2), 3),
                           "dimensions": vec(hi - lo, 3), "location": vec(obj.matrix_world.translation, 3)}
    if nudged:
        out["nudged_m"] = r(nudged, 3)
        out["note"] = "Moved sideways to clear neighbouring objects; pass avoid_overlap=false to force the exact xy."
    return out


# -- lighting ---------------------------------------------------------------

def _rig_collection() -> bpy.types.Collection:
    col = bpy.data.collections.get(RIG_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(RIG_COLLECTION)
        bpy.context.scene.collection.children.link(col)
    elif col.name not in {c.name for c in bpy.context.scene.collection.children_recursive}:
        bpy.context.scene.collection.children.link(col)
    for old in list(col.objects):
        data = old.data
        bpy.data.objects.remove(old, do_unlink=True)
        if data is not None and data.users == 0:
            (bpy.data.lights if isinstance(data, bpy.types.Light) else bpy.data.meshes).remove(data)
    return col


def _area_light(col, name: str, position: Vector, aim: Vector, watts: float, size: float,
                color=(1.0, 1.0, 1.0)) -> bpy.types.Object:
    data = bpy.data.lights.new(name, 'AREA')
    data.energy, data.size, data.color = watts, size, color
    obj = bpy.data.objects.new(name, data)
    col.objects.link(obj)
    obj.location = position
    obj.rotation_euler = (position - aim).to_track_quat('Z', 'Y').to_euler()
    return obj


def _sky_world(sun_elevation_deg: float, sun_rotation_deg: float, strength: float) -> str:
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    nodes.clear()
    sky = nodes.new("ShaderNodeTexSky")
    background = nodes.new("ShaderNodeBackground")
    output = nodes.new("ShaderNodeOutputWorld")
    sky.location, background.location, output.location = (-400, 300), (-150, 300), (80, 300)
    for attr, value in (("sun_elevation", math.radians(sun_elevation_deg)),
                        ("sun_rotation", math.radians(sun_rotation_deg))):
        if hasattr(sky, attr):
            setattr(sky, attr, value)
    background.inputs["Strength"].default_value = strength
    links.new(sky.outputs["Color"], background.inputs["Color"])
    links.new(background.outputs["Background"], output.inputs["Surface"])
    return sky.sky_type


def _neutral_world(grey: float) -> None:
    scene = bpy.context.scene
    world = scene.world or bpy.data.worlds.new("World")
    scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    nodes.clear()
    background = nodes.new("ShaderNodeBackground")
    output = nodes.new("ShaderNodeOutputWorld")
    background.inputs["Color"].default_value = (grey, grey, grey, 1.0)
    background.inputs["Strength"].default_value = 1.0
    links.new(background.outputs["Background"], output.inputs["Surface"])


@tool(
    "light_rig",
    """Build a lighting setup sized to the subject, replacing any previous Unix rig:
    'three_point' (key, fill and rim area lights, the default for characters and props),
    'product' (large soft overhead key, two side fills and a seamless curved backdrop),
    'outdoor_sun' (sun lamp plus a physical sky world) or 'overcast' (soft sky, no hard sun).
    Light distance, size and wattage are derived from the subject's bounding box. Follow with
    look(shading='rendered'). For photographic realism use asset_hdri instead or as well.""",
    {
        "style": {"type": "string", "enum": ["three_point", "product", "outdoor_sun", "overcast"]},
        "targets": {"type": "array", "items": {"type": "string"}, "description": "Subject objects. Omit to light everything."},
        "intensity": {"type": "number", "description": "Multiplier on the computed energy (default 1.0)."},
        "key_angle_deg": {"type": "number", "description": "Key light direction around Z: 0 is from -Y (front), 45 is front-right (default 40)."},
        "set_world": {"type": "boolean", "description": "Also replace the world background/sky to suit the style (default true)."},
        "keep_other_lights": {"type": "boolean", "description": "Leave lamps that are not part of the rig switched on (default false: they are switched off for rendering, so the rig is the lighting)."},
    },
    ["style"],
    mutates=True,
    label="Built a lighting rig",
)
def light_rig(style: str, targets: Optional[List[str]] = None, intensity: float = 1.0,
              key_angle_deg: float = 40.0, set_world: bool = True, keep_other_lights: bool = False) -> Dict[str, Any]:
    ensure_object_mode()
    subjects = [o for o in resolve_objects(targets, _GEOMETRY) if not o.name.startswith("Unix_")]
    box = _bounds(subjects)
    if box is None:
        raise ToolError("Nothing to light. Create geometry first or name the target objects.")
    _turn.mark_mutation()
    lo, hi = box
    centre = (lo + hi) / 2
    radius = max((hi - lo).length / 2, 0.05)
    distance = radius * 3.0
    col = _rig_collection()
    made: List[Dict[str, Any]] = []

    def polar(angle_deg: float, elevation_deg: float, dist: float) -> Vector:
        a, e = math.radians(angle_deg - 90.0), math.radians(elevation_deg)  # 0 deg = from -Y
        return centre + Vector((math.cos(a) * math.cos(e), math.sin(a) * math.cos(e), math.sin(e))) * dist

    def add(name, angle, elevation, watts_per_m2, size_factor, color=(1.0, 1.0, 1.0), dist=distance):
        watts = watts_per_m2 * dist * dist * intensity
        light = _area_light(col, name, polar(angle, elevation, dist), centre, watts, radius * size_factor, color)
        made.append({"name": light.name, "watts": r(watts, 1), "size_m": r(radius * size_factor, 3)})

    world_note = "unchanged"
    if style == "three_point":
        # energies are set so that an 18% grey card comes out mid-grey (about 0.5 on screen) under the
        # default view transform: brighter than that, dark things render grey and everything looks washed
        add("Unix_Key", key_angle_deg, 40.0, 9.0, 1.2, (1.0, 0.96, 0.9))
        add("Unix_Fill", key_angle_deg - 85.0, 15.0, 3.0, 2.2, (0.9, 0.95, 1.0))
        add("Unix_Rim", key_angle_deg + 150.0, 45.0, 7.5, 0.9)
        if set_world:
            _neutral_world(0.03)
            world_note = "dark neutral grey"
    elif style == "product":
        add("Unix_Key", 0.0, 80.0, 15.0, 3.0)
        add("Unix_Fill_L", -70.0, 20.0, 5.5, 2.0)
        add("Unix_Fill_R", 70.0, 20.0, 5.5, 2.0)
        # Seamless backdrop: floor sweeping up into a back wall through a quarter-circle.
        depth, width, bend, wall = radius * 6.0, radius * 10.0, radius * 1.5, radius * 5.0
        profile = [(-depth * 0.5, 0.0)]
        for i in range(13):
            t = math.pi / 2 * i / 12
            profile.append((depth * 0.5 - bend + math.sin(t) * bend, bend - math.cos(t) * bend))
        profile.append((depth * 0.5, bend + wall))
        verts = [(x * side, centre.y + y, lo.z + z) for (y, z) in profile for x in (-width / 2,) for side in (1, -1)]
        faces = [(2 * i, 2 * i + 1, 2 * i + 3, 2 * i + 2) for i in range(len(profile) - 1)]
        mesh = bpy.data.meshes.new("Unix_Backdrop")
        mesh.from_pydata([(v[0] + centre.x, v[1], v[2]) for v in verts], [], faces)
        for poly in mesh.polygons:
            poly.use_smooth = True
        backdrop = bpy.data.objects.new("Unix_Backdrop", mesh)
        col.objects.link(backdrop)
        mat = bpy.data.materials.get("Unix_Backdrop") or bpy.data.materials.new("Unix_Backdrop")
        mat.use_nodes = True
        bsdf = next((n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (0.8, 0.8, 0.8, 1.0)
            bsdf.inputs["Roughness"].default_value = 0.7
        mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
        mesh.materials.append(mat)
        made.append({"name": backdrop.name, "type": "backdrop"})
        if set_world:
            _neutral_world(0.05)
            world_note = "dark neutral grey"
    else:
        hard = style == "outdoor_sun"
        elevation = 42.0 if hard else 70.0
        data = bpy.data.lights.new("Unix_Sun", 'SUN')
        data.energy = (4.0 if hard else 1.2) * intensity
        data.angle = math.radians(0.6 if hard else 25.0)
        sun = bpy.data.objects.new("Unix_Sun", data)
        col.objects.link(sun)
        sun.location = polar(key_angle_deg, elevation, distance * 2.0)
        sun.rotation_euler = (sun.location - centre).to_track_quat('Z', 'Y').to_euler()
        made.append({"name": sun.name, "strength": r(data.energy, 2)})
        if set_world:
            sky_type = _sky_world(elevation, key_angle_deg + 90.0, 0.25 if hard else 0.6)
            world_note = f"sky texture ({sky_type})"
    switched_off = []
    if not keep_other_lights:           # a lamp left from before (Blender's default one is 1000 W) would drown a rig sized to the subject
        for lamp in bpy.context.scene.objects:
            if lamp.type == 'LIGHT' and col not in lamp.users_collection and not lamp.hide_render:
                lamp.hide_render = lamp.hide_viewport = True
                switched_off.append(lamp.name)
    bpy.context.view_layer.update()
    return {"style": style, "subject_radius_m": r(radius, 3), "lights": made, "world": world_note,
            **({"other_lights_switched_off": switched_off} if switched_off else {}),
            "collection": RIG_COLLECTION, "note": "Re-running replaces this rig. Check with look(shading='rendered')."}
