# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""A column of fire, smoke, dust, steam or energy whose shape and motion are given as numbers.

The presets in ``vfx.py`` each make one fixed thing, and a gas simulation makes whatever the
physics makes: neither can be told "narrow at the foot, wide at the top, turning, leaning in
the wind". This effect is described instead of simulated. A box holds a volume shader that
works out, for every point inside it, how far that point is from a wandering axis, how wide the
column is at that height (the ``profile``), and what a noise pattern says there after it has
been turned about the axis, wound with height and carried upward. Nothing is baked, so it is
there at once, it is as sharp as the render and it can be changed and looked at again in a
second. Small solid things (embers, debris, motes) are carried round by the same flow with
geometry nodes.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import bpy
from mathutils import Vector

from . import _turn, vfx
from ._util import ensure_object_mode
from .registry import ToolError, tool

TAU = 2.0 * math.pi
_FIRE = [(0.0, (0.0, 0.0, 0.0)), (0.16, (0.32, 0.01, 0.0)), (0.4, (1.0, 0.11, 0.0)),
         (0.72, (1.0, 0.27, 0.01)), (1.0, (1.0, 0.52, 0.09))]
_ENERGY = [(0.0, (0.0, 0.0, 0.0)), (0.3, (0.05, 0.15, 0.6)), (0.7, (0.2, 0.55, 1.0)), (1.0, (0.85, 0.95, 1.0))]
# look: glow ramp (None = it does not glow), gas colour, how thick the gas is, how bright the glow is,
#       colour of the things it carries (None = dull bits of its own colour), and how far its pattern
#       is drawn out along the flow (flames and beams are streaks, smoke and steam are billows)
LOOKS: Dict[str, Tuple[Any, Tuple[float, float, float], float, float, Any, float]] = {
    "fire": (_FIRE, (0.03, 0.028, 0.027), 2.2, 2.4, (1.0, 0.32, 0.04), 0.35),
    "smoke": (None, (0.16, 0.16, 0.17), 1.5, 0.0, None, 0.8),
    "dust": (None, (0.55, 0.43, 0.3), 1.6, 0.0, None, 0.5),
    "steam": (None, (0.92, 0.94, 0.96), 0.9, 0.0, None, 0.8),
    "energy": (_ENERGY, (0.0, 0.0, 0.0), 0.0, 1.6, (0.5, 0.8, 1.0), 0.35),
}
# What an engine that cannot show the column itself falls back to (Roblox, when its pictures may not be uploaded)
_AS_PRESET = {"fire": "fire", "smoke": "smoke", "dust": "smoke", "steam": "smoke", "energy": "fire"}
MAX_CARRIED = 4000
_OPAQUE = 0.55         # how much glowing gas hides what is behind it (see _material)


