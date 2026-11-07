# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Roblox Studio: what a Blender material looks like, in the pieces Roblox can show.

A Principled BSDF is read socket by socket. Each of colour, alpha, roughness, metal, normal and
light given off is a plain value, a picture (found behind the socket, through the nodes people
put in between), or something only shader nodes can make. Roblox shows a surface through a
``SurfaceAppearance`` on a MeshPart: a colour map (whose alpha cuts the surface out), a normal
map, a roughness map, a metalness map and a mask for the light it gives off. So:

* pictures travel as they are, in their own size up to Roblox's 1024 px, once however many
  surfaces use them. A roughness or metal value kept in one channel of a packed picture is taken
  out of it, and a plain value that Roblox's stock materials cannot show (polished or metal)
  travels as a few pixels of that grey;
* what nodes make is baked on a stand-in copy of the object, every channel that varies, onto one
  fresh set of UVs (``Baked``). The same happens when a picture is laid on by anything but the
  UVs (box projection, object coordinates): Roblox only has the UVs;
* colour is kept in linear light the way the shader works and written out as sRGB, whatever the
  picture was stored as (an 8-bit sRGB file, a 32-bit linear one);
* Roblox multiplies the light a surface gives off by its own colour (measured in Studio: a glow
  over a black colour map is black), so where a surface glows its colour map is given the glow's
  colour and the mask its strength. One unit of strength is one unit of light in both programs.

Roblox keeps a picture only as an image asset on the user's account, like a mesh: without the
upload the pictures are shown from memory while the place is edited, and that is all.

A native Part cannot carry a ``SurfaceAppearance``: it gets Roblox's nearest stock material, and
a shape with pictures on it is sent as a mesh instead. Plugins before version 13 know one colour
picture and nothing else: for them ``surface`` answers in that older form.
"""

from __future__ import annotations

import contextlib

import base64
import hashlib
import math
from typing import Any, Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Euler, Matrix

from . import bake_cache
from ._util import ui_context

PICTURE_PX = 1024               # Roblox's limit for a picture; smaller pictures travel in their own size
LEGACY_PX = 512                 # what a plugin before SURFACE_PLUGIN gets: one colour picture inside the mesh's job
BAKE_PX = 1024                  # a look made by shader nodes is baked into pictures of at most this size ...
BAKE_PX_PER_M = 768             # ... by the object's size: this many pixels for each metre of the square root of its surface
BAKE_PX_LEAST = 128
BAKE_TRIANGLES = 150_000        # heavier than this, unwrapping takes too long: it goes as a plain colour
FLAT_PX = 4                     # a plain value as a picture: a few pixels of one grey
BAND_BYTES = 1 << 20              # a picture travels in bands of rows of about this many bytes, one job each
SURFACE_PLUGIN = 13             # the first version of the bridge plugin that builds SurfaceAppearances
SOFT_ALPHA = 0.02               # MeshPart.Transparency from which Roblox blends a colour map's alpha instead of cutting
ROLES = ("color", "normal", "roughness", "metalness", "emissive")
ROLE_WORDS = {"color": "colour", "normal": "normal", "roughness": "roughness", "metalness": "metalness", "emissive": "glow"}
_LUMA = np.array([0.2126, 0.7152, 0.0722])
_PASS_THROUGH = {'HUE_SAT', 'BRIGHTCONTRAST', 'GAMMA', 'CURVE_RGB', 'SEPARATE_COLOR', 'COMBINE_COLOR', 'RGBTOBW'}
_BAKED = ("color", "alpha", "roughness", "metallic", "emission", "normal")       # the order they are baked in
_SOCKETS = {"color": "Base Color", "alpha": "Alpha", "roughness": "Roughness", "metallic": "Metallic"}


def plugin_shows_surfaces(session: Dict[str, Any]) -> bool:
    version = str(session.get("version") or "")
    return version.isdigit() and int(version) >= SURFACE_PLUGIN


class Pictures(dict):
    """The pictures of one send, by what they were made from, and what is known about them.
    ``full`` says the plugin at the other end builds whole surfaces; without it only a colour
    picture travels, the way older plugins take it."""

    def __init__(self, full: bool = False, objects: Optional[List[bpy.types.Object]] = None) -> None:
        super().__init__()
        self.full = full
        self.objects = list(objects or [])
        self.sent = {role: 0 for role in ROLES}     # pictures that travelled, by what they are for
        self.surfaces = 0                           # mesh pieces that got a SurfaceAppearance
        self.notes: set = set()                     # what was approximated, in words for the report
        self._expected: Optional[Dict[str, int]] = None

    def expected(self) -> Dict[str, int]:
        """How many pictures this send will need at most, by what they are for: counted from the
        materials alone, before anything is read or baked, so the user can be asked first."""
        if self._expected is None:
            self._expected = expected_pictures(self.objects) if self.full else {}
        return self._expected

    def in_words(self, counts: Optional[Dict[str, int]] = None) -> str:
        counts = self.sent if counts is None else counts
        return ", ".join(f"{counts[role]} {ROLE_WORDS[role]}" for role in ROLES if counts.get(role))


def srgb(value: float) -> float:
    value = max(0.0, min(1.0, float(value)))
    return round(value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1 / 2.4) - 0.055, 3)


def _encode(linear: np.ndarray) -> np.ndarray:
    linear = np.clip(linear, 0.0, 1.0)
    return np.where(linear <= 0.0031308, linear * 12.92, 1.055 * np.power(linear, 1 / 2.4) - 0.055)


def _decode(encoded: np.ndarray) -> np.ndarray:
    return np.where(encoded <= 0.04045, encoded / 12.92, np.power((encoded + 0.055) / 1.055, 2.4))


def _to_bytes(values: np.ndarray) -> np.ndarray:
    return (np.clip(values, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------------------------------------
# reading the node tree


def _through(socket):
    """(node, output socket) that drives this input, looking through reroutes. None if nothing does."""
    if socket is None or not socket.is_linked:
        return None
    link = socket.links[0]
    for _ in range(16):
        node = link.from_node
        if node.type != 'REROUTE' or not node.inputs[0].is_linked:
            break
        link = node.inputs[0].links[0]
    return link.from_node, link.from_socket


def _image_behind(socket, depth: int = 0):
    """(image node, gain, lift) for the picture that drives a colour socket, looking through the
    nodes people put between an image and the surface: a Mix that tints it, colour adjustments.
    gain and lift are per channel: surface colour = picture * gain + lift. None when no picture
    drives it (a procedural shader)."""
    found = _through(socket) if depth <= 6 else None
    if found is None:
        return None
    node = found[0]
    if node.type == 'TEX_IMAGE':
        return (node, np.ones(3), np.zeros(3)) if node.image is not None else None
    if node.type in {'MIX_RGB', 'MIX'}:
        if node.type == 'MIX' and getattr(node, "data_type", 'RGBA') != 'RGBA':
            return None
        colours = [i for i in node.inputs if i.type == 'RGBA' and i.enabled]
        factor = next((i for i in node.inputs if i.name in ("Fac", "Factor") and i.enabled and i.type == 'VALUE'), None)
        if len(colours) < 2:
            return None
        first, second = colours[0], colours[1]
        amount = 1.0 if factor is None or factor.is_linked else float(factor.default_value)
        for picture, other, picture_is_first in ((first, second, True), (second, first, False)):
            behind = _image_behind(picture, depth + 1)
            if behind is None:
                continue
            image_node, gain, lift = behind
            if other.is_linked:
                return image_node, gain, lift                  # two pictures mixed: carry the first found
            constant = np.array(other.default_value[:3], dtype=np.float64)
            blend = getattr(node, "blend_type", 'MIX')
            if blend == 'MULTIPLY':
                tint = 1.0 - amount + amount * constant
                return image_node, gain * tint, lift * tint
            if blend == 'MIX':
                keep = 1.0 - amount if picture_is_first else amount
                return image_node, gain * keep, lift * keep + constant * (1.0 - keep)
            return image_node, gain, lift                      # another blend mode: the picture, untinted
        return None
    if node.type in _PASS_THROUGH:
        for candidate in node.inputs:
            if candidate.type == 'RGBA' and candidate.is_linked:
                return _image_behind(candidate, depth + 1)
    return None


def _laid_by(image_node) -> Optional[Tuple[str, Optional[np.ndarray], bool]]:
    """How the picture is laid on: ("uv", mapping, False) by the mesh's UVs, or ("object" |
    "generated", mapping, box) projected from the object's own coordinates, flat or from the three
    sides (Box), as models without UVs often are. ``mapping`` is the 4 x 4 a Mapping node in front
    does to the coordinates, or None. None for any other way, which only a bake can carry."""
    box = getattr(image_node, "projection", 'FLAT') == 'BOX'
    if not box and getattr(image_node, "projection", 'FLAT') != 'FLAT':
        return None
    found = _through(image_node.inputs.get("Vector"))
    mapping = None
    if found is not None and found[0].type == 'MAPPING':
        node = found[0]
        parts = [node.inputs[name] for name in ("Location", "Rotation", "Scale")]
        if node.vector_type != 'POINT' or any(part.is_linked for part in parts):
            return None
        shift, turn, scale = (part.default_value for part in parts)
        mapping = np.array(Matrix.Translation(shift) @ Euler(turn, 'XYZ').to_matrix().to_4x4()
                           @ Matrix.Diagonal((*scale, 1.0)), dtype=np.float64)
        found = _through(node.inputs["Vector"])
    if found is None or found[0].type == 'UVMAP' or (found[0].type == 'TEX_COORD' and found[1].name == "UV"):
        return None if box else ("uv", mapping, False)      # nothing plugged in: Blender uses the UVs
    node, out = found
    if node.type == 'TEX_COORD' and out.name in ("Object", "Generated") and getattr(node, "object", None) is None:
        return out.name.lower(), mapping, box
    return None


def _on_uvs(image_node) -> bool:
    """Can this picture travel as a picture? It is laid on by the UVs, or projected in a way the
    sent mesh's UVs can be made to repeat (``projected_uvs``)."""
    return _laid_by(image_node) is not None


