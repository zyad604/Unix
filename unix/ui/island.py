# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""The Unix island: a floating chat panel drawn in the 3D viewport with the GPU.

Layout, top to bottom: a status pill, a row of agent-mode tabs with history / new chat,
the conversation, a text box you type into directly, and an action row (Attach, Auto,
model, Send or Stop). Blender's own widgets cannot do rounded cards, gradients or image
previews, so everything here is drawn with ``gpu`` and ``blf`` and made interactive by one
always-on modal operator that handles clicks, the mouse wheel and typing.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import blf
import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from ..agent.runner import find_claude, find_codex
from ..core import DEFAULT_MODEL, EFFORTS, MODELS, engine
from ..tools import interact, team
from ..tools._util import data_dir, status as tool_status
from . import fly

ACCENT = (0.60, 0.36, 0.95)
ACCENT_DARK = (0.27, 0.18, 0.47)
GRAD_A = (0.431, 0.353, 0.961)   # #6E5AF5
GRAD_B = (0.745, 0.235, 0.882)   # #BE3CE1
CARD = (0.075, 0.072, 0.092)
FIELD = (0.045, 0.043, 0.058)
CHIP = (0.125, 0.120, 0.150)
TEXT = (0.90, 0.90, 0.92)
DIM = (0.60, 0.60, 0.66)
WARN = (1.0, 0.47, 0.44)
FONT = 0

# Agent modes: (id, tab label, placeholder, examples, instruction added to the prompt)
MODES: Tuple[Tuple[str, str, str, Tuple[str, ...], str], ...] = (
    ("agent", "Agent", "Ask Unix to build, change or check anything…",
     ("Build a cosy reading nook with an armchair and a lamp", "Check my meshes and fix the problems",
      "Light this scene for a product shot"),
     "[Mode: Agent] If this asks for something to be built that has several parts (a building with its surroundings, "
     "a furnished room, a character with clothes and gear, a vehicle, a set of props), work as a team: read the scene, "
     "settle the shared facts (sizes, positions, facing, style), then call work_in_parallel with two to four parts, and "
     "when they return join the parts up, check the whole and report. Build it alone only when it is one thing (an object, a "
     "visual effect, an animation), a question, or a change to what is already there."),
    ("model", "3D", "Describe the object to model, or paste a photo of it (Ctrl+V)…",
     ("A wooden treasure chest with metal bands, 50 cm wide", "A low-poly pine tree, 3 m tall",
      "A desk lamp with a brass arm and a cone shade"),
     "[Mode: 3D] Build this as a 3D object in the scene. Work in stages: blockout at real-world size, refine, "
     "materials, then verify with looks and check_mesh. If an image is attached, build what it shows: load the "
     "image-to-3d-by-modelling skill and correct the model against the photo with match_reference. A generated "
     "reference sheet helps but is optional: if image generation is not available, go on without it."),
    ("image", "Image", "Describe the image to generate…",
     ("Concept art of a small robot gardener, three-quarter view", "A logo decal: a purple fox head, flat",
      "A misty pine forest backdrop at dawn"),
     "[Mode: Image] The user is on the Image tab. If they want a picture, generate it with generate_image (purpose "
     "'concept' unless the request is clearly a decal, a backdrop or a reference sheet), show the result, give the "
     "file path and do not change the scene. But the tab is only a hint: if the message asks for a thing to be made "
     "or built (for example 'make this house' with a photo attached), they want it in the 3D scene, so build it as in "
     "3D mode, straight from their photo, whether or not image generation is available."),
    ("material", "Material", "Describe the surface for the selected objects…",
     ("Worn oak planks", "Stylised mossy cobblestone", "Brushed aluminium with light scratches"),
     "[Mode: Material] Make a material for the selected objects (or the ones named). For real-world surfaces "
     "try asset_search and asset_material first; for stylised or unusual ones use generate_texture. Make sure the "
     "objects have UVs, apply it, and finish with a rendered look."),
    ("vfx", "VFX", "Describe the effect: fire, explosion, magic, rain…",
     ("A campfire with rising sparks", "A blue magic swirl around the selected object",
      "An explosion, then turn it into a sprite sheet"),
     "[Mode: VFX] Build this visual effect. Start from a vfx_create preset (fire, smoke, explosion, sparks, "
     "magic_swirl, shockwave, energy_shield, rain, snow, dust_burst) and tune it; load the vfx skills for anything "
     "custom. Preview with look(shading='rendered') on a mid-effect frame. If it is for a game engine, make a sprite "
     "sheet with vfx_flipbook."),
    ("engine", "Engine", "Or tell Unix what to send and where…",
     ("Send the whole scene to Roblox Studio", "Send the selected asset to Unity",
      "Make a sprite sheet of this effect for Unreal"),
     "[Mode: Engine] The user wants this in a game engine, quickly. Call send_to_engine straight away with the engine "
     "they named (it finds the open project itself; call engine_targets only if it says it cannot). Do not check or "
     "repair the scene first unless the user asked for that. Then report in a few lines: what arrived, and anything "
     "the result says did not."),
    ("library", "Library", "Search free HDRIs, materials and models…",
     ("A soft studio HDRI for product shots", "An armchair model", "A concrete floor material"),
     "[Mode: Library] Find this in the asset library with asset_search (pick the kind that fits: hdri, texture or "
     "model). List the best matches briefly, then bring in or apply the best one and show a look."),
)
MODE_HELP = {
    "agent": "Ask for anything: build, change, fix or check the scene.",
    "model": "Describe an object and Unix models it. Paste a photo (Ctrl+V) or attach one to copy a real one.",
    "image": "Describe a picture and Unix generates it. The scene is not changed.",
    "material": "Select objects, describe a surface, and Unix makes and applies it.",
    "library": "Search free real-world lighting, materials and models, and bring one in.",
    "vfx": "Describe an effect and Unix builds it, animated. Press Play (top left) to watch it move.",
    "engine": "Send the selected objects straight into your game engine. One click, no AI needed.",
}
MODE_IDS = [m[0] for m in MODES]
MODE_PROMPTS = {m[0]: m[4] for m in MODES}

settings = {"visible": True, "collapsed": False, "mode": "agent", "auto": False, "scroll": 0.0, "notice": "",
            # layout the user can change: position offset (pixels), overall scale, width and
            # conversation height (interface units; 0 = default for the workspace)
            "dx": 0.0, "dy": 0.0, "scale": 1.0, "width": 0.0, "view_h": 460.0,
            # "beginner" hides Blender's own interface, "advanced" shows it; None = not chosen yet
            "level": None}
_LAYOUT_KEYS = ("dx", "dy", "scale", "width", "view_h", "collapsed", "auto", "level")
VIEW_H = (220.0, 1400.0)        # the conversation's height, interface units: at 100 it showed three lines
VIEW_SHARE = 0.85               # of the viewport's height it may take
FOLDED_LINES = 8                # a long message (a pasted spec) shows this many lines until it is opened
_opened: set = set()            # long messages the user opened, by their index in the chat
_state = {"was_busy": False, "level_applied": False}
composer = {"text": "", "cursor": 0, "focused": False}
hit_rects: Dict[int, List[Tuple[str, float, float, float, float]]] = {}
_handle = None
_textures: Dict[str, Tuple[Any, int, int]] = {}
_wrap_cache: Dict[Tuple[str, int, int], Tuple[List[str], List[bool]]] = {}
SHOWN_MESSAGES = 60     # the chat scrolls back this far; Copy > The whole chat has everything
# Selecting chat text: a line is known by (message, line) and a place in it by its character. ``a`` is where
# the drag began, ``b`` where it is now. ``_drawn`` is where each line was drawn last (for the mouse),
# ``_view["rows"]`` the conversation's rows as last drawn (for Copy).
selection: Dict[str, Any] = {"a": None, "b": None}
_drawn: Dict[int, List[Dict[str, Any]]] = {}
_view: Dict[str, List[Dict[str, Any]]] = {"rows": []}
# A finished message's rows do not change: they are made once, not every frame (60 messages, one of them a
# pasted spec of a thousand lines, made the island stutter while typing).
_row_cache: Dict[int, Tuple[Tuple, List[Dict[str, Any]]]] = {}
_widths: Dict[Tuple[int, int, str, int], float] = {}
SELECT = (0.60, 0.36, 0.95, 0.45)
# the events the free camera may take: only for these is it worth asking whether the mouse is over the island
_CAMERA_EVENTS = {'RIGHTMOUSE', 'F', *fly.KEYS}
_cli = {"checked": 0.0, "claude": True, "codex": True}
# A pasted design document must arrive whole: it was cut at 4,000 characters, and a goal built from it never
# saw most of what was asked.
COMPOSER_MAX = 200_000