class _Graph:
    """Shorthand for wiring maths in a node tree: every method returns an output socket."""

    def __init__(self, tree: bpy.types.NodeTree):
        self.tree = tree

    def put(self, socket: Any, value: Any) -> None:
        if isinstance(value, bpy.types.NodeSocket):
            self.tree.links.new(value, socket)
        elif value is not None:
            socket.default_value = value

    def node(self, kind: str, **props: Any) -> bpy.types.Node:
        node = self.tree.nodes.new(kind)
        for key, value in props.items():
            setattr(node, key, value)
        return node

    def math(self, op: str, a: Any, b: Any = None, c: Any = None, clamp: bool = False) -> Any:
        node = self.node("ShaderNodeMath", operation=op, use_clamp=clamp)
        for index, value in enumerate((a, b, c)):
            self.put(node.inputs[index], value)
        return node.outputs[0]

    def smooth(self, value: Any, lo: float, hi: float, out_lo: float = 0.0, out_hi: float = 1.0) -> Any:
        """``out_lo`` below ``lo``, ``out_hi`` above ``hi``, eased in between."""
        node = self.node("ShaderNodeMapRange", interpolation_type='SMOOTHSTEP')
        self.put(node.inputs["Value"], value)
        for name, number in (("From Min", lo), ("From Max", hi), ("To Min", out_lo), ("To Max", out_hi)):
            node.inputs[name].default_value = number
        return node.outputs["Result"]

    def xyz(self, x: Any = 0.0, y: Any = 0.0, z: Any = 0.0) -> Any:
        node = self.node("ShaderNodeCombineXYZ")
        for index, value in enumerate((x, y, z)):
            self.put(node.inputs[index], value)
        return node.outputs[0]

    def curve(self, value: Any, stops: Sequence[Tuple[float, float]]) -> Any:
        node = self.node("ShaderNodeFloatCurve")
        points = node.mapping.curves[0].points
        points[0].location, points[1].location = stops[0], stops[-1]
        for stop in stops[1:-1]:
            points.new(*stop)
        node.mapping.use_clip = True
        node.mapping.update()
        self.put(node.inputs["Value"], value)
        return node.outputs["Value"]

    def noise(self, vector: Any, scale: float, detail: float, roughness: float, distortion: float, w: Any = None) -> Any:
        node = self.node("ShaderNodeTexNoise", noise_dimensions='3D' if w is None else '4D')
        self.put(node.inputs["Vector"], vector)
        if w is not None:
            self.put(node.inputs["W"], w)
        for name, number in (("Scale", scale), ("Detail", detail), ("Roughness", roughness), ("Distortion", distortion)):
            node.inputs[name].default_value = number
        return node

    def ramp(self, value: Any, stops: Sequence[Tuple[float, Tuple[float, float, float]]]) -> Any:
        node = self.node("ShaderNodeValToRGB")
        el = node.color_ramp.elements
        el[0].position, el[0].color = stops[0][0], (*stops[0][1], 1.0)
        el[1].position, el[1].color = stops[-1][0], (*stops[-1][1], 1.0)
        for pos, rgb in stops[1:-1]:
            el.new(pos).color = (*rgb, 1.0)
        self.put(node.inputs["Fac"], value)
        return node.outputs["Color"]


def _tinted(color: Sequence[float]) -> List[Tuple[float, Tuple[float, float, float]]]:
    return [(0.0, (0.0, 0.0, 0.0)), (0.2, tuple(c * 0.25 for c in color)), (0.55, tuple(color)),
            (1.0, tuple(c + (1.0 - c) * 0.7 for c in color))]


