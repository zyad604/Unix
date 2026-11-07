# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Baked looks kept between sends.

Baking what shader nodes make takes Cycles a few seconds an object, and a send bakes every such object
again: the Lasso Ranch world spent about ten of its twenty-four minutes baking the same 106 objects it
had baked for the send before. A bake is kept here (its fresh UVs and its pictures) under a digest of
everything it was made from: the evaluated mesh with all its attributes, every material's node tree
with its settings and pictures, and the picture size. Change any of it and the object is baked afresh.
"""
from __future__ import annotations

import hashlib
import os
import threading
from typing import Any, Dict, List, Optional

import bpy
import numpy as np

from . import _util

VERSION = 1                     # raise when what a bake makes changes: older bakes are then not used
LIMIT_BYTES = 4 * 2 ** 30       # kept bakes past this size go, the longest unused first
GREY = {"alpha", "roughness", "metallic"}   # pictures that are one value a pixel: kept as one channel

# node and socket properties that do not change what a material looks like
_SKIP = {"rna_type", "name", "label", "location", "location_absolute", "width", "height", "dimensions",
         "select", "color", "color_tag", "use_custom_color", "show_options", "show_preview", "show_texture",
         "hide", "parent", "internal_links", "inputs", "outputs", "type", "warning_propagation", "node",
         "links", "id_data", "bl_idname", "bl_label", "bl_description", "bl_icon", "bl_static_type",
         "bl_width_default", "bl_width_min", "bl_width_max", "bl_height_default", "bl_height_min",
         "bl_height_max", "identifier", "display_shape", "link_limit", "is_output", "enabled",
         "show_expanded", "is_unavailable", "is_multi_input", "is_icon_visible"}
# nodes whose output depends on where the object stands: a bake of them is not one for a copy elsewhere
_PLACED = {"NEW_GEOMETRY", "OBJECT_INFO", "VECT_TRANSFORM", "CAMERA", "LIGHT_PATH", "TEX_COORD", "ATTRIBUTE"}
_WIDTH = {"FLOAT_VECTOR": 3, "FLOAT2": 2, "FLOAT_COLOR": 4, "BYTE_COLOR": 4, "QUATERNION": 4, "INT32_2D": 2,
          "FLOAT4X4": 16, "INT16_2D": 2, "INT8": 1}
_FIELD = {"FLOAT_VECTOR": "vector", "FLOAT2": "vector", "FLOAT_COLOR": "color", "BYTE_COLOR": "color"}
_writing: List[threading.Thread] = []


def _array(collection, attr: str, width: int, dtype) -> np.ndarray:
    out = np.empty(len(collection) * width, dtype=dtype)
    if len(collection):
        collection.foreach_get(attr, out)
    return out


def _mesh(h, mesh: bpy.types.Mesh) -> None:
    h.update(_array(mesh.vertices, "co", 3, np.float32).tobytes())
    h.update(_array(mesh.loops, "vertex_index", 1, np.int32).tobytes())
    h.update(_array(mesh.polygons, "loop_total", 1, np.int32).tobytes())
    h.update(_array(mesh.polygons, "material_index", 1, np.int32).tobytes())
    for attribute in sorted(mesh.attributes, key=lambda a: a.name):
        if attribute.name.startswith("."):           # Blender's own bookkeeping (selection, topology read above)
            continue
        kind = attribute.data_type
        h.update(f"|{attribute.name}:{attribute.domain}:{kind}".encode())
        dtype = bool if kind == "BOOLEAN" else np.int32 if kind.startswith("INT") else np.float32
        try:
            h.update(_array(attribute.data, _FIELD.get(kind, "value"), _WIDTH.get(kind, 1), dtype).tobytes())
        except Exception:                            # a kind this Blender reads otherwise: its name and size still count
            h.update(str(len(attribute.data)).encode())


def _image(picture: bpy.types.Image) -> str:
    parts = [picture.name, picture.source, picture.filepath_raw, tuple(picture.size),
             picture.colorspace_settings.name, picture.alpha_mode]
    if picture.packed_file is not None:
        parts.append(("packed", picture.packed_file.size))
    path = bpy.path.abspath(picture.filepath_raw) if picture.filepath_raw else ""
    if path and os.path.isfile(path):
        stat = os.stat(path)
        parts.append((stat.st_size, int(stat.st_mtime)))
    if picture.is_dirty or (picture.source == "GENERATED" and picture.packed_file is None):
        if picture.size[0] and picture.size[1]:      # painted and not saved: only the pixels say what it is
            pixels = np.empty(len(picture.pixels), dtype=np.float32)
            picture.pixels.foreach_get(pixels)
            parts.append(hashlib.sha1(pixels.tobytes()).hexdigest())
    return repr(parts)


def _value(value, seen: set, depth: int) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, bpy.types.Image):
        return _image(value)
    if isinstance(value, bpy.types.NodeTree):
        return _tree(value, seen)
    if isinstance(value, bpy.types.ID):
        return "ID:" + value.name
    if isinstance(value, bpy.types.bpy_struct):
        return _struct(value, seen, depth + 1) if depth < 3 else None
    try:
        return tuple(_value(v, seen, depth) for v in value)
    except TypeError:
        return repr(value)


def _struct(struct, seen: set, depth: int = 0) -> tuple:
    out = []
    for prop in struct.bl_rna.properties:
        name = prop.identifier
        if name in _SKIP:
            continue
        try:
            value = getattr(struct, name)
        except Exception:
            continue
        if prop.type == "COLLECTION":
            out.append((name, tuple(_struct(item, seen, depth + 1) for item in value) if depth < 3 else len(value)))
        else:
            out.append((name, _value(value, seen, depth)))
    return tuple(out)


def _tree(tree: bpy.types.NodeTree, seen: set) -> str:
    if tree.name in seen:                            # a group used twice is read once
        return "tree:" + tree.name
    seen.add(tree.name)
    h = hashlib.sha1()
    for node in sorted(tree.nodes, key=lambda n: n.name):
        h.update(repr((node.bl_idname, node.name, node.mute, _struct(node, seen))).encode())
        for socket in node.inputs:
            value = getattr(socket, "default_value", None)
            h.update(repr((socket.identifier, socket.is_linked, _value(value, seen, 1))).encode())
    for link in sorted(tree.links, key=lambda l: (l.to_node.name, l.to_socket.identifier, l.from_node.name)):
        h.update(repr((link.from_node.name, link.from_socket.identifier, link.to_node.name,
                       link.to_socket.identifier, link.is_muted)).encode())
    return h.hexdigest()


def _placed(tree: Optional[bpy.types.NodeTree], seen: Optional[set] = None) -> bool:
    """True when what the tree makes depends on where the object stands in the world."""
    if tree is None:
        return False
    seen = seen if seen is not None else set()
    if tree.name in seen:
        return False
    seen.add(tree.name)
    for node in tree.nodes:
        if node.type in _PLACED:
            if node.type == "TEX_COORD" and getattr(node, "object", None) is None and not any(
                    s.is_linked for s in node.outputs if s.name in {"Window", "Camera", "Reflection"}):
                continue                             # UV, generated, object and normal coordinates go with the object
            if node.type == "ATTRIBUTE" and node.attribute_type == "GEOMETRY":
                continue
            return True
        if node.type == "GROUP" and _placed(node.node_tree, seen):
            return True
    return False


def digest(obj: bpy.types.Object, mesh: bpy.types.Mesh, size: int) -> str:
    """What a bake of ``obj`` (whose evaluated mesh is ``mesh``) at ``size`` px is made from."""
    h = hashlib.sha1(f"bake{VERSION}:{size}".encode())
    _mesh(h, mesh)
    seen: set = set()
    for material in mesh.materials:
        if material is None:
            h.update(b"|none")
            continue
        h.update(repr((material.name, material.use_nodes, tuple(material.diffuse_color),
                       material.blend_method if hasattr(material, "blend_method") else "")).encode())
        if material.use_nodes and material.node_tree is not None:
            h.update(_tree(material.node_tree, seen).encode())
            if _placed(material.node_tree):
                h.update(repr([round(v, 5) for row in obj.matrix_world for v in row]).encode())
    return h.hexdigest()[:24]


def _path(key: str) -> str:
    return os.path.join(_util.data_dir("bake_cache"), key + ".npz")


def load(key: str) -> Optional[Dict[str, Any]]:
    """The kept bake: {"uv": (loops*2) float32, "pictures": {name: (h, w, channels) float16}}, or None."""
    path = _path(key)
    if not os.path.isfile(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as kept:
            names = [str(n) for n in kept["names"]]
            out = {"uv": kept["uv"], "pictures": {name: kept["p_" + name] for name in names}}
        os.utime(path)                               # used now: the last to go when the cache is trimmed
        return out
    except Exception as exc:                         # cut short by a crash, or from another version: baked again
        print(f"[Unix] a kept bake could not be read ({exc!r}): baking afresh")
        return None


def kept_pixels(picture: bpy.types.Image, name: str) -> np.ndarray:
    """The pixels of a fresh bake as the cache keeps them, written back into the picture: a bake read from the
    cache and a fresh one then make the very same pictures (and keys), so Studio's copies stay in use."""
    w, h = picture.size
    pixels = np.empty(w * h * 4, dtype=np.float32)
    picture.pixels.foreach_get(pixels)
    pixels = pixels.reshape(h, w, 4)
    kept = (pixels[..., :1] if name in GREY else pixels[..., :3]).astype(np.float16)
    picture.pixels.foreach_set(spread(kept).ravel())
    return kept