def projection(material: Optional[bpy.types.Material]) -> Optional[Tuple[str, np.ndarray, bool]]:
    """("object" | "generated", 4 x 4, box) when the material's pictures are projected from the
    object's coordinates rather than laid on by UVs."""
    shader = _shader(material)
    if shader is None:
        return None
    return _plan(shader)["projection"]


def projected_uvs(mesh: bpy.types.Mesh, materials: List[Optional[bpy.types.Material]],
                  texspace: Tuple[Any, Any]) -> Optional[np.ndarray]:
    """UVs for every corner of the mesh, (loops, 2), in which the faces of a projected material
    have the coordinates Blender projects its pictures by, and the others their own UVs (zero
    without any). ``materials`` are the object's, by slot. Then the pictures go as they are, sharp and repeating, instead of one bake
    stretched over a whole floor. ``texspace`` is the original mesh's (location, size): Generated
    coordinates run 0..1 over it. A Box projection gives each face the side it faces most, flipped
    as Cycles flips it so no side is mirrored; where Blender blends sides over a soft edge, the sent
    mesh has a seam. None when no material of the mesh is projected."""
    laid = {index: projection(material) for index, material in enumerate(materials)}
    laid = {index: how for index, how in laid.items() if how is not None}
    if not laid or not mesh.loops:
        return None
    loops = len(mesh.loops)
    uv = np.zeros((loops, 2), dtype=np.float64)
    layer = next((layer for layer in mesh.uv_layers if layer.active_render), mesh.uv_layers.active)
    if layer is not None:
        flat = np.empty(loops * 2, dtype=np.float32)
        layer.data.foreach_get("uv", flat)
        uv[:] = flat.reshape(-1, 2)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    vertex = np.empty(loops, dtype=np.int32)
    mesh.loops.foreach_get("vertex_index", vertex)
    count = len(mesh.polygons)
    slot, totals = np.empty(count, dtype=np.int32), np.empty(count, dtype=np.int32)
    mesh.polygons.foreach_get("material_index", slot)
    mesh.polygons.foreach_get("loop_total", totals)
    corner_slot = np.repeat(slot, totals)                      # polygons' corners are stored in polygon order
    facing = np.empty(count * 3, dtype=np.float32)
    mesh.polygons.foreach_get("normal", facing)
    corner_facing = np.repeat(facing.reshape(-1, 3), totals, axis=0)
    points = co.reshape(-1, 3).astype(np.float64)[vertex]
    middle, half = (np.array(v, dtype=np.float64) for v in texspace)
    for index, (kind, mapping, box) in laid.items():
        chosen = corner_slot == index
        at = points[chosen]
        if kind == "generated":
            at = (at - (middle - half)) / np.where(np.abs(half) < 1e-9, 1.0, 2.0 * half)
        at = at @ mapping[:3, :3].T + mapping[:3, 3]
        if not box:
            uv[chosen] = at[:, :2]
            continue
        n = corner_facing[chosen]
        side = np.abs(n).argmax(axis=1)
        x, y, z = at[:, 0], at[:, 1], at[:, 2]
        uv[chosen] = np.select([side[:, None] == 0, side[:, None] == 1],          # as Cycles' box mapping reads it
                               [np.stack([np.where(n[:, 0] < 0, 1.0 - y, y), z], axis=1),
                                np.stack([np.where(n[:, 1] > 0, 1.0 - x, x), z], axis=1)],
                               np.stack([np.where(n[:, 2] > 0, 1.0 - y, y), x], axis=1))
    return uv


def texspace(obj: bpy.types.Object, mesh: bpy.types.Mesh) -> Tuple[Any, Any]:
    """The (middle, half size) Generated coordinates run over: the object's own texture space."""
    data = obj.original.data if obj.original is not None else obj.data
    if hasattr(data, "texspace_location"):
        return tuple(data.texspace_location), tuple(data.texspace_size)
    co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
    mesh.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) if len(co) else np.zeros((1, 3), dtype=np.float32)
    low, high = co.min(axis=0), co.max(axis=0)
    return tuple((low + high) / 2.0), tuple((high - low) / 2.0)