def _material(name: str, look: str, *, height: float, radius: float, stops: Sequence[Tuple[float, float]],
              swirl: float, twist: float, rise: float, turbulence: float, hollow: float, sway: float,
              intensity: float, density: float, color: Optional[Sequence[float]], start: int, fps: float) -> bpy.types.Material:
    glow_stops, gas_colour, thickness, glow, _carried, drawn = LOOKS[look]
    if color is not None:
        if glow_stops is not None:
            glow_stops = _tinted(color)
        else:
            gas_colour = tuple(color)
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    tree = mat.node_tree
    tree.nodes.clear()
    g = _Graph(tree)

    clock = g.node("ShaderNodeValue")
    clock.name = clock.label = "Seconds"
    vfx._drive(clock.outputs[0], "default_value", -1, "(frame-%d)/%f" % (start, fps))
    t = clock.outputs[0]
    coord = g.node("ShaderNodeTexCoord")
    split = g.node("ShaderNodeSeparateXYZ")
    tree.links.new(coord.outputs["Object"], split.inputs[0])
    x, y, z = split.outputs
    h = g.math('ADD', g.math('DIVIDE', z, height), 0.5)                 # 0 at the foot, 1 at the top

    # the axis wanders, more the higher it is, and never at the foot
    wander = g.noise(g.xyz(g.math('MULTIPLY', h, 1.6), g.math('MULTIPLY', t, 0.22)), 1.0, 1.0, 0.5, 0.0)
    lean = g.node("ShaderNodeSeparateColor")
    tree.links.new(wander.outputs["Color"], lean.inputs[0])
    reach = g.math('MULTIPLY', g.math('MULTIPLY', h, 2.0 * sway * radius), g.smooth(h, 0.0, 0.25))
    px = g.math('SUBTRACT', x, g.math('MULTIPLY', g.math('SUBTRACT', lean.outputs[0], 0.5), reach))
    py = g.math('SUBTRACT', y, g.math('MULTIPLY', g.math('SUBTRACT', lean.outputs[1], 0.5), reach))

    wide = g.math('MAXIMUM', g.math('MULTIPLY', g.curve(h, stops), radius), radius * 0.03)
    rho = g.math('SQRT', g.math('ADD', g.math('MULTIPLY', px, px), g.math('MULTIPLY', py, py)))
    q = g.math('DIVIDE', rho, wide)                                       # 0 on the axis, 1 at the edge

    # The pattern turns about the axis and climbs. It is also wound: more turned the higher it
    # is (what a rising, turning gas draws) and held back toward the edge, so streaks are helices.
    turn = g.math('SUBTRACT', g.math('MULTIPLY', q, 1.4 if (swirl or twist) else 0.0),
                  g.math('ADD', g.math('MULTIPLY', t, TAU * swirl), g.math('MULTIPLY', h, TAU * twist)))
    spin = g.node("ShaderNodeVectorRotate", rotation_type='Z_AXIS')
    tree.links.new(g.xyz(px, py, 0.0), spin.inputs["Vector"])
    tree.links.new(turn, spin.inputs["Angle"])
    flat = g.node("ShaderNodeSeparateXYZ")
    tree.links.new(spin.outputs[0], flat.inputs[0])
    climb = g.math('MULTIPLY', g.math('SUBTRACT', z, g.math('MULTIPLY', t, rise)), drawn)
    where = g.xyz(flat.outputs[0], flat.outputs[1], climb)
    grain = 2.2 / max(radius, 1e-3)
    big = g.noise(where, grain, 3.0, 0.55, 0.6 * turbulence, w=g.math('MULTIPLY', t, 0.35))
    fine = g.noise(where, grain * 3.3, 5.0, 0.62, 0.9 * turbulence, w=g.math('MULTIPLY', t, 0.7))
    # billows, with thin sheets folded through them (where the fine noise crosses its middle)
    sheets = g.math('SUBTRACT', 1.0, g.math('ABSOLUTE', g.math('MULTIPLY', g.math('SUBTRACT', fine.outputs["Fac"], 0.5), 5.0)),
                    clamp=True)
    rough = g.math('ADD', g.math('MULTIPLY', g.math('SUBTRACT', big.outputs["Fac"], 0.5), 2.2),
                   g.math('MULTIPLY', g.math('SUBTRACT', sheets, 0.4), 0.7))

    # A soft edge at q = 1 and an emptier middle when hollow, both eaten into by the noise. The
    # noise wins more and more toward the top, so the column ends in torn pieces and not in a lid.
    inside = g.smooth(q, 0.3, 1.0, 1.0, 0.0)
    core = g.smooth(q, 0.0, 0.5, 1.0 - hollow, 1.0)
    body = g.math('SUBTRACT', g.math('MULTIPLY', inside, core), g.smooth(h, 0.4, 1.0, 0.0, 0.6))
    eaten = g.math('ADD', body, g.math('MULTIPLY', rough, 0.5 * turbulence))
    ends = g.math('MULTIPLY', g.smooth(h, 0.0, 0.02), g.smooth(h, 0.85, 1.0, 1.0, 0.0))
    # the noise tears the edge and throws wisps off it, but makes nothing out in the open
    near = g.math('MULTIPLY', ends, g.smooth(q, 0.9, 1.5, 1.0, 0.0))
    gas = g.math('MULTIPLY', g.smooth(eaten, 0.3, 0.5), near)
    # a ray through a narrow part crosses less gas: thicken it there, so the foot is not a ghost
    across = g.math('DIVIDE', 1.0, g.math('MAXIMUM', wide, radius * 0.2))

    out = g.node("ShaderNodeOutputMaterial")
    vol = g.node("ShaderNodeVolumePrincipled")
    vol.inputs["Blackbody Intensity"].default_value = 0.0
    vol.inputs["Color"].default_value = (*gas_colour, 1.0)
    soot = gas
    if glow_stops is not None:
        # hot where the gas is thick and low; toward the edge and the top it cools (fire: into smoke)
        cool = g.smooth(h, 0.45, 1.0, 1.0, 0.45 if thickness else 0.7)
        heat = g.math('MULTIPLY', g.math('MULTIPLY', g.smooth(eaten, 0.35, 1.15), cool), near, clamp=True)
        tree.links.new(g.ramp(heat, glow_stops), vol.inputs["Emission Color"])
        strength = g.node("ShaderNodeMath", operation='MULTIPLY')
        strength.name = strength.label = "Flame Strength"
        tree.links.new(g.math('MULTIPLY', g.math('POWER', heat, 1.5), across), strength.inputs[0])
        strength.inputs[1].default_value = glow * intensity
        tree.links.new(strength.outputs[0], vol.inputs["Emission Strength"])
        soot = g.math('MULTIPLY', gas, g.math('SUBTRACT', 1.0, g.math('MULTIPLY', heat, 0.85)))
    thick = g.node("ShaderNodeMath", operation='MULTIPLY')
    thick.name = thick.label = "Smoke Density"
    tree.links.new(g.math('MULTIPLY', soot, across), thick.inputs[0])
    thick.inputs[1].default_value = thickness * density
    murk = thick.outputs[0]
    if glow_stops is not None:
        # Glowing gas also hides what is behind it. Without that the glow of everything along a ray
        # adds up, and a thick column (or two effects in line) burns out to white; with it the
        # brightness levels off at the flame's own colour and the near strands stand out from the far.
        murk = g.math('ADD', murk, g.math('MULTIPLY', g.math('MULTIPLY', heat, across), _OPAQUE))
    tree.links.new(murk, vol.inputs["Density"])
    tree.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    shown = glow_stops[-2][1] if glow_stops is not None else gas_colour
    mat.diffuse_color = (*shown, 1.0)
    return mat


