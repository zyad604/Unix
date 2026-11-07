# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Roblox Studio tools: build things natively in the open place, look at them, inspect, remove.

``send_to_engine`` carries what was made in Blender over, effects included. These tools are
for work that belongs in Roblox itself: Luau scripts, UI, controls, and things the user asks to
have built out of Roblox's own parts, emitters, beams and lights. Everything goes through the Unix Bridge
plugin (``bridge/roblox_plugin.lua``) as data; nothing here touches ``bpy``, so the tools run
off Blender's main thread.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from ..bridge import window_shot
from ..bridge.roblox_link import link
from ._util import data_dir
from .registry import ToolError, ToolOutput, tool

# Textures that ship inside every Roblox Studio and client: always valid, no upload needed.
BUILT_IN_TEXTURES = {
    "soft glow / plasma": "rbxasset://textures/particles/forcefield_glow_main.dds",
    "swirling vortex": "rbxasset://textures/particles/forcefield_vortex_main.dds",
    "sparkle dot": "rbxasset://textures/particles/sparkles_main.dds",
    "fire wisp": "rbxasset://textures/particles/fire_main.dds",
    "fire sparks": "rbxasset://textures/particles/fire_sparks_main.dds",
    "smoke puff": "rbxasset://textures/particles/smoke_main.dds",
    "explosion core": "rbxasset://textures/particles/explosion01_core_main.dds",
    "implosion streaks": "rbxasset://textures/particles/explosion01_implosion_main.dds",
    "shockwave ring": "rbxasset://textures/particles/explosion01_shockwave_main.dds",
    "explosion smoke": "rbxasset://textures/particles/explosion01_smoke_main.dds",
    "round glow": "rbxasset://textures/glow.png",
    "white circle": "rbxasset://textures/whiteCircle.png",
    "gradient": "rbxasset://textures/gradient.png",
    "star sparkle": "rbxasset://textures/sparkle.png",
}
_SCRIPTS = {"Script", "LocalScript", "ModuleScript"}


def _session(place: str) -> Dict[str, Any]:
    from . import roblox
    try:
        return roblox.pick_session(place)
    except ToolError as exc:
        if str(exc) != "no_link":
            raise
        from . import engines
        raise ToolError("Roblox Studio is not linked. " + engines.link_help())


