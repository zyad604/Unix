# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""VFX: build ready-to-render animated effects from presets, and bake them to sprite sheets.

Two qualities. ``realistic`` (fire and explosion) runs a real gas simulation: an emitter
and a domain, baked to a cache on disk in under a minute or two, shaded from the simulated
flame. It is what real fire looks like: turbulent, never the same twice. ``fast``
is procedural and cache-free: animated emission/volume shaders driven by ``frame`` drivers or
keyframes, keyframed transforms and visibility, legacy particle systems with instanced
emissive meshes, and keyframed point lights; it appears at once and looks stylised. Each
effect lives under an empty ``VFX_<name>`` in the ``Unix_VFX`` collection; re-running with
the same name replaces it cleanly.
"""

from __future__ import annotations

import math
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

import bmesh
import bpy
from mathutils import Matrix, Vector

from . import _turn, _util
from ._util import data_dir, eevee_engine_id, ensure_object_mode, get_object, ui_context
from .registry import Pending, ToolError, ToolOutput, tool

VFX_COLLECTION = "Unix_VFX"
_RENDERABLE = {'MESH', 'CURVE', 'SURFACE', 'META', 'FONT', 'CURVES', 'POINTCLOUD', 'VOLUME'}

EFFECTS = ["fire", "smoke", "explosion", "sparks", "magic_swirl", "shockwave",
           "energy_shield", "rain", "snow", "dust_burst"]
_DURATION = {"fire": 96, "smoke": 96, "explosion": 36, "sparks": 48, "magic_swirl": 96,
             "shockwave": 30, "energy_shield": 96, "rain": 96, "snow": 120, "dust_burst": 40}
_LOOP = {"fire": True, "smoke": True, "explosion": False, "sparks": False, "magic_swirl": True,
         "shockwave": False, "energy_shield": True, "rain": True, "snow": True, "dust_burst": False}
_COLOR = {"fire": (1.0, 0.4, 0.05), "smoke": (0.12, 0.12, 0.13), "explosion": (1.0, 0.45, 0.1),
          "sparks": (1.0, 0.7, 0.2), "magic_swirl": (0.6, 0.2, 1.0), "shockwave": (0.5, 0.8, 1.0),
          "energy_shield": (0.1, 0.5, 1.0), "rain": (0.6, 0.7, 0.9), "snow": (1.0, 1.0, 1.0),
          "dust_burst": (0.6, 0.5, 0.42)}
# Simulated effects: frames run before the effect's own range so it starts fully developed,
# default length, and default grid resolution along the domain's longest side.
SIMULATED = ("fire", "explosion")
_SIM_WARM = {"fire": 24, "explosion": 0}
_SIM_DURATION = {"fire": 72, "explosion": 44}
_SIM_DETAIL = {"fire": 72, "explosion": 64}
_TIP = {
    "fire": "Raise intensity for a taller, brighter flame; set color for coloured fire.",
    "smoke": "Raise intensity for thicker smoke; lower it for a thin wisp.",
    "explosion": "duration_frames controls how long the smoke lingers after the flash.",
    "sparks": "intensity scales the spark count; size_m scales how far they fly.",
    "magic_swirl": "color sets the glow hue; intensity scales brightness and orbit size.",
    "shockwave": "size_m sets the final radius; shorten duration_frames for a snappier pulse.",
    "energy_shield": "color sets the hue; intensity controls the pulse brightness.",
    "rain": "intensity scales the droplet count; size_m scales the area covered.",
    "snow": "intensity scales the flake count; lower intensity for a gentle flurry.",
    "dust_burst": "size_m sets the spread; intensity scales the number of puffs.",
}


# -- shared builders --------------------------------------------------------

def _mesh_from_bm(name: str, bm: bmesh.types.BMesh) -> bpy.types.Object:
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for poly in me.polygons:
        poly.use_smooth = True
    return bpy.data.objects.new(name, me)


def _icosphere(name: str, radius: float, subdiv: int = 3) -> bpy.types.Object:
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=radius)
    return _mesh_from_bm(name, bm)


def _uvsphere(name: str, radius: float, seg: int = 12, ring: int = 6) -> bpy.types.Object:
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=ring, radius=radius)
    return _mesh_from_bm(name, bm)


def _cone(name: str, r1: float, r2: float, depth: float) -> bpy.types.Object:
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, segments=20, radius1=r1, radius2=r2, depth=depth, cap_ends=True)
    return _mesh_from_bm(name, bm)


def _box(name: str, sx: float, sy: float, sz: float) -> bpy.types.Object:
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(sx, sy, sz), verts=bm.verts)
    return _mesh_from_bm(name, bm)


def _plane(name: str, sx: float, sy: float) -> bpy.types.Object:
    bm = bmesh.new()
    bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=1.0)
    bmesh.ops.scale(bm, vec=(sx, sy, 1.0), verts=bm.verts)
    return _mesh_from_bm(name, bm)


def _torus(name: str, major: float, minor: float, maj_seg: int = 48, min_seg: int = 10) -> bpy.types.Object:
    bm = bmesh.new()
    rings = []
    for i in range(maj_seg):
        a = 2 * math.pi * i / maj_seg
        ring = []
        for j in range(min_seg):
            b = 2 * math.pi * j / min_seg
            rr = minor * math.cos(b)
            z = minor * math.sin(b)
            ring.append(bm.verts.new((math.cos(a) * (major + rr), math.sin(a) * (major + rr), z)))
        rings.append(ring)
    for i in range(maj_seg):
        r0, r1 = rings[i], rings[(i + 1) % maj_seg]
        for j in range(min_seg):
            j1 = (j + 1) % min_seg
            bm.faces.new((r0[j], r0[j1], r1[j1], r1[j]))
    return _mesh_from_bm(name, bm)


def _drive(owner: Any, path: str, index: int, expr: str) -> None:
    try:
        fc = owner.driver_add(path, index)
        fc.driver.expression = expr
    except Exception:
        pass


def _const_interp(obj: bpy.types.Object) -> None:
    ad = obj.animation_data
    if ad and ad.action:
        for fc in ad.action.fcurves:
            for kp in fc.keyframe_points:
                kp.interpolation = 'CONSTANT'


def _emission_mat(name: str, color, strength: float) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    emit.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
    emit.inputs["Strength"].default_value = strength
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    mat.diffuse_color = (color[0], color[1], color[2], 1.0)
    return mat


def _noise_emission_mat(name: str, stops, strength: float, scale: float, scroll: float,
                        detail: float = 5.0, soft: bool = False, fade_height: float = 0.0) -> bpy.types.Material:
    """Emissive material whose colour comes from a scrolling noise through a colour ramp.

    ``stops`` is a list of (position, (r, g, b)). ``scroll`` drives the mapping along Z by
    ``frame`` so the pattern animates without any cache.
    """
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    mapping = nt.nodes.new("ShaderNodeMapping")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    noise.inputs["Scale"].default_value = scale
    noise.inputs["Detail"].default_value = detail
    emit.inputs["Strength"].default_value = strength
    el = ramp.color_ramp.elements
    el[0].position, el[0].color = stops[0][0], (*stops[0][1], 1.0)
    el[1].position, el[1].color = stops[-1][0], (*stops[-1][1], 1.0)
    for pos, rgb in stops[1:-1]:
        e = ramp.color_ramp.elements.new(pos)
        e.color = (*rgb, 1.0)
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], emit.inputs["Color"])
    if not soft:
        nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    else:
        # Soft look: transparent towards the silhouette, optionally fading and breaking up with
        # height, times an "Opacity" value that presets can keyframe to fade the effect out.
        mix = nt.nodes.new("ShaderNodeMixShader")
        clear = nt.nodes.new("ShaderNodeBsdfTransparent")
        facing = nt.nodes.new("ShaderNodeLayerWeight")
        edge = nt.nodes.new("ShaderNodeMapRange")
        opacity = nt.nodes.new("ShaderNodeValue")
        opacity.name = opacity.label = "Opacity"
        opacity.outputs[0].default_value = 1.0
        facing.inputs["Blend"].default_value = 0.5
        edge.inputs["From Min"].default_value, edge.inputs["From Max"].default_value = 0.08, 0.7
        edge.inputs["To Min"].default_value, edge.inputs["To Max"].default_value = 1.0, 0.0
        nt.links.new(facing.outputs["Facing"], edge.inputs["Value"])
        alpha = nt.nodes.new("ShaderNodeMath")
        alpha.operation = 'MULTIPLY'
        nt.links.new(edge.outputs["Result"], alpha.inputs[0])
        nt.links.new(opacity.outputs[0], alpha.inputs[1])
        last = alpha
        if fade_height:
            split = nt.nodes.new("ShaderNodeSeparateXYZ")
            height = nt.nodes.new("ShaderNodeMapRange")   # 1 at the base, 0 at the tip
            ragged = nt.nodes.new("ShaderNodeMath")       # noise eats into the upper part
            ragged.operation = 'MULTIPLY_ADD'
            cut = nt.nodes.new("ShaderNodeMapRange")
            both = nt.nodes.new("ShaderNodeMath")
            both.operation = 'MULTIPLY'
            height.inputs["From Min"].default_value, height.inputs["From Max"].default_value = -fade_height * 0.2, fade_height * 0.5
            height.inputs["To Min"].default_value, height.inputs["To Max"].default_value = 1.0, 0.0
            nt.links.new(coord.outputs["Object"], split.inputs["Vector"])
            nt.links.new(split.outputs["Z"], height.inputs["Value"])
            nt.links.new(noise.outputs["Fac"], ragged.inputs[0])
            ragged.inputs[1].default_value = 0.9
            nt.links.new(height.outputs["Result"], ragged.inputs[2])
            cut.inputs["From Min"].default_value, cut.inputs["From Max"].default_value = 0.72, 1.2
            nt.links.new(ragged.outputs[0], cut.inputs["Value"])
            nt.links.new(alpha.outputs[0], both.inputs[0])
            nt.links.new(cut.outputs["Result"], both.inputs[1])
            last = both
        nt.links.new(last.outputs[0], mix.inputs["Fac"])
        nt.links.new(clear.outputs["BSDF"], mix.inputs[1])
        nt.links.new(emit.outputs["Emission"], mix.inputs[2])
        nt.links.new(mix.outputs["Shader"], out.inputs["Surface"])
        if hasattr(mat, "surface_render_method"):
            mat.surface_render_method = 'BLENDED'
        elif hasattr(mat, "blend_method"):
            mat.blend_method = 'BLEND'
        mat.use_backface_culling = True  # blended back faces show through as stray triangles
        for attr in ("use_transparency_overlap", "show_transparent_back"):
            if hasattr(mat, attr):
                setattr(mat, attr, False)
    _drive(mapping.inputs["Location"], "default_value", 2, "frame * %f" % scroll)
    mat.diffuse_color = (*stops[-1][1], 1.0)
    return mat


def _smoke_volume_mat(name: str, color, density: float, scroll: float) -> bpy.types.Material:
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    noise = nt.nodes.new("ShaderNodeTexNoise")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    mapping = nt.nodes.new("ShaderNodeMapping")
    coord = nt.nodes.new("ShaderNodeTexCoord")
    noise.inputs["Scale"].default_value = 2.5
    noise.inputs["Detail"].default_value = 6.0
    vol.inputs["Color"].default_value = (color[0], color[1], color[2], 1.0)
    ramp.color_ramp.elements[0].position = 0.45  # cut the low noise so the smoke is wispy
    ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
    ramp.color_ramp.elements[1].position = 0.75
    ramp.color_ramp.elements[1].color = (density, density, density, 1.0)
    nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], vol.inputs["Density"])
    nt.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    _drive(mapping.inputs["Location"], "default_value", 2, "frame * %f" % scroll)
    mat.diffuse_color = (color[0], color[1], color[2], 1.0)
    return mat


def _assign(obj: bpy.types.Object, mat: bpy.types.Material) -> None:
    obj.data.materials.append(mat)


class _FX:
    """Collects the objects of one effect, parents them to its root and tracks names."""

    def __init__(self, name: str, location, size_m: float, color, intensity: float,
                 start: int, end: int, tinted: bool = False, detail: int = 64, warm: int = 0):
        self.tinted = tinted      # the user chose the colour: tint the flame instead of natural fire colours
        self.detail = detail
        self.warm = warm
        self.sim: Dict[str, Any] = {}
        self.prefix = "VFX_" + name
        self.size = max(0.01, float(size_m))
        self.color = color
        self.intensity = max(0.01, float(intensity))
        self.start = start
        self.end = end
        self.coll = _vfx_collection()
        self.root = bpy.data.objects.new(self.prefix, None)
        self.root.empty_display_type = 'PLAIN_AXES'
        self.root.empty_display_size = self.size * 0.5
        self.root.location = Vector(location)
        self.coll.objects.link(self.root)
        self.objects: List[str] = []

    def child(self, obj: bpy.types.Object, local=(0.0, 0.0, 0.0)) -> bpy.types.Object:
        self.coll.objects.link(obj)
        obj.location = Vector(local)
        obj.parent = self.root
        obj.matrix_parent_inverse = Matrix.Identity(4)
        self.objects.append(obj.name)
        return obj

    def light(self, suffix: str, color, energy: float, local=(0.0, 0.0, 0.0), soft=0.1):
        data = bpy.data.lights.new(self.prefix + "_" + suffix, 'POINT')
        data.color = color
        data.energy = energy
        data.shadow_soft_size = soft
        return self.child(bpy.data.objects.new(self.prefix + "_" + suffix, data), local), data

    def bounds(self, side: float, local=(0.0, 0.0, 0.0)) -> None:
        """A render-hidden box so look/flipbook frame the whole spread of a particle burst."""
        b = _box(self.prefix + "_bounds", side, side, side)
        b.hide_render = True  # excluded from renders, still framed (depsgraph sees it)
        b.display_type = 'BOUNDS'
        self.child(b, local)

    def particles(self, emitter: bpy.types.Object, suffix: str) -> Any:
        mod = emitter.modifiers.new(self.prefix + "_" + suffix, 'PARTICLE_SYSTEM')
        ps = mod.particle_system.settings
        ps.name = self.prefix + "_" + suffix
        emitter.show_instancer_for_render = False
        emitter.show_instancer_for_viewport = False
        return ps


def _vfx_collection() -> bpy.types.Collection:
    col = bpy.data.collections.get(VFX_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(VFX_COLLECTION)
    if col.name not in {c.name for c in bpy.context.scene.collection.children_recursive}:
        bpy.context.scene.collection.children.link(col)
    return col


def _remove_existing(name: str) -> None:
    prefix = "VFX_" + name
    doomed = [o for o in bpy.data.objects if o.name == prefix or o.name.startswith(prefix + "_")]
    caches = []
    for obj in doomed:      # a simulated effect owns a cache folder: it goes with the effect
        for mod in getattr(obj, "modifiers", []):
            if mod.type == 'FLUID' and mod.fluid_type == 'DOMAIN':
                caches.append(os.path.normpath(bpy.path.abspath(mod.domain_settings.cache_directory)))
    for obj in doomed:
        bpy.data.objects.remove(obj, do_unlink=True)
    ours = os.path.normpath(data_dir("vfx_cache"))
    for folder in caches:
        if os.path.dirname(folder) == ours and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
    # node groups before materials: a material used only inside one is free once the group is gone
    for coll in (bpy.data.meshes, bpy.data.lights, bpy.data.particles, bpy.data.node_groups,
                 bpy.data.materials, bpy.data.textures):
        for db in list(coll):
            if (db.name == prefix or db.name.startswith(prefix + "_")) and db.users == 0:
                coll.remove(db)


# -- per-effect builders ----------------------------------------------------

def _build_fire(fx: _FX) -> int:
    s, col = fx.size, fx.color
    body = fx.child(_cone(fx.prefix + "_flame", r1=s * 0.5, r2=s * 0.03, depth=s * 1.8),
                    local=(0, 0, s * 0.9))
    stops = [(0.0, (0.0, 0.0, 0.0)), (0.22, (0.55, 0.03, 0.0)),
             (0.5, (col[0], col[1] * 0.55, col[2] * 0.3)), (0.72, (1.0, 0.5, 0.06)),
             (1.0, (1.0, 0.82, 0.32))]
    for poly in body.data.polygons:
        poly.use_smooth = True
    _assign(body, _noise_emission_mat(fx.prefix + "_flame", stops, 1.9 * fx.intensity,
                                      scale=3.5 / s, scroll=-0.08 * s, detail=6.0, soft=True, fade_height=s * 1.8))
    _, data = fx.light("glow", (1.0, 0.5, 0.15), 60.0 * s * s * fx.intensity, local=(0, 0, s * 0.8))
    _drive(data, "energy", -1, "%f*(0.75+0.25*sin(frame*1.3))" % (60.0 * s * s * fx.intensity))
    return fx.start + _DURATION["fire"] // 2


def _build_smoke(fx: _FX) -> int:
    s = fx.size
    dom = fx.child(_box(fx.prefix + "_column", s * 1.2, s * 1.2, s * 3.0), local=(0, 0, s * 1.5))
    _assign(dom, _smoke_volume_mat(fx.prefix + "_smoke", fx.color, 3.0 * fx.intensity, scroll=-0.06))
    return fx.start + _DURATION["smoke"] // 2


def _build_explosion(fx: _FX) -> int:
    s, col = fx.size, fx.color
    ball = fx.child(_icosphere(fx.prefix + "_ball", s * 0.5, subdiv=4), local=(0, 0, s * 0.6))
    mat = _noise_emission_mat(fx.prefix + "_ball",
                              [(0.0, (0.15, 0.0, 0.0)), (0.35, (0.8, 0.08, 0.0)),
                               (0.6, (col[0], col[1] * 0.6, col[2] * 0.3)), (0.85, (1.0, 0.55, 0.1)),
                               (1.0, (1.0, 0.85, 0.4))],
                              2.5 * fx.intensity, scale=2.5, scroll=-0.05, detail=6.0, soft=True)
    _assign(ball, mat)
    emit = mat.node_tree.nodes["Emission"]
    strength = emit.inputs["Strength"]
    f0 = fx.start
    opacity = mat.node_tree.nodes["Opacity"].outputs[0]  # fade to clear, not to black: sprite sheets need alpha
    for frame, value in ((f0, 1.0), (f0 + 7, 1.0), (f0 + 22, 0.45), (f0 + _DURATION["explosion"] - 2, 0.0)):
        opacity.default_value = value
        opacity.keyframe_insert("default_value", frame=frame)
    for frame, scale, stg, hide in ((f0, 0.15, 0.3, False), (f0 + 2, 1.3, 5.0 * fx.intensity, False),
                                    (f0 + 7, 1.75, 2.8 * fx.intensity, False),
                                    (f0 + 22, 2.2, 0.25 * fx.intensity, False),
                                    (f0 + _DURATION["explosion"] - 2, 2.4, 0.0, True)):
        ball.scale = (scale, scale, scale)
        ball.keyframe_insert("scale", frame=frame)
        ball.hide_render = hide
        ball.hide_viewport = hide
        ball.keyframe_insert("hide_render", frame=frame)
        ball.keyframe_insert("hide_viewport", frame=frame)
        strength.default_value = stg
        strength.keyframe_insert("default_value", frame=frame)
    _, data = fx.light("flash", (1.0, 0.6, 0.25), 0.0, local=(0, 0, s * 0.6))
    for frame, energy in ((f0, 0.0), (f0 + 2, 400.0 * s * s * fx.intensity), (f0 + 10, 0.0)):
        data.energy = energy
        data.keyframe_insert("energy", frame=frame)
    return f0 + 7


def _grain(size: float) -> float:
    """How big one spark, drop or flake is in an effect of this size. Up to a metre it grows with the
    effect; past that only slowly: a storm over a street is not made of drops as long as a door."""
    return size if size <= 1.0 else size ** 0.4


def _build_sparks(fx: _FX) -> int:
    s = fx.size
    src = fx.child(_uvsphere(fx.prefix + "_spark_src", _grain(s) * 0.05, 8, 4))
    src.hide_render = src.hide_viewport = True
    emitter = fx.child(_icosphere(fx.prefix + "_emitter", s * 0.08, subdiv=1), local=(0, 0, s * 0.1))
    ps = fx.particles(emitter, "sparks")
    ps.type, ps.count = 'EMITTER', int(220 * fx.intensity)
    ps.frame_start, ps.frame_end = fx.start, fx.start + 2
    ps.lifetime, ps.lifetime_random = _DURATION["sparks"] - 4, 0.4
    ps.normal_factor, ps.factor_random = 4.0 * s, 3.0 * s
    ps.render_type, ps.instance_object = 'OBJECT', src
    ps.particle_size, ps.size_random = 1.0, 0.6
    ps.effector_weights.gravity = 1.0
    _assign(src, _emission_mat(fx.prefix + "_spark", fx.color, 4.0 * fx.intensity))
    fx.bounds(s * 6, local=(0, 0, s * 1.2))
    return fx.start + 20


def _build_magic_swirl(fx: _FX) -> int:
    s, col = fx.size, fx.color
    ring = fx.child(_torus(fx.prefix + "_ring", major=s * 0.5, minor=s * 0.05))
    mat = _noise_emission_mat(fx.prefix + "_glow",
                              [(0.0, (col[0] * 0.2, col[1] * 0.2, col[2] * 0.3)),
                               (0.5, (col[0] * 0.8, col[1] * 0.7, col[2])), (1.0, (col[0], col[1] * 0.9, 1.0))],
                              1.8 * fx.intensity, scale=4.0, scroll=-0.15)
    _assign(ring, mat)
    orb = fx.child(_uvsphere(fx.prefix + "_orb", s * 0.07, 16, 10), local=(s * 0.5, 0, s * 0.1))
    _assign(orb, _emission_mat(fx.prefix + "_orb", (min(1.0, col[0] + 0.2), col[1] * 0.8, col[2]),
                               2.8 * fx.intensity))
    _drive(fx.root, "rotation_euler", 2, "frame*0.12")
    _, data = fx.light("glow", col, 40.0 * s * s * fx.intensity, local=(0, 0, s * 0.3))
    return fx.start + _DURATION["magic_swirl"] // 2


def _build_shockwave(fx: _FX) -> int:
    s, col = fx.size, fx.color
    ring = fx.child(_torus(fx.prefix + "_wave", major=1.0, minor=0.05), local=(0, 0, s * 0.05))
    mat = _emission_mat(fx.prefix + "_wave", col, 2.5 * fx.intensity)
    _assign(ring, mat)
    emit = mat.node_tree.nodes["Emission"].inputs["Strength"]
    f0, dur = fx.start, _DURATION["shockwave"]
    for frame, scale, stg, hide in ((f0, 0.05 * s, 3.0 * fx.intensity, False),
                                    (f0 + int(dur * 0.6), s, 1.5 * fx.intensity, False),
                                    (f0 + dur - 2, s * 1.3, 0.0, True)):
        ring.scale = (scale, scale, max(scale, s * 0.1))
        ring.keyframe_insert("scale", frame=frame)
        ring.hide_render = ring.hide_viewport = hide
        ring.keyframe_insert("hide_render", frame=frame)
        ring.keyframe_insert("hide_viewport", frame=frame)
        emit.default_value = stg
        emit.keyframe_insert("default_value", frame=frame)
    return f0 + int(dur * 0.45)


def _build_energy_shield(fx: _FX) -> int:
    s, col = fx.size, fx.color
    sphere = fx.child(_icosphere(fx.prefix + "_shield", s * 0.5, subdiv=4), local=(0, 0, s * 0.5))
    mat = bpy.data.materials.new(fx.prefix + "_shield")
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    fres = nt.nodes.new("ShaderNodeFresnel")
    mul = nt.nodes.new("ShaderNodeMath")
    mul.operation = 'MULTIPLY'
    pulse = nt.nodes.new("ShaderNodeValue")
    fres.inputs["IOR"].default_value = 1.4
    emit.inputs["Color"].default_value = (col[0], col[1], col[2], 1.0)
    nt.links.new(fres.outputs["Fac"], mul.inputs[0])
    nt.links.new(pulse.outputs["Value"], mul.inputs[1])
    nt.links.new(mul.outputs["Value"], emit.inputs["Strength"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    _drive(pulse.outputs["Value"], "default_value", -1,
           "%f*(1.5+0.8*sin(frame*0.25))" % fx.intensity)
    mat.diffuse_color = (col[0], col[1], col[2], 1.0)
    _assign(sphere, mat)
    _, _d = fx.light("glow", col, 30.0 * s * s * fx.intensity, local=(0, 0, s * 0.5))
    return fx.start + _DURATION["energy_shield"] // 2


def _build_rain(fx: _FX) -> int:
    s = fx.size
    drop = fx.child(_box(fx.prefix + "_drop_src", _grain(s) * 0.004, _grain(s) * 0.004, _grain(s) * 0.12))
    drop.hide_render = drop.hide_viewport = True
    emitter = fx.child(_plane(fx.prefix + "_cloud", s, s), local=(0, 0, s * 2.5))
    ps = fx.particles(emitter, "rain")
    ps.type, ps.count = 'EMITTER', int(1500 * fx.intensity)
    ps.frame_start, ps.frame_end, ps.lifetime = fx.start, fx.end, 40
    ps.normal_factor, ps.factor_random = -6.0 * s, 0.2 * s
    ps.render_type, ps.instance_object = 'OBJECT', drop
    ps.particle_size, ps.size_random = 1.0, 0.2
    ps.effector_weights.gravity = 1.0
    _assign(drop, _emission_mat(fx.prefix + "_drop", fx.color, 1.0 * fx.intensity))
    return min(fx.end, fx.start + 45)


def _build_snow(fx: _FX) -> int:
    s = fx.size
    flake = fx.child(_uvsphere(fx.prefix + "_flake_src", _grain(s) * 0.02, 8, 4))
    flake.hide_render = flake.hide_viewport = True
    emitter = fx.child(_plane(fx.prefix + "_cloud", s, s), local=(0, 0, s * 2.5))
    ps = fx.particles(emitter, "snow")
    ps.type, ps.count = 'EMITTER', int(1200 * fx.intensity)
    ps.frame_start, ps.frame_end, ps.lifetime = fx.start, fx.end, 110
    ps.normal_factor, ps.factor_random = -0.6 * s, 0.2 * s
    ps.render_type, ps.instance_object = 'OBJECT', flake
    ps.particle_size, ps.size_random = 1.0, 0.5
    ps.effector_weights.gravity, ps.brownian_factor = 0.08, 0.4 * s
    _assign(flake, _emission_mat(fx.prefix + "_flake", fx.color, 1.2 * fx.intensity))
    return min(fx.end, fx.start + 60)


def _build_dust_burst(fx: _FX) -> int:
    s = fx.size
    puff = fx.child(_icosphere(fx.prefix + "_puff_src", s * 0.16, subdiv=2))
    puff.hide_render = puff.hide_viewport = True
    emitter = fx.child(_icosphere(fx.prefix + "_emitter", s * 0.1, subdiv=1), local=(0, 0, s * 0.05))
    ps = fx.particles(emitter, "dust")
    ps.type, ps.count = 'EMITTER', int(120 * fx.intensity)
    ps.frame_start, ps.frame_end = fx.start, fx.start + 3
    ps.lifetime, ps.lifetime_random = _DURATION["dust_burst"] - 4, 0.3
    ps.normal_factor, ps.factor_random = 2.5 * s, 2.0 * s
    ps.render_type, ps.instance_object = 'OBJECT', puff
    ps.particle_size, ps.size_random = 1.0, 0.6
    ps.effector_weights.gravity = 0.15
    # Dust is lit, not emissive: a touch of emission keeps it readable without a scene light,
    # and a soft fill light gives it form.
    mat = bpy.data.materials.new(fx.prefix + "_dust")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes.get("Principled BSDF")
    if bsdf is not None:
        bsdf.inputs["Base Color"].default_value = (*fx.color, 1.0)
        bsdf.inputs["Roughness"].default_value = 1.0
        bsdf.inputs["Emission Color"].default_value = (*fx.color, 1.0)
        bsdf.inputs["Emission Strength"].default_value = 0.35 * fx.intensity
    mat.diffuse_color = (*fx.color, 1.0)
    _assign(puff, mat)
    fx.light("fill", (1.0, 0.96, 0.9), 120.0 * s * s * fx.intensity, local=(s * 1.5, -s * 1.5, s * 2.0), soft=s)
    fx.bounds(s * 3.5, local=(0, 0, s * 0.9))
    return fx.start + 16


# -- simulated effects (fire, explosion) --------------------------------------

def _sim_material(name: str, tint, natural: bool, flame_strength: float, smoke_density: float, smoke_color) -> bpy.types.Material:
    """Volume shader for a gas simulation: glow from the simulated ``flame`` grid through a
    fire colour ramp, and absorbing smoke from the ``density`` grid."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    flame = nt.nodes.new("ShaderNodeAttribute")
    flame.attribute_name = "flame"
    density = nt.nodes.new("ShaderNodeAttribute")
    density.attribute_name = "density"
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    if natural:
        stops = [(0.0, (0.0, 0.0, 0.0)), (0.1, (0.3, 0.008, 0.0)), (0.35, (1.0, 0.1, 0.0)),
                 (0.7, (1.0, 0.33, 0.015)), (1.0, (1.0, 0.72, 0.22))]
    else:
        hot = tuple(c + (1.0 - c) * 0.6 for c in tint)
        stops = [(0.0, (0.0, 0.0, 0.0)), (0.1, tuple(c * 0.25 for c in tint)), (0.4, tuple(tint)),
                 (0.75, tuple(c + (1.0 - c) * 0.3 for c in tint)), (1.0, hot)]
    el = ramp.color_ramp.elements
    el[0].position, el[0].color = stops[0][0], (*stops[0][1], 1.0)
    el[1].position, el[1].color = stops[-1][0], (*stops[-1][1], 1.0)
    for pos, rgb in stops[1:-1]:
        ramp.color_ramp.elements.new(pos).color = (*rgb, 1.0)
    power = nt.nodes.new("ShaderNodeMath")       # keeps the thin outer flame dim and the core bright
    power.operation = 'POWER'
    power.inputs[1].default_value = 1.6
    strength = nt.nodes.new("ShaderNodeMath")
    strength.operation = 'MULTIPLY'
    strength.name = strength.label = "Flame Strength"
    strength.inputs[1].default_value = flame_strength
    thick = nt.nodes.new("ShaderNodeMath")
    thick.operation = 'MULTIPLY'
    thick.name = thick.label = "Smoke Density"
    thick.inputs[1].default_value = smoke_density
    nt.links.new(flame.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(flame.outputs["Fac"], power.inputs[0])
    nt.links.new(power.outputs[0], strength.inputs[0])
    nt.links.new(density.outputs["Fac"], thick.inputs[0])
    nt.links.new(ramp.outputs["Color"], vol.inputs["Emission Color"])
    nt.links.new(strength.outputs[0], vol.inputs["Emission Strength"])
    nt.links.new(thick.outputs[0], vol.inputs["Density"])
    vol.inputs["Color"].default_value = (*smoke_color, 1.0)
    vol.inputs["Blackbody Intensity"].default_value = 0.0
    nt.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    mat.diffuse_color = (*stops[-2][1], 1.0)
    return mat


def _sim_setup(fx: _FX, kind: str) -> bpy.types.Object:
    """A real gas simulation: an emitter and a domain around it, ready to bake. Returns the domain."""
    s, power = fx.size, fx.intensity
    scene = bpy.context.scene
    first, last = fx.start - fx.warm, fx.end
    if kind == "explosion":
        emitter = fx.child(_icosphere(fx.prefix + "_source", s * 0.42, subdiv=2), local=(0, 0, s * 0.45))
        box, centre = (s * 3.2, s * 3.2, s * 3.6), (0, 0, s * 1.8)
    else:
        emitter = fx.child(_icosphere(fx.prefix + "_source", s * 0.22, subdiv=2), local=(0, 0, s * 0.08))
        emitter.scale = (1.0, 1.0, 0.35)
        box, centre = (s * 1.5, s * 1.5, s * 2.5), (0, 0, s * 1.25)
    emitter.hide_render = True
    emitter.display_type = 'WIRE'
    flow = emitter.modifiers.new("Fluid", 'FLUID')
    flow.fluid_type = 'FLOW'
    fs = flow.flow_settings
    fs.flow_type = 'BOTH'
    amount = min(max(power, 0.3), 3.0) ** 0.5
    if kind == "explosion":
        # fuel for the first frames only, thrown outward: a fireball that rises and turns to smoke
        fs.flow_behavior = 'INFLOW'
        fs.use_initial_velocity = True
        fs.velocity_normal = 5.0
        fs.temperature = 3.0
        fs.fuel_amount, fs.density = 2.5 * amount, 1.0
        for frame, on in ((first, True), (first + 6, True), (first + 7, False)):
            fs.use_inflow = on
            fs.keyframe_insert("use_inflow", frame=frame)
    else:
        fs.flow_behavior = 'INFLOW'
        fs.fuel_amount = 1.6 * amount
        fs.density = 0.25
        fs.temperature = 2.0
        # a drifting noise mask breaks the source into separate licks of flame
        noise = bpy.data.textures.new(fx.prefix + "_noise", 'CLOUDS')
        noise.noise_scale = s * 0.18
        fs.use_texture, fs.noise_texture, fs.texture_map_type = True, noise, 'AUTO'
        for frame, offset in ((first, 0.0), (last, (last - first) * 0.05)):
            fs.texture_offset = offset
            fs.keyframe_insert("texture_offset", frame=frame)

    domain = fx.child(_box(fx.prefix + "_domain", *box), local=centre)
    for poly in domain.data.polygons:
        poly.use_smooth = False
    mod = domain.modifiers.new("Fluid", 'FLUID')
    mod.fluid_type = 'DOMAIN'
    ds = mod.domain_settings
    ds.domain_type = 'GAS'
    ds.resolution_max = fx.detail
    ds.use_adaptive_domain = False
    for side in ("front", "back", "left", "right", "top"):     # open walls: gas leaves instead of filling the box
        setattr(ds, "use_collision_border_" + side, False)
    if kind == "fire":
        ds.vorticity, ds.alpha, ds.beta = 0.35, 0.6, 2.2
        ds.burning_rate, ds.flame_smoke, ds.flame_vorticity = 0.5, 1.0, 1.4
        ds.use_dissolve_smoke, ds.dissolve_speed = True, 40
    else:
        ds.vorticity, ds.alpha, ds.beta = 0.5, 0.4, 2.6
        ds.burning_rate, ds.flame_smoke, ds.flame_vorticity = 0.16, 2.0, 1.0
        ds.use_dissolve_smoke, ds.dissolve_speed = True, 45
    # One folder per effect ever built: two files (or two open windows) that both have a
    # "VFX_fire" must never write into each other's cache.
    cache = os.path.join(data_dir("vfx_cache"), f"{fx.prefix}_{uuid.uuid4().hex[:8]}")
    ds.cache_type = 'ALL'
    ds.cache_resumable = True          # baked a few frames at a time (see _sim_bake)
    ds.cache_directory = cache
    ds.cache_frame_start, ds.cache_frame_end = first, last

    smoke_color = (0.05, 0.05, 0.055) if kind == "fire" else (0.2, 0.19, 0.18)
    _assign(domain, _sim_material(fx.prefix + "_gas", fx.color, natural=not fx.tinted, flame_strength=7.0 * power,
                                  smoke_density=9.0, smoke_color=smoke_color))

    if kind == "fire":
        base = 60.0 * s * s * power
        _, light = fx.light("glow", (1.0, 0.5, 0.15), base, local=(0, 0, s * 0.6), soft=s * 0.3)
        _drive(light, "energy", -1, "%f*(0.75+0.18*sin(frame*1.3)+0.07*sin(frame*3.1))" % base)
    else:
        _, light = fx.light("flash", (1.0, 0.6, 0.25), 0.0, local=(0, 0, s * 0.6), soft=s * 0.4)
        for frame, energy in ((first, 0.0), (first + 3, 500.0 * s * s * power), (first + 16, 60.0 * s * s * power),
                              (first + 34, 0.0)):
            light.energy = energy
            light.keyframe_insert("energy", frame=frame)

    if scene.frame_end < last:
        scene.frame_end = last
    view_layer = bpy.context.view_layer
    for obj in view_layer.objects:
        obj.select_set(False)
    domain.select_set(True)
    view_layer.objects.active = domain
    view_layer.update()
    fx.sim = {"resolution": fx.detail, "frames_baked": last - first + 1, "cache": cache}
    return domain


def _sim_bake(domain: bpy.types.Object, upto: int) -> None:
    """Bake the simulation up to a frame, continuing from wherever the last call stopped.

    Baking the whole range in one go freezes Blender for minutes and Windows greys the window
    out as "Not Responding". Baking a second's worth at a time keeps it alive, and the user
    watches the fire grow. (Blender's own background bake job is not used: started from a
    timer it sometimes ran through every frame without simulating anything.)"""
    settings = next(m for m in domain.modifiers if m.type == 'FLUID').domain_settings
    settings.cache_frame_end = upto
    view_layer = bpy.context.view_layer
    if view_layer.objects.active != domain:
        for obj in view_layer.objects:
            obj.select_set(False)
        domain.select_set(True)
        view_layer.objects.active = domain
    try:
        with ui_context():
            bpy.ops.fluid.bake_all('EXEC_DEFAULT')
    except RuntimeError as exc:
        raise ToolError(f"The simulation could not be baked ({str(exc).strip()[:200]}). Try quality='fast'.")


def _sim_finish(fx: _FX, kind: str, domain: bpy.types.Object, began: float) -> int:
    """After the bake: check the cache, sharpen the scene's volume settings, pick the preview frame."""
    scene = bpy.context.scene
    domain.select_set(False)
    files = sum(len(names) for _root, _dirs, names in os.walk(fx.sim["cache"]))
    if not files:
        raise ToolError("The simulation produced no cache files. Try quality='fast'.")
    fx.sim["bake_seconds"] = round(time.time() - began, 1)
    changed = _sharp_volumes(scene)
    if changed:
        fx.sim["scene_settings_changed"] = changed
    return fx.start + (10 if kind == "explosion" else (fx.end - fx.start) // 2)


def _sharp_volumes(scene: bpy.types.Scene, shadows: bool = False) -> Dict[str, Any]:
    """Volumes are blurry at EEVEE's default settings: raise the ones that are lower. ``shadows`` also
    lets gas shade itself, which is what gives smoke lit by a sun its form. Returns what changed."""
    changed = {}
    eevee = scene.eevee
    wanted = [("volumetric_tile_size", '2'), ("volumetric_samples", 128)]
    if shadows:
        wanted.append(("use_volumetric_shadows", True))
    for attr, value in wanted:
        try:
            old = getattr(eevee, attr)
            better = int(old) > int(value) if attr == "volumetric_tile_size" else int(old) < int(value)
            if better:
                setattr(eevee, attr, value)
                changed[attr] = [old, value]
        except (AttributeError, TypeError, ValueError):
            pass
    return changed


_BUILDERS = {
    "fire": _build_fire, "smoke": _build_smoke, "explosion": _build_explosion,
    "sparks": _build_sparks, "magic_swirl": _build_magic_swirl, "shockwave": _build_shockwave,
    "energy_shield": _build_energy_shield, "rain": _build_rain, "snow": _build_snow,
    "dust_burst": _build_dust_burst,
}


# -- vfx_create -------------------------------------------------------------

@tool(
    "vfx_create",
    """Build a ready-to-render, animated visual effect from a preset in one call: fire, smoke,
    explosion, sparks, magic_swirl, shockwave, energy_shield, rain, snow or dust_burst. Fire
    and explosion come in two qualities: 'realistic' (default) runs a real gas simulation and
    bakes it, which takes about a minute (the user watches it grow in the viewport), and gives
    turbulent, natural flames; 'fast' is an instant procedural stand-in that looks stylised. The other effects are always instant. Everything is animated: tell the user to
    press Play on the Unix island to watch it (it starts by itself when your turn ends). The
    effect lives under an empty VFX_<name> in the Unix_VFX collection; call again with the same
    name to replace it. The scene frame is left on a representative mid-effect frame, so follow
    with look(shading='rendered').""",
    {
        "effect": {"type": "string", "enum": EFFECTS, "description": "Which preset to build."},
        "name": {"type": "string", "description": "Effect name (root is VFX_<name>). Default the effect name."},
        "location": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] world origin of the effect. Default origin."},
        "size_m": {"type": "number", "description": "Overall size in meters (default 1.0)."},
        "color": {"type": "array", "items": {"type": "number"}, "description": "Optional [r, g, b] linear 0-1 tint."},
        "start_frame": {"type": "integer", "description": "First frame. Default the scene's current frame."},
        "duration_frames": {"type": "integer", "description": "Length in frames. Sensible default per effect."},
        "intensity": {"type": "number", "description": "Brightness/count multiplier (default 1.0)."},
        "quality": {"type": "string", "enum": ["realistic", "fast"],
                    "description": "fire and explosion only. 'realistic' (default) = simulated and baked; 'fast' = instant, stylised."},
        "detail": {"type": "integer", "description": "Simulation resolution, 32-160 (default about 70). Bake time grows with its cube: "
                                                     "64 is quick, 96 is sharp and takes a few minutes."},
    },
    ["effect"],
    timeout=3600.0,
    mutates=True,
    label="Built a VFX preset",
)
def vfx_create(effect: str, name: str = "", location: Optional[List[float]] = None, size_m: float = 1.0,
               color: Optional[List[float]] = None, start_frame: Optional[int] = None,
               duration_frames: Optional[int] = None, intensity: float = 1.0, quality: str = "realistic",
               detail: Optional[int] = None) -> Dict[str, Any]:
    if effect not in _BUILDERS:
        raise ToolError(f"Unknown effect '{effect}'. Choose one of: {', '.join(EFFECTS)}.")
    name = (name or effect).strip()
    if not name or any(c in name for c in r'\/:*?"<>|'):
        raise ToolError("name must be a simple label with no path separators.")
    location = location or [0.0, 0.0, 0.0]
    if len(location) != 3:
        raise ToolError("location must be [x, y, z].")
    if not (0.01 <= float(size_m) <= 1000.0):
        raise ToolError("size_m must be between 0.01 and 1000 meters.")
    if color is not None and len(color) != 3:
        raise ToolError("color must be [r, g, b] in 0-1.")
    if not (0.01 <= float(intensity) <= 50.0):
        raise ToolError("intensity must be between 0.01 and 50.")
    if quality not in ("realistic", "fast"):
        raise ToolError("quality must be 'realistic' or 'fast'.")
    simulate = quality == "realistic" and effect in SIMULATED
    if detail is not None and not (32 <= int(detail) <= 160):
        raise ToolError("detail must be between 32 and 160.")
    ensure_object_mode()
    scene = bpy.context.scene
    start = int(start_frame) if start_frame is not None else int(scene.frame_current)
    duration = int(duration_frames) if duration_frames is not None else (_SIM_DURATION if simulate else _DURATION)[effect]
    if not (2 <= duration <= (600 if simulate else 100000)):
        raise ToolError(f"duration_frames must be between 2 and {600 if simulate else 100000}.")
    warm = _SIM_WARM[effect] if simulate else 0
    start += warm                     # the effect's own range begins once the simulation has developed
    end = start + duration - 1

    _turn.mark_mutation()
    _remove_existing(name)
    col = color if color is None else [float(c) for c in color]
    fx = _FX(name, location, size_m, col if col is not None else _COLOR[effect], intensity, start, end,
             tinted=col is not None, detail=int(detail) if detail is not None else _SIM_DETAIL.get(effect, 64), warm=warm)
    def guarded(step):
        try:
            return step()
        except Exception:
            _util.status["text"] = ""
            _remove_existing(name)        # do not leave half an effect behind
            raise

    if not simulate:
        return _finish_effect(fx, effect, guarded(lambda: _BUILDERS[effect](fx)), simulate)
    domain = guarded(lambda: _sim_setup(fx, effect))
    began = time.time()
    first, last = start - warm, end
    progress = {"done": first - 1, "chunk": 1}

    def poll():
        def step():
            target = min(last, progress["done"] + progress["chunk"])
            clock = time.time()
            _sim_bake(domain, target)
            spent, frames = time.time() - clock, target - progress["done"]
            progress["done"] = target
            # Every call has a fixed start-up cost (it reloads the last frame), so more frames per
            # call is faster, but a call longer than about five seconds makes Windows grey the
            # window out as "Not Responding". Steer each call toward 2.5 seconds, never more than
            # doubling: frames get slower as the fire fills the domain.
            progress["chunk"] = max(1, min(12, frames * 2, int(frames * 2.5 / max(spent, 0.05))))
            if target < last:
                _util.status["text"] = f"Simulating {effect}  {round(100 * (target - first + 1) / (last - first + 1))}%"
                return None
            _util.status["text"] = ""
            return _finish_effect(fx, effect, _sim_finish(fx, effect, domain, began), simulate)
        return guarded(step)

    def cancel():
        _util.status["text"] = ""
        _remove_existing(name)            # half a fire nobody is waiting for

    _util.status["text"] = f"Simulating {effect}  0%"
    return Pending(poll, every=0.05, cancel=cancel)


def _finish_effect(fx: _FX, effect: str, preview: int, simulate: bool) -> Dict[str, Any]:
    scene = bpy.context.scene
    start, end = fx.start, fx.end
    preview = max(start, min(preview, end))

    loop = _LOOP[effect]
    fx.root["unix_start"] = start
    fx.root["unix_end"] = end
    fx.root["unix_loop"] = loop
    fx.root["unix_effect"] = effect
    fx.root["unix_size"] = fx.size                 # read by the Roblox link to rebuild the effect natively
    if fx.tinted:
        fx.root["unix_color"] = [float(c) for c in fx.color]
    if scene.frame_end < end:
        scene.frame_end = end
    # Legacy particle systems simulate incrementally, so walk the frames in order up to the
    # preview to fill the point cache; a direct jump would leave them under-simulated.
    if effect in ("sparks", "rain", "snow", "dust_burst"):
        for f in range(start, preview + 1):
            scene.frame_set(f)
    else:
        scene.frame_set(preview)
    bpy.context.view_layer.update()
    out: Dict[str, Any] = {
        "root": fx.root.name,
        "effect": effect,
        "quality": "realistic (simulated)" if simulate else "fast (procedural)",
        "objects": fx.objects,
        "collection": VFX_COLLECTION,
        "frame_range": [start, end],
        "preview_frame": preview,
        "loops": loop,
        "tip": _TIP[effect],
        "note": "Scene is on the preview frame. Run look(shading='rendered') to see it; "
                "re-run vfx_create with the same name to adjust. It is animated: the user presses Play on the "
                "island to watch it, and it starts playing by itself when your turn ends.",
    }
    if simulate:
        out["simulation"] = fx.sim
        out["tip"] = ("Raise detail (96) for sharper flames; intensity brightens the glow; size_m scales the whole fire. "
                      "The cache is on this computer: sending the .blend elsewhere needs a re-bake.")
    elif effect in SIMULATED:
        out["tip"] += " This is the stylised stand-in; quality='realistic' simulates the real thing."
    return out


# -- vfx_flipbook -----------------------------------------------------------

def _family(obj: bpy.types.Object) -> List[bpy.types.Object]:
    return [obj] + list(obj.children_recursive)


def _range_bounds(family, frames_to_sample, scene):
    """World-space bounds of the renderable family accumulated across the sampled frames."""
    renderable = [o for o in family if o.type in _RENDERABLE]
    lo = Vector((1e18, 1e18, 1e18))
    hi = Vector((-1e18, -1e18, -1e18))
    found = False
    for f in frames_to_sample:
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        for obj in renderable:
            ev = obj.evaluated_get(dg)
            for corner in ev.bound_box:
                p = ev.matrix_world @ Vector(corner)
                lo = Vector((min(lo.x, p.x), min(lo.y, p.y), min(lo.z, p.z)))
                hi = Vector((max(hi.x, p.x), max(hi.y, p.y), max(hi.z, p.z)))
                found = True
    if not found:
        # fall back to the root location so a particle effect with no live frame still frames
        c = family[0].matrix_world.translation
        lo, hi = c - Vector((1, 1, 1)), c + Vector((1, 1, 1))
    return lo, hi


_VIEW_DIR = {"front": Vector((0, -1, 0)), "side": Vector((1, 0, 0)), "top": Vector((0, 0, 1))}


def _as_shown(scene: bpy.types.Scene):
    """A function that turns scene-linear colour (an N x 3 float array, changed in place) into the
    colour the user sees on screen, with the scene's own view transform, exposure and gamma."""
    import numpy as np
    view = scene.view_settings
    gain, gamma = 2.0 ** float(view.exposure), max(float(view.gamma), 1e-3)
    convert = None
    try:
        import PyOpenColorIO as ocio
        path = os.environ.get("OCIO") or os.path.join(bpy.utils.system_resource('DATAFILES'), "colormanagement", "config.ocio")
        config = ocio.Config.CreateFromFile(path)
        transform = ocio.DisplayViewTransform(src=ocio.ROLE_SCENE_LINEAR, display=scene.display_settings.display_device,
                                              view=view.view_transform)
        convert = config.getProcessor(transform).getDefaultCPUProcessor()
    except Exception:
        convert = None                      # no colour library: plain sRGB, which is close for all but the brightest

    def shown(colour):
        colour *= gain
        if convert is not None:
            convert.applyRGB(colour)
        else:
            np.clip(colour, 0.0, 1.0, out=colour)
            colour[:] = np.where(colour <= 0.0031308, colour * 12.92, 1.055 * colour ** (1 / 2.4) - 0.055)
        np.clip(colour, 0.0, 1.0, out=colour)
        if abs(gamma - 1.0) > 1e-4:
            colour **= 1.0 / gamma
        return colour

    return shown


class Film:
    """An effect's frames rendered one by one, each a square RGBA picture of the effect alone
    against nothing (rows top to bottom, straight alpha).

    The colours are the ones on the user's screen. Blender's own way of writing a see-through
    picture divides the light by the opacity before it applies the view transform, so a flame
    (much light, little opacity) comes out washed to pale. Here the float render is read, the
    view transform is applied to the light itself, and the opacity is raised to at least the
    brightness: laid over anything with ordinary alpha blending the picture then adds exactly
    the colour that was rendered. ``step`` renders the next frame and
    returns True when all are in ``cells``; nothing waits inside it, so a caller on a timer keeps
    Blender alive between frames. Everything else in the scene is hidden while it runs and every
    setting is put back by ``close`` (call it whatever happens). ``centre``, ``width`` and
    ``height`` say what part of the world a cell shows, in metres about ``centre``.

    Plain, a cell is a square round the effect's bounding box. With ``fit`` the effect is first
    looked at quickly and small, and each cell is then framed on what was actually seen: a tall
    thin column fills the whole cell (squeezed sideways: ``width`` and ``height`` differ, by at
    most ``fit`` times) instead of standing as a sliver in an empty square."""

    def __init__(self, obj: bpy.types.Object, frames: List[int], cell_px: int, view: str = "front", fit: float = 0.0):
        from .vision import _Restore
        if bpy.app.is_job_running('RENDER'):
            raise ToolError("A render is already running; try again when it finishes.")
        scene = bpy.context.scene
        self.scene, self.frames, self.cell_px = scene, list(frames), int(cell_px)
        self.cells: List[Any] = []
        self.centre, self.width, self.height = obj.matrix_world.translation.copy(), 1.0, 1.0
        self._restore = _Restore()
        self._saved_frame = scene.frame_current
        self._camera: Optional[bpy.types.Object] = None
        self._files: List[str] = []
        self._stamp = time.strftime("%H%M%S") + uuid.uuid4().hex[:4]
        self._open = True
        family = _family(obj)
        keep = {o.name for o in family}
        render, restore = scene.render, self._restore
        try:
            for other in scene.objects:          # the effect alone
                if other.name not in keep and other.type in _RENDERABLE and not other.hide_render:
                    restore.set(other, "hide_render", True)
            restore.set(render, "engine", eevee_engine_id())
            restore.set(scene.eevee, "taa_render_samples", 24)
            restore.set(render, "resolution_x", self.cell_px)
            restore.set(render, "resolution_y", self.cell_px)
            restore.set(render, "resolution_percentage", 100)
            restore.set(render, "film_transparent", True)
            restore.set(render, "use_file_extension", True)
            restore.set(render, "filepath", render.filepath)
            restore.set(render.image_settings, "file_format", 'OPEN_EXR')
            restore.set(render.image_settings, "color_mode", 'RGBA')
            restore.set(render.image_settings, "color_depth", '16')
            restore.set(scene, "camera", scene.camera)
            self._shown = _as_shown(scene)
            if view != "camera":
                lo, hi = _range_bounds(family, self.frames, scene)
                centre = (lo + hi) / 2
                extent = max((hi - lo).x, (hi - lo).y, (hi - lo).z, 0.01)
                direction = _VIEW_DIR[view].normalized()
                cam_data = bpy.data.cameras.new("_vfx_flip")
                cam_data.type = 'ORTHO'
                cam_data.ortho_scale = extent * 1.15
                cam_data.clip_start = 0.001
                cam_data.clip_end = extent * 20 + 1000.0
                self._camera = bpy.data.objects.new("_vfx_flip", cam_data)
                scene.collection.objects.link(self._camera)
                self._camera.location = centre + direction * (extent * 4 + 1.0)
                self._camera.rotation_euler = direction.to_track_quat('Z', 'Y').to_euler()
                scene.camera = self._camera
                self.centre, self.width, self.height = centre, extent * 1.15, extent * 1.15
                bpy.context.view_layer.update()
                if fit > 1.0:
                    self._fit(float(fit), direction)
        except Exception:
            self.close()
            raise

    def _shot(self, frame: int, label: str) -> Any:
        """One frame through the film's camera, as rows top to bottom."""
        import numpy as np
        scene = self.scene
        scene.frame_set(frame)
        cell_path = os.path.join(data_dir("flipbooks", "_cells"), f"cell_{self._stamp}_{label}.exr")
        scene.render.filepath = cell_path
        bpy.ops.render.render(write_still=True)
        self._files.append(cell_path)
        img = bpy.data.images.load(cell_path)
        try:
            w, h = img.size
            buf = np.empty(w * h * 4, dtype=np.float32)
            img.pixels.foreach_get(buf)
        finally:
            bpy.data.images.remove(img)
        light = np.ascontiguousarray(np.nan_to_num(buf.reshape(h * w, 4)[:, :3]).clip(0.0, None))     # over black, scene-linear
        colour = self._shown(light)
        cover = np.clip(np.maximum(buf.reshape(h * w, 4)[:, 3], colour.max(axis=1)), 0.0, 1.0)
        cell = np.concatenate([colour / np.maximum(cover, 1e-6)[:, None], cover[:, None]], axis=1)
        cell[cover <= 0.0] = 0.0
        return np.clip(cell, 0.0, 1.0).reshape(h, w, 4)[::-1].copy()  # Blender stores bottom-up

    def _fit(self, most: float, direction: Vector) -> None:
        """Frame the cells on what the effect really covers. A few frames are rendered small through
        the square framing; the part of the picture that holds all but the faintest edge of what
        showed (stray sparks do not count) becomes the cell, and the camera's pixels are made as
        much taller or wider than square as that part is (no more than ``most`` times)."""
        import numpy as np
        render, small = self.scene.render, 64
        render.resolution_x = render.resolution_y = small
        picks = sorted({self.frames[round(i * (len(self.frames) - 1) / 5)] for i in range(6)})
        seen = np.zeros((small, small), dtype=np.float32)
        for number, frame in enumerate(picks):
            seen = np.maximum(seen, self._shot(frame, f"fit{number}")[:small, :small, 3])
        render.resolution_x = render.resolution_y = self.cell_px
        total = float(seen.sum())
        if total <= 1e-3:
            return                          # nothing showed: keep the square
        edges = []
        for line in (seen.sum(axis=0), seen.sum(axis=1)):      # left to right, then top to bottom
            run = np.cumsum(line) / total
            lo = int(np.searchsorted(run, 0.004))
            hi = int(np.searchsorted(run, 0.996)) + 1
            edges.append((max(lo - 3, 0) / small, min(hi + 3, small) / small))
        (left, right), (top, bottom) = edges
        span = self.width
        width, height = max((right - left) * span, span * 0.02), max((bottom - top) * span, span * 0.02)
        width, height = max(width, height / most), max(height, width / most)
        camera = self._camera
        across = camera.matrix_world.col[0].xyz.normalized()
        upward = camera.matrix_world.col[1].xyz.normalized()
        shift = across * (((left + right) / 2 - 0.5) * span) + upward * ((0.5 - (top + bottom) / 2) * span)
        camera.location = camera.location + shift
        camera.data.ortho_scale = max(width, height)
        self._restore.set(render, "pixel_aspect_x", max(width / height, 1.0))
        self._restore.set(render, "pixel_aspect_y", max(height / width, 1.0))
        self.centre, self.width, self.height = self.centre + shift, width, height
        bpy.context.view_layer.update()

    def step(self) -> bool:
        index = len(self.cells)
        if index >= len(self.frames):
            return True
        size = self.cell_px
        self.cells.append(self._shot(self.frames[index], f"{index:03d}")[:size, :size])
        return len(self.cells) >= len(self.frames)

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._restore.restore()
        self.scene.frame_set(self._saved_frame)
        if self._camera is not None:
            cam_data = self._camera.data
            bpy.data.objects.remove(self._camera, do_unlink=True)
            bpy.data.cameras.remove(cam_data)
            self._camera = None
        for path in self._files:
            try:
                os.remove(path)
            except OSError:
                pass


@tool(
    "vfx_flipbook",
    """Render an effect's animation to a single sprite-sheet PNG for game engines. Give the
    VFX_<name> root (or any object; its whole family is rendered). Frames are sampled evenly
    across the effect's own range, each through an orthographic camera framed on the effect with
    a transparent background, then packed row by row (first frame top-left) into one RGBA image.
    All other objects are hidden for the render and every setting is restored afterwards.""",
    {
        "root": {"type": "string", "description": "Name of a VFX_... root or any object to render."},
        "frames": {"type": "integer", "description": "Number of cells, 4-64 (default 16)."},
        "columns": {"type": "integer", "description": "Columns in the sheet. Default a square-ish layout."},
        "cell_px": {"type": "integer", "description": "Pixel size of each cell, 64-512 (default 256)."},
        "start_frame": {"type": "integer", "description": "Override the first frame sampled."},
        "end_frame": {"type": "integer", "description": "Override the last frame sampled."},
        "path": {"type": "string", "description": "Output .png. Default a file in the flipbooks folder."},
        "view": {"type": "string", "enum": ["front", "side", "top", "camera"], "description": "Default 'front'."},
    },
    ["root"],
    timeout=1800.0,
    label="Rendered a flipbook",
)
def vfx_flipbook(root: str, frames: int = 16, columns: int = 0, cell_px: int = 256,
                 start_frame: Optional[int] = None, end_frame: Optional[int] = None,
                 path: str = "", view: str = "front") -> ToolOutput:
    import numpy as np

    frames = int(frames)
    if not (4 <= frames <= 64):
        raise ToolError("frames must be between 4 and 64.")
    cell_px = int(cell_px)
    if not (64 <= cell_px <= 512):
        raise ToolError("cell_px must be between 64 and 512.")
    if view not in ("front", "side", "top", "camera"):
        raise ToolError("view must be one of front, side, top, camera.")
    ensure_object_mode()
    obj = get_object(root)
    scene = bpy.context.scene
    family = _family(obj)
    renderable = [o for o in family if o.type in _RENDERABLE]
    if not renderable and view != "camera":
        raise ToolError(f"'{root}' and its children have nothing renderable to frame.")

    s = int(start_frame) if start_frame is not None else int(obj.get("unix_start", scene.frame_start))
    e = int(end_frame) if end_frame is not None else int(obj.get("unix_end", scene.frame_end))
    if e < s:
        raise ToolError(f"end_frame ({e}) is before start_frame ({s}).")
    if frames == 1:
        frame_list = [s]
    else:
        frame_list = [int(round(s + (e - s) * i / (frames - 1))) for i in range(frames)]

    cols = int(columns) if columns else max(1, int(math.ceil(math.sqrt(frames))))
    if cols < 1 or cols > frames:
        raise ToolError(f"columns must be between 1 and {frames}.")
    rows = int(math.ceil(frames / cols))

    if view == "camera" and scene.camera is None:
        raise ToolError("view='camera' needs an active scene camera.")
    out_path = bpy.path.abspath(path) if path else os.path.join(
        data_dir("flipbooks"), time.strftime(f"{obj.name}_%Y%m%d_%H%M%S.png"))
    if not out_path.lower().endswith(".png"):
        out_path += ".png"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    sheet = np.zeros((rows * cell_px, cols * cell_px, 4), dtype=np.float32)
    film = Film(obj, frame_list, cell_px, view)
    try:
        while not film.step():
            pass
        for idx, cell in enumerate(film.cells):
            rr, cc = idx // cols, idx % cols
            sheet[rr * cell_px:(rr + 1) * cell_px, cc * cell_px:(cc + 1) * cell_px] = cell
        out_img = bpy.data.images.new(obj.name + "_sheet", cols * cell_px, rows * cell_px, alpha=True)
        out_img.alpha_mode = 'STRAIGHT'
        out_img.pixels.foreach_set(sheet[::-1].ravel())  # back to bottom-up for Blender
        out_img.filepath_raw, out_img.file_format = out_path, 'PNG'
        out_img.save()
        preview = out_path
        pw, ph = cols * cell_px, rows * cell_px
        if max(pw, ph) > 1024:
            scale = 1024 / max(pw, ph)
            out_img.scale(max(1, int(pw * scale)), max(1, int(ph * scale)))
            preview = os.path.join(data_dir("flipbooks"), "preview.png")
            out_img.filepath_raw, out_img.file_format = preview, 'PNG'
            out_img.save()
        bpy.data.images.remove(out_img)
    finally:
        film.close()

    span = max(1, e - s + 1)
    fps_suggest = max(1, int(round(frames * scene.render.fps / span)))
    data = {
        "file": out_path,
        "columns": cols,
        "rows": rows,
        "frames": frames,
        "cell_px": cell_px,
        "frame_range": [s, e],
        "fps_suggestion": fps_suggest,
        "usage": {
            "unity": f"Sprite (2D/UI), Multiple mode, slice {cols}x{rows}; play as a flipbook or in Shader Graph.",
            "unreal": f"Import as Texture, use a SubUV / FlipbookParticle module with {cols}x{rows} sub-images.",
            "godot": f"AnimatedSprite2D or a SpriteFrames sheet split into {cols} columns x {rows} rows.",
            "roblox": f"ImageLabel with a SpriteSheet: set ImageRectSize and step ImageRectOffset over {cols}x{rows} cells.",
        },
        "note": "Rows top to bottom, first frame top-left. Transparent background (RGBA).",
    }
    return ToolOutput(data, [preview])