def _random(g: _Graph, seed: int, lo: float, hi: float) -> Any:
    node = g.node("FunctionNodeRandomValue", data_type='FLOAT')
    for socket in node.inputs:
        if socket.enabled and socket.name == "Min":
            socket.default_value = lo
        elif socket.enabled and socket.name == "Max":
            socket.default_value = hi
        elif socket.name == "Seed":
            socket.default_value = seed
    return next(s for s in node.outputs if s.enabled)


def _carried_things(fx: vfx._FX, look: str, count: int, *, height: float, radius: float,
                    stops: Sequence[Tuple[float, float]], swirl: float, twist: float, rise: float,
                    intensity: float, color: Optional[Sequence[float]], start: int, fps: float) -> bpy.types.Object:
    """Embers, debris or motes carried up and round by the column: points whose places are worked
    out from the time by geometry nodes, so they need no cache and follow any change to the frame."""
    mesh = bpy.data.meshes.new(fx.prefix + "_carried")
    mesh.from_pydata([(0.0, 0.0, 0.0)] * count, [], [])
    obj = fx.child(bpy.data.objects.new(fx.prefix + "_carried", mesh))
    tree = bpy.data.node_groups.new(fx.prefix + "_carried", 'GeometryNodeTree')
    tree.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    g = _Graph(tree)
    source, sink = g.node("NodeGroupInput"), g.node("NodeGroupOutput")
    seconds = g.math('SUBTRACT', g.node("GeometryNodeInputSceneTime").outputs["Seconds"], start / fps)

    pace = _random(g, 3, 0.7, 1.4)
    up = g.math('FRACT', g.math('ADD', _random(g, 0, 0.0, 1.0),
                                g.math('MULTIPLY', g.math('MULTIPLY', seconds, rise / height), pace)))
    out_to = g.math('MULTIPLY', g.math('MULTIPLY', g.curve(up, stops), radius), _random(g, 2, 0.5, 1.35))
    angle = g.math('ADD', _random(g, 1, 0.0, TAU),
                   g.math('ADD', g.math('MULTIPLY', g.math('MULTIPLY', seconds, TAU * swirl), pace),
                          g.math('MULTIPLY', up, TAU * twist)))
    place = g.xyz(g.math('MULTIPLY', out_to, g.math('COSINE', angle)), g.math('MULTIPLY', out_to, g.math('SINE', angle)),
                  g.math('MULTIPLY', up, height))
    move = g.node("GeometryNodeSetPosition")
    tree.links.new(source.outputs[0], move.inputs["Geometry"])
    tree.links.new(place, move.inputs["Position"])

    # each one swells in at the foot and dies away toward the top
    life = g.math('MULTIPLY', g.smooth(up, 0.0, 0.08), g.smooth(up, 0.55, 1.0, 1.0, 0.0))
    base = min(max(height * 0.0022, 0.002), 0.04)
    size = g.math('MULTIPLY', g.math('MULTIPLY', life, _random(g, 4, 0.5, 1.6)), base)
    ball = g.node("GeometryNodeMeshIcoSphere")
    ball.inputs["Radius"].default_value, ball.inputs["Subdivisions"].default_value = 1.0, 1
    place_on = g.node("GeometryNodeInstanceOnPoints")
    tree.links.new(move.outputs[0], place_on.inputs["Points"])
    tree.links.new(ball.outputs["Mesh"], place_on.inputs["Instance"])
    tree.links.new(size, place_on.inputs["Scale"])
    carried = color if color is not None else LOOKS[look][4]
    if carried is not None:
        mat = vfx._emission_mat(fx.prefix + "_carried", carried, 12.0 * intensity)
    else:
        mat = bpy.data.materials.new(fx.prefix + "_carried")
        mat.diffuse_color = (*(c * 0.45 for c in LOOKS[look][1]), 1.0)
        mat.use_nodes = True
        bsdf = mat.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = mat.diffuse_color
            bsdf.inputs["Roughness"].default_value = 1.0
    paint = g.node("GeometryNodeSetMaterial")
    tree.links.new(place_on.outputs[0], paint.inputs["Geometry"])
    paint.inputs["Material"].default_value = mat
    tree.links.new(paint.outputs[0], sink.inputs[0])
    obj.modifiers.new("Carried", 'NODES').node_group = tree
    return obj


