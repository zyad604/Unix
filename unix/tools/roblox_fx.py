# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Roblox-native effect recipes, as ``roblox_build`` trees.

A Blender effect cannot be sent to Roblox as geometry: its look is shaders and simulation.
These recipes rebuild the *idea* of an effect with Roblox's own parts, emitters, beams and
lights, in layers, and animate it with the plugin's Spin / Pulse / Flicker attributes, which
play in Edit mode and in the running game alike. They are also the worked example for the
``roblox-native-vfx`` skill.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

_PARTICLES = "rbxasset://textures/particles/"
GLOW, VORTEX = _PARTICLES + "forcefield_glow_main.dds", _PARTICLES + "forcefield_vortex_main.dds"
SPARK, SMOKE = _PARTICLES + "sparkles_main.dds", _PARTICLES + "smoke_main.dds"
ROUND_GLOW = "rbxasset://textures/glow.png"
WHITE, CYAN = [1.0, 1.0, 1.0], [0.3, 0.9, 1.0]


def _mix(a: Sequence[float], b: Sequence[float], t: float) -> List[float]:
    return [round(x + (y - x) * t, 3) for x, y in zip(a, b)]


def _ball(name: str, size: float, centre: Sequence[float], color: Sequence[float], material: str,
          transparency: float = 0.0, attributes: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"class": "Part", "name": name, "attributes": attributes or {}, "props": {
        "Shape": "Enum.PartType.Ball", "Size": {"v3": [size, size, size]}, "CFrame": {"cf": list(centre)},
        "Color": {"c3": list(color)}, "Material": material, "Transparency": transparency,
        "CanCollide": False, "CanQuery": False, "CanTouch": False, "CastShadow": False}}


def _emitter(name: str, **props: Any) -> Dict[str, Any]:
    base: Dict[str, Any] = {"LockedToPart": True, "LightInfluence": 0, "Shape": "Enum.ParticleEmitterShape.Sphere",
                            "ShapeStyle": "Enum.ParticleEmitterShapeStyle.Volume"}
    base.update(props)
    return {"class": "ParticleEmitter", "name": name, "props": base}


def _arcs(radius: float, count: int, hot: Sequence[float], cool: Sequence[float]) -> List[Dict[str, Any]]:
    """Thin beams between points scattered over the shell; Flicker re-rolls their curve."""
    nodes: List[Dict[str, Any]] = []
    r = radius * 0.95
    for i in range(count):
        a = 2 * math.pi * i / count
        tilt = math.radians(-50 + 100 * ((i * 7) % count) / count)
        b = a + math.radians(95 + 40 * (i % 3))
        start = [r * math.cos(a) * math.cos(tilt), r * math.sin(tilt), r * math.sin(a) * math.cos(tilt)]
        end = [r * math.cos(b) * math.cos(-tilt * 0.6), r * math.sin(-tilt * 0.6), r * math.sin(b) * math.cos(-tilt * 0.6)]
        nodes.append({"class": "Attachment", "name": f"A{i}", "id": f"a{i}", "props": {"Position": {"v3": [round(c, 3) for c in start]}}})
        nodes.append({"class": "Attachment", "name": f"B{i}", "id": f"b{i}", "props": {"Position": {"v3": [round(c, 3) for c in end]}}})
        bend = radius * 0.85 * (1 if i % 2 else -1)
        nodes.append({"class": "Beam", "name": f"Arc{i}", "attributes": {"Flicker": round(radius * 1.1, 2)}, "props": {
            "Attachment0": {"ref": f"a{i}"}, "Attachment1": {"ref": f"b{i}"}, "CurveSize0": bend, "CurveSize1": -bend,
            "Segments": 24, "FaceCamera": True, "Width0": 0.13, "Width1": 0.02, "LightEmission": 1, "LightInfluence": 0,
            "Brightness": 4, "Color": {"cseq": [[0, *WHITE], [0.4, *hot], [1, *cool]]},
            "Transparency": {"nseq": [[0, 0.1], [0.6, 0.35], [1, 1]]}}})
    return nodes