def _uv_mapping(image_node) -> Optional[Dict[str, Any]]:
    """What a Mapping node in front of the image does to the UVs (tiling, shift, turn), so the
    same can be done to the UVs that are sent. None when it does nothing, or when the picture is
    projected (``projected_uvs`` lays the Mapping into the UVs)."""
    laid = _laid_by(image_node)
    if laid is None or laid[0] != "uv":
        return None
    found = _through(image_node.inputs.get("Vector"))
    if found is None:
        return None
    node = found[0]
    if node.type != 'MAPPING' or node.vector_type not in {'POINT', 'TEXTURE'}:
        return None
    scale, shift, turn = node.inputs["Scale"], node.inputs["Location"], node.inputs["Rotation"]
    if scale.is_linked or shift.is_linked or turn.is_linked or node.vector_type != 'POINT':
        return None
    out = {"scale": [float(scale.default_value[0]), float(scale.default_value[1])],
           "shift": [float(shift.default_value[0]), float(shift.default_value[1])], "turn": float(turn.default_value[2])}
    if out["scale"] == [1.0, 1.0] and out["shift"] == [0.0, 0.0] and abs(out["turn"]) < 1e-9:
        return None
    return out


def _number_behind(socket, depth: int = 0):
    """("image", image node, channel, steps) for the picture behind a number socket (roughness,
    metal, alpha). ``channel`` is 0-3 or 'luma'; ``steps`` is the arithmetic the nodes in between
    do to it, to be done to the pixels. None when it takes shader nodes to make the number."""
    found = _through(socket) if depth <= 8 else None
    if found is None:
        return None
    node, out = found
    kind = node.type
    if kind == 'TEX_IMAGE':
        if node.image is None or not _on_uvs(node):
            return None
        return "image", node, 3 if out.name == "Alpha" else 'luma', ()
    if kind in {'SEPARATE_COLOR', 'SEPRGB', 'SEPARATE_XYZ'}:       # one channel of a packed picture
        if kind == 'SEPARATE_COLOR' and node.mode != 'RGB':
            return None
        behind = _through(node.inputs[0])
        if behind is None or behind[0].type != 'TEX_IMAGE' or behind[0].image is None or behind[1].name != "Color" \
                or not _on_uvs(behind[0]):
            return None
        index = next((i for i, candidate in enumerate(node.outputs) if candidate == out), 0)
        return "image", behind[0], min(index, 2), ()
    if kind == 'RGBTOBW':
        behind = _through(node.inputs[0])
        if behind is None or behind[0].type != 'TEX_IMAGE' or behind[0].image is None or not _on_uvs(behind[0]):
            return None
        return "image", behind[0], 'luma', ()

    def after(source, *steps):
        behind = _number_behind(source, depth + 1)
        return None if behind is None else behind[:3] + (behind[3] + steps,)

    if kind == 'INVERT':
        amount = node.inputs["Fac"]
        if amount.is_linked or abs(float(amount.default_value) - 1.0) > 1e-6:
            return None
        return after(node.inputs["Color"], ("mul", -1.0), ("add", 1.0))
    if kind == 'MATH':
        a, b = node.inputs[0], node.inputs[1]
        if a.is_linked == b.is_linked:
            return None
        first = a.is_linked
        value = float((b if first else a).default_value)
        steps = {'MULTIPLY': [("mul", value)], 'ADD': [("add", value)], 'MINIMUM': [("min", value)], 'MAXIMUM': [("max", value)],
                 'SUBTRACT': [("add", -value)] if first else [("mul", -1.0), ("add", value)],
                 'DIVIDE': [("mul", 1.0 / value)] if first and abs(value) > 1e-9 else None,
                 'POWER': [("pow", value)] if first else None}.get(node.operation)
        if steps is None:
            return None
        return after(a if first else b, *steps, *([("clamp", 0.0, 1.0)] if node.use_clamp else []))
    if kind == 'MAP_RANGE':
        if getattr(node, "data_type", 'FLOAT') != 'FLOAT' or node.interpolation_type != 'LINEAR' \
                or any(node.inputs[i].is_linked for i in range(1, 5)):
            return None
        low, high, to_low, to_high = (float(node.inputs[i].default_value) for i in range(1, 5))
        if abs(high - low) < 1e-9:
            return None
        slope = (to_high - to_low) / (high - low)
        clamp = [("clamp", min(to_low, to_high), max(to_low, to_high))] if node.clamp else []
        return after(node.inputs[0], ("mul", slope), ("add", to_low - low * slope), *clamp)
    if kind == 'CLAMP':
        if node.inputs[1].is_linked or node.inputs[2].is_linked:
            return None
        return after(node.inputs[0], ("clamp", float(node.inputs[1].default_value), float(node.inputs[2].default_value)))
    if kind == 'VALTORGB':                                         # a colour ramp: looked up, 256 steps
        ramp = node.color_ramp
        table = tuple(round(float(ramp.evaluate(i / 255.0)[3] if out.name == "Alpha"
                                  else np.array(ramp.evaluate(i / 255.0)[:3]) @ _LUMA), 4) for i in range(256))
        return after(node.inputs[0], ("clamp", 0.0, 1.0), ("table", table))
    return None


def _normal_behind(socket):
    """("image", image node, strength) for a tangent-space normal map plugged into Normal."""
    found = _through(socket)
    if found is None or found[0].type != 'NORMAL_MAP' or found[0].space != 'TANGENT':
        return None
    node = found[0]
    strength = node.inputs["Strength"]
    behind = _through(node.inputs["Color"])
    if strength.is_linked or behind is None or behind[0].type != 'TEX_IMAGE' or behind[0].image is None \
            or behind[1].name != "Color" or not _on_uvs(behind[0]):
        return None
    return "image", behind[0], float(strength.default_value)


def _shader(material: Optional[bpy.types.Material]):
    if material is None or not material.use_nodes or not material.node_tree:
        return None
    return next((n for n in material.node_tree.nodes if n.type == 'BSDF_PRINCIPLED'), None)