# -- drawing primitives ----------------------------------------------------------

def _rounded(x, y, w, h, radius, steps: int = 6):
    radius = max(0.0, min(radius, w / 2, h / 2))
    pts = []
    for cx, cy, start in ((x + w - radius, y + h - radius, 0.0), (x + radius, y + h - radius, 90.0),
                          (x + radius, y + radius, 180.0), (x + w - radius, y + radius, 270.0)):
        for i in range(steps + 1):
            a = math.radians(start + 90.0 * i / steps)
            pts.append((cx + math.cos(a) * radius, cy + math.sin(a) * radius))
    return pts


def _fan(points):
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return [(cx, cy)] + points + [points[0]]


def _fill(points, color) -> None:
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'TRI_FAN', {"pos": _fan(points)}).draw(shader)


def _fill_gradient(points, left=GRAD_A, right=GRAD_B, alpha: float = 1.0) -> None:
    shader = gpu.shader.from_builtin('SMOOTH_COLOR')
    xs = [p[0] for p in points]
    lo, span = min(xs), max(max(xs) - min(xs), 1e-6)
    pos = _fan(points)
    colors = [tuple(left[i] + (right[i] - left[i]) * ((p[0] - lo) / span) for i in range(3)) + (alpha,) for p in pos]
    batch_for_shader(shader, 'TRI_FAN', {"pos": pos, "color": colors}).draw(shader)


def _outline(points, color, width: float = 1.0) -> None:
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.line_width_set(width)
    shader.bind()
    shader.uniform_float("color", color)
    batch_for_shader(shader, 'LINE_STRIP', {"pos": points + [points[0]]}).draw(shader)


def _text(text: str, x, y, size, color=TEXT, alpha: float = 1.0) -> None:
    blf.size(FONT, size)
    blf.color(FONT, color[0], color[1], color[2], alpha)
    blf.position(FONT, round(x), round(y), 0)      # between pixels the glyphs are smeared over two: soft, uneven text
    blf.draw(FONT, text)


def _width(text: str, size) -> float:
    blf.size(FONT, size)
    return blf.dimensions(FONT, text)[0]


def _wrap(text: str, max_width: float, size: float) -> List[str]:
    return _wrap_ends(text, max_width, size)[0]


def _wrap_ends(text: str, max_width: float, size: float) -> Tuple[List[str], List[bool]]:
    """The text in lines that fit ``max_width``, and for each line whether a paragraph ends there."""
    key = (text, int(max_width), int(size * 10))
    cached = _wrap_cache.pop(key, None)
    if cached is not None:
        _wrap_cache[key] = cached          # now the newest: what is on screen stays cached
        return cached
    blf.size(FONT, size)
    lines: List[str] = []
    ends: List[bool] = []
    for raw in text.replace("**", "").replace("`", "").splitlines():
        raw = raw.rstrip()
        if not raw.strip():
            lines.append("")
            ends.append(True)
            continue
        raw = raw.strip()
        if raw.startswith(("- ", "* ")):
            raw = "•  " + raw[2:]
        current = ""
        for word in raw.split(" "):
            trial = f"{current} {word}" if current else word
            if current and blf.dimensions(FONT, trial)[0] > max_width:
                lines.append(current)
                ends.append(False)
                current = word
            else:
                current = trial
        lines.append(current)
        ends.append(True)
    if len(_wrap_cache) > 600:          # forget the oldest only: clearing it all re-wrapped a long goal mid-typing
        for old in list(_wrap_cache)[:200]:
            del _wrap_cache[old]
    _wrap_cache[key] = (lines, ends)
    return lines, ends


def _image(path: str, x, y, w, h) -> None:
    entry = _textures.get(path)
    if entry is None:
        return
    shader = gpu.shader.from_builtin('IMAGE')
    shader.bind()
    shader.uniform_sampler("image", entry[0])
    batch_for_shader(shader, 'TRI_FAN', {"pos": [(x, y), (x + w, y), (x + w, y + h), (x, y + h)],
                                         "texCoord": [(0, 0), (1, 0), (1, 1), (0, 1)]}).draw(shader)
    gpu.state.blend_set('ALPHA')


def prepare(path: str) -> None:
    """Main thread, outside drawing: load an image file into a GPU texture (cached)."""
    if not path or path in _textures or not os.path.isfile(path):
        return
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            return
        if max(w, h) > 512:
            factor = 512 / max(w, h)
            img.scale(max(1, int(w * factor)), max(1, int(h * factor)))
            w, h = img.size
        pixels = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        rgba = pixels.reshape(-1, 4)
        rgba[:, :3] = np.power(np.clip(rgba[:, :3], 0.0, 1.0), 2.2)  # the viewport expects linear colour
        if len(_textures) > 16:
            _textures.clear()
        _textures[path] = (gpu.types.GPUTexture((w, h), format='RGBA16F', data=gpu.types.Buffer('FLOAT', w * h * 4, pixels)), w, h)
    except Exception:
        pass
    finally:
        bpy.data.images.remove(img)


def _images_in_view() -> List[str]:
    paths = []
    for message in engine.messages[-6:]:
        for part in message.get("parts", []):
            if part["type"] == "steps":
                for step in part["steps"]:
                    if step.get("images"):
                        paths.append(step["images"][0])
    return paths[-3:]


# -- engine targets (found on a worker thread: the lookup runs PowerShell) --------------
_targets: Dict[str, Any] = {"list": [], "loading": False, "time": 0.0}


def refresh_targets() -> None:
    import threading
    if _targets["loading"]:
        return
    _targets["loading"] = True

    def work():
        from ..tools import engines
        try:
            _targets["list"] = engines.find_targets(refresh=True)
        except Exception:
            _targets["list"] = []
        _targets.update(loading=False, time=time.monotonic())

    threading.Thread(target=work, daemon=True, name="UnixEngineScan").start()


def _engine_rows(ui: float) -> List[Dict[str, Any]]:
    small, lh = 11 * ui, 17 * ui
    rows: List[Dict[str, Any]] = [{"kind": "text", "text": "Send the selected objects to:", "h": lh, "color": DIM, "size": small}]
    if _targets["loading"] and not _targets["list"]:
        rows.append({"kind": "text", "text": "Looking for your engines…", "h": lh, "color": DIM, "size": small})
    for index, target in enumerate(_targets["list"][:6]):
        where = target["name"] if target["project"] else "import file + drag tile"
        label = f"{target['label']}   ·   {where}" + ("   ·   open now" if target["running"] else "")
        rows.append({"kind": "chip", "text": label, "h": 26 * ui, "name": f"send:{index}"})
    rows.append({"kind": "chip", "text": "Drag tile   ·   drop the file into any window", "h": 26 * ui, "name": "send:drag"})
    rows.append({"kind": "gap", "h": 6 * ui})
    return rows


# -- conversation rows -----------------------------------------------------------

def _made_from(message, width: float, ui: float, index: int) -> Tuple:
    """What a finished message's rows depend on: if none of it changed, they are the same."""
    parts = tuple((p["type"], id(p.get("text")), len(p.get("steps") or ()),
                   any(s.get("images") and s["images"][0] in _textures for s in p.get("steps") or ()))
                  for p in message.get("parts", ()))
    return (id(message), id(message.get("text")), message.get("meta"), parts, round(width, 2), round(ui, 3),
            index in _opened)


