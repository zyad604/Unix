# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Unreal Editor: send a scene into the level that is open, over Unreal's Python remote execution.

Blender does the exporting and Unreal does the building (``bridge/unreal_build.py``, run inside
the editor over the link in ``bridge/unreal_link.py``):

* each distinct mesh is exported once, as an FBX at its own origin with its modifiers applied:
  objects that share mesh data, and every copy a collection instance makes, use the same file.
  Its material slots are renamed ``UnixSlot0..n`` in the file, so Unreal's slots can be found
  by name whatever Unreal does to the order;
* every object, light and camera gets its world transform in Unreal's space (below);
* each Blender material becomes numbers and pictures for one master material that Unix owns:
  base colour, opacity, roughness and metal, normal, light given off. The pictures are read the
  way ``roblox_surface`` reads them (through tints, packed channels, Math and Map Range) and a
  look made by shader nodes is baked into pictures first (``roblox_surface.Baked``);
* lights become Unreal lights, the active camera a CameraActor. Light keeps Blender's numbers
  times LIGHT_SCALE: a sun of strength S gives S x LIGHT_SCALE lux, a point or spot of P watts
  P / 4 pi candela times it, an area light P / pi, a glow of strength E an emissive E, all times
  it, and the view's exposure is folded in. LIGHT_SCALE = 1.2 / 0.18 makes a level at Unreal's
  fixed exposure (EV100 0, scene x 0.18 / 1.2: what a level without auto exposure gets, as VR
  projects mostly are) show what Blender shows at exposure 0 (scene x 1). Measured on a sent
  interior: Unreal's raw light matched Blender's to 1%, and its picture matched at 6 to 8 times
  the light. A level with auto exposure adapts to either. The physical 683 lumens to the watt
  were tried first: they burnt a default level out to white;
* an object that casts no shadow in Blender (Object > Visibility > Shadow off: lamp globes,
  shades, glasses) casts none in Unreal, or the globe around a bulb would put the room in its
  shadow.

The work is done a piece at a time (``Sender.step``), so Blender stays responsive; while the
editor builds, Blender only waits for the link on a thread.

Blender is right-handed, Z up, in metres; Unreal is left-handed, Z up, in centimetres. Unreal's
FBX import mirrors Y, so a point goes (x, y, z) -> (x, -y, z) x 100, and an object's rotation R
becomes F R F with F = diag(1, -1, 1). Blender's lights and cameras look down their -Z with +Y
up, Unreal's down +X with +Z up, so for them the rotation is F R A, A taking Unreal's local axes
to Blender's (X -> -Z, Y -> X, Z -> Y).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import struct
import threading
import time
import zlib
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import bpy
import numpy as np
from mathutils import Matrix

from ..bridge import unreal_link
from . import _util, roblox_surface as rs
from .registry import ToolError

PICTURE_PX = 2048               # pictures travel in their own size up to this
CM = 100.0
LIGHT_SCALE = 1.2 / 0.18        # Unreal light per unit of Blender light (see the module text)
LEAST_LUX = 0.01                # a point or spot light reaches as far as it gives this much Blender light ...
REACH_M = (2.0, 100.0)          # ... within these bounds, unless Blender sets its own distance
SPOT_MAX = 80.0                 # Unreal's widest spot cone (half angle, degrees)
SLICE_SECONDS = 0.05
# Adding, re-pointing or removing any datablock makes Blender rebuild the whole scene's dependency graph at its
# next look: on a 8,000-object city 1.6 to 2 s, twice for each mesh, and a send of its 1,221 meshes ran for an
# hour. The meshes are now exported from a private scene this long back to back, and what they leave behind
# goes in one removal: one rebuild a batch.
BATCH_SECONDS = 3.0
PNG_LEVEL = 1                   # zlib effort: Unreal unpacks the file once, so speed beats size
WRITERS = max(2, min(8, (os.cpu_count() or 4) - 1))     # pictures are compressed on these threads (zlib frees the GIL)
LINK_SECONDS = 3600.0           # how long the editor may take to build one send
_FLIP = np.diag([1.0, -1.0, 1.0])
_AIM = np.array([[0.0, 1.0, 0.0], [0.0, 0.0, 1.0], [-1.0, 0.0, 0.0]])      # columns: Unreal's X, Y, Z in Blender's light axes
_SHAPES = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
BUILDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge", "unreal_build.py")


# -- Unreal's space --------------------------------------------------------------------

def to_unreal(matrix, unit: float = 1.0, aim: bool = False) -> Dict[str, List[float]]:
    """A Blender world matrix as Unreal's location (cm), rotation (quaternion x, y, z, w) and scale.
    ``aim`` is for lights and cameras, which look down another axis in each program."""
    m = np.array(matrix, dtype=np.float64)
    basis, origin = m[:3, :3], m[:3, 3]
    scale = np.linalg.norm(basis, axis=0)
    scale = np.where(scale < 1e-12, 1e-12, scale)
    if np.linalg.det(basis) < 0:            # mirrored: one negative scale keeps the rotation a rotation
        scale[0] = -scale[0]
    u, _s, vt = np.linalg.svd(basis / scale)
    rotation = u @ vt                       # shear has no place in Unreal's transform: the nearest rotation
    turned = _FLIP @ rotation @ (_AIM if aim else _FLIP)
    w, x, y, z = Matrix(turned.tolist()).to_quaternion()
    return {"location": [round(float(v), 4) for v in _FLIP @ origin * unit * CM],
            "quat": [round(float(v), 7) for v in (x, y, z, w)],
            "scale": [1.0, 1.0, 1.0] if aim else [round(float(v), 6) for v in scale]}


def point_to_unreal(point, unit: float = 1.0) -> np.ndarray:
    """Where a point of a mesh lands after Unreal's FBX import."""
    return _FLIP @ np.asarray(point, dtype=np.float64) * unit * CM


def rotate(quat, vector) -> np.ndarray:
    """What Unreal's FQuat::RotateVector does."""
    q, v = np.array(quat[:3], dtype=np.float64), np.asarray(vector, dtype=np.float64)
    t = 2.0 * np.cross(q, v)
    return v + quat[3] * t + np.cross(q, t)


def _reach(candela: float, light: bpy.types.Light, unit: float) -> float:
    """How far the light goes, judged in Blender's own light as Blender's threshold is."""
    candela /= LIGHT_SCALE
    if getattr(light, "use_custom_distance", False):
        return light.cutoff_distance * unit * CM
    return min(max(math.sqrt(max(candela, 0.0) / LEAST_LUX), REACH_M[0]), REACH_M[1]) * CM