def energy_orb(radius: float = 4.0, height: Optional[float] = None, color: Optional[Sequence[float]] = None,
               accent: Optional[Sequence[float]] = None, arcs: int = 16, base: Sequence[float] = (0.0, 0.0, 0.0)) -> List[Dict[str, Any]]:
    """Children for ``roblox_build``: a dark core, two counter-rotating energy shells, plasma,
    dark smoke at the heart, sparks, a halo, a light and flickering arcs. Sizes in studs."""
    main = list(color or [0.45, 0.15, 1.0])
    second = list(accent or _mix(main, [1.0, 0.15, 0.75], 0.75))
    r = max(0.5, float(radius))
    centre = [base[0], base[1] + (height if height is not None else r * 1.75), base[2]]
    deep = _mix(main, [0.0, 0.0, 0.0], 0.6)
    return [
        _ball("Core", r * 0.62, centre, _mix(main, [0.0, 0.0, 0.0], 0.95), "Enum.Material.Neon",
              attributes={"Pulse": 0.07, "PulseSpeed": 3.1}),
        _ball("ShellInner", r * 1.35, centre, second, "Enum.Material.ForceField", 0.15,
              attributes={"Spin": {"v3": [14, -52, 0]}, "Pulse": 0.04}),
        _ball("ShellOuter", r * 2.0, centre, main, "Enum.Material.ForceField", 0.3,
              attributes={"Spin": {"v3": [0, 32, 9]}, "Pulse": 0.03, "PulseSpeed": 1.7}),
        {"class": "Part", "name": "Field", "props": {
            "Shape": "Enum.PartType.Ball", "Size": {"v3": [r * 1.7] * 3}, "CFrame": {"cf": centre}, "Transparency": 1,
            "CanCollide": False, "CanQuery": False, "CanTouch": False, "CastShadow": False},
         "children": [
            _emitter("Plasma", Texture=GLOW, Rate=26, Lifetime={"range": [0.9, 1.6]}, Speed={"range": [0.2, 1.2]},
                     Rotation={"range": [0, 360]}, RotSpeed={"range": [-140, 140]}, LightEmission=0.9, Brightness=1.6,
                     Size={"nseq": [[0, r * 0.5], [0.5, r * 0.95], [1, r * 0.4]]},
                     Transparency={"nseq": [[0, 1], [0.25, 0.55], [0.75, 0.7], [1, 1]]},
                     Color={"cseq": [[0, *second], [0.5, *main], [1, *deep]]}),
            _emitter("Wisps", Texture=VORTEX, Rate=14, Lifetime={"range": [1.2, 2.0]}, Speed={"range": [0.0, 0.6]},
                     Rotation={"range": [0, 360]}, RotSpeed={"range": [-220, 220]}, LightEmission=1, Brightness=2,
                     Size={"nseq": [[0, r * 0.9], [1, r * 1.5]]},
                     Transparency={"nseq": [[0, 1], [0.3, 0.45], [0.8, 0.75], [1, 1]]},
                     Color={"cseq": [[0, *main], [0.6, *second], [1, *CYAN]]}),
            _emitter("DarkHeart", Texture=SMOKE, Rate=10, Lifetime={"range": [0.8, 1.3]}, Speed={"range": [0, 0.3]},
                     Rotation={"range": [0, 360]}, RotSpeed={"range": [-60, 60]}, LightEmission=0, ZOffset=1.5,
                     ShapeStyle="Enum.ParticleEmitterShapeStyle.Surface",
                     Size={"nseq": [[0, r * 0.5], [1, r * 0.75]]},
                     Transparency={"nseq": [[0, 1], [0.3, 0.25], [1, 1]]}, Color={"cseq": [0.01, 0.0, 0.03]}),
            _emitter("Sparks", Texture=SPARK, Rate=40, Lifetime={"range": [0.35, 0.9]}, Speed={"range": [r * 1.2, r * 2.7]},
                     Drag=3.5, ShapeStyle="Enum.ParticleEmitterShapeStyle.Surface",
                     ShapeInOut="Enum.ParticleEmitterShapeInOut.Outward", LockedToPart=False, LightEmission=1, Brightness=3,
                     Size={"nseq": [[0, r * 0.125], [1, 0]]}, Transparency={"nseq": [[0, 0], [0.7, 0.2], [1, 1]]},
                     Color={"cseq": [[0, *WHITE], [0.3, *CYAN], [1, *main]]}),
            _emitter("Halo", Texture=ROUND_GLOW, Rate=5, Lifetime={"range": [1.4, 2.0]}, Speed={"range": [0, 0]},
                     LightEmission=1, Brightness=1.2, ZOffset=-2,
                     Size={"nseq": [[0, r * 3.2], [1, r * 3.8]]}, Transparency={"nseq": [[0, 1], [0.4, 0.86], [1, 1]]},
                     Color={"cseq": main}),
            {"class": "PointLight", "name": "Glow", "attributes": {"Pulse": 0.06, "PulseSpeed": 2.4},
             "props": {"Color": {"c3": _mix(main, WHITE, 0.25)}, "Range": round(r * 6.5, 1), "Brightness": 2.4, "Shadows": False}},
            *_arcs(r, arcs, CYAN, main),
        ]},
    ]


ORB_EFFECTS = ("magic_swirl", "energy_shield", "shockwave")