def _message_rows(index: int, message, width: float, ui: float, live: bool) -> List[Dict[str, Any]]:
    """One message as drawable rows. Each line is known as (message, line), what a selection holds on to;
    a box of lines (a prompt, a note) by its first line's key."""
    body, small, lh = 12.5 * ui, 11 * ui, 17 * ui
    rows: List[Dict[str, Any]] = []
    counter = [0]

    def key(lines: int = 1) -> Tuple[int, int]:
        counter[0] += lines
        return index, counter[0] - lines

    if message["role"] == "user":
        lines, ends = _wrap_ends(message["text"], width * 0.72, body)
        label = next((m[1] for m in MODES if m[0] == message.get("mode")), "")
        folded = len(lines) > FOLDED_LINES + 2 and index not in _opened
        hidden = len(lines) - FOLDED_LINES
        if folded:
            lines, ends = lines[:FOLDED_LINES], ends[:FOLDED_LINES]
        rows.append({"kind": "user", "lines": lines, "h": len(lines) * lh + 12 * ui, "key0": key(len(lines)), "ends": ends,
                     "tag": label if message.get("mode", "agent") != "agent" else "",
                     "files": len(message.get("attachments") or [])})
        if folded or index in _opened and hidden > 2:
            rows.append({"kind": "chip", "text": f"Show all  ·  {hidden:,} more lines" if folded else "Show less",
                         "h": 26 * ui, "name": f"fold:{index}", "right": True})
        rows.append({"kind": "gap", "h": 6 * ui})
        return rows
    for part in message.get("parts", []):
        if part["type"] == "steps":
            steps = part["steps"]
            if not live:
                failed = sum(1 for s in steps if s["status"] == "error")
                summary = f"{len(steps)} step{'s' if len(steps) != 1 else ''}" + (f"  ·  {failed} failed" if failed else "")
                rows.append({"kind": "text", "text": summary, "h": lh, "color": DIM, "size": small, "key": key(), "end": True})
            for step in (steps[-3:] if live else []):
                rows.append({"kind": "step", "step": step, "h": lh})
            image = next((s["images"][0] for s in reversed(steps) if s.get("images")), "")
            if image in _textures:
                _tex, tw, th = _textures[image]
                ih = min(120 * ui, width * 0.5 * th / tw)
                rows.append({"kind": "image", "path": image, "h": ih + 6 * ui, "ih": ih, "iw": ih * tw / th})
        elif part["type"] == "text":
            for line, end in zip(*_wrap_ends(part["text"], width, body)):
                rows.append({"kind": "text", "text": line, "h": lh if line else lh * 0.5, "color": TEXT, "size": body,
                             "key": key(), "end": end})
        else:  # error / note
            lines, ends = _wrap_ends(part["text"], width - 22 * ui, body)
            rows.append({"kind": "notice", "lines": lines, "h": len(lines) * lh + 14 * ui, "key0": key(len(lines)),
                         "ends": ends, "color": WARN if part["type"] == "error" else DIM})
    if live:
        # what is happening right now and for how long, under everything else, so a long silence
        # (the model writing a big script, a bake) never looks like nothing is happening
        since = engine.activity_since or engine.started_at
        now = (engine.activity or "Thinking").rstrip("…")
        rows.append({"kind": "text", "text": f"{now}…  ·  {int(time.monotonic() - since)}s", "h": lh,
                     "color": DIM, "size": body})
    if message.get("meta"):
        rows.append({"kind": "text", "text": message["meta"], "h": lh, "color": DIM, "size": small, "key": key(), "end": True})
    rows.append({"kind": "gap", "h": 8 * ui})
    return rows


def _rows(width: float, ui: float) -> List[Dict[str, Any]]:
    """The conversation as drawable rows, oldest first."""
    body, small, lh = 12.5 * ui, 11 * ui, 17 * ui
    rows: List[Dict[str, Any]] = []
    mode = settings["mode"]
    if mode == "engine":
        rows += _engine_rows(ui)
    if not engine.messages:
        spec = MODES[MODE_IDS.index(mode)]
        rows.append({"kind": "text", "text": "Or ask Unix:" if mode == "engine" else "Try one of these:",
                     "h": lh, "color": DIM, "size": small})
        for index, example in enumerate(spec[3]):
            rows.append({"kind": "chip", "text": example, "h": 26 * ui, "name": f"chip:{index}"})
        return rows
    first = max(0, len(engine.messages) - SHOWN_MESSAGES)
    if len(_row_cache) > SHOWN_MESSAGES * 3:
        _row_cache.clear()
    for index, message in enumerate(engine.messages[first:], first):
        if message is engine.messages[-1] and engine.busy:
            rows += _message_rows(index, message, width, ui, live=True)
            continue
        made_from = _made_from(message, width, ui, index)
        cached = _row_cache.get(index)
        if cached is None or cached[0] != made_from:
            cached = _row_cache[index] = (made_from, _message_rows(index, message, width, ui, live=False))
        rows += cached[1]
    if engine.goal:
        lines = _wrap(engine.goal_line(), width - 22 * ui, small)[:3]
        rows.append({"kind": "notice", "lines": lines, "h": len(lines) * lh + 14 * ui,
                     "color": (0.45, 0.85, 0.60) if engine.goal["state"] == "done" else ACCENT})
    plan = interact.get_plan()
    if plan:
        rows.append({"kind": "plan", "done": sum(1 for s in plan if s["status"] == "done"), "total": len(plan), "h": 20 * ui})
    question = interact.pending_question()
    if question:
        lines = _wrap(question["text"], width - 22 * ui, body)
        rows.append({"kind": "notice", "lines": lines, "h": len(lines) * lh + 14 * ui, "color": ACCENT})
        for index, option in enumerate(question["options"]):
            rows.append({"kind": "chip", "text": option, "h": 26 * ui, "name": f"opt:{index}"})
        rows.append({"kind": "text", "text": "…or type your answer below and press Enter.", "h": lh, "color": DIM, "size": small})
    if settings["notice"]:
        lines = _wrap(settings["notice"], width - 22 * ui, body)
        rows.append({"kind": "notice", "lines": lines, "h": len(lines) * lh + 14 * ui, "color": WARN})
    return rows


# -- the island ---------------------------------------------------------------------

def _status() -> Tuple[str, Tuple[float, float, float]]:
    model = getattr(bpy.context.window_manager, "unix_model", DEFAULT_MODEL)
    available = _cli["codex"] if model == "codex" else _cli["claude"]
    if not available:
        return ("Offline  ·  " + ("Codex" if model == "codex" else "Claude") + " CLI not found", WARN)
    if engine.status == "waiting":
        return "Needs your answer", ACCENT
    if engine.busy:
        return f"{tool_status['text'] or 'Working'}  ·  {int(time.monotonic() - engine.started_at)}s", ACCENT
    return "Ready", (0.45, 0.85, 0.60)


def _draw() -> None:
    if not settings["visible"]:
        return
    region, area = bpy.context.region, bpy.context.area
    if region is None or area is None or region.width < 560 or region.height < 320:
        if region is not None:
            hit_rects.pop(region.as_pointer(), None)
        return
    try:
        gpu.state.blend_set('ALPHA')
        _draw_island(region, area)
    except Exception as exc:  # a drawing error must never break the viewport
        print(f"[Unix] island draw error: {exc!r}")
    finally:
        gpu.state.blend_set('NONE')
        gpu.state.line_width_set(1.0)