def light_item(name: str, light: bpy.types.Light, matrix, unit: float, notes: set, exposure: float = 0.0) -> Dict[str, Any]:
    power = light.energy * 2.0 ** (getattr(light, "exposure", 0.0) + exposure)
    item: Dict[str, Any] = {"kind": "light", "name": name, **to_unreal(matrix, unit, aim=True),
                            "color": [round(float(c), 5) for c in light.color] + [1.0], "shadows": bool(light.use_shadow)}
    if light.type == 'SUN':
        item.update(type="sun", intensity=round(power * LIGHT_SCALE, 4), angle=round(math.degrees(light.angle), 4))
    elif light.type == 'AREA':
        stretch = np.linalg.norm(np.array(matrix, dtype=np.float64)[:3, :2], axis=0)
        across = light.size_y if light.shape in {'RECTANGLE', 'ELLIPSE'} else light.size
        width, height = light.size * stretch[0], across * stretch[1]
        if light.shape in {'DISK', 'ELLIPSE'}:
            notes.add("Round area lights are rectangles in Unreal (rect lights), of the same width and height.")
        if not getattr(light, "normalize", True):
            power *= width * height
        candela = power * LIGHT_SCALE / math.pi           # one side, spread like a lit wall
        item.update(type="area", intensity=round(candela, 3), width=round(width * unit * CM, 3),
                    height=round(height * unit * CM, 3), radius=round(_reach(candela, light, unit), 2))
    else:
        candela = power / (4.0 * math.pi) * LIGHT_SCALE         # a spot is given as if it were a point, in both programs
        item.update(type="spot" if light.type == 'SPOT' else "point", intensity=round(candela, 3),
                    source_radius=round(light.shadow_soft_size * unit * CM, 3), radius=round(_reach(candela, light, unit), 2))
        if light.type == 'SPOT':
            outer = math.degrees(light.spot_size) / 2.0
            if outer > SPOT_MAX:
                notes.add(f"Spot lights wider than {2 * SPOT_MAX:.0f} degrees are {2 * SPOT_MAX:.0f} degrees in Unreal.")
                outer = SPOT_MAX
            item.update(outer=round(outer, 4), inner=round(outer * (1.0 - light.spot_blend), 4))
    return item


def _has_picture(material: Optional[bpy.types.Material]) -> bool:
    """Is a picture laid on by UVs a part of the material's look? Plain numbers need no UVs."""
    shader = rs._shader(material) if material is not None else None
    if shader is None:
        return False
    plan = rs._plan(shader)
    return plan["normal"] is not None or any(isinstance(v, tuple) and v and v[0] == "image" for v in plan.values())


def camera_item(name: str, camera: bpy.types.Camera, matrix, scene: bpy.types.Scene, unit: float,
                active: bool) -> Dict[str, Any]:
    render = scene.render
    aspect = (render.resolution_x * render.pixel_aspect_x) / max(render.resolution_y * render.pixel_aspect_y, 1e-6)
    item: Dict[str, Any] = {"kind": "camera", "name": name, **to_unreal(matrix, unit, aim=True),
                            "aspect": round(aspect, 6), "active": active}
    if camera.type == 'ORTHO':
        item["ortho"] = round(camera.ortho_scale * (1.0 if aspect >= 1.0 else aspect) * unit * CM, 3)
        return item
    fit = camera.sensor_fit
    if fit == 'AUTO':
        fit, sensor = ('HORIZONTAL' if aspect >= 1.0 else 'VERTICAL'), camera.sensor_width
    else:
        sensor = camera.sensor_width if fit == 'HORIZONTAL' else camera.sensor_height
    half = math.atan(sensor / 2.0 / camera.lens)
    if fit == 'VERTICAL':
        half = math.atan(math.tan(half) * aspect)
    item["fov"] = round(math.degrees(2.0 * half), 5)        # Unreal's field of view is horizontal
    return item


# -- pictures ------------------------------------------------------------------------

def write_png(path: str, pixels: np.ndarray) -> None:
    """8-bit grey, RGB or RGBA rows (top row first) as a PNG file."""
    pixels = np.ascontiguousarray(pixels, dtype=np.uint8)
    if pixels.ndim == 2:
        pixels = pixels[..., None]
    height, width, channels = pixels.shape
    rows = np.concatenate([np.zeros((height, 1), dtype=np.uint8), pixels.reshape(height, width * channels)], axis=1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    header = struct.pack(">IIBBBBB", width, height, 8, {1: 0, 3: 2, 4: 6}[channels], 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows.tobytes(), PNG_LEVEL))
                 + chunk(b"IEND", b""))


def _last_files(staging: str) -> Dict[str, str]:
    """{file: digest} of what the last send of this asset left in its folder, for files still there."""
    entries: List[Dict[str, Any]] = []
    for name in ("manifest.json", "progress.json"):     # a send stopped half way keeps the meshes it wrote
        try:
            with open(os.path.join(staging, name), encoding="utf-8") as fh:
                last = json.load(fh)
        except (OSError, ValueError):
            continue
        entries += last.get("meshes", []) + last.get("textures", [])
    return {entry["file"]: entry["digest"] for entry in entries
            if entry.get("digest") and os.path.isfile(os.path.join(staging, *entry["file"].split("/")))}


def _drop_others(staging: str, keep: set) -> None:
    """Unix's own folder: the files of meshes and pictures this send no longer has go."""
    for sub in ("meshes", "textures"):
        for name in os.listdir(os.path.join(staging, sub)):
            if f"{sub}/{name}" not in keep:
                os.remove(os.path.join(staging, sub, name))


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _safe(name: str) -> str:
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in name).strip("_")
    return out or "Asset"


def _unique(name: str, taken: set) -> str:
    out, number = name, 2
    while out.lower() in taken:
        out, number = f"{name}_{number}", number + 1
    taken.add(out.lower())
    return out


MESH_FORMAT = "1"               # change with the FBX settings: meshes sent before are then imported again
SKY_PX = (64, 32)               # the world's light as a long-lat picture: its sky half and its ground half
SKY_READ_PX = (128, 64)         # a world made by nodes is rendered this big to be averaged


def _plain_world(world: bpy.types.World) -> Optional[np.ndarray]:
    """The world's light when it is one colour everywhere (a Background node with nothing plugged
    in, or no nodes), else None."""
    if not world.use_nodes or world.node_tree is None:
        return np.array(world.color[:3], dtype=np.float64)
    out = next((n for n in world.node_tree.nodes if n.type == 'OUTPUT_WORLD' and n.is_active_output), None)
    found = rs._through(out.inputs["Surface"]) if out is not None else None
    if found is None:
        return np.zeros(3)
    node = found[0]
    if node.type != 'BACKGROUND' or node.inputs["Color"].is_linked or node.inputs["Strength"].is_linked:
        return None
    return np.array(node.inputs["Color"].default_value[:3], dtype=np.float64) * float(node.inputs["Strength"].default_value)