@tool(
    "vfx_column",
    """Build an animated column of fire, smoke, dust, steam or energy whose shape and motion you
    describe with numbers: anything that rises, jets, pours or spins round an axis and that the
    vfx_create presets cannot be bent into. Fire whirls and tornadoes, dust devils, waterspouts,
    smoke plumes, geysers, jet and rocket flames, beams and pillars of light, vortexes and
    portals are all this one effect with different numbers. It is built at once (no bake) and is
    as sharp as the render. The shape is `profile`: radius against height, so a funnel is narrow
    at 0 and wide at 1, a jet flame the other way round, a pillar flat. The motion is `swirl`
    (how fast it turns), `twist` (how tightly its streaks wind up it), `rise_m_s`, `sway` (how
    much the whole column snakes) and `turbulence` (how torn it is). `hollow` empties the middle
    as in a tornado. `carried` adds small things flying round and up with it: embers in fire,
    debris in dust, motes in energy. Aim it anywhere with `rotation_deg` (it points up by
    default). It lives under VFX_<name> like every effect; call again with the same name to
    change it, and look(shading='rendered') after each change: a second look costs a second.""",
    {
        "look": {"type": "string", "enum": list(LOOKS), "description": "What it is made of."},
        "name": {"type": "string", "description": "Effect name (root is VFX_<name>). Default the look."},
        "location": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] of the foot of the column. Default origin."},
        "height_m": {"type": "number", "description": "How tall (long) it is, in meters. Default 3."},
        "radius_m": {"type": "number", "description": "Its radius where the profile is 1, in meters. Default a quarter of the height."},
        "profile": {"type": "array", "items": {"type": "array", "items": {"type": "number"}},
                    "description": "Radius along the column as [[height 0-1, radius 0-1], ...], two to eight points. "
                                   "Default [[0, 0.6], [1, 1]] (widens upward)."},
        "swirl": {"type": "number", "description": "Turns per second about the axis; negative turns the other way. Default 0."},
        "twist": {"type": "number", "description": "How many turns the streaks wind over the whole height. Default 0 (straight up)."},
        "rise_m_s": {"type": "number", "description": "How fast the pattern travels from foot to top, m/s. Default 0.4 of the height per second."},
        "turbulence": {"type": "number", "description": "0 = smooth and solid, 1 = natural (default), 2 = shredded."},
        "hollow": {"type": "number", "description": "0 = filled (default), 1 = only a wall round an empty middle."},
        "sway": {"type": "number", "description": "0 = dead straight (default), 0.3 = snakes like a whirlwind, 1 = wild."},
        "carried": {"type": "integer", "description": f"How many small things fly round and up with it (0 = none, up to {MAX_CARRIED})."},
        "color": {"type": "array", "items": {"type": "number"}, "description": "Optional [r, g, b] linear 0-1: tints the glow, or the gas if it does not glow."},
        "intensity": {"type": "number", "description": "Glow brightness multiplier (default 1)."},
        "density": {"type": "number", "description": "How thick the smoke, dust or steam is (default 1)."},
        "rotation_deg": {"type": "array", "items": {"type": "number"}, "description": "[x, y, z] degrees to tip the column over. Default upright."},
        "start_frame": {"type": "integer", "description": "First frame. Default the scene's current frame."},
        "duration_frames": {"type": "integer", "description": "Length in frames (default 120). It moves for as long as it plays."},
    },
    ["look"],
    mutates=True,
    label="Built a column effect",
)
def vfx_column(look: str, name: str = "", location: Optional[List[float]] = None, height_m: float = 3.0,
               radius_m: Optional[float] = None, profile: Optional[List[List[float]]] = None, swirl: float = 0.0,
               twist: float = 0.0, rise_m_s: Optional[float] = None, turbulence: float = 1.0, hollow: float = 0.0,
               sway: float = 0.0, carried: int = 0, color: Optional[List[float]] = None, intensity: float = 1.0,
               density: float = 1.0, rotation_deg: Optional[List[float]] = None, start_frame: Optional[int] = None,
               duration_frames: int = 120) -> Dict[str, Any]:
    if look not in LOOKS:
        raise ToolError(f"Unknown look '{look}'. Choose one of: {', '.join(LOOKS)}.")
    name = (name or look).strip()
    if not name or any(c in name for c in r'\/:*?"<>|'):
        raise ToolError("name must be a simple label with no path separators.")
    location = location or [0.0, 0.0, 0.0]
    if len(location) != 3:
        raise ToolError("location must be [x, y, z].")
    height = float(height_m)
    if not 0.02 <= height <= 2000.0:
        raise ToolError("height_m must be between 0.02 and 2000 meters.")
    radius = float(radius_m) if radius_m is not None else height * 0.25
    if not 0.005 <= radius <= 1000.0:
        raise ToolError("radius_m must be between 0.005 and 1000 meters.")
    stops = [(0.0, 0.6), (1.0, 1.0)]
    if profile is not None:
        try:
            stops = sorted((float(a), float(b)) for a, b in profile)
        except (TypeError, ValueError):
            raise ToolError("profile must be a list of [height, radius] pairs, each from 0 to 1.")
        if not 2 <= len(stops) <= 8 or any(not (0.0 <= a <= 1.0 and 0.0 <= b <= 1.0) for a, b in stops):
            raise ToolError("profile needs two to eight [height, radius] pairs, each number from 0 to 1.")
        if len({a for a, _b in stops}) != len(stops) or max(b for _a, b in stops) <= 0.0:
            raise ToolError("profile heights must all differ, and at least one radius must be above 0.")
        stops[0], stops[-1] = (0.0, stops[0][1]), (1.0, stops[-1][1])
    for label, value, lo, hi in (("swirl", swirl, -10.0, 10.0), ("twist", twist, -20.0, 20.0), ("turbulence", turbulence, 0.0, 2.0),
                                 ("hollow", hollow, 0.0, 1.0), ("sway", sway, 0.0, 1.0), ("intensity", intensity, 0.01, 50.0),
                                 ("density", density, 0.01, 50.0), ("carried", carried, 0, MAX_CARRIED),
                                 ("duration_frames", duration_frames, 2, 100000)):
        if not lo <= float(value) <= hi:
            raise ToolError(f"{label} must be between {lo:g} and {hi:g}.")
    if color is not None and len(color) != 3:
        raise ToolError("color must be [r, g, b] in 0-1.")
    if rotation_deg is not None and len(rotation_deg) != 3:
        raise ToolError("rotation_deg must be [x, y, z] in degrees.")
    rise = float(rise_m_s) if rise_m_s is not None else height * 0.4
    ensure_object_mode()
    scene = bpy.context.scene
    start = int(start_frame) if start_frame is not None else int(scene.frame_current)
    end = start + int(duration_frames) - 1
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    tint = None if color is None else [float(c) for c in color]

    _turn.mark_mutation()
    vfx._remove_existing(name)
    fx = vfx._FX(name, location, height, tint if tint is not None else LOOKS[look][1], intensity, start, end,
                 tinted=tint is not None)
    try:
        if rotation_deg is not None:
            fx.root.rotation_euler = [math.radians(float(a)) for a in rotation_deg]
        side = 2.0 * radius * (1.5 + float(sway))
        box = fx.child(vfx._box(fx.prefix + "_column", side, side, height), local=(0, 0, height * 0.5))
        for poly in box.data.polygons:
            poly.use_smooth = False
        shape = dict(height=height, radius=radius, stops=stops, swirl=float(swirl), twist=float(twist), rise=rise,
                     intensity=fx.intensity, color=tint, start=start, fps=fps)
        vfx._assign(box, _material(fx.prefix + "_gas", look, turbulence=float(turbulence), hollow=float(hollow),
                                   sway=float(sway), density=float(density), **shape))
        if carried:
            _carried_things(fx, look, int(carried), **shape)
        if LOOKS[look][0] is not None:           # it glows: it lights what is round it, and flickers
            shade = tint if tint is not None else LOOKS[look][4]
            watts = 55.0 * height * height * fx.intensity
            mean = radius * sum(b for _a, b in stops) / len(stops)
            _, light = fx.light("glow", shade, watts, local=(0, 0, height * 0.3), soft=max(mean * 0.6, 0.02))
            vfx._drive(light, "energy", -1, "%f*(0.8+0.14*sin(frame*1.3)+0.06*sin(frame*3.1))" % watts)
    except Exception:
        vfx._remove_existing(name)            # do not leave half an effect behind
        raise

    preview = start + (end - start) // 2
    fx.root["unix_start"], fx.root["unix_end"], fx.root["unix_loop"] = start, end, True
    fx.root["unix_effect"] = _AS_PRESET[look]
    fx.root["unix_column"] = look
    fx.root["unix_size"] = height
    if tint is not None or LOOKS[look][0] is None or look == "energy":
        fx.root["unix_color"] = tint if tint is not None else list(LOOKS[look][4] or LOOKS[look][1])
    if scene.frame_end < end:
        scene.frame_end = end
    changed = vfx._sharp_volumes(scene, shadows=True)
    scene.frame_set(preview)
    bpy.context.view_layer.update()
    out: Dict[str, Any] = {
        "root": fx.root.name, "effect": "column", "look": look, "objects": fx.objects, "collection": vfx.VFX_COLLECTION,
        "height_m": height, "radius_m": radius, "frame_range": [start, end], "preview_frame": preview,
        "note": "Scene is on the preview frame. Run look(shading='rendered') to see it, against a dark or plain "
                "background; then change the numbers and call again with the same name until the shape reads. It is "
                "animated: it starts playing by itself when your turn ends, and the user has Play on the island.",
    }
    if changed:
        out["scene_settings_changed"] = changed
    return out