def _draw_island(region, area) -> None:
    ui = bpy.context.preferences.system.ui_scale * settings["scale"]
    wm = bpy.context.window_manager
    pad = 14 * ui
    small, body = 11.5 * ui, 13 * ui
    lh = 18 * ui
    rects: List[Tuple[str, float, float, float, float]] = []
    hit_rects[region.as_pointer()] = rects

    def mark(name, x, y, w, h):
        rects.append((name, x, y, w, h))

    def pill(name, x, y, label, *, active=False, gradient=False, size=small, height=24 * ui, color=TEXT, fill=CHIP,
             pad_x=10 * ui):
        w = _width(label, size) + pad_x * 2
        shape = _rounded(x, y, w, height, height / 2)
        if gradient:
            _fill_gradient(shape)
        else:
            _fill(shape, (*(ACCENT_DARK if active else fill), 0.96))
            if not active:
                _outline(shape, (1.0, 1.0, 1.0, 0.10))
        _text(label, x + pad_x, y + (height - size) / 2 + 1.5 * ui, size, (1, 1, 1) if (active or gradient) else color)
        if name:
            mark(name, x, y, w, height)
        return w

    if settings["collapsed"]:
        _draw_dock(region, ui, mark)
        return

    toolbar = next((r.width for r in area.regions if r.type == 'TOOLS' and r.width > 1), 0)
    workspace = bpy.context.workspace
    from . import simple
    zen = (workspace is not None and workspace.name == "Zen") or simple.level() == "beginner"   # centred
    width = min((settings["width"] or (700 if zen else 660)) * ui, region.width - 16 * ui)
    x0 = ((region.width - width) / 2 if zen else toolbar + 18 * ui) + settings["dx"]
    y0 = 20 * ui + settings["dy"]
    inner = width - pad * 2

    # heights, bottom up
    action_h, tabs_h, help_h = 32 * ui, 30 * ui, 20 * ui
    draft = composer["text"]
    if len(draft) > 2000:        # only the last lines show: wrapping a pasted document every frame would stall the window
        tail = draft[-2000:]
        draft = tail[tail.find("\n") + 1:] if "\n" in tail[:1500] else tail
    draft_lines = _wrap(draft, inner - 20 * ui, body) if draft else [""]
    if draft.endswith("\n"):
        draft_lines = draft_lines + [""]
    visible_lines = draft_lines[-4:]
    composer_h = len(visible_lines) * lh + 16 * ui
    attached = [p for p in wm.unix_attachments.split("|") if p]
    attach_h = 22 * ui if attached else 0
    rows = [] if settings["collapsed"] else _rows(inner, ui)
    content_h = sum(r["h"] for r in rows)
    view_h = 0.0 if settings["collapsed"] else min(max(content_h, 60 * ui), settings["view_h"] * ui, region.height * VIEW_SHARE)
    height = pad + action_h + 8 * ui + composer_h + attach_h + (view_h + 10 * ui if view_h else 0) + help_h + tabs_h + pad

    x0 = min(max(x0, 6.0), max(6.0, region.width - width - 6.0))  # never let it leave the viewport
    y0 = min(max(y0, 6.0), max(6.0, region.height - height - 34 * ui))

    # card with a soft shadow
    for i in range(4, 0, -1):
        _fill(_rounded(x0 - i * 2, y0 - i * 2 - 3, width + i * 4, height + i * 4, 16 * ui + i * 2), (0.0, 0.0, 0.0, 0.05))
    card = _rounded(x0, y0, width, height, 16 * ui)
    _fill(card, (*CARD, 0.96))
    _outline(card, (1.0, 1.0, 1.0, 0.10))
    mark("card", x0, y0, width, height)
    mark("move", x0, y0 + height - pad - tabs_h, width, tabs_h + pad)  # the top strip drags the island
    gx, gy = x0 + width - 5 * ui, y0 + height - 5 * ui                 # resize grip in the top-right corner
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader.bind()
    shader.uniform_float("color", (1.0, 1.0, 1.0, 0.35))
    gpu.state.line_width_set(1.5)
    batch_for_shader(shader, 'LINES', {"pos": [(gx - 9 * ui, gy), (gx, gy - 9 * ui), (gx - 5 * ui, gy), (gx, gy - 5 * ui)]}).draw(shader)
    mark("resize", x0 + width - 15 * ui, y0 + height - 15 * ui, 15 * ui, 15 * ui)

    # status pill, above the card
    label, color = _status()
    sw = _width(label, small) + 30 * ui
    sy = y0 + height + 6 * ui
    shape = _rounded(x0, sy, sw, 22 * ui, 11 * ui)
    _fill(shape, (*CARD, 0.96))
    _outline(shape, (1.0, 1.0, 1.0, 0.10))
    pulse = 0.55 + 0.45 * math.sin(time.monotonic() * 4.0) if engine.busy else 1.0
    _fill(_rounded(x0 + 9 * ui, sy + 7.5 * ui, 7 * ui, 7 * ui, 3.5 * ui), (*color, pulse))
    _text(label, x0 + 21 * ui, sy + 6.5 * ui, small, TEXT)
    mark("status", x0, sy, sw, 22 * ui)
    screen = bpy.context.screen
    playing = screen is not None and screen.is_animation_playing
    play_w = pill("play", x0 + sw + 6 * ui, sy, "■  Stop" if playing else "▶  Play", active=playing,
                  height=22 * ui, fill=CARD)
    sw += play_w + 6 * ui
    bx = x0 + width
    for name, text in (("bigger", "+"), ("smaller", "–")):
        bw = _width(text, small) + 18 * ui
        bx -= bw
        shape = _rounded(bx, sy, bw, 22 * ui, 11 * ui)
        _fill(shape, (*CARD, 0.96))
        _outline(shape, (1.0, 1.0, 1.0, 0.10))
        _text(text, bx + 9 * ui, sy + 6.5 * ui, small, TEXT)
        mark(name, bx, sy, bw, 22 * ui)
        bx -= 4 * ui
    size_label = "Size"
    bx -= _width(size_label, small) + 4 * ui
    _text(size_label, bx, sy + 6.5 * ui, small, DIM)
    bx -= 10 * ui                           # (Beginner / Advanced is in the top bar)
    if composer["focused"]:
        hint = "Enter to send   ·   Shift+Enter for a new line   ·   Esc to stop typing"
        if x0 + sw + 12 * ui + _width(hint, small) < bx - 10 * ui:
            _text(hint, bx - _width(hint, small) - 12 * ui, sy + 6.5 * ui, small, DIM)

    # the team: one chip per helper working on a part of this turn, stacked above the status pill
    helpers = team.state["workers"] if engine.busy else []
    chip_h, chip_w = 26 * ui, min(330 * ui, width * 0.6)
    for index, helper in enumerate(helpers):
        hy = sy + 28 * ui + (len(helpers) - 1 - index) * (chip_h + 5 * ui)
        if hy + chip_h > region.height - 6 * ui:
            continue
        working = helper["status"] == "working"
        shape = _rounded(x0, hy, chip_w, chip_h, chip_h / 2)
        _fill(shape, (*CARD, 0.96))
        _outline(shape, (*helper["colour"], 0.55 if working else 0.2))
        glow = 0.55 + 0.45 * math.sin(time.monotonic() * 4.0 + index) if working else 1.0
        dot = helper["colour"] if helper["status"] in {"working", "done"} else WARN
        _fill(_rounded(x0 + 10 * ui, hy + 9 * ui, 8 * ui, 8 * ui, 4 * ui), (*dot, glow))
        name = helper["name"]
        _text(name, x0 + 25 * ui, hy + 8 * ui, small, TEXT)
        doing = helper["doing"] if working else {"done": "done", "failed": "failed", "stopped": "stopped"}.get(helper["status"], "")
        if working:
            doing = f"{doing}  ·  {int(time.monotonic() - helper['started'])}s"
        room = chip_w - 25 * ui - _width(name, small) - (40 * ui if working else 18 * ui)
        while doing and _width(doing, small) > room and len(doing) > 4:
            doing = doing[:-4] + "…"
        if doing and room > 30 * ui:
            _text(doing, x0 + 33 * ui + _width(name, small), hy + 8 * ui, small, DIM)
        if working:
            _text("✕", x0 + chip_w - 22 * ui, hy + 8 * ui, small, TEXT, 0.8)
            mark(f"helper_stop:{index}", x0 + chip_w - 30 * ui, hy, 30 * ui, chip_h)
        mark("status", x0, hy, chip_w - (30 * ui if working else 0), chip_h)

    # tabs
    ty = y0 + height - pad - tabs_h + 3 * ui
    tx = x0 + pad
    for mode_id, tab_label, *_rest in MODES:
        tx += pill(f"tab:{mode_id}", tx, ty, tab_label, active=settings["mode"] == mode_id, pad_x=8 * ui) + 4 * ui
    rx = x0 + width - pad
    for name, text in (("collapse", "Hide" if not settings["collapsed"] else "Show"), ("new", "New"), ("history", "History"),
                       ("copy", "Copy")):
        w = _width(text, small) + 16 * ui
        rx -= w
        if rx > tx + 4 * ui:  # leave the buttons out rather than draw them over the tabs
            pill(name, rx, ty, text, pad_x=8 * ui)
        rx -= 4 * ui
    _text(MODE_HELP[settings["mode"]], x0 + pad + 2 * ui, ty - help_h + 5 * ui, small, DIM)

    # conversation
    if view_h:
        v_bottom = y0 + pad + action_h + 8 * ui + composer_h + attach_h + 6 * ui
        v_top = v_bottom + view_h
        max_scroll = max(0.0, content_h - view_h)
        settings["scroll"] = min(max(settings["scroll"], 0.0), max_scroll)
        offset = -settings["scroll"] if content_h > view_h else view_h - content_h
        mark("chat", x0 + pad, v_bottom, inner, view_h)      # drag over the text to select it
        _view["rows"] = rows
        drawn: List[Dict[str, Any]] = []
        acc = 0.0
        # rows half in view are cut at its edge by the GPU, mid-line if need be: nothing is left out or overlaps
        old = gpu.state.scissor_get()       # the region's own box, in the coordinates the scissor takes
        left, bottom = (old[0], old[1]) if old[2] > 0 and old[3] > 0 else (region.x, region.y)
        gpu.state.scissor_test_set(True)    # (not region.x/y: with the timeline under the viewport that cut off the chat's bottom)
        gpu.state.scissor_set(int(left + x0), int(bottom + v_bottom), int(math.ceil(width)), int(math.ceil(view_h)))
        try:
            for row in reversed(rows):
                yb = v_bottom + acc + offset
                acc += row["h"]
                if yb + row["h"] < v_bottom or yb > v_top:
                    continue
                _draw_row(row, x0 + pad, yb, inner, ui, mark, drawn, (v_bottom, v_top))
        finally:
            if old[2] > 0 and old[3] > 0:
                gpu.state.scissor_set(*old)
            else:
                gpu.state.scissor_test_set(False)
        _drawn[region.as_pointer()] = [d for d in drawn if v_bottom - lh <= d["y"] <= v_top]
        if max_scroll > 0:  # scrollbar
            track_h = view_h
            thumb_h = max(24 * ui, track_h * view_h / content_h)
            pos = (settings["scroll"] / max_scroll) * (track_h - thumb_h)
            _fill(_rounded(x0 + width - 7 * ui, v_bottom + pos, 3 * ui, thumb_h, 1.5 * ui), (1.0, 1.0, 1.0, 0.18))

    # attachments line
    cy = y0 + pad + action_h + 8 * ui
    if attached:
        names = ", ".join(os.path.basename(p) for p in attached[:3]) + (" …" if len(attached) > 3 else "")
        _text(f"Attached: {names}", x0 + pad + 4 * ui, cy + composer_h + 5 * ui, small, DIM)
        cw = _width("clear", small) + 16 * ui
        pill("clear_attach", x0 + width - pad - cw, cy + composer_h + 1 * ui, "clear", height=18 * ui)

    # composer: the text box
    field = _rounded(x0 + pad, cy, inner, composer_h, 12 * ui)
    _fill(field, (*FIELD, 0.98))
    _outline(field, (*ACCENT, 0.85) if composer["focused"] else (1.0, 1.0, 1.0, 0.16), 1.5 if composer["focused"] else 1.0)
    mark("composer", x0 + pad, cy, inner, composer_h)
    text_y = cy + composer_h - 8 * ui - lh * 0.72
    if draft:
        for line in visible_lines:
            _text(line, x0 + pad + 10 * ui, text_y, body, TEXT)
            text_y -= lh
        if composer["focused"] and int(time.monotonic() * 2) % 2 == 0:
            last = visible_lines[-1]
            cx = x0 + pad + 10 * ui + _width(last, body) + 1 * ui
            caret_y = cy + composer_h - 8 * ui - lh * 0.72 - (len(visible_lines) - 1) * lh
            _fill([(cx, caret_y - 3 * ui), (cx + 1.5 * ui, caret_y - 3 * ui), (cx + 1.5 * ui, caret_y + 11 * ui), (cx, caret_y + 11 * ui)], (*TEXT, 0.9))
    else:
        if composer["focused"] and int(time.monotonic() * 2) % 2 == 0:
            cx = x0 + pad + 9 * ui
            _fill([(cx, text_y - 3 * ui), (cx + 1.5 * ui, text_y - 3 * ui), (cx + 1.5 * ui, text_y + 11 * ui), (cx, text_y + 11 * ui)], (*TEXT, 0.9))
        placeholder = "Type your answer…" if interact.pending_question() else MODES[MODE_IDS.index(settings["mode"])][2]
        _text(placeholder, x0 + pad + 14 * ui, text_y, body, DIM, 0.8)

    # action row
    ay = y0 + pad
    ax = x0 + pad
    ax += pill("attach", ax, ay + 4 * ui, f"+ Attach ({len(attached)})" if attached else "+ Attach") + 6 * ui
    model = next((m[1] for m in MODELS if m[0] == wm.unix_model), MODELS[0][1])
    ax += pill("model", ax, ay + 4 * ui, f"Model: {model}  ▾") + 6 * ui
    effort = next((e[1] for e in EFFORTS if e[0] == wm.unix_effort), "Medium")
    ax += pill("effort", ax, ay + 4 * ui, f"Effort: {effort}  ▾") + 10 * ui
    # Auto: a real switch, so on and off are unmistakable
    _text("Auto", ax, ay + 10.5 * ui, small, TEXT if settings["auto"] else DIM)
    sw_x, sw_w, sw_h = ax + _width("Auto", small) + 7 * ui, 30 * ui, 16 * ui
    track = _rounded(sw_x, ay + 8 * ui, sw_w, sw_h, sw_h / 2)
    if settings["auto"]:
        _fill_gradient(track)
    else:
        _fill(track, (1.0, 1.0, 1.0, 0.14))
    knob = sw_x + (sw_w - sw_h + 2 * ui if settings["auto"] else 2 * ui)
    _fill(_rounded(knob, ay + 10 * ui, sw_h - 4 * ui, sw_h - 4 * ui, (sw_h - 4 * ui) / 2), (1.0, 1.0, 1.0, 0.95))
    mark("auto", ax - 4 * ui, ay + 3 * ui, sw_x + sw_w - ax + 8 * ui, 26 * ui)
    if engine.busy and not interact.pending_question():
        label = "Stop"
        w = _width(label, body) + 34 * ui
        shape = _rounded(x0 + width - pad - w, ay + 1 * ui, w, 28 * ui, 14 * ui)
        _fill(shape, (0.62, 0.18, 0.25, 0.96))
        _text(label, x0 + width - pad - w + 17 * ui, ay + 9.5 * ui, body, (1, 1, 1))
        mark("stop", x0 + width - pad - w, ay + 1 * ui, w, 28 * ui)
    else:
        label = "Send"
        w = _width(label, body) + 34 * ui
        shape = _rounded(x0 + width - pad - w, ay + 1 * ui, w, 28 * ui, 14 * ui)
        _fill_gradient(shape, alpha=1.0 if (draft.strip() or engine.busy) else 0.45)
        _text(label, x0 + width - pad - w + 17 * ui, ay + 9.5 * ui, body, (1, 1, 1))
        mark("send", x0 + width - pad - w, ay + 1 * ui, w, 28 * ui)