def _plan(shader) -> Dict[str, Any]:
    """Where each part of the look comes from: ("value", v), ("image", node, ...), ("nodes",) for
    what only shader nodes can make, or None for a part that is not there. ``uv`` is what a
    Mapping node does to the pictures; ``bake`` says the look cannot travel as it is."""

    def number(name: str, default: float):
        socket = shader.inputs.get(name)
        if socket is None:
            return "value", default
        if not socket.is_linked:
            return "value", float(socket.default_value)
        return _number_behind(socket) or ("nodes",)

    def colour(socket, scale: float = 1.0):
        if not socket.is_linked:
            return "value", np.array(socket.default_value[:3], dtype=np.float64) * scale
        found = _image_behind(socket)
        if found is None or not _on_uvs(found[0]):
            return ("nodes",)
        return "image", found[0], found[1] * scale, found[2] * scale

    plan: Dict[str, Any] = {"color": colour(shader.inputs["Base Color"]), "alpha": number("Alpha", 1.0),
                            "roughness": number("Roughness", 0.5), "metallic": number("Metallic", 0.0),
                            "normal": None, "emission": None}
    normal = shader.inputs.get("Normal")
    if normal is not None and normal.is_linked:
        plan["normal"] = _normal_behind(normal) or ("nodes",)
    power, glow = shader.inputs.get("Emission Strength"), shader.inputs.get("Emission Color")
    if power is not None and glow is not None:
        if power.is_linked:
            plan["emission"] = ("nodes",)
        elif float(power.default_value) > 0.0:
            light = colour(glow, float(power.default_value))
            if light[0] != "value" or float(np.max(light[1])) > 1e-4:
                plan["emission"] = light
    glass = shader.inputs.get("Transmission Weight")
    plan["transmission"] = float(glass.default_value) if glass is not None and not glass.is_linked else 0.0
    pictures = [source[1] for source in plan.values() if isinstance(source, tuple) and source[0] == "image"]
    mappings = [_uv_mapping(node) for node in pictures]
    plan["uv"] = mappings[0] if mappings else None
    ways = [_laid_by(node) for node in pictures]
    projected = [way for way in ways if way[0] != "uv"]
    plan["projection"] = (projected[0][0], projected[0][1] if projected[0][1] is not None else np.eye(4),
                          projected[0][2]) if projected else None
    # a sent mesh has one set of UVs: pictures laid on differently cannot all keep their place
    same_way = all(way[0] == ways[0][0] and way[2] == ways[0][2] and _same(way[1], ways[0][1]) for way in ways)
    plan["bake"] = any(isinstance(source, tuple) and source[0] == "nodes" for source in plan.values()) \
        or any(mapping != mappings[0] for mapping in mappings) or not same_way
    return plan