def _ask(session: Dict[str, Any], job: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    """Queue one job for a Studio window and wait for its report (worker thread)."""
    try:
        job_id = link.post(session["session"], job)
    except KeyError:
        raise ToolError(f"Roblox Studio ('{session['place']}') was closed.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = link.result(job_id)
        if reply is not None:
            if not reply["ok"]:
                raise ToolError(f"Roblox Studio could not do that: {reply['message']}")
            return reply["report"]
        time.sleep(0.1)
    raise ToolError(f"Roblox Studio ('{session['place']}') did not answer within {timeout:.0f}s. Is a play test running? "
                    f"The link works in Edit mode.")


def _count(node: Dict[str, Any], tally: Dict[str, int]) -> None:
    if not isinstance(node, dict) or not isinstance(node.get("class"), str):
        raise ToolError("Every node needs a `class` (a Roblox class name such as Part, ParticleEmitter, Script).")
    tally[node["class"]] = tally.get(node["class"], 0) + 1
    if node["class"] in _SCRIPTS and not isinstance((node.get("props") or {}).get("Source"), str):
        raise ToolError(f"{node.get('name') or node['class']}: a script needs its Luau text in props.Source.")
    for child in node.get("children") or []:
        _count(child, tally)


@tool(
    "roblox_build",
    """Build something natively inside the user's open Roblox Studio place: Luau scripts, UI,
    Parts, lights, and effects made of ParticleEmitters, Beams and Trails. Only for what the user
    asked to have made or changed inside Roblox itself, or for adding to something sent from
    Unix what cannot travel (a script that triggers it, a sound, a control). A thing the user
    asked you to make, and then to send, get or import to Roblox, is made in Blender and sent
    with send_to_engine, never built here instead: an effect made in Unix arrives looking the
    way it looks in Unix. You describe a tree of instances; it is created under one
    Model (or Folder) named `name`, on the ground in front of the Studio camera. Building the
    same `name` again replaces it where it stands, so iterate freely. Load the skill
    roblox-native-vfx first.
    Animation without code: put these in a node's `attributes` and it moves both in Edit mode and
    in the running game: Spin {"v3":[x,y,z]} degrees per second (parts), Pulse 0.05 (size of
    parts, brightness of lights; PulseSpeed optional), Flicker 4 (Beams: the curve is re-rolled
    within that many studs twenty times a second). The model's Enabled attribute stops and starts
    it. Write a Script only for behaviour these do not cover.
    Node: {class, name, props, children, id, attributes}. Positions and CFrames of parts are
    relative to the model's origin on the ground (Y is up, units are studs); parts are Anchored
    unless you say otherwise. Property values: numbers, booleans and strings as they are; enums
    as "Enum.Material.Neon"; {"v3":[x,y,z]}; {"v2":[x,y]}; {"c3":[r,g,b]} 0-1 or
    {"rgb":[r,g,b]} 0-255; {"cf":[x,y,z]} or {"cf":[x,y,z,rx,ry,rz]} in degrees;
    {"range":[min,max]}; {"nseq":[[t,value],...]} (t from 0 to 1) or {"nseq":number};
    {"cseq":[[t,r,g,b],...]} or {"cseq":[r,g,b]}; {"udim2":[sx,ox,sy,oy]}; {"ref":"id"} to
    point at another node of this tree by its `id` (Beam.Attachment0, Weld.Part1, Adornee).
    Scripts are nodes of class Script, LocalScript or ModuleScript with props.Source holding the
    Luau text; they run when the user presses Play. The reply lists every property Studio
    refused, by name: fix those and build again. Then call roblox_look to see it.""",
    {
        "name": {"type": "string", "description": "Name of the model in Studio, e.g. 'EnergyOrb'."},
        "children": {"type": "array", "items": {"type": "object"}, "description": "The nodes inside the model."},
        "root_class": {"type": "string", "enum": ["Model", "Folder", "ScreenGui"], "description": "Default Model."},
        "parent": {"type": "string", "description": "Where to put it. Default 'Workspace'. Also e.g. 'ReplicatedStorage', "
                                                    "'ServerScriptService', 'StarterGui', 'StarterPlayer.StarterPlayerScripts'."},
        "position": {"type": "string", "enum": ["camera", "origin"],
                     "description": "'camera' (default): on the ground in front of the Studio camera. 'origin': at 0,0,0."},
        "place": {"type": "string", "description": "Name of the open place, when more than one Studio window is open."},
    },
    ["name", "children"],
    main_thread=False,
    timeout=180.0,
    mutates=True,
    label="Built it in Roblox Studio",
)
def roblox_build(name: str, children: List[Dict[str, Any]], root_class: str = "Model", parent: str = "Workspace",
                 position: str = "camera", place: str = "") -> Dict[str, Any]:
    name = name.strip()
    if not name or "." in name:
        raise ToolError("name must be a plain name without dots.")
    if not children:
        raise ToolError("Give at least one child node.")
    tally: Dict[str, int] = {}
    for child in children:
        _count(child, tally)
    session = _session(place)
    report = _ask(session, {"kind": "tree", "asset": name, "parent": parent, "place": position,
                            "root": {"class": root_class, "name": name, "children": children}}, timeout=150.0)
    out: Dict[str, Any] = {"place": session["place"], "in_studio": report.get("model") or f"{parent}.{name}",
                           "created": report.get("created", 0), "classes": tally, "replaced_existing": bool(report.get("replaced")),
                           "origin_studs": report.get("origin")}
    notes = [str(n) for n in (report.get("notes") or [])]
    if notes:
        out["refused_by_studio"] = notes[:40]
        out["fix"] = "Each line is a property or class Studio rejected: correct them and call roblox_build again with the same name."
    if report.get("animated"):
        out["animated"] = (f"{report['animated']} instances move through Spin/Pulse/Flicker: in Edit mode now, and in Play "
                           f"through the UnixAnimator script added to the model (Enabled attribute stops and starts it).")
    if any(c in _SCRIPTS for c in tally):
        out["scripts"] = "Your scripts run when the user presses Play, not in Edit mode. Emitters, beams and lights show at once."
    out["next"] = "roblox_look to see it (pass focus to frame it, unless the user asked you to leave their camera alone)."
    return out


@tool(
    "roblox_look",
    """See the user's Roblox Studio window: returns a screenshot of it, viewport and Explorer
    included. Use it after roblox_build or send_to_engine to judge the result with your own
    eyes, and before claiming anything about how it looks. `focus` first moves Studio's camera
    to frame an instance (e.g. 'Workspace.EnergyOrb'); leave it out to keep the user's view.
    Particle effects move, so one picture is one moment: look twice if timing matters.""",
    {
        "focus": {"type": "string", "description": "Path of a Model or Part to frame first, e.g. 'Workspace.EnergyOrb'."},
        "distance": {"type": "number", "description": "With focus: camera distance in studs (default fits the model)."},
        "place": {"type": "string", "description": "Name of the open place, when more than one Studio window is open."},
    },
    main_thread=False,
    timeout=90.0,
    label="Looked at Roblox Studio",
)
def roblox_look(focus: str = "", distance: Optional[float] = None, place: str = "") -> ToolOutput:
    session = _session(place)
    data: Dict[str, Any] = {"place": session["place"]}
    if focus:
        job: Dict[str, Any] = {"kind": "camera", "path": focus}
        if distance:
            job["distance"] = float(distance)
        data["camera"] = _ask(session, job, timeout=30.0)
        time.sleep(1.2)          # let particles spawn and the viewport redraw
    path = os.path.join(data_dir("looks"), time.strftime("studio_%H%M%S.png"))
    try:
        data["window"] = window_shot.shoot("RobloxStudioBeta.exe", path, title_contains=session["place"].split(" (")[0])
    except window_shot.ShotError as exc:
        raise ToolError(f"Could not capture Roblox Studio: {exc}")
    data["note"] = "This is the Studio window as it is now. Judge shape, colour, brightness and density against the request."
    return ToolOutput(data, [path])


@tool(
    "roblox_query",
    """Read the open Roblox Studio place: the instances under a path, with their classes, to a
    given depth, and optionally the values of named properties. Use it to find where things are
    before changing them, and to check what a build really created.""",
    {
        "path": {"type": "string", "description": "Default 'Workspace'. E.g. 'Workspace.EnergyOrb', 'ReplicatedStorage'."},
        "depth": {"type": "integer", "description": "Levels of children to list, 0-4 (default 1)."},
        "props": {"type": "array", "items": {"type": "string"}, "description": "Property names to read on every listed instance."},
        "place": {"type": "string", "description": "Name of the open place, when more than one Studio window is open."},
    },
    main_thread=False,
    timeout=60.0,
    label="Read the Roblox place",
)
def roblox_query(path: str = "Workspace", depth: int = 1, props: Optional[List[str]] = None, place: str = "") -> Dict[str, Any]:
    session = _session(place)
    return _ask(session, {"kind": "query", "path": path, "depth": max(0, min(int(depth), 4)), "props": (props or [])[:12]},
                timeout=40.0)


@tool(
    "roblox_remove",
    """Remove one instance (and everything inside it) from the open Roblox Studio place, e.g. an
    earlier attempt. The user can undo it in Studio with Ctrl+Z. Services cannot be removed.""",
    {
        "path": {"type": "string", "description": "E.g. 'Workspace.EnergyOrb_Model'."},
        "place": {"type": "string", "description": "Name of the open place, when more than one Studio window is open."},
    },
    ["path"],
    main_thread=False,
    timeout=60.0,
    mutates=True,
    label="Removed it from Roblox Studio",
)
def roblox_remove(path: str, place: str = "") -> Dict[str, Any]:
    if "." not in path:
        raise ToolError("Give the full path of one instance, e.g. 'Workspace.OldModel'.")
    return _ask(_session(place), {"kind": "remove", "path": path}, timeout=40.0)