def _last_reply() -> str:
    """What Unix said last, as plain text ("" while it works or when the last word was the user's)."""
    if engine.busy or not engine.messages or engine.messages[-1].get("role") != "assistant":
        return ""
    return "\n".join(p.get("text") or "" for p in engine.messages[-1].get("parts", ()) if p.get("type") == "text").strip()


def _draw_dock(region, ui: float, mark) -> None:
    """The chat closed: one slim bar at the bottom of the viewport. Type and send in it, see what Unix is
    doing, and open the whole chat with the button on its right. Unix's last reply shows above it for a while."""
    small, body = 11.5 * ui, 13 * ui
    height = 44 * ui
    width = min(600 * ui, region.width - 24 * ui)
    x0, y0 = (region.width - width) / 2, 22 * ui
    for i in range(4, 0, -1):
        _fill(_rounded(x0 - i * 2, y0 - i * 2 - 3, width + i * 4, height + i * 4, height / 2 + i * 2), (0.0, 0.0, 0.0, 0.05))
    bar = _rounded(x0, y0, width, height, height / 2)
    _fill(bar, (*CARD, 0.94))
    _outline(bar, (*ACCENT, 0.85) if composer["focused"] else (1.0, 1.0, 1.0, 0.12), 1.5 if composer["focused"] else 1.0)
    mark("card", x0, y0, width, height)

    # right: the round Send (or Stop) button, and Chat to open the whole conversation
    knob = height - 10 * ui
    kx = x0 + width - 5 * ui - knob
    if engine.busy and not interact.pending_question():
        _fill(_rounded(kx, y0 + 5 * ui, knob, knob, knob / 2), (0.62, 0.18, 0.25, 0.96))
        _fill(_rounded(kx + knob * 0.34, y0 + 5 * ui + knob * 0.34, knob * 0.32, knob * 0.32, 2 * ui), (1, 1, 1, 0.95))
        mark("stop", kx, y0 + 5 * ui, knob, knob)
    else:
        _fill_gradient(_rounded(kx, y0 + 5 * ui, knob, knob, knob / 2), alpha=1.0 if composer["text"].strip() else 0.55)
        _text("↑", kx + knob / 2 - _width("↑", body) / 2, y0 + height / 2 - body * 0.36, body, (1, 1, 1))
        mark("send", kx, y0 + 5 * ui, knob, knob)
    label = "Chat"
    cw = _width(label, small) + 22 * ui
    cx = kx - 8 * ui - cw
    chip = _rounded(cx, y0 + 10 * ui, cw, height - 20 * ui, (height - 20 * ui) / 2)
    _fill(chip, (*CHIP, 0.96))
    _outline(chip, (1.0, 1.0, 1.0, 0.10))
    _text(label, cx + 11 * ui, y0 + height / 2 - small * 0.36, small, TEXT)
    mark("collapse", cx, y0 + 6 * ui, cw, height - 12 * ui)

    # left: what Unix is doing, then the text box
    status, color = _status()
    pulse = 0.55 + 0.45 * math.sin(time.monotonic() * 4.0) if engine.busy else 1.0
    _fill(_rounded(x0 + 16 * ui, y0 + height / 2 - 4 * ui, 8 * ui, 8 * ui, 4 * ui), (*color, pulse))
    tx, room = x0 + 32 * ui, cx - 10 * ui - (x0 + 32 * ui)
    mark("composer", x0, y0, cx - x0 - 4 * ui, height)
    ty = y0 + height / 2 - body * 0.36
    draft = composer["text"].replace("\n", " ")
    if draft:
        shown = draft[-400:]
        while shown and _width(shown, body) > room - 6 * ui:
            shown = shown[max(1, len(shown) // 12):]
        _text(shown, tx, ty, body, TEXT)
        caret = tx + _width(shown, body) + 1 * ui
    else:
        if engine.busy or status.startswith("Offline"):
            hint, tint = status, TEXT
        else:
            hint, tint = MODES[MODE_IDS.index(settings["mode"])][2], DIM
        while hint and _width(hint, body) > room:
            hint = hint[:-4] + "…"
        _text(hint, tx, ty, body, tint, 0.85)
        caret = tx - 2 * ui
    if composer["focused"] and int(time.monotonic() * 2) % 2 == 0:
        _fill([(caret, ty - 3 * ui), (caret + 1.5 * ui, ty - 3 * ui), (caret + 1.5 * ui, ty + 11 * ui), (caret, ty + 11 * ui)],
              (*TEXT, 0.9))

    # above: Unix's last reply, for half a minute after it came (a click on it opens the chat)
    reply = _last_reply()
    if reply != _state.get("reply"):
        _state.update(reply=reply, reply_at=time.monotonic())
    age = time.monotonic() - _state.get("reply_at", 0.0)
    if reply and age < 30.0:
        lines = _wrap(" ".join(reply.split()), width - 36 * ui, small)      # paragraphs run on: no empty lines
        lines = lines[:3] if len(lines) <= 3 else lines[:2] + [lines[2].rstrip(".") + " …"]
        bh = len(lines) * 16 * ui + 16 * ui
        by = y0 + height + 8 * ui
        fade = min(1.0, (30.0 - age) / 3.0)
        card = _rounded(x0 + 10 * ui, by, width - 20 * ui, bh, 12 * ui)
        _fill(card, (*CARD, 0.92 * fade))
        _outline(card, (1.0, 1.0, 1.0, 0.10 * fade))
        line_y = by + bh - 8 * ui - small
        for line in lines:
            _text(line, x0 + 26 * ui, line_y, small, TEXT, fade)
            line_y -= 16 * ui
        mark("collapse", x0 + 10 * ui, by, width - 20 * ui, bh)


def _selectable(row) -> List[Tuple[Tuple[int, int], str, bool]]:
    """(key, text, paragraph ends) of each line of the row that can be selected and copied."""
    if row.get("key") is not None:
        return [(row["key"], row["text"], row["end"])]
    first = row.get("key0")
    if first is None:
        return []
    return [((first[0], first[1] + i), line, end) for i, (line, end) in enumerate(zip(row["lines"], row["ends"]))]


def _chosen(key, text: str) -> Optional[Tuple[int, int]]:
    """The characters [start, end) of this line that are selected, or None."""
    a, b = selection["a"], selection["b"]
    if a is None or b is None or a == b:
        return None
    low, high = min(a, b), max(a, b)
    if not low[0] <= key <= high[0]:
        return None
    start = low[1] if key == low[0] else 0
    end = high[1] if key == high[0] else len(text)
    return (start, end) if end > start or (key != high[0] and not text) else None


def _line(text: str, x, ty, size, color, key, drawn, ui, alpha: float = 1.0) -> None:
    """One line of chat text at baseline ty: its selected part highlighted, where it is remembered for the mouse."""
    lh = 17 * ui
    if key is not None:
        part = _chosen(key, text)
        if part is not None:
            left = x + _width(text[:part[0]], size)
            right = x + (_width(text[:part[1]], size) if text else 6 * ui)
            _fill(_rounded(left, ty - 4.5 * ui, max(right - left, 3 * ui), lh, 2 * ui), SELECT)
        drawn.append({"key": key, "text": text, "x": x, "y": ty, "size": size})
    _text(text, x, ty, size, color, alpha)


def _widest(lines: List[str], size: float) -> float:
    """The widest of these lines (remembered: a pasted spec is a thousand lines, measured every frame)."""
    key = (id(lines), len(lines), lines[0] if lines else "", int(size * 10))
    if key not in _widths:
        if len(_widths) > 400:
            _widths.clear()
        _widths[key] = max((_width(line, size) for line in lines), default=0.0)
    return _widths[key]


def _draw_row(row, x, y, width, ui, mark, drawn=None, view=None) -> None:
    """Draw one row with its bottom at y. ``drawn`` collects where its selectable lines went; ``view``
    (bottom, top) leaves out the lines of a tall message that are out of sight."""
    body, small, lh = 12.5 * ui, 11 * ui, 17 * ui
    drawn = [] if drawn is None else drawn
    box_y, box_h = y, row["h"]
    low, high = view if view else (-1e9, 1e9)

    kind = row["kind"]
    if kind == "text":
        _line(row["text"], x, y + 4 * ui, row["size"], row["color"], row.get("key"), drawn, ui)
    elif kind == "user":
        lines = row["lines"]
        bubble_w = min(width, _widest(lines, body) + 22 * ui)
        bx = x + width - bubble_w
        _fill_gradient(_rounded(bx, box_y, bubble_w, box_h, 11 * ui), alpha=0.58)
        ty = y + row["h"] - 6 * ui - lh * 0.72
        first = row["key0"]
        for i, line in enumerate(lines):
            if low - lh <= ty <= high + lh:
                _line(line, bx + 11 * ui, ty, body, (1, 1, 1), (first[0], first[1] + i), drawn, ui)
            ty -= lh
        tag = " · ".join(t for t in (row["tag"], f"{row['files']} file{'s' if row['files'] != 1 else ''}" if row["files"] else "") if t)
        if tag:
            _text(tag, bx - _width(tag, small) - 8 * ui, y + row["h"] / 2 - 4 * ui, small, DIM)
    elif kind == "step":
        step = row["step"]
        running, failed = step["status"] == "running", step["status"] == "error"
        color = WARN if failed else (ACCENT if step["acted"] else DIM)
        alpha = 0.5 + 0.5 * math.sin(time.monotonic() * 5.0) if running else 1.0
        _fill(_rounded(x + 2 * ui, y + 6 * ui, 6 * ui, 6 * ui, 1.5 * ui if step["acted"] else 3 * ui), (*color, alpha))
        label = step["label"] + (f": {step['detail']}" if step["detail"] else "")
        label += " …" if running else (f"  —  {step['result']}" if step.get("result") else "")
        while _width(label, small) > width - 16 * ui and len(label) > 8:
            label = label[:-6] + "…"
        _text(label, x + 15 * ui, y + 4 * ui, small, WARN if failed else TEXT, 0.92)
    elif kind == "image":
        _fill(_rounded(x - 2, y + 4 * ui, row["iw"] + 4, row["ih"] + 4, 4 * ui), (*ACCENT, 0.4))
        _image(row["path"], x, y + 4 * ui + 2, row["iw"], row["ih"])
    elif kind == "notice":
        top, bottom = y + row["h"] - 2 * ui, y + 2 * ui
        _fill(_rounded(x, bottom, width, top - bottom, 8 * ui), (*CHIP, 0.95))
        _fill(_rounded(x, bottom, 3 * ui, top - bottom, 1.5 * ui), (*row["color"], 1.0))
        ty = y + row["h"] - 7 * ui - lh * 0.72
        first = row.get("key0")
        for i, line in enumerate(row["lines"]):
            if low - lh <= ty <= high + lh:
                _line(line, x + 13 * ui, ty, body, TEXT, (first[0], first[1] + i) if first else None, drawn, ui)
            ty -= lh
    elif kind == "chip":
        label = row["text"]
        while _width(label, body) > width - 26 * ui and len(label) > 8:
            label = label[:-6] + "…"
        w = _width(label, body) + 22 * ui
        if row.get("right"):
            x = x + width - w
        shape = _rounded(x, y + 2 * ui, w, row["h"] - 4 * ui, 10 * ui)
        _fill(shape, (*CHIP, 0.96))
        _outline(shape, (*ACCENT, 0.45))
        _text(label, x + 11 * ui, y + 8.5 * ui, body, TEXT)
        mark(row["name"], x, y + 2 * ui, w, row["h"] - 4 * ui)
    elif kind == "plan":
        _text(f"Plan  {row['done']}/{row['total']}", x, y + 5 * ui, small, DIM)
        bx, bw = x + 62 * ui, width - 62 * ui
        _fill(_rounded(bx, y + 7 * ui, bw, 6 * ui, 3 * ui), (1.0, 1.0, 1.0, 0.10))
        if row["done"]:
            _fill_gradient(_rounded(bx, y + 7 * ui, max(6 * ui, bw * row["done"] / row["total"]), 6 * ui, 3 * ui))


# -- interaction ---------------------------------------------------------------------

def place_at(region_pointer: int, x: float, y: float, ui: float) -> Optional[Tuple[Tuple[int, int], int]]:
    """(line key, character) under region point (x, y), or at the nearest line drawn when it is between lines."""
    drawn = _drawn.get(region_pointer) or []
    if not drawn:
        return None
    lh = 17 * ui
    line = min(drawn, key=lambda d: 0.0 if d["y"] - 4.5 * ui <= y < d["y"] - 4.5 * ui + lh else abs(d["y"] + 4 * ui - y))
    text, size, start = line["text"], line["size"], line["x"]
    low, high = 0, len(text)                 # the first character whose middle is right of x
    while low < high:
        middle = (low + high) // 2
        if start + (_width(text[:middle], size) + _width(text[:middle + 1], size)) / 2 < x:
            low = middle + 1
        else:
            high = middle
    return line["key"], low


def selected_text() -> str:
    """The selected chat text, with its paragraph breaks."""
    a, b = selection["a"], selection["b"]
    if a is None or b is None or a == b:
        return ""
    out = []
    for key, text, end in (entry for row in _view["rows"] for entry in _selectable(row)):
        part = _chosen(key, text)
        if part is None:
            continue
        out.append(text[part[0]:part[1]])
        if key != max(a, b)[0]:
            out.append("\n" if end else " ")
    return "".join(out).strip()


def select_message(key: Tuple[int, int]) -> None:
    """Select all of the message this line is in."""
    lines = [(k, text) for row in _view["rows"] for k, text, _end in _selectable(row) if k[0] == key[0]]
    if lines:
        selection.update(a=(lines[0][0], 0), b=(lines[-1][0], len(lines[-1][1])))


def copy_selection(context) -> bool:
    text = selected_text()
    if not text:
        return False
    context.window_manager.clipboard = text
    settings["notice"] = ""
    return True


def hit(region_pointer: int, x: float, y: float) -> str:
    """Name of the control at region coordinates (x, y); '' when the point is off the island."""
    for name, rx, ry, rw, rh in reversed(hit_rects.get(region_pointer) or []):
        if rx <= x <= rx + rw and ry <= y <= ry + rh:
            return name
    return ""


def _redraw(context) -> None:
    for area in context.window.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()


def paste_attachments(context) -> int:
    """Attach what is on the clipboard, if it can be attached: a copied picture (a screenshot,
    'Copy image' in a browser), files copied in Explorer, or the path of such a file as text.
    Returns how many files were added."""
    from . import operators
    from ..bridge import clipboard
    from ..tools._util import data_dir
    wm = context.window_manager
    try:
        found = clipboard.grab(data_dir("attachments"))
    except Exception as exc:        # a paste must never break typing
        print(f"[Unix] clipboard: {exc!r}")
        found = []
    if not found:
        text = (wm.clipboard or "").strip().strip('"')
        if text and len(text) < 400 and "\n" not in text and text.lower().endswith(clipboard.ATTACHABLE) and os.path.isfile(text):
            found = [text]
    if not found:
        return 0
    current = operators.attachments(wm)
    added = [path for path in found if path not in current]
    wm.unix_attachments = operators.SEPARATOR.join((current + added)[:10])
    pictures = sum(1 for path in added if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".tif", ".tiff")))
    if added:
        what = "picture" if pictures == len(added) else "file"
        settings["notice"] = (f"Attached {len(added)} {what}{'s' if len(added) != 1 else ''} from the clipboard. "
                              + ("Say what to do with it, for example: make this in 3D." if pictures else ""))
    return len(added)