def spread(kept: np.ndarray) -> np.ndarray:
    """(h, w, 1 or 3) kept pixels as the RGBA float32 a Blender picture holds."""
    h, w = kept.shape[:2]
    out = np.ones((h, w, 4), dtype=np.float32)
    out[..., :3] = kept.astype(np.float32)          # one channel fills all three
    return out


def save(key: str, uv: np.ndarray, pictures: Dict[str, np.ndarray]) -> None:
    """Keep a bake. Compressing and writing happen on a thread of their own (no Blender data is touched there)."""
    def write():
        path = _path(key)
        part = path + ".part"
        try:
            with open(part, "wb") as handle:
                np.savez_compressed(handle, names=np.array(list(pictures)), uv=uv,
                                    **{"p_" + name: data for name, data in pictures.items()})
            os.replace(part, path)
            _trim()
        except Exception as exc:
            print(f"[Unix] a bake could not be kept: {exc!r}")
            try:
                os.remove(part)
            except OSError:
                pass
    _writing[:] = [t for t in _writing if t.is_alive()]
    thread = threading.Thread(target=write, daemon=True, name="unix-bake-cache")
    _writing.append(thread)
    thread.start()


def finish() -> None:
    """Wait for kept bakes still being written (tests, and before Blender quits)."""
    for thread in list(_writing):
        thread.join()


def _trim() -> None:
    folder = _util.data_dir("bake_cache")
    files = []
    for name in os.listdir(folder):
        if name.endswith(".npz"):
            path = os.path.join(folder, name)
            try:
                stat = os.stat(path)
            except OSError:
                continue
            files.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= LIMIT_BYTES:
            break
        try:
            os.remove(path)
            total -= size
        except OSError:
            pass