def _rendered_world(world: bpy.types.World, folder: str) -> Tuple[np.ndarray, np.ndarray]:
    """(sky, ground) mean light of a world made by nodes: Cycles renders it all around once, small."""
    scene = bpy.data.scenes.new("_unix_sky")
    camera = bpy.data.objects.new("_unix_sky", bpy.data.cameras.new("_unix_sky"))
    try:
        scene.world, scene.camera = world, camera
        scene.collection.objects.link(camera)
        camera.data.type, camera.data.panorama_type = 'PANO', 'EQUIRECTANGULAR'
        camera.rotation_euler = (math.pi / 2.0, 0.0, 0.0)                   # level: the picture's top row is straight up
        scene.render.engine = 'CYCLES'
        scene.cycles.samples, scene.cycles.use_denoising = 16, False
        scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = *SKY_READ_PX, 100
        scene.render.image_settings.file_format, scene.render.image_settings.color_depth = 'OPEN_EXR', '32'
        scene.render.filepath = os.path.join(folder, "_world.exr")
        shown = "Render Result" in bpy.data.images
        with _util.ui_context():
            bpy.ops.render.render(write_still=True, scene=scene.name)
        if not shown and "Render Result" in bpy.data.images:
            bpy.data.images.remove(bpy.data.images["Render Result"])     # it was not there before: none is left
        shot = bpy.data.images.load(scene.render.filepath)
        try:
            pixels = np.empty(SKY_READ_PX[0] * SKY_READ_PX[1] * 4, dtype=np.float32)
            shot.pixels.foreach_get(pixels)
        finally:
            bpy.data.images.remove(shot)
        os.remove(scene.render.filepath)
    finally:
        lens = camera.data
        bpy.data.objects.remove(camera)
        bpy.data.cameras.remove(lens)
        bpy.data.scenes.remove(scene)
    rows = pixels.reshape(SKY_READ_PX[1], SKY_READ_PX[0], 4)[..., :3].astype(np.float64)     # bottom row first
    weight = np.cos((np.arange(SKY_READ_PX[1]) + 0.5) / SKY_READ_PX[1] * math.pi - math.pi / 2.0)   # a row's share of the sphere
    half = SKY_READ_PX[1] // 2
    mean = lambda part, w: (part * w[:, None, None]).sum(axis=(0, 1)) / (w.sum() * part.shape[1])
    return mean(rows[half:], weight[half:]), mean(rows[:half], weight[:half])