def submit(context) -> None:
    """Send the composer text, or answer a pending question with it."""
    from . import operators
    text = composer["text"].strip()
    if not text:
        return
    if interact.pending_question():
        interact.answer_question(text)
    else:
        interact.interactive["enabled"] = not settings["auto"]
        error = operators._send(context, text, mode=settings["mode"], auto=settings["auto"])
        if error:
            settings["notice"] = error
            return
    composer.update(text="", cursor=0)
    settings.update(notice="", scroll=0.0)


_MOUSE = {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE', 'WHEELUPMOUSE',
          'WHEELDOWNMOUSE', 'TRACKPADPAN', 'TRACKPADZOOM', 'MOUSEROTATE', 'MOUSESMARTZOOM', 'NONE',
          'WINDOW_DEACTIVATE', 'ACTIONZONE_AREA', 'ACTIONZONE_REGION', 'ACTIONZONE_FULLSCREEN'}


class UNIX_OT_island_events(bpy.types.Operator):
    """Makes the Unix island interactive (runs quietly in the background)"""
    bl_idname = "unix.island_events"
    bl_label = "Unix Island Events"
    bl_options = {'INTERNAL'}

    running = False
    _drag = None  # {"kind": "move" | "resize", start mouse and start values}

    def invoke(self, context, event):
        cls = type(self)
        if cls.running or context.window is None:
            return {'CANCELLED'}
        cls.running = True
        self._drag = None
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def cancel(self, context):
        type(self).running = False

    def _dragging(self, context, event):
        """Handle an event while the island is being moved or resized."""
        drag = self._drag
        if drag["kind"] == "select":
            if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
                found = place_at(drag["region"], event.mouse_x - drag["rx"], event.mouse_y - drag["ry"], drag["ui"])
                if found is not None:
                    selection["b"] = found
                _redraw(context)
            elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
                if selection["a"] == selection["b"]:
                    selection.update(a=None, b=None)     # a click, not a drag: nothing stays selected
                self._drag = None
                _redraw(context)
            elif event.type == 'ESC':
                selection.update(a=None, b=None)
                self._drag = None
                _redraw(context)
            return {'RUNNING_MODAL'}
        if event.type in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE'}:
            dx, dy = event.mouse_x - drag["mx"], event.mouse_y - drag["my"]
            if drag["kind"] == "move":
                settings.update(dx=drag["dx"] + dx, dy=drag["dy"] + dy)
            else:
                ui = context.preferences.system.ui_scale * settings["scale"]
                settings["width"] = min(max(drag["width"] + dx / ui, 480.0), 1100.0)
                settings["view_h"] = min(max(drag["view_h"] + dy / ui, VIEW_H[0]), VIEW_H[1])
            _redraw(context)
        elif event.type == 'LEFTMOUSE' and event.value == 'RELEASE':
            self._drag = None
            save_layout()
        elif event.type in {'ESC', 'RIGHTMOUSE'}:
            settings.update({k: drag[k] for k in ("dx", "dy", "width", "view_h")})
            self._drag = None
            _redraw(context)
        return {'RUNNING_MODAL'}

    @staticmethod
    def _locate(context, event):
        for area in context.window.screen.areas:
            if area.type != 'VIEW_3D':
                continue
            for region in area.regions:
                if region.type != 'WINDOW':
                    continue
                x, y = event.mouse_x - region.x, event.mouse_y - region.y
                if 0 <= x <= region.width and 0 <= y <= region.height:
                    return area, region, hit(region.as_pointer(), x, y)
        return None, None, ""

    def modal(self, context, event):
        if _handle is None:  # add-on unregistered
            type(self).running = False
            return {'CANCELLED'}
        kind, value = event.type, event.value
        if self._drag is None:
            over = settings["visible"] and kind in _CAMERA_EVENTS and bool(self._locate(context, event)[2])
            taken = fly.handle(context, event, over, settings["visible"] and composer["focused"])
            if taken is not None:
                return taken
        if not settings["visible"]:
            return {'PASS_THROUGH'}
        if kind.startswith('TIMER'):
            return {'PASS_THROUGH'}
        if self._drag is not None:
            return self._dragging(context, event)

        if kind == 'LEFTMOUSE' and value in {'PRESS', 'DOUBLE_CLICK'}:
            area, region, part = self._locate(context, event)
            if not part:
                if composer["focused"] or selection["a"] is not None:
                    composer["focused"] = False
                    selection.update(a=None, b=None)
                    _redraw(context)
                return {'PASS_THROUGH'}
            if part == "chat":
                ui = context.preferences.system.ui_scale * settings["scale"]
                found = place_at(region.as_pointer(), event.mouse_x - region.x, event.mouse_y - region.y, ui)
                composer["focused"] = False
                if found is not None and value == 'DOUBLE_CLICK':
                    select_message(found[0])           # a double click takes the whole message
                elif found is not None:
                    selection.update(a=found, b=found)
                    self._drag = {"kind": "select", "region": region.as_pointer(), "rx": region.x, "ry": region.y, "ui": ui}
                _redraw(context)
                return {'RUNNING_MODAL'}
            if value == 'DOUBLE_CLICK':
                return {'RUNNING_MODAL'}
            if part in {"move", "status", "resize"}:
                width = settings["width"] or (700.0 if context.workspace and context.workspace.name == "Zen" else 660.0)
                self._drag = {"kind": "resize" if part == "resize" else "move", "mx": event.mouse_x, "my": event.mouse_y,
                              "dx": settings["dx"], "dy": settings["dy"], "width": width, "view_h": settings["view_h"]}
                return {'RUNNING_MODAL'}
            self._click(context, area, region, part)
            _redraw(context)
            return {'RUNNING_MODAL'}
        if kind in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'}:
            _area, _region, part = self._locate(context, event)
            if part:
                up = kind == 'WHEELUPMOUSE'
                if event.ctrl:  # Ctrl + wheel over the island changes its size
                    set_scale(settings["scale"] + (0.1 if up else -0.1))
                else:
                    step = 48 * context.preferences.system.ui_scale
                    settings["scroll"] = max(0.0, settings["scroll"] + (step if up else -step))
                _redraw(context)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if kind == 'C' and value == 'PRESS' and (event.ctrl or event.oskey) and selection["a"] is not None:
            if copy_selection(context):              # Ctrl+C copies the selected chat text, wherever the mouse is
                return {'RUNNING_MODAL'}
        if kind == 'ESC' and value == 'PRESS' and selection["a"] is not None and not composer["focused"]:
            selection.update(a=None, b=None)
            _redraw(context)
            return {'RUNNING_MODAL'}
        if kind == 'V' and value == 'PRESS' and (event.ctrl or event.oskey) and not composer["focused"]:
            # Ctrl+V with the mouse over the island attaches a copied picture, without clicking the text box first
            _area, _region, part = self._locate(context, event)
            if part and paste_attachments(context):
                composer["focused"] = True
                _redraw(context)
                return {'RUNNING_MODAL'}
            return {'PASS_THROUGH'}
        if kind in _MOUSE or not composer["focused"]:
            return {'PASS_THROUGH'}

        if value == 'PRESS':  # typing into the composer
            self._key(context, event)
            _redraw(context)
        return {'RUNNING_MODAL'}  # while typing, keys must not reach Blender's shortcuts

    def _click(self, context, area, region, part: str) -> None:
        wm = context.window_manager
        override = dict(window=context.window, area=area, region=region)
        if part.startswith("tab:"):
            settings.update(mode=part[4:], scroll=0.0, notice="")
            composer["focused"] = part != "tab:engine"
            if part == "tab:engine":
                refresh_targets()
        elif part.startswith("send:"):
            which = part[5:]
            with context.temp_override(**override):
                if which == "drag":
                    bpy.ops.unix.send_to_engine(engine="file", drag=True)
                else:
                    target = _targets["list"][int(which)]
                    bpy.ops.unix.send_to_engine(engine=target["engine"], project=target["project"])
            settings["scroll"] = 0.0
        elif part == "composer":
            composer["focused"] = True
        elif part.startswith("chip:"):
            examples = MODES[MODE_IDS.index(settings["mode"])][3]
            composer.update(text=examples[int(part[5:])], focused=True)
        elif part.startswith("fold:"):
            index = int(part[5:])
            _opened.symmetric_difference_update({index})
        elif part.startswith("opt:"):
            question = interact.pending_question()
            if question:
                interact.answer_question(question["options"][int(part[4:])])
        elif part == "send":
            submit(context)
        elif part == "stop":
            engine.stop_turn()
        elif part.startswith("helper_stop:"):
            helpers = team.state["workers"]
            if int(part[12:]) < len(helpers):
                team.stop(helpers[int(part[12:])]["name"])
        elif part == "play":
            with context.temp_override(**override):
                bpy.ops.unix.play()
        elif part.startswith("level:"):
            with context.temp_override(**override):
                bpy.ops.unix.ui_level(level=part[6:])
        elif part == "auto":
            settings["auto"] = not settings["auto"]
        elif part == "collapse":
            settings["collapsed"] = not settings["collapsed"]
            save_layout()
        elif part in {"bigger", "smaller"}:
            set_scale(settings["scale"] + (0.1 if part == "bigger" else -0.1))
        elif part == "clear_attach":
            wm.unix_attachments = ""
        elif part == "new":
            engine.new_chat()
            settings.update(scroll=0.0, notice="")
            selection.update(a=None, b=None)
            _opened.clear()
        elif part in {"history", "model", "effort", "attach", "copy"}:
            composer["focused"] = False
            menus = {"history": "UNIX_MT_history", "model": "UNIX_MT_models", "effort": "UNIX_MT_effort",
                     "copy": "UNIX_MT_copy"}
            with context.temp_override(**override):
                if part == "attach":
                    bpy.ops.unix.attach('INVOKE_DEFAULT')
                else:
                    bpy.ops.wm.call_menu(name=menus[part])

    def _key(self, context, event) -> None:
        text, kind = composer["text"], event.type
        if kind in {'RET', 'NUMPAD_ENTER'}:
            if event.shift:
                composer["text"] = text + "\n"
            else:
                submit(context)
        elif kind == 'ESC':
            composer["focused"] = False
        elif kind == 'BACK_SPACE':
            if event.ctrl:
                trimmed = text.rstrip()
                composer["text"] = trimmed[:trimmed.rfind(" ") + 1] if " " in trimmed else ""
            else:
                composer["text"] = text[:-1]
        elif kind == 'V' and (event.ctrl or event.oskey):
            if not paste_attachments(context):      # a picture or copied files are attached; anything else is text
                clip = context.window_manager.clipboard or ""
                composer["text"] = (text + clip.replace("\r", ""))[:COMPOSER_MAX]
        elif kind == 'C' and (event.ctrl or event.oskey):
            if not copy_selection(context):
                context.window_manager.clipboard = text
        elif kind == 'A' and (event.ctrl or event.oskey):
            pass
        elif kind == 'X' and (event.ctrl or event.oskey):
            context.window_manager.clipboard = text
            composer["text"] = ""
        elif event.unicode and not event.ctrl and not event.oskey and ord(event.unicode[0]) >= 32:
            composer["text"] = (text + event.unicode)[:COMPOSER_MAX]


