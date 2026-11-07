# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Structured material creation: one call for a correct Principled PBR material."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import bpy

from . import _turn
from ._util import get_object
from .registry import ToolError, tool

# Principled input names changed across versions; first existing name wins.
_INPUT_ALIASES = {
    "emission_color": ("Emission Color", "Emission"),
    "transmission": ("Transmission Weight", "Transmission"),
    "coat": ("Coat Weight", "Clearcoat"),
    "specular": ("Specular IOR Level", "Specular"),
}
_TEXTURE_SLOTS = {  # argument key -> (principled input, colour space)
    "base_color": ("Base Color", "sRGB"), "roughness": ("Roughness", "Non-Color"),
    "metallic": ("Metallic", "Non-Color"), "normal": ("Normal", "Non-Color"),
    "emission": ("Emission Color", "sRGB"), "alpha": ("Alpha", "Non-Color"),
}


def _socket(bsdf: bpy.types.Node, *names: str):
    for name in names:
        sock = bsdf.inputs.get(name)
        if sock is not None:
            return sock
    return None


def _rgba(color: List[float]) -> List[float]:
    if len(color) not in (3, 4) or any(not isinstance(c, (int, float)) for c in color):
        raise ToolError("Colours are [r, g, b] or [r, g, b, a] with components 0-1 (linear).")
    return [float(c) for c in color[:3]] + [float(color[3]) if len(color) == 4 else 1.0]


@tool(
    "set_material",
    """Create or update a Principled BSDF material and (optionally) assign it to objects.
    Colours are linear RGB 0-1. Texture paths are wired with the correct colour space and a
    Normal Map node for normals. Re-using a name updates that material in place, so objects
    sharing it update together.""",
    {
        "name": {"type": "string", "description": "Material name (created if missing)."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Objects to assign the material to."},
        "slot": {"type": "integer", "description": "Slot index to put it in (default 0; appended if out of range)."},
        "base_color": {"type": "array", "items": {"type": "number"}, "description": "[r,g,b] or [r,g,b,a], linear 0-1."},
        "metallic": {"type": "number"}, "roughness": {"type": "number"},
        "emission_color": {"type": "array", "items": {"type": "number"}},
        "emission_strength": {"type": "number"},
        "alpha": {"type": "number", "description": "Opacity 0-1. Below 1 also enables blended transparency."},
        "transmission": {"type": "number", "description": "Glass-like transmission 0-1."},
        "ior": {"type": "number"},
        "textures": {"type": "object", "description": "Image file paths keyed by base_color, roughness, metallic, normal, emission, alpha.",
                     "properties": {k: {"type": "string"} for k in _TEXTURE_SLOTS}},
    },
    ["name"],
    mutates=True,
    label="Set a material",
)
def set_material(name: str, objects: Optional[List[str]] = None, slot: int = 0,
                 base_color: Optional[List[float]] = None, metallic: Optional[float] = None,
                 roughness: Optional[float] = None, emission_color: Optional[List[float]] = None,
                 emission_strength: Optional[float] = None, alpha: Optional[float] = None,
                 transmission: Optional[float] = None, ior: Optional[float] = None,
                 textures: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    targets = [get_object(n) for n in (objects or [])]
    bad = [o.name for o in targets if not hasattr(o.data, "materials")]
    if bad:
        raise ToolError(f"These objects cannot hold materials: {', '.join(bad)}")
    for key, path in (textures or {}).items():
        if key not in _TEXTURE_SLOTS:
            raise ToolError(f"Unknown texture key '{key}'. Use: {', '.join(_TEXTURE_SLOTS)}")
        if not os.path.exists(bpy.path.abspath(path)):
            raise ToolError(f"Texture file not found: {path}")

    _turn.mark_mutation()
    mat = bpy.data.materials.get(name)
    created = mat is None
    if created:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        out = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None) or nodes.new("ShaderNodeOutputMaterial")
        links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])

    def put(value, *names):
        sock = _socket(bsdf, *names)
        if value is not None and sock is not None:
            sock.default_value = value

    if base_color is not None:
        put(_rgba(base_color), "Base Color")
        mat.diffuse_color = _rgba(base_color)  # solid-view colour
    put(metallic, "Metallic")
    put(roughness, "Roughness")
    put(ior, "IOR")
    put(transmission, *_INPUT_ALIASES["transmission"])
    put(emission_strength, "Emission Strength")
    if emission_color is not None:
        put(_rgba(emission_color), *_INPUT_ALIASES["emission_color"])
    if alpha is not None:
        put(float(alpha), "Alpha")
        if alpha < 1.0:
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = 'BLENDED'
            elif hasattr(mat, "blend_method"):
                mat.blend_method = 'BLEND'

    wired = []
    for index, (key, path) in enumerate((textures or {}).items()):
        input_name, space = _TEXTURE_SLOTS[key]
        sock = _socket(bsdf, input_name, *(_INPUT_ALIASES["emission_color"] if key == "emission" else ()))
        if sock is None:
            continue
        label = f"unix_{key}"
        tex = nodes.get(label) or nodes.new("ShaderNodeTexImage")
        tex.name = tex.label = label
        tex.location = (bsdf.location.x - 620, bsdf.location.y - 280 * index)
        tex.image = bpy.data.images.load(bpy.path.abspath(path), check_existing=True)
        tex.image.colorspace_settings.name = space
        for link in list(sock.links):
            links.remove(link)
        if key == "normal":
            nmap = nodes.get("unix_normal_map") or nodes.new("ShaderNodeNormalMap")
            nmap.name = "unix_normal_map"
            nmap.location = (bsdf.location.x - 300, tex.location.y)
            links.new(tex.outputs["Color"], nmap.inputs["Color"])
            links.new(nmap.outputs["Normal"], sock)
        else:
            links.new(tex.outputs["Color"], sock)
        wired.append(key)

    for obj in targets:
        slots = obj.data.materials
        if 0 <= slot < len(slots):
            slots[slot] = mat
        else:
            slots.append(mat)
    # Receipt: what actually drives the surface now, and who else shares this material.
    base = bsdf.inputs.get("Base Color")
    driven_by = (f"image '{getattr(base.links[0].from_node, 'image', None).name}'"
                 if base is not None and base.is_linked and getattr(base.links[0].from_node, "image", None)
                 else "linked node" if base is not None and base.is_linked
                 else [round(c, 3) for c in base.default_value[:3]] if base is not None else None)
    output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output), None)
    reaches_output = bool(output and output.inputs["Surface"].is_linked
                          and output.inputs["Surface"].links[0].from_node == bsdf)
    target_names = {o.name for o in targets}
    shared = sorted(o.name for o in bpy.data.objects
                    if o.name not in target_names and any(s.material == mat for s in o.material_slots))
    out = {"material": mat.name, "created": created, "assigned_to": [o.name for o in targets],
           "textures_wired": wired, "base_color_from": driven_by, "bsdf_reaches_output": reaches_output}
    if shared:
        out["also_used_by"] = shared[:20]
        out["note"] = "This material is shared: the objects in also_used_by changed too."
    return out