def write_sky(scene: bpy.types.Scene, path: str, gain: float) -> Optional[str]:
    """Blender's world as Unreal's sky light: a long-lat HDR picture whose upper half is the world's
    mean light from above and lower half from below, times ``gain`` (the lights' scale). A mean
    has no direction to get wrong; it is what lights a scene's shade. Returns its digest, or None
    when the world gives no light."""
    world = scene.world
    if world is None:
        return None
    plain = _plain_world(world)
    sky, ground = (plain, plain) if plain is not None else _rendered_world(world, os.path.dirname(path))
    if max(float(np.max(sky)), float(np.max(ground))) <= 1e-6:
        return None
    width, height = SKY_PX
    pixels = np.empty((height, width, 3), dtype=np.float64)
    pixels[:height // 2], pixels[height // 2:] = sky * gain, ground * gain       # top row first: the sky, then the ground
    write_hdr(path, pixels)
    return hashlib.sha1(pixels.tobytes()).hexdigest()


def write_hdr(path: str, pixels: np.ndarray) -> None:
    """(rows, columns, 3) linear floats, top row first, as a Radiance HDR file with run-length rows.
    Written here because Blender's own HDR saving turns a float picture's numbers into sRGB."""
    height, width = pixels.shape[:2]
    peak = pixels.max(axis=2)
    mantissa, exponent = np.frexp(peak)
    scale = np.where(peak > 1e-32, mantissa * 256.0 / np.maximum(peak, 1e-32), 0.0)
    rgbe = np.zeros((height, width, 4), dtype=np.uint8)
    rgbe[..., :3] = np.clip(pixels * scale[..., None], 0, 255).astype(np.uint8)
    rgbe[..., 3] = np.where(peak > 1e-32, exponent + 128, 0).astype(np.uint8)
    rows = []
    for row in rgbe:                        # each row: 2 2 width, then each channel in chunks of at most 128 plain bytes
        rows.append(bytes([2, 2, width >> 8, width & 255]))
        for channel in range(4):
            values = row[:, channel].tobytes()
            rows.extend(bytes([len(values[i:i + 128])]) + values[i:i + 128] for i in range(0, width, 128))
    with open(path, "wb") as fh:
        fh.write(f"#?RADIANCE\nFORMAT=32-bit_rle_rgbe\n\n-Y {height} +X {width}\n".encode("ascii") + b"".join(rows))


def mesh_digest(mesh: bpy.types.Mesh) -> str:
    """What the exported file of this mesh holds: its points, faces, slots, UVs and normals. The
    editor skips importing a mesh whose digest it already has, so sending again rebuilds only
    what changed."""
    digest = hashlib.sha1(MESH_FORMAT.encode())
    for collection, attribute, kind in ((mesh.vertices, "co", np.float32), (mesh.loops, "vertex_index", np.int32),
                                        (mesh.polygons, "loop_total", np.int32), (mesh.polygons, "material_index", np.int32),
                                        (mesh.corner_normals, "vector", np.float32)):
        values = np.empty(len(collection) * (3 if attribute in ("co", "vector") else 1), dtype=kind)
        collection.foreach_get(attribute, values)
        digest.update(np.round(values, 5).tobytes() if kind is np.float32 else values.tobytes())
    for layer in mesh.uv_layers:
        values = np.empty(len(mesh.loops) * 2, dtype=np.float32)
        layer.data.foreach_get("uv", values)
        digest.update(np.round(values, 5).tobytes())
    return digest.hexdigest()


ORIGINAL_FORMATS = {'PNG': ".png", 'JPEG': ".jpg", 'TARGA': ".tga", 'BMP': ".bmp"}
ORIGINAL_PX = 4096              # an untouched picture up to this size travels as its own file


def original(image: bpy.types.Image, colour: bool) -> Optional[bpy.types.Image]:
    """The image, when its own file can go to Unreal untouched: an 8-bit picture Unreal reads, on
    disk or packed, not painted on since it was loaded, stored as the map wants it (colour in sRGB,
    data and normals as numbers). Its pixels are then never decoded, re-encoded or rewritten."""
    if image.source != 'FILE' or image.is_dirty or image.is_float or image.file_format not in ORIGINAL_FORMATS:
        return None
    if max(image.size[:]) > ORIGINAL_PX or min(image.size[:]) < 1:
        return None
    space = image.colorspace_settings.name
    if (space != 'sRGB') if colour else (space not in {'Non-Color', 'Raw'}):
        return None
    if image.packed_file is None and not os.path.isfile(bpy.path.abspath(image.filepath, library=image.library)):
        return None
    return image


def look(material: Optional[bpy.types.Material], images: Dict[Any, Any], notes: set) -> Dict[str, Any]:
    """A Blender material as the master material's parameters: {blend, vectors, scalars, maps}, the
    maps being {parameter: (kind, 8-bit pixels or an untouched image)} with kind colour, data or normal."""
    out: Dict[str, Any] = {"blend": "opaque", "vectors": {}, "scalars": {}, "maps": {}}
    if material is None:
        out["vectors"]["BaseColor"] = [0.8, 0.8, 0.8, 1.0]
        return out
    shader = rs._shader(material)
    if shader is None:                      # no Principled shader: what the viewport shows
        out["vectors"]["BaseColor"] = [float(c) for c in material.diffuse_color[:3]] + [1.0]
        out["scalars"].update(Roughness=float(material.roughness), Metallic=float(material.metallic))
        return out
    plan = rs._plan(shader)
    made = [name for name in ("color", "alpha", "roughness", "metallic", "normal", "emission")
            if plan[name] is not None and plan[name][0] == "nodes"]
    if made:
        notes.add(f"{material.name}: its {', '.join(made)} is made by shader nodes and could not be baked, so a plain "
                  f"value stands in for it.")

    def value(name: str, default: float) -> float:
        return float(plan[name][1]) if plan[name][0] == "value" else default

    # colour, with what the alpha cuts out in the colour map's alpha
    colour, cut = plan["color"], plan["alpha"]
    whole = original(colour[1].image, True) if colour[0] == "image" and cut[0] != "image" \
        and float(np.abs(colour[3]).max()) < 1e-6 else None
    if whole is not None:                   # the picture as it is; a tint in front of it is the master's colour
        out["maps"]["BaseColorMap"] = ("color", whole)
        out["vectors"]["BaseColor"] = [round(float(c), 5) for c in colour[2]] + [1.0]
        colour = ("done",)
    shade = rs._colour(colour, images, PICTURE_PX) if colour[0] == "image" else None
    hole = rs._number(cut, images, PICTURE_PX) if cut[0] == "image" else None
    plain = list(colour[1]) if colour[0] == "value" else list(material.diffuse_color[:3])
    if colour[0] == "image" and shade is None:
        notes.add(f"{material.name}: its colour picture could not be read (missing file?), so a plain colour stands in.")
    if hole is not None and float(hole.min()) > 0.999:
        hole = None
    if shade is not None or hole is not None:
        sizes = [layer.shape[:2] for layer in (shade, hole) if layer is not None]
        rows, columns = max(s[0] for s in sizes), max(s[1] for s in sizes)
        rgb = rs._encode(rs._fit(shade, rows, columns)) if shade is not None else np.ones((1, 1, 3), dtype=np.float32)
        alpha = rs._fit(hole, rows, columns) if hole is not None else np.ones((1, 1), dtype=np.float32)
        rgb = np.broadcast_to(rgb, (rows, columns, 3))
        alpha = np.broadcast_to(alpha, (rows, columns))
        out["maps"]["BaseColorMap"] = ("color", rs._to_bytes(np.concatenate([rgb, alpha[..., None]], axis=2)))
        out["vectors"]["BaseColor"] = [1.0, 1.0, 1.0, 1.0] if shade is not None else [float(c) for c in plain] + [1.0]
    elif whole is None:
        out["vectors"]["BaseColor"] = [float(c) for c in plain] + [1.0]
    opacity = value("alpha", 1.0)
    if hole is not None:                    # mostly fully in or out: cut out (sorts and shadows right); soft: blended
        out["blend"] = "translucent" if float(((hole > 0.1) & (hole < 0.9)).mean()) > 0.02 else "masked"
    if opacity < 0.999:
        out["blend"] = "translucent"
        out["scalars"]["Opacity"] = round(opacity, 4)
    if plan["transmission"] > 0.5:
        out["blend"] = "translucent"
        out["scalars"]["Opacity"] = round(min(opacity, 0.25), 4)
        notes.add("Glass is see-through in Unreal (a translucent material at a quarter opacity), without Blender's "
                  "bending of light.")

    # roughness in R and metal in G of one data map, when either varies over the surface
    sources = {"roughness": plan["roughness"], "metallic": plan["metallic"]}
    varying = {name: rs._number(source, images, PICTURE_PX) for name, source in sources.items() if source[0] == "image"}
    if any(v is not None for v in varying.values()):
        sizes = [v.shape[:2] for v in varying.values() if v is not None]
        rows, columns = max(s[0] for s in sizes), max(s[1] for s in sizes)
        channels = []
        for name, default in (("roughness", 0.5), ("metallic", 0.0)):
            layer = varying.get(name)
            channels.append(np.broadcast_to(rs._fit(layer, rows, columns), (rows, columns)) if layer is not None
                            else np.full((rows, columns), value(name, default), dtype=np.float32))
        channels.append(np.zeros((rows, columns), dtype=np.float32))
        out["maps"]["MaskMap"] = ("data", rs._to_bytes(np.stack(channels, axis=2)))
        out["scalars"].update(Roughness=1.0, Metallic=1.0)
    else:
        out["scalars"].update(Roughness=round(value("roughness", 0.5), 4), Metallic=round(value("metallic", 0.0), 4))

    bumps = plan["normal"]
    if bumps is not None and bumps[0] == "image" and abs(bumps[2] - 1.0) <= 1e-3 and original(bumps[1].image, False):
        out["maps"]["NormalMap"] = ("normal", bumps[1].image)     # Unreal turns its green over on import
    elif bumps is not None and bumps[0] == "image":
        pixels = rs._read(bumps[1].image, images, PICTURE_PX)
        if pixels is not None:
            normal = pixels[..., :3] * 2.0 - 1.0
            if abs(bumps[2] - 1.0) > 1e-3:          # the Normal Map node's strength, put into the picture
                flat = np.array([0.0, 0.0, 1.0], dtype=np.float32)
                normal = flat + (normal - flat) * bumps[2]
                normal[..., 2] = np.maximum(normal[..., 2], 0.05)
            normal = normal / np.maximum(np.linalg.norm(normal, axis=2, keepdims=True), 1e-6)
            normal[..., 1] = -normal[..., 1]        # Blender's normal maps are OpenGL (green up), Unreal's DirectX
            out["maps"]["NormalMap"] = ("normal", rs._to_bytes(normal * 0.5 + 0.5))

    # light given off, on the same scale as the lights
    light = plan["emission"]
    if light is not None and light[0] == "value":
        out["vectors"]["EmissiveColor"] = [round(float(c) * LIGHT_SCALE, 5) for c in light[1]] + [1.0]
    elif light is not None and light[0] == "image":
        glow = rs._colour(light, images, PICTURE_PX)
        if glow is not None:
            peak = max(float(glow.max()), 1e-6)
            out["maps"]["EmissiveMap"] = ("color", rs._to_bytes(rs._encode(glow / peak)))
            out["vectors"]["EmissiveColor"] = [round(peak * LIGHT_SCALE, 5)] * 3 + [1.0]

    mapping = plan["uv"]
    if mapping and out["maps"]:             # Blender's Mapping node on the UVs: tiling and shift (Unreal's V runs downwards)
        (su, sv), (tu, tv) = mapping["scale"], mapping["shift"]
        out["vectors"]["UVTransform"] = [su, sv, tu, 1.0 - sv - tv]
        if abs(mapping["turn"]) > 1e-6:
            notes.add(f"{material.name}: a turned texture (Mapping rotation) arrives unturned.")
    return out


# -- the send -------------------------------------------------------------------------

def _solid_viewports() -> List[Tuple[Any, str]]:
    """Put viewports that show materials (Material Preview, Rendered) on Solid; what they showed, to put back.
    While a bake runs the window shows its private scene and then the user's again, and such a viewport loads
    the same pictures on its own threads: on 31 Oct a city's send to Unreal died in the picture reader that way
    (0xc0000409 in OpenImageIO), and each bake took 20 s instead of 7."""
    changed: List[Tuple[Any, str]] = []
    wm = bpy.context.window_manager
    for window in wm.windows if wm else []:
        for area in window.screen.areas:
            for space in area.spaces if area.type == 'VIEW_3D' else []:
                if space.type == 'VIEW_3D' and space.shading.type in {'MATERIAL', 'RENDERED'}:
                    changed.append((space, space.shading.type))
                    space.shading.type = 'SOLID'
    return changed


def _restore_viewports(changed: List[Tuple[Any, str]]) -> None:
    for space, shading in changed:
        try:
            space.shading.type = shading
        except ReferenceError:
            pass                                # the window was closed or its layout changed meanwhile


def _key(obj: bpy.types.Object) -> Tuple[str, str]:
    """Objects with the same key share one exported mesh: plain meshes share their data; anything a
    modifier changes, or that is not a mesh, is its own."""
    if obj.type == 'MESH' and not any(m.show_viewport for m in obj.modifiers) and not obj.data.shape_keys:
        return ("data", obj.data.name)
    return ("object", obj.name)


class Sender:
    """Sends a scene to one Unreal editor. ``step`` (main thread) does a moment of the work and returns
    None, until the editor has built everything: then it returns the report."""

    def __init__(self, objects: Optional[List[bpy.types.Object]], asset: str, editor: Dict[str, Any], staging: str) -> None:
        scene = bpy.context.scene
        self.asset, self.editor, self.staging = asset, editor, staging
        self.unit = scene.unit_settings.scale_length or 1.0
        self.exposure = float(scene.view_settings.exposure)      # what the view adds, lights and glows take along
        self.notes: set = set()
        self.meshes: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.actors: List[Dict[str, Any]] = []
        self.skipped = {"geometry": 0, "rigged": set()}
        names = {"mesh": set(), "texture": set(), "material": set()}
        self.names = names
        wanted = {o.name for o in objects} if objects is not None else None
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for instance in depsgraph.object_instances:
            source = instance.object.original
            owner = instance.parent.original if instance.is_instance and instance.parent is not None else source
            if instance.is_instance and source == owner:
                self.skipped["geometry"] += 1       # geometry that nodes make and instance: no object of its own
                continue
            if wanted is not None and owner.name not in wanted:
                continue
            if wanted is None and (owner.hide_render or not owner.visible_get()):
                continue
            label = f"{owner.name}_{source.name}" if instance.is_instance else source.name
            matrix = np.array(instance.matrix_world, dtype=np.float64)
            if source.type in _SHAPES:
                key = _key(source)
                if key not in self.meshes:
                    self.meshes[key] = {"id": f"m{len(self.meshes)}", "object": source.name,
                                        "name": _unique("SM_" + _safe(source.name if key[0] == "object" else source.data.name), names["mesh"]),
                                        "bake": rs.made_by_nodes(source)}
                if any(m.type == 'ARMATURE' and m.show_viewport for m in source.modifiers):
                    self.skipped["rigged"].add(source.name)
                self.actors.append({"kind": "mesh", "name": label, "mesh": self.meshes[key]["id"], "_key": key,
                                    "_object": source.name, "shadows": bool(owner.visible_shadow and source.visible_shadow),
                                    **to_unreal(matrix, self.unit)})
            elif source.type == 'LIGHT':
                self.actors.append(light_item(label, source.data, matrix, self.unit, self.notes, self.exposure))
            elif source.type == 'CAMERA':
                self.actors.append(camera_item(label, source.data, matrix, scene, self.unit, source == scene.camera))
        if not self.actors:
            raise ToolError("Nothing to send to Unreal: no meshes, lights or cameras among these objects.")
        # the world's light goes with a whole scene or one with its lights (a place); a prop goes alone
        self.with_world = wanted is None or any(a["kind"] == "light" for a in self.actors)
        self.images: Dict[Any, Any] = {}
        self.materials: Dict[Any, Dict[str, Any]] = {}       # by what they were made from
        self.textures: Dict[str, Dict[str, Any]] = {}        # by pixels
        self.writers = ThreadPoolExecutor(WRITERS, thread_name_prefix="unix_png")
        self.previous = _last_files(staging)                  # file -> digest, of the last send of this asset
        self.pending: List[Future] = []                       # pictures being written while the meshes export
        self.exported: List[Dict[str, Any]] = []
        self.baked: Dict[Tuple[str, str], List[str]] = {}     # mesh key -> its baked materials, by slot
        self.used: Dict[Tuple[str, str], List[int]] = {}      # mesh key -> the Blender slots its Unreal slots are
        self.thread: Optional[threading.Thread] = None
        self.answer: Dict[str, Any] = {}
        self.stage: Optional[bpy.types.Scene] = None          # the private scene the meshes are exported from
        self.slots: List[bpy.types.Material] = []             # UnixSlot0..n, shared by every mesh of the send
        self.trash: List[Any] = []                            # this batch's mesh copies and holder, removed together
        self.shading: List[Tuple[Any, str]] = []              # viewports put on Solid for the send, and what they showed
        self.began = time.monotonic()
        self.work = self._run()

    # -- one mesh ------------------------------------------------------------------------

    def _export(self, key: Tuple[str, str], entry: Dict[str, Any], depsgraph=None) -> None:
        """One mesh into its FBX. ``depsgraph`` is the scene's, looked at once for the whole batch: nothing a
        mesh of the batch does may make Blender look again."""
        obj = bpy.data.objects.get(entry["object"])
        if obj is None:
            return
        stand = None
        if entry["bake"]:
            stand = rs.Baked(obj)
            if not stand.ok:
                stand.remove()
                stand = None
                self.notes.add(f"{obj.name}: its look is made by shader nodes and could not be baked (no Cycles, or too "
                               f"heavy), so plain values stand in for it.")
        try:
            source = stand.object if stand is not None else obj
            if depsgraph is None or stand is not None:
                depsgraph = bpy.context.evaluated_depsgraph_get()
            mesh = bpy.data.meshes.new_from_object(source.evaluated_get(depsgraph), preserve_all_data_layers=True,
                                                   depsgraph=depsgraph)
            self.trash.append(mesh)
            count = len(mesh.polygons)
            if count == 0:
                return
            laid = rs.projected_uvs(mesh, [s.material for s in obj.material_slots], rs.texspace(obj, mesh))                     if stand is None else None
            if laid is not None:            # pictures projected from the object's coordinates: UVs that repeat them
                layer = next((layer for layer in mesh.uv_layers if layer.active_render), None) or mesh.uv_layers.new(name="UVMap")
                layer.data.foreach_set("uv", laid.astype(np.float32).ravel())
            slots = np.zeros(count, dtype=np.int32)
            mesh.polygons.foreach_get("material_index", slots)
            slots = np.clip(slots, 0, max(len(mesh.materials), 1) - 1)
            used = sorted(set(int(s) for s in slots))
            mesh.polygons.foreach_set("material_index", np.searchsorted(used, slots).astype(np.int32))
            mesh.materials.clear()
            for index in range(len(used)):      # Unreal finds each slot by this name
                mesh.materials.append(self._slot(index))
            keep = "UnixBake" if stand is not None else next((layer.name for layer in mesh.uv_layers if layer.active_render), None)
            for layer in [layer for layer in mesh.uv_layers if layer.name != keep]:
                mesh.uv_layers.remove(layer)    # one set of UVs, the one the pictures are laid on
            if not mesh.uv_layers and entry["bake"] is False and any(_has_picture(s.material) for s in obj.material_slots):
                self.notes.add(f"{obj.name} has no UVs: pictures cannot be laid on it.")
            file, digest = f"meshes/{entry['name']}.fbx", mesh_digest(mesh)
            if self.previous.get(file) != digest:           # else the last send's file holds this very mesh
                self._write_fbx(mesh, os.path.join(self.staging, "meshes", entry["name"] + ".fbx"))
            self.used[key] = used
            if stand is not None:
                self.baked[key] = [self._material(stand.materials[i] if i < len(stand.materials) else None,
                                                  f"{obj.name}_{i}") for i in used]
            self.exported.append({"key": entry["id"], "name": entry["name"], "file": file, "digest": digest})
        finally:
            if stand is not None:
                stand.remove()

    def _slot(self, index: int) -> bpy.types.Material:
        while len(self.slots) <= index:
            placeholder = bpy.data.materials.new(f"UnixSlot{len(self.slots)}")
            placeholder.name = f"UnixSlot{len(self.slots)}"        # takes the name even from an older one
            self.slots.append(placeholder)
        return self.slots[index]

    def _write_fbx(self, mesh: bpy.types.Mesh, path: str) -> None:
        """``mesh`` alone into an FBX at ``path``, from the private scene's own collection: the exporter looks
        at that scene only (not at the user's), and no selection is involved (a new scene's selection is not
        reliably what the exporter sees: the first city send wrote empty files that way)."""
        if self.stage is None:
            self.stage = bpy.data.scenes.new("_unix_unreal")
            self.stage.unit_settings.scale_length = bpy.context.scene.unit_settings.scale_length
            self.stage.collection.children.link(bpy.data.collections.new("_unix_unreal"))
        holder = bpy.data.objects.new("UnixExport", mesh)
        self.trash.append(holder)
        box = self.stage.collection.children[0]
        box.objects.link(holder)
        with _util.ui_context(), bpy.context.temp_override(scene=self.stage, view_layer=self.stage.view_layers[0]):
            # FBX_SCALE_NONE: Unreal reads the file's metre unit and turns it into centimetres itself
            bpy.ops.export_scene.fbx(filepath=path, collection=box.name, object_types={'MESH'}, use_mesh_modifiers=False,
                                     mesh_smooth_type='FACE', axis_forward='-Z', axis_up='Y', apply_unit_scale=True,
                                     apply_scale_options='FBX_SCALE_NONE', bake_space_transform=False, path_mode='STRIP',
                                     embed_textures=False, add_leaf_bones=False, bake_anim=False, use_custom_props=False)
        box.objects.unlink(holder)
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ToolError("The FBX exporter ran but wrote no file.")

    def _tidy(self, final: bool = False) -> None:
        """Remove the batch's mesh copies and holders in one go (one rebuild of Blender's graph); at the end
        of the send also the slot materials and the private scene."""
        stage: List[Any] = []
        if final and self.stage is not None:
            try:
                stage = [*self.stage.collection.children, self.stage]
            except ReferenceError:
                pass
        gone = self.trash + ((self.slots + stage) if final else [])
        self.trash = []
        if final:
            self.slots, self.stage = [], None
            _restore_viewports(self.shading)
            self.shading = []
        alive = []
        for block in gone:
            try:
                block.name                          # an undo or another file frees what we held: leave it
            except ReferenceError:
                continue
            alive.append(block)
        if alive:
            bpy.data.batch_remove(alive)

    # -- materials -----------------------------------------------------------------------

    def _material(self, material: Optional[bpy.types.Material], name: str = "") -> str:
        """The key of the Unreal material instance for this Blender material (made on first use)."""
        known = material.name if material is not None and not material.get("unix_baked") else None
        if material is None:
            known = "(none)"
        if known is not None and known in self.materials:
            return self.materials[known]["key"]
        label = name or (material.name if material is not None else "Unix_Default")
        found = look(material, self.images, self.notes)
        glow = found["vectors"].get("EmissiveColor")
        if glow is not None:
            found["vectors"]["EmissiveColor"] = [round(c * 2.0 ** self.exposure, 5) for c in glow[:3]] + [1.0]
        entry = {"key": f"mat{len(self.materials)}", "name": _unique("MI_" + _safe(label), self.names["material"]),
                 "blend": found["blend"], "vectors": found["vectors"], "scalars": found["scalars"], "textures": {}}
        for parameter, (kind, pixels) in found["maps"].items():
            called = f"T_{_safe(label)}_{parameter.replace('Map', '')}"
            entry["textures"][parameter] = self._original(called, kind, pixels) if isinstance(pixels, bpy.types.Image) \
                else self._texture(called, kind, pixels)
        self.materials[known if known is not None else ("baked", entry["key"])] = entry
        return entry["key"]

    def _texture(self, name: str, kind: str, pixels: np.ndarray) -> str:
        digest = hashlib.sha1(np.ascontiguousarray(pixels).tobytes() + str(pixels.shape).encode()).hexdigest() + kind
        if digest not in self.textures:
            name = _unique(name, self.names["texture"])
            self.textures[digest] = {"key": f"t{len(self.textures)}", "name": name, "file": f"textures/{name}.png", "kind": kind,
                                     "digest": digest}
            if self.previous.get(f"textures/{name}.png") != digest:
                self.pending.append(self.writers.submit(write_png, os.path.join(self.staging, "textures", name + ".png"),
                                                        np.array(pixels, copy=True)))
        return self.textures[digest]["key"]

    def _original(self, name: str, kind: str, image: bpy.types.Image) -> str:
        """An untouched picture: its own file goes, once however many materials use it."""
        known = ("file", image.name, kind)
        if known not in self.textures:
            name = _unique(_safe(os.path.splitext(image.name)[0]) if image.name else name, self.names["texture"])
            file = f"textures/{name}{ORIGINAL_FORMATS[image.file_format]}"
            target = os.path.join(self.staging, *file.split("/"))
            if image.packed_file is not None:
                data = bytes(image.packed_file.data)
                digest = hashlib.sha1(data).hexdigest()
                job = (_write_bytes, target, data)
            else:
                source = bpy.path.abspath(image.filepath, library=image.library)
                facts = os.stat(source)
                digest = hashlib.sha1(f"{os.path.normcase(os.path.abspath(source))}|{facts.st_size}|{facts.st_mtime_ns}".encode()).hexdigest()
                job = (shutil.copyfile, source, target)
            if self.previous.get(file) != digest + kind:
                self.pending.append(self.writers.submit(*job))
            self.textures[known] = {"key": f"t{len(self.textures)}", "name": name, "file": file, "kind": kind,
                                    "flip_green": kind == "normal",       # Blender's normal maps are OpenGL, Unreal's DirectX
                                    "digest": digest + kind}
        return self.textures[known]["key"]

    # -- the loop ------------------------------------------------------------------------

    def _run(self):
        report = os.path.join(self.staging, "report.json")
        if os.path.isfile(report):
            os.remove(report)                   # the last send's: only this send's own report may be read
        for sub in ("meshes", "textures", "defaults"):
            os.makedirs(os.path.join(self.staging, sub), exist_ok=True)
        if any(entry["bake"] for entry in self.meshes.values()):
            self.shading = _solid_viewports()
        items, number = list(self.meshes.items()), 0
        while number < len(items):
            until, depsgraph = time.monotonic() + BATCH_SECONDS, bpy.context.evaluated_depsgraph_get()
            while True:
                key, entry = items[number]
                number += 1
                _util.status["text"] = f"Exporting for Unreal  {number} of {len(items)}"
                try:
                    self._export(key, entry, depsgraph)
                except ReferenceError:
                    pass                            # deleted while we were sending
                # a bake looks at the scene again itself: the batch ends with it
                if number == len(items) or entry["bake"] or time.monotonic() > until:
                    break
            self._tidy()
            with open(os.path.join(self.staging, "progress.json"), "w", encoding="utf-8") as fh:
                json.dump({"meshes": self.exported}, fh)
            yield
        self._tidy(final=True)
        for actor in self.actors:
            if actor["kind"] != "mesh":
                continue
            key = actor.pop("_key")
            obj = bpy.data.objects.get(actor.pop("_object"))
            if key in self.baked:
                actor["materials"] = self.baked[key]
            else:
                slots = obj.material_slots if obj is not None else []
                actor["materials"] = [self._material(slots[i].material if i < len(slots) else None)
                                      for i in self.used.get(key, [0])]
            yield
        while self.pending:
            left = [job for job in self.pending if not job.done()]
            for job in self.pending:
                if job.done():
                    job.result()                    # a failed write stops the send here, with its error
            self.pending = left
            _util.status["text"] = f"Writing pictures for Unreal  {len(left)} left"
            if left:
                yield "wait"
        self.writers.shutdown()
        defaults = {"color": ("T_UnixWhite.png", np.full((4, 4, 3), 255, np.uint8)),
                    "data": ("T_UnixWhiteData.png", np.full((4, 4, 3), 255, np.uint8)),
                    "normal": ("T_UnixFlatNormal.png", np.tile(np.array([128, 128, 255], np.uint8), (4, 4, 1)))}
        for file, pixels in defaults.values():
            write_png(os.path.join(self.staging, "defaults", file), pixels)
        manifest = {"asset": self.asset, "meshes": self.exported,
                    "textures": list(self.textures.values()),
                    "materials": list(self.materials.values()),
                    "actors": [a for a in self.actors if a["kind"] != "mesh" or any(m["key"] == a["mesh"] for m in self.exported)],
                    "defaults": {kind: f"defaults/{file}" for kind, (file, _pixels) in defaults.items()}}
        if self.with_world:
            os.makedirs(os.path.join(self.staging, "sky"), exist_ok=True)
            digest = write_sky(bpy.context.scene, os.path.join(self.staging, "sky", "T_Unix_World.hdr"),
                               LIGHT_SCALE * 2.0 ** self.exposure)
            if digest is not None:
                manifest["sky"] = {"file": "sky/T_Unix_World.hdr", "name": "T_Unix_World", "digest": digest}
        _drop_others(self.staging, {m["file"] for m in manifest["meshes"]} | {t["file"] for t in manifest["textures"]})
        path = os.path.join(self.staging, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        if os.path.isfile(os.path.join(self.staging, "progress.json")):
            os.remove(os.path.join(self.staging, "progress.json"))      # the manifest says it all now
        self.manifest = manifest
        _util.status["text"] = "Building in Unreal"
        command = (f"import runpy; runpy.run_path({BUILDER!r}, init_globals={{'MANIFEST': {path!r}}}, "
                   f"run_name='unix_build')")
        self.thread = threading.Thread(target=self._remote, args=(command,), daemon=True)
        self.thread.start()
        while self.thread.is_alive():
            yield "wait"
        _util.status["text"] = ""
        return self._report()

    def _remote(self, command: str) -> None:
        try:
            self.answer = unreal_link.run_once(self.editor["node"], command, seconds=LINK_SECONDS)
        except (unreal_link.LinkError, OSError) as exc:
            self.answer = {"success": False, "result": str(exc), "output": []}

    def step(self) -> Optional[Dict[str, Any]]:
        """None while there is more to do; the report when the editor has built it. ``waiting`` says
        the last call had nothing to do but wait (for pictures being written, or the editor)."""
        until = time.monotonic() + SLICE_SECONDS
        self.waiting = False
        try:
            while True:
                if next(self.work) == "wait":
                    self.waiting = True
                    return None
                if time.monotonic() > until:
                    return None
        except StopIteration as done:
            return done.value

    def close(self) -> None:
        """The send was stopped: nothing of it stays in the scene; the meshes already written are kept for
        the next send (progress.json); an editor that is already building finishes on its own."""
        self.work.close()
        self._tidy(final=True)
        self.writers.shutdown(wait=False, cancel_futures=True)
        _util.status["text"] = ""

    def _report(self) -> Dict[str, Any]:
        project = self.editor.get("project_name") or os.path.basename(os.path.normpath(self.editor.get("project_root", "")))
        report_path = os.path.join(self.staging, "report.json")
        built: Dict[str, Any] = {}
        try:
            with open(report_path, encoding="utf-8") as fh:
                built = json.load(fh)
        except (OSError, ValueError):
            pass
        if not built:
            errors = [o.get("output", "") for o in self.answer.get("output", []) if o.get("type") == "Error"]
            raise ToolError(f"Unreal ({project}) did not build the send: {self.answer.get('result') or 'no answer'} "
                            + " ".join(errors[-3:])[:1500])
        if not built.get("ok"):
            raise ToolError(f"Unreal ({project}) stopped while building: {built.get('error')}\n{built.get('trace', '')[-1200:]}")
        lights = [a for a in self.actors if a["kind"] == "light"]
        out: Dict[str, Any] = {
            "engine": "Unreal", "project": project, "level": built["level"], "asset": self.asset,
            "content": built["content"], "outliner_folder": f"Unix/{self.asset}",
            "built": built["spawned"], "imported": built["imported"],
            **({"unchanged": {k: v for k, v in built["unchanged"].items() if v},
                "unchanged_note": "Sent before exactly as they are now, so the editor kept them instead of importing them again."}
               if any((built.get("unchanged") or {}).values()) else {}),
            "replaced_actors": built["replaced"], "seconds": round(time.monotonic() - self.began, 1),
            "next": (f"It stands in the open level where it stood in Blender, in the outliner folder Unix/{self.asset}. "
                     f"Sending the same asset again replaces it in place. The pictures, meshes and materials are saved "
                     f"under {built['content']}; save the level to keep the actors."),
        }
        meshes = len(self.meshes)
        copies = sum(1 for a in self.actors if a["kind"] == "mesh")
        if copies > meshes:
            out["copies"] = f"{copies} objects are {meshes} different meshes: each was imported once and its copies share it."
        if lights:
            out["lights"] = ("The lights are Movable, so they light at once without a lighting build, and are as bright "
                             "as Unreal needs to show what Blender's view shows at its fixed exposure (EV100 0, the one a "
                             "level without auto exposure has). A level with auto exposure adapts to them; a level whose "
                             "exposure is set for physical light (a 100,000 lux sun) makes them look dim.")
        approximated = sorted(self.notes) + list(built.get("notes") or [])
        if self.skipped["rigged"]:
            approximated.append(f"Rigged meshes arrive as static meshes in their current pose (no skeleton or animation yet): "
                                f"{', '.join(sorted(self.skipped['rigged'])[:6])}.")
        if self.skipped["geometry"]:
            approximated.append(f"{self.skipped['geometry']} pieces of geometry instanced by geometry nodes were not sent "
                                f"(only instances of real objects travel).")
        if approximated:
            out["approximated"] = approximated
        out["not_sent"] = "Animation is not sent." + ("" if self.with_world else " The world's light goes only with a whole "
                                                     "scene or one that has its lights (a place), not with props.")
        return out


def pick_editor(editors: List[Dict[str, Any]], root: Optional[str]) -> Optional[Dict[str, Any]]:
    """The answering editor to send to: the one with ``root`` open when a project is known, else the
    only one there is. None when no editor fits."""
    if root:
        return next((e for e in editors if unreal_link.same_folder(e.get("project_root", ""), root)), None)
    if len(editors) > 1:
        raise ToolError("More than one Unreal editor is linked: " + ", ".join(f"'{e.get('project_name')}'" for e in editors)
                        + ". Ask the user which project, then send again with `project` set to its folder.")
    return editors[0] if editors else None


def link_help(root: str) -> str:
    """No editor with this project answered: switch the link on in its files if it is not, and say what is left to do."""
    name = os.path.splitext(os.path.basename(unreal_link.uproject(root) or root))[0]
    state = unreal_link.link_state(root)
    if state["plugins_missing"] or not state["remote_on"]:
        try:
            unreal_link.enable(root)
        except (OSError, ValueError, unreal_link.LinkError) as exc:
            return (f"Unix could not turn on the Unreal link in {name} ({exc}). In the editor: Edit > Project Settings > "
                    f"Plugins > Python > Enable Remote Execution, with the Python Editor Script Plugin and Editor Scripting "
                    f"Utilities plugins enabled, then restart it.")
        return f"Turned on the Unreal link in {name}. Restart the Unreal editor once, then send again."
    return (f"The Unreal link is on in {name}, but no editor with that project answered. Open the project in Unreal (restart "
            f"the editor once if it was already open when the link was turned on), then send again.")