def set_scale(value: float) -> None:
    settings["scale"] = round(min(max(value, 0.7), 1.8), 2)
    _wrap_cache.clear()
    save_layout()


def _layout_file() -> str:
    return os.path.join(data_dir(), "island.json")


def save_layout() -> None:
    import json
    try:
        with open(_layout_file(), "w", encoding="utf-8") as fh:
            json.dump({k: settings[k] for k in _LAYOUT_KEYS}, fh)
    except OSError:
        pass


def load_layout() -> None:
    import json
    try:
        with open(_layout_file(), encoding="utf-8") as fh:
            saved = json.load(fh)
        settings.update({k: saved[k] for k in _LAYOUT_KEYS if k in saved})
    except (OSError, ValueError):
        pass
    if settings["view_h"] < VIEW_H[0]:
        settings["view_h"] = 460.0      # a height saved before the floor was raised: the chat was a slit


def _ensure_events() -> None:
    """Start the event handler if it is not running (it dies when a file is loaded)."""
    if UNIX_OT_island_events.running or bpy.app.background:
        return
    wm = bpy.context.window_manager
    if not wm or not wm.windows:
        return
    window = wm.windows[0]
    try:
        with bpy.context.temp_override(window=window, screen=window.screen):
            bpy.ops.unix.island_events('INVOKE_DEFAULT')
    except Exception:
        pass