def _same(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> bool:
    return bool(np.allclose(np.eye(4) if a is None else a, np.eye(4) if b is None else b))      # no Mapping node is no change


def _neon(plan: Dict[str, Any]) -> bool:
    """A plain light of its own, strong enough to hide the surface under it: Roblox's Neon."""
    light = plan["emission"]
    return light is not None and light[0] == "value" and float(np.max(light[1])) > 0.5


def made_by_nodes(obj: bpy.types.Object, whole: bool = True) -> bool:
    """Does any of the object's materials need shader nodes for a part of its look (or lay a
    picture on by something other than one set of UVs)? Then the look is baked. Without
    ``whole`` only the colour counts: it is all an older plugin can show."""
    for slot in obj.material_slots:
        shader = _shader(slot.material)
        if shader is None:
            continue
        plan = _plan(shader)
        if (plan["bake"] if whole else plan["color"][0] == "nodes") and not _neon(plan):
            return True
    return False


# ---------------------------------------------------------------------------------------------
# pixels


def _read(image: bpy.types.Image, images: Dict[Any, Any], limit: int = PICTURE_PX) -> Optional[np.ndarray]:
    """The image as (rows, columns, 4) floats, top row first, at most ``limit`` a side: colour in
    linear light, as the shader gets it, and data as it is stored. The last few are remembered,
    because one picture often feeds several maps."""
    recent = images.setdefault(("recent",), [])
    for name, size, pixels in recent:
        if name == image.name and size == limit:
            return pixels
    width, height = image.size
    if not width or not height:
        return None
    pixels = np.empty(width * height * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    pixels = pixels.reshape(height, width, 4)[::-1]            # Blender stores rows bottom-up
    step = max(1, math.ceil(max(width, height) / limit))
    if step > 1:                                               # average each block: a shrunk picture, not a sieve
        rows, columns = (height // step) * step, (width // step) * step
        pixels = pixels[:rows, :columns].reshape(rows // step, step, columns // step, step, 4).mean(axis=(1, 3))
    pixels = np.maximum(pixels, 0.0)
    space = image.colorspace_settings.name
    stored_linear = image.is_float or space in {'Non-Color', 'Raw', 'ACEScg', 'XYZ'} or space.startswith("Linear")
    if not stored_linear:
        pixels = pixels.copy()
        pixels[..., :3] = _decode(np.minimum(pixels[..., :3], 1.0))
    recent.append((image.name, limit, pixels))
    del recent[:-3]
    return pixels


def _fit(values: np.ndarray, rows: int, columns: int) -> np.ndarray:
    """The array at another size (nearest pixel): maps of one surface have to line up."""
    if values.shape[0] == rows and values.shape[1] == columns:
        return values
    if values.shape[0] == 1 and values.shape[1] == 1:
        return values                                          # one value: it fits any size as it is
    row = np.arange(rows) * values.shape[0] // rows
    column = np.arange(columns) * values.shape[1] // columns
    return values[row][:, column]


def _steps(values: np.ndarray, steps: Tuple) -> np.ndarray:
    for step in steps:
        name = step[0]
        if name == "mul":
            values = values * step[1]
        elif name == "add":
            values = values + step[1]
        elif name == "pow":
            values = np.power(np.maximum(values, 0.0), step[1])
        elif name == "min":
            values = np.minimum(values, step[1])
        elif name == "max":
            values = np.maximum(values, step[1])
        elif name == "clamp":
            values = np.clip(values, step[1], step[2])
        elif name == "table":
            values = np.interp(values, np.linspace(0.0, 1.0, len(step[1])), np.array(step[1]))
    return values


def _picture(name: str, pixels: np.ndarray) -> Dict[str, Any]:
    """A picture ready to travel: bytes, (rows, columns) for one grey channel or (rows, columns, 4).
    The key names the pixels, not just the image: a repainted or re-tinted image is a new picture
    in Roblox, and the same pixels under two names are one."""
    pixels = np.ascontiguousarray(pixels)
    channels = 1 if pixels.ndim == 2 else int(pixels.shape[2])
    if channels == 1 and int(pixels.min()) == int(pixels.max()):          # one grey all over: a few pixels say it
        value = int(pixels.flat[0])
        return {"key": f"flat:{value}", "w": FLAT_PX, "h": FLAT_PX, "c": 1, "flat": True,
                "pixels": np.full((FLAT_PX, FLAT_PX), value, dtype=np.uint8)}
    return {"key": f"{name}:{hashlib.sha1(pixels.tobytes()).hexdigest()[:10]}", "w": int(pixels.shape[1]),
            "h": int(pixels.shape[0]), "c": channels, "pixels": pixels}


def _flat(value: float) -> Dict[str, Any]:
    return _picture("flat", np.full((1, 1), int(_to_bytes(np.array(value))), dtype=np.uint8))


def _number(source, images: Dict[Any, Any], limit: int = PICTURE_PX) -> Optional[np.ndarray]:
    """(rows, columns) floats for a number that is a value or comes from a picture."""
    if source[0] == "value":
        return np.full((1, 1), float(source[1]), dtype=np.float32)
    _kind, node, channel, steps = source
    pixels = _read(node.image, images, limit)
    if pixels is None:
        return None
    values = pixels[..., :3] @ _LUMA.astype(np.float32) if channel == 'luma' else pixels[..., channel]
    return _steps(values, steps)


def _colour(source, images: Dict[Any, Any], limit: int = PICTURE_PX) -> Optional[np.ndarray]:
    """(rows, columns, 3) floats in linear light for a colour that is a value or comes from a picture."""
    if source[0] == "value":
        return np.array(source[1], dtype=np.float32).reshape(1, 1, 3)
    _kind, node, gain, lift = source
    pixels = _read(node.image, images, limit)
    if pixels is None:
        return None
    return pixels[..., :3] * gain.astype(np.float32) + lift.astype(np.float32)


def _glowing(base: np.ndarray, light: np.ndarray) -> Tuple[np.ndarray, np.ndarray, float]:
    """(colour, mask, strength) that make Roblox give off ``light`` from a surface of colour ``base``.
    Roblox gives off strength x mask x the surface's own colour. Where the light is strong the
    colour becomes the light's own (nothing else of the surface is seen there anyway); where it
    is weak the colour stays and the mask is set so that as much light leaves."""
    peak = light.max(axis=2, keepdims=True)
    weight = np.clip(peak, 0.0, 1.0)
    hue = light / np.maximum(peak, 1e-6)
    colour = np.clip(base, 0.0, 1.0) * (1.0 - weight) + hue * weight
    leaving = (peak[..., 0] * (hue @ _LUMA.astype(np.float32))) / np.maximum(colour @ _LUMA.astype(np.float32), 1e-4)
    leaving = np.where(peak[..., 0] > 1e-5, leaving, 0.0)
    strength = float(np.percentile(leaving, 99.9)) if leaving.size > 1 else float(leaving.max())
    strength = max(strength, 1e-3)
    return colour, np.clip(leaving / strength, 0.0, 1.0), strength


def _texture(image: bpy.types.Image, images: Dict[Any, Any], gain: Optional[np.ndarray] = None,
             lift: Optional[np.ndarray] = None) -> Optional[Dict[str, Any]]:
    """The older form: the image as RGBA bytes of at most LEGACY_PX a side, inside the mesh's own
    job. ``gain`` and ``lift`` are what the material does to the picture between the image and
    the surface (a tint): put into the pixels, in linear light, as the shader would."""
    pixels = _read(image, images, LEGACY_PX)
    if pixels is None:
        return None
    colour = pixels[..., :3]
    if gain is not None or lift is not None:
        colour = colour * (gain if gain is not None else 1.0) + (lift if lift is not None else 0.0)
    data = _to_bytes(np.concatenate([_encode(colour), pixels[..., 3:]], axis=2)).tobytes()
    return {"key": f"{image.name}:{hashlib.sha1(data).hexdigest()[:10]}", "w": int(pixels.shape[1]), "h": int(pixels.shape[0]),
            "data": base64.b64encode(data).decode("ascii")}


# ---------------------------------------------------------------------------------------------
# one material


def _stock(roughness: float, metallic: float) -> Dict[str, Any]:
    """Roblox's own material that is nearest to these two numbers."""
    if metallic > 0.5:
        return {"material": "Metal"}
    if roughness < 0.3:
        return {"material": "SmoothPlastic", "reflectance": round((0.3 - roughness) * 0.5, 3)}
    return {}


def surface(material: Optional[bpy.types.Material], images: Dict[Any, Any], part: bool = False) -> Dict[str, Any]:
    """What a mesh piece (or, with ``part``, a native Part) of this material is given: colour,
    Roblox material, transparency, and under ``_look`` the pictures and settings of its
    SurfaceAppearance. ``_pictured`` is set when the look needs pictures (a Part cannot carry
    them), ``_procedural`` when it takes shader nodes that were not baked: only plain values can
    be carried then. With a plain dict for ``images`` (or a plugin that knows no surfaces) the
    answer is the older form: colour, material and at most one colour ``texture``."""
    full = bool(getattr(images, "full", False))
    remembered = ("surface", material.name, part) if material is not None and not material.get("unix_baked") else None
    if remembered is not None and remembered in images:
        return dict(images[remembered])
    out = _surface(material, images, full, part)
    if remembered is not None:
        images[remembered] = out
    return dict(out)


def _surface(material: Optional[bpy.types.Material], images: Dict[Any, Any], full: bool, part: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"color": [0.64, 0.64, 0.64], "material": "Plastic", "transparency": 0.0, "reflectance": 0.0}
    if material is None:
        return out
    shader = _shader(material)
    if shader is None:
        out["color"] = [srgb(c) for c in material.diffuse_color[:3]]
        return out
    plan = _plan(shader)

    def value(name: str, default: float) -> float:
        return float(plan[name][1]) if plan[name][0] == "value" else default

    base = shader.inputs["Base Color"]
    out["color"] = [srgb(c) for c in base.default_value[:3]]
    roughness, metallic, alpha = value("roughness", 0.5), value("metallic", 0.0), value("alpha", 1.0)
    neon, glass = _neon(plan), plan["transmission"] > 0.5
    if neon:                                # Neon shines by itself: it is given the light's colour at full value
        out.update(material="Neon", color=[srgb(c) for c in plan["emission"][1] / float(np.max(plan["emission"][1]))])
    elif glass:
        out.update(material="Glass", transparency=0.5)
    else:
        out.update(_stock(roughness, metallic))
    if alpha < 0.999:
        out["transparency"] = round(1.0 - alpha, 3)
    if plan["color"][0] == "nodes" and tuple(material.diffuse_color[:3]) != (0.8, 0.8, 0.8):
        out["color"] = [srgb(c) for c in material.diffuse_color[:3]]      # the colour the user sees in the viewport
    if neon:
        return out                          # the light hides whatever else the surface is
    sources = [plan[name] for name in ("color", "alpha", "roughness", "metallic", "normal", "emission")]
    pictured = any(source is not None and source[0] != "value" for source in sources)
    if plan["bake"] if full else plan["color"][0] == "nodes":
        out["_procedural"] = material.name  # not baked (too heavy, or the bake failed): the plain values are all that travels
        return out
    if part or not full:
        if part and (pictured if full else plan["color"][0] == "image"):
            out["_pictured"] = True         # a Part cannot carry pictures: this shape has to go as a mesh
        if part or plan["color"][0] != "image":
            return out
        _kind, node, gain, lift = plan["color"]                 # the older form: one colour picture
        plain = np.allclose(gain, 1.0) and np.allclose(lift, 0.0)
        name = node.image.name + ("" if plain else "|" + ",".join(f"{v:.3f}" for v in (*gain, *lift)))
        if name not in images:
            try:
                images[name] = _texture(node.image, images, None if plain else gain, None if plain else lift)
            except Exception:
                images[name] = None
        if images[name]:
            out["texture"] = images[name]
            if plan["uv"]:
                out["_uv"] = plan["uv"]
        else:
            out["_unreadable"] = node.image.name
        return out
    try:
        look = _look(material, plan, out, images, pictured, glass)
    except Exception as exc:                # an image that cannot be read: the plain values travel
        print(f"[Unix] could not read the pictures of {material.name}: {exc!r}")
        look = None
    if look is None and pictured:
        out["_unreadable"] = material.name
    elif look:
        out["_look"] = look
        if plan["uv"]:
            out["_uv"] = plan["uv"]
    return out


def _look(material: bpy.types.Material, plan: Dict[str, Any], out: Dict[str, Any], images: Dict[Any, Any],
          pictured: bool, glass: bool) -> Optional[Dict[str, Any]]:
    """The SurfaceAppearance for a material whose look is values and pictures: {"maps": {role:
    picture}, ...}, an empty dict when Roblox's stock material shows it well enough, None when a
    picture could not be read. Adjusts ``out`` (colour, transparency) to go with it."""
    roughness, metallic = plan["roughness"], plan["metallic"]
    polished = roughness[0] == "value" and metallic[0] == "value" and (metallic[1] >= 0.5 or roughness[1] < 0.3)
    if not pictured and (glass or not polished) and plan["emission"] is None:
        return {}
    name = material.get("unix_named") or material.name
    maps: Dict[str, Dict[str, Any]] = {}
    look: Dict[str, Any] = {"maps": maps}
    notes = getattr(images, "notes", set())

    # colour, with what cuts it out in its alpha, and with the light's colour where it glows
    colour, cut, light = plan["color"], plan["alpha"], plan["emission"]
    tint = None
    if colour[0] == "image" and light is None and np.allclose(colour[3], 0.0) and float(np.max(colour[2])) <= 1.0 \
            and not np.allclose(colour[2], 1.0):
        tint, colour = colour[2], colour[:2] + (np.ones(3), np.zeros(3))      # a tint Roblox can apply itself: the picture stays shareable
    shade = _colour(colour, images)
    hole = _number(cut, images) if cut[0] == "image" else None
    glow = _colour(light, images) if light is not None else None
    if shade is None or (cut[0] == "image" and hole is None) or (light is not None and glow is None):
        return None
    if hole is not None and float(hole.min()) > 0.999:
        hole = None                                                           # an alpha that cuts nothing out
    sizes = [layer.shape[:2] for layer in (shade, hole, glow) if layer is not None]
    rows, columns = max(size[0] for size in sizes), max(size[1] for size in sizes)
    if glow is not None:
        shade, glow = np.broadcast_arrays(_fit(shade, rows, columns), _fit(glow, rows, columns))
        shade, mask, strength = _glowing(shade, glow)
        maps["emissive"] = _picture(name + "_glow", _to_bytes(mask))
        look["emissive_strength"] = round(strength, 3)
        notes.add("Light given off: Roblox multiplies it by the surface's own colour, so where a surface glows its colour "
                  "map carries the glow's colour. Strong glows match; a weak glow over a differently coloured surface "
                  "comes out paler.")
    if rows * columns > 1:
        rgb = np.broadcast_to(_encode(_fit(shade, rows, columns)), (rows, columns, 3))
        opacity = np.broadcast_to(_fit(hole, rows, columns), (rows, columns)) if hole is not None \
            else np.ones((rows, columns), dtype=np.float32)
        source = colour[1].image.name if colour[0] == "image" else name + "_colour"
        maps["color"] = _picture(source, _to_bytes(np.concatenate([rgb, opacity[..., None]], axis=2)))
        if tint is not None:
            look["tint"] = [srgb(c) for c in tint]
    else:
        out["color"] = [srgb(c) for c in shade.reshape(3)]
    if hole is not None:
        look["alpha"] = "Transparency"
        between = float(((hole > 0.1) & (hole < 0.9)).mean())
        if between > 0.02:                  # soft edges or a see-through film: blended. Otherwise cut out, which sorts and shadows right
            out["transparency"] = max(out["transparency"], SOFT_ALPHA)
    if out["transparency"] > 0.0 and maps:
        look.setdefault("alpha", "Transparency")

    for role, source in (("roughness", roughness), ("metalness", metallic)):
        if source[0] == "value":
            if role == "roughness" or float(source[1]) >= 0.02:               # without a map Roblox takes no metal, and a matt surface
                maps[role] = _flat(float(source[1]))
            continue
        values = _number(source, images)
        if values is None:
            return None
        maps[role] = _picture(source[1].image.name, _to_bytes(values))
    bumps = plan["normal"]
    if bumps is not None:
        pixels = _read(bumps[1].image, images)
        if pixels is None:
            return None
        normal = pixels[..., :3] * 2.0 - 1.0
        if abs(bumps[2] - 1.0) > 1e-3:      # the Normal Map node's strength: between the flat surface and the picture
            flat = np.array([0.0, 0.0, 1.0], dtype=np.float32)
            normal = flat + (normal - flat) * bumps[2]
            normal[..., 2] = np.maximum(normal[..., 2], 0.05)
        normal = normal / np.maximum(np.linalg.norm(normal, axis=2, keepdims=True), 1e-6)
        packed = np.concatenate([normal * 0.5 + 0.5, np.ones(normal.shape[:2] + (1,), dtype=np.float32)], axis=2)
        maps["normal"] = _picture(bumps[1].image.name, _to_bytes(packed))
    if glass and maps:
        notes.add("Glass with pictures on it: Roblox shows the pictures at half transparency, without Glass's bending of light.")
    return look


# ---------------------------------------------------------------------------------------------
# on the way out


def without_uvs(surface: Dict[str, Any]) -> None:
    """A mesh without UVs cannot have a picture laid on it: only what is the same all over stays."""
    surface.pop("texture", None)
    look = surface.get("_look")
    if look:
        maps = {role: picture for role, picture in look["maps"].items() if picture.get("flat")}
        if maps:
            surface["_look"] = dict(look, maps=maps)
        else:
            del surface["_look"]


def bands(picture: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The jobs that carry one picture: bands of rows, so no job is more than about a megabyte."""
    pixels = picture["pixels"]
    width, height, channels = picture["w"], picture["h"], picture["c"]
    rows = max(1, BAND_BYTES // (width * channels))
    return [{"kind": "picture", "key": picture["key"], "w": width, "h": height, "c": channels, "y": top,
             "rows": int(min(rows, height - top)),
             "data": base64.b64encode(pixels[top:top + rows].tobytes()).decode("ascii")} for top in range(0, height, rows)]


def carry(piece: Dict[str, Any], images: Dict[Any, Any], sent: set, queue: List[Dict[str, Any]]) -> None:
    """Give a mesh piece its surface for the wire: the pictures it is the first to use go into the
    queue of jobs ahead of it, and it names them. A picture travels once per send."""
    look = piece.pop("_look", None)
    if not look or not look["maps"]:
        return
    named, first = {}, 0
    for role, picture in look["maps"].items():
        key = picture["key"]
        if key not in sent:
            sent.add(key)
            queue.extend(bands(picture))
            images.sent[role] += 1
            first += 1
        picture["pixels"] = None            # they are on their way: the next user only needs the name
        named[role] = key
    surface = {key: value for key, value in look.items() if key != "maps"}
    surface["maps"] = named
    if first:
        surface["first"] = first            # pictures Studio has to take in (and upload) for this piece
    piece["surface"] = surface
    images.surfaces += 1


def expected_pictures(objects: List[bpy.types.Object]) -> Dict[str, int]:
    """At most how many pictures these objects need in Roblox, by what they are for. Counted from
    the node trees alone: nothing is read or baked, so it is quick enough to ask the user first.
    Pictures that turn out to hold the same pixels, and shapes that become native Parts, make
    the real number smaller."""
    seen: set = set()
    plans: Dict[str, Any] = {}
    for obj in objects:
        try:
            slots = [slot.material for slot in obj.material_slots]
            baked = made_by_nodes(obj)
        except ReferenceError:
            continue
        for material in slots:
            shader = _shader(material)
            if shader is None:
                continue
            if material.name not in plans:
                plans[material.name] = _plan(shader)
            plan = plans[material.name]
            if _neon(plan):
                continue
            varies = {name: plan[name] is not None and plan[name][0] != "value"
                      for name in ("color", "alpha", "roughness", "metallic", "normal", "emission")}
            roughness, metallic = plan["roughness"], plan["metallic"]
            polished = roughness[0] == "value" and metallic[0] == "value" and (metallic[1] >= 0.5 or roughness[1] < 0.3)
            if not any(varies.values()) and plan["emission"] is None and (plan["transmission"] > 0.5 or not polished):
                continue
            owner = obj.name if baked else material.name       # a bake is the object's own; pictures belong to the material

            def source(name: str) -> str:
                node = plan[name][1] if plan[name][0] == "image" and not baked else None
                return node.image.name if node is not None else owner
            if varies["color"] or varies["alpha"] or varies["emission"]:
                seen.add(("color", source("color") if varies["color"] else owner, owner if plan["emission"] is not None else ""))
            if plan["emission"] is not None:
                seen.add(("emissive", owner))
            if varies["normal"]:
                seen.add(("normal", source("normal")))
            for role, name in (("roughness", "roughness"), ("metalness", "metallic")):
                if varies[name]:
                    seen.add((role, source(name), str(plan[name][2:]) if not baked else ""))
                elif role == "roughness" or float(plan[name][1]) >= 0.02:
                    seen.add((role, "flat", int(_to_bytes(np.array(float(plan[name][1]))))))
    counts = {role: 0 for role in ROLES}
    for entry in seen:
        counts[entry[0]] += 1
    return counts


# ---------------------------------------------------------------------------------------------
# baking what nodes make


def bake_px(obj: bpy.types.Object, mesh: bpy.types.Mesh) -> int:
    """The size of the pictures an object's look is baked into: a power of two that gives a small
    prop a small picture and anything from about two metres of surface on the full 1024 px."""
    area = sum(polygon.area for polygon in mesh.polygons) * abs(obj.matrix_world.to_3x3().determinant()) ** (2.0 / 3.0)
    wanted = math.sqrt(max(area, 0.0)) * BAKE_PX_PER_M
    return int(min(BAKE_PX, max(BAKE_PX_LEAST, 2 ** round(math.log2(max(wanted, 1.0))))))


@contextlib.contextmanager
def _showing(scene: bpy.types.Scene):
    """The window shows ``scene`` for the time of the block, so operators work on it, then the user's
    scene again. In background mode there is no window: the scene is put in the context instead."""
    window = bpy.context.window
    if window is None:
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]):
            yield
        return
    shown = window.scene
    window.scene = scene
    try:
        yield
    finally:
        window.scene = shown


class Baked:
    """A stand-in for an object whose look is made by shader nodes: the same shape with its
    modifiers applied, unwrapped afresh, and every part of the look that varies over the surface
    (colour, alpha, roughness, metal, the light it gives off, its bumps as a normal map) baked to
    a picture its materials now read. It exists only while the object is converted; ``remove``
    takes every trace of it away."""

    def __init__(self, obj: bpy.types.Object, size: Optional[int] = None) -> None:
        depsgraph = bpy.context.evaluated_depsgraph_get()
        # The bake happens in a scene of its own that holds only the stand-in. Baked in the user's scene,
        # Cycles loaded every object and picture of the whole map for each part of each object's look: on a
        # 300-object map that was 15-70 s per object, and a send to Roblox ran for most of an hour.
        self.stage = bpy.data.scenes.new("_unix_bake")
        scene, layer = self.stage, self.stage.view_layers[0]
        self.mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                                    depsgraph=depsgraph)
        size = size or bake_px(obj, self.mesh)
        self.object = bpy.data.objects.new("_unix_bake", self.mesh)
        self.images: Dict[str, bpy.types.Image] = {}
        self.materials: List[bpy.types.Material] = []
        self.baked: List[str] = []
        self.ok = False
        scene.collection.objects.link(self.object)
        self.object.matrix_world = obj.matrix_world.copy()
        if scene.world is None and bpy.context.scene.world is not None:
            scene.world = bpy.context.scene.world
        self.from_cache = False
        if not self.mesh.polygons or len(self.mesh.polygons) > BAKE_TRIANGLES:
            return
        key = bake_cache.digest(obj, self.mesh, size)       # read from the user's own materials, before the copies
        wired = []
        for index, material in enumerate(self.mesh.materials):
            copy = material.copy() if material is not None else bpy.data.materials.new("_unix_bake")
            copy.use_nodes = True
            copy["unix_baked"] = True
            self.materials.append(copy)
            self.mesh.materials[index] = copy
            nodes, links = copy.node_tree.nodes, copy.node_tree.links
            shader = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
            output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None) \
                or nodes.new("ShaderNodeOutputMaterial")
            if shader is None:              # no Principled shader: its viewport colour is its look
                shader = nodes.new("ShaderNodeBsdfPrincipled")
                shader.inputs["Base Color"].default_value = material.diffuse_color if material is not None else (0.8, 0.8, 0.8, 1.0)
                links.new(shader.outputs["BSDF"], output.inputs["Surface"])
            # one part of the look at a time, as light: a bake of that is the value itself, whatever the lighting
            glow = nodes.new("ShaderNodeEmission")
            target = nodes.new("ShaderNodeTexImage")
            for node in nodes:
                node.select = False
            target.select = True
            nodes.active = target
            surface_link = output.inputs["Surface"].links[0].from_socket if output.inputs["Surface"].is_linked else None
            wired.append((copy, shader, output, glow, target, surface_link, _plan(shader)))
        if not wired:
            return
        # what varies over the surface of any of its materials is baked for all of them: one picture per part of the look
        wanted = [name for name in _BAKED
                  if any(plan[name] is not None and plan[name][0] != "value" for *_rest, plan in wired)]
        if not wanted:
            wanted = ["color"]
        shown = self.mesh.uv_layers.active.name if self.mesh.uv_layers.active is not None else None
        bake_layer = self.mesh.uv_layers.new(name="UnixBake", do_init=False)
        if shown is not None:
            self.mesh.uv_layers[shown].active_render = True       # the nodes keep reading the UVs they were made for
        self.mesh.uv_layers.active = bake_layer                   # ... and the pictures are laid out on a fresh one
        earlier = bake_cache.load(key)                            # baked for an earlier send, from exactly this
        if earlier is not None and (set(earlier["pictures"]) != set(wanted) or len(earlier["uv"]) != 2 * len(self.mesh.loops)
                                    or any(p.shape[:2] != (size, size) for p in earlier["pictures"].values())):
            earlier = None
        selected, active = [o for o in layer.objects if o.select_get()], layer.objects.active
        engine, bake = scene.render.engine, scene.render.bake
        kept = (bake.margin, bake.use_selected_to_active, bake.target, bake.normal_space, bake.normal_r, bake.normal_g,
                bake.normal_b)
        cycles = getattr(scene, "cycles", None)
        samples = (cycles.samples, cycles.device) if cycles is not None else None
        try:
            layer.update()                      # the stand-in was just linked: the layer knows it only after this
            for other in selected:
                other.select_set(False, view_layer=layer)
            self.object.select_set(True, view_layer=layer)
            layer.objects.active = self.object
            layer.update()
            scene.render.engine = 'CYCLES'
            if scene.cycles is not None:
                scene.cycles.samples, scene.cycles.device = 1, 'CPU'
            bake.margin, bake.use_selected_to_active, bake.target = max(4, size // 85), False, 'IMAGE_TEXTURES'
            bake.normal_space, bake.normal_r, bake.normal_g, bake.normal_b = 'TANGENT', 'POS_X', 'POS_Y', 'POS_Z'
            if earlier is not None:
                bake_layer.data.foreach_set("uv", earlier["uv"])
                for name in wanted:
                    picture = bpy.data.images.new(f"Baked_{obj.name}_{name}", size, size, alpha=False, float_buffer=True,
                                                  is_data=True)
                    picture.pixels.foreach_set(bake_cache.spread(earlier["pictures"][name]).ravel())
                    self.images[name] = picture
                self.from_cache = True
            else:
                self._bake(obj, scene, wired, wanted, size)
                uv = np.empty(len(self.mesh.loops) * 2, dtype=np.float32)
                # looked up again: leaving Edit Mode rebuilt the mesh's layers, and the one held before is freed memory
                self.mesh.uv_layers["UnixBake"].data.foreach_get("uv", uv)
                bake_cache.save(key, uv, {name: bake_cache.kept_pixels(self.images[name], name) for name in wanted})
            for copy, shader, output, glow, target, surface_link, _plan_of in wired:      # from here the material is: these pictures, on these UVs
                nodes, links = copy.node_tree.nodes, copy.node_tree.links
                for name in wanted:
                    reader = nodes.new("ShaderNodeTexImage")
                    reader.image = self.images[name]
                    if name == "normal":
                        bumps = nodes.new("ShaderNodeNormalMap")
                        links.new(reader.outputs["Color"], bumps.inputs["Color"])
                        links.new(bumps.outputs["Normal"], shader.inputs["Normal"])
                    elif name == "emission":
                        power = shader.inputs["Emission Strength"]
                        for link in list(power.links):
                            links.remove(link)
                        power.default_value = 1.0
                        links.new(reader.outputs["Color"], shader.inputs["Emission Color"])
                    else:
                        links.new(reader.outputs["Color"], shader.inputs[_SOCKETS[name]])
                links.new(shader.outputs["BSDF"], output.inputs["Surface"])
            self.baked = wanted
            self.ok = True
        except Exception as exc:            # no Cycles in this build, an operator refused: the object goes as a plain colour
            print(f"[Unix] could not bake {obj.name}: {exc!r}")
        finally:
            if self.object.mode != 'OBJECT':
                try:
                    with ui_context(), _showing(scene):
                        bpy.ops.object.mode_set(mode='OBJECT')
                except Exception:
                    pass
            scene.render.engine = engine
            (bake.margin, bake.use_selected_to_active, bake.target, bake.normal_space, bake.normal_r, bake.normal_g,
             bake.normal_b) = kept
            if samples is not None:
                scene.cycles.samples, scene.cycles.device = samples
            self.object.select_set(False, view_layer=layer)
            for other in selected:
                try:
                    other.select_set(True, view_layer=layer)
                except ReferenceError:
                    pass
            layer.objects.active = active
            layer.update()

    def _bake(self, obj: bpy.types.Object, scene: bpy.types.Scene, wired: list, wanted: List[str], size: int) -> None:
        """Unwrap the stand-in afresh and bake each wanted part of its look into a picture of its own."""
        with ui_context(), _showing(scene):
            bpy.ops.object.mode_set(mode='EDIT')
            bpy.ops.mesh.select_all(action='SELECT')
            bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.02)
            bpy.ops.object.mode_set(mode='OBJECT')
            for name in wanted:
                picture = bpy.data.images.new(f"Baked_{obj.name}_{name}", size, size, alpha=False, float_buffer=True,
                                              is_data=True)
                self.images[name] = picture
                for copy, shader, output, glow, target, surface_link, _plan_of in wired:
                    links = copy.node_tree.links
                    target.image = picture
                    if name == "normal":            # the surface itself: its bumps are read off the real shader
                        if surface_link is not None:
                            links.new(surface_link, output.inputs["Surface"])
                        else:
                            links.new(shader.outputs["BSDF"], output.inputs["Surface"])
                        continue
                    for socket in (glow.inputs["Color"], glow.inputs["Strength"]):
                        for link in list(socket.links):
                            links.remove(link)
                    glow.inputs["Strength"].default_value = 1.0
                    if name == "emission":
                        colour, power = shader.inputs.get("Emission Color"), shader.inputs.get("Emission Strength")
                        self._feed(links, colour, glow.inputs["Color"])
                        self._feed(links, power, glow.inputs["Strength"])
                    else:
                        self._feed(links, shader.inputs.get(_SOCKETS[name]), glow.inputs["Color"])
                    links.new(glow.outputs["Emission"], output.inputs["Surface"])
                bpy.ops.object.bake(type='NORMAL' if name == "normal" else 'EMIT', use_clear=True)

    @staticmethod
    def _feed(links, socket, into) -> None:
        """Plug what drives ``socket`` (or its plain value) into an input of the baking light."""
        if socket is None:
            return
        if socket.is_linked:
            links.new(socket.links[0].from_socket, into)
        elif into.type == 'RGBA':
            value = socket.default_value
            into.default_value = tuple(value) if hasattr(value, "__len__") else (value, value, value, 1.0)
        else:
            into.default_value = float(socket.default_value)

    def remove(self) -> None:
        bpy.data.objects.remove(self.object, do_unlink=True)
        bpy.data.scenes.remove(self.stage)
        bpy.data.meshes.remove(self.mesh)
        for material in self.materials:
            bpy.data.materials.remove(material)
        for picture in self.images.values():
            bpy.data.images.remove(picture)
        bpy.context.view_layer.update()