@bpy.app.handlers.persistent
def _on_load(_dummy=None) -> None:
    UNIX_OT_island_events.running = False
    hit_rects.clear()
    composer["focused"] = False
    fly.reset(bpy.context)
    _state["level_applied"] = False


def tick() -> bool:
    """Main-thread upkeep from the add-on timer. Returns True when the island needs redrawing."""
    _ensure_events()                # the free camera runs in it too, with the island hidden as well
    if not settings["visible"]:
        return False
    if settings["collapsed"] and interact.pending_question():
        settings["collapsed"] = False   # Unix asks something: the answers to click are in the chat
    for path in _images_in_view():
        prepare(path)
    from . import simple
    if not _state["level_applied"]:
        _state["level_applied"] = True
        simple.startup()
    if _state["was_busy"] and not engine.busy:
        simple.after_turn()
    _state["was_busy"] = engine.busy
    now = time.monotonic()
    if now - _cli["checked"] > 15.0:
        _cli.update(checked=now, claude=find_claude() is not None, codex=find_codex() is not None)
    return composer["focused"]


def register() -> None:
    global _handle
    bpy.utils.register_class(UNIX_OT_island_events)
    load_layout()
    if _on_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load)
    if _handle is None and not bpy.app.background:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')


def unregister() -> None:
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    if _on_load in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load)
    UNIX_OT_island_events.running = False
    bpy.utils.unregister_class(UNIX_OT_island_events)
    _textures.clear()
    hit_rects.clear()
