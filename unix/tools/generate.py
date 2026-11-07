# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""AI image generation through the user's Codex CLI (OpenAI, ChatGPT login, no API key).

``generate_image`` makes concept art, orthographic reference sheets, decals and
backdrops. ``generate_texture`` makes a surface texture, derives a normal and a roughness
map from it locally with numpy, and builds a tiling PBR material in one call.

The CLI runs with its shell and every other tool disabled and with the user's Codex
config ignored: it can only generate the image. The subprocess runs on the calling
worker thread; only the bpy part hops to the main thread.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import bpy

from . import _turn
from ._util import data_dir, get_object
from .materials import set_material
from .registry import ToolError, ToolOutput, registry, tool
from .vision import _IMAGE_EXTS, view_image

settings = {"enabled": True}
SIGNED_OUT = ("AI images are not available right now: the Codex (OpenAI) sign-in has expired, and only the user can renew "
              "it by running `codex login` in a terminal. Image generation always goes through that sign-in, whichever "
              "model is chatting. Do not stop and do not ask the user to try again: if you are building something, keep "
              "building without generated images (model straight from the user's photo with match_reference, take surfaces "
              "from asset_material or plain set_material), and mention the sign-in in one line at the end. Only if a "
              "picture is the whole request, say that it needs `codex login`.")
_signed_out = {"until": 0.0}        # after a refusal, do not spend half a minute per call finding out again
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_DISABLED_FEATURES = ("shell_tool", "unified_exec", "apps", "plugins", "browser_use", "computer_use",
                      "multi_agent", "skill_search", "tool_suggest")
_PURPOSES = {
    "concept": "{prompt}. A single subject, three-quarter view, plain light grey background, soft even studio "
               "lighting, no text, no watermark.",
    "reference_sheet": "Orthographic reference sheet of {prompt}: a front view, a side view and a back view, side by "
                       "side, the same scale and the same height, flat neutral lighting, plain white background, "
                       "no perspective distortion, no text, no labels, no shadows.",
    "texture": "Seamless tileable surface texture of {prompt}. Top-down orthographic view, flat even lighting, no "
               "shadows, no highlights, no perspective, no objects, the surface fills the whole square frame.",
    "decal": "{prompt}, centred, flat front view, on a plain pure white background, no shadow, no text unless asked.",
    "backdrop": "{prompt}. A wide environment image with no foreground subject, suitable as a background plate.",
}


def find_codex() -> Optional[str]:
    override = os.environ.get("CODEX_BIN")
    if override:
        return override
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return found
    for cand in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\OpenAI\Codex\bin\codex.exe"),
                 os.path.expanduser("~/.local/bin/codex"), "/usr/local/bin/codex", "/opt/homebrew/bin/codex"):
        if os.path.isfile(cand):
            return cand
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "image"


def run_codex_image(prompt: str, reference: str = "", timeout: float = 300.0) -> str:
    """Generate one image; returns the path of the PNG the CLI wrote. Worker thread."""
    if not settings["enabled"]:
        raise ToolError("AI image generation is turned off in the add-on preferences.")
    if time.monotonic() < _signed_out["until"]:
        raise ToolError(SIGNED_OUT)
    codex = find_codex()
    if not codex:
        raise ToolError("AI images are not available: the Codex CLI is not installed. Keep going without generated images "
                        "(model from the user's photo, use asset_material or set_material); the user can install it and run "
                        "`codex login` later.")
    workdir = data_dir("workspace", "codex_images")
    cmd = [codex, "exec", "--skip-git-repo-check", "--json", "-s", "read-only",
           "--ignore-user-config", "--ignore-rules", "-C", workdir]
    for feature in _DISABLED_FEATURES:
        cmd += ["--disable", feature]
    if reference:
        cmd += ["-i", reference]
    cmd.append("-")
    text = ("Generate exactly one image with your image generation tool and do nothing else: " + prompt +
            "\nAfter the image is generated, reply with the single word done.")
    try:
        proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=timeout,
                              creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise ToolError(f"Image generation timed out after {timeout:.0f}s.")
    thread_id, errors = None, []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            thread_id = event.get("thread_id")
        elif event.get("type") in {"error", "turn.failed"}:
            errors.append(str(event.get("message") or (event.get("error") or {}).get("message") or event)[:300])
    folder = os.path.join(os.path.expanduser("~"), ".codex", "generated_images", thread_id or "")
    images = sorted(glob.glob(os.path.join(folder, "*.png")), key=os.path.getmtime) if thread_id else []
    if not images:
        detail = "; ".join(dict.fromkeys(errors)) or proc.stderr.decode("utf-8", errors="replace")[-300:] or "no image was produced"
        if "401" in detail or "unauthorized" in detail.lower() or "login" in detail.lower():
            _signed_out["until"] = time.monotonic() + 600.0
            raise ToolError(SIGNED_OUT)
        raise ToolError(f"Image generation failed: {detail[:600]}")
    return images[-1]


def _keep(source: str, name: str, sub: str) -> str:
    dest = os.path.join(data_dir("generated", sub), f"{_slug(name)}_{time.strftime('%H%M%S')}.png")
    shutil.copyfile(source, dest)
    return dest


@tool(
    "generate_image",
    """Generate an image with AI (OpenAI, through the user's Codex login; about a minute per
    image, so use it deliberately). It is a helper, never a requirement: when it fails, carry
    on with the task without it. Purposes: 'concept' (a design to build towards),
    'reference_sheet' (front/side/back orthographic views of a subject, the best input for
    modelling something from a single photo: pass that photo as `reference`), 'decal' (logo or
    label art on white), 'backdrop' (background plate), 'texture' (prefer generate_texture,
    which also builds the material). The image is returned so you can see it; the file path
    can be passed to match_reference, set_material or run_python.""",
    {
        "prompt": {"type": "string", "description": "What to draw: subject, style, materials, colours."},
        "purpose": {"type": "string", "enum": sorted(_PURPOSES)},
        "reference": {"type": "string", "description": "Optional path of an image to base it on (e.g. the user's photo)."},
    },
    ["prompt", "purpose"],
    main_thread=False,
    timeout=420.0,
    label="Generated an image",
)
def generate_image(prompt: str, purpose: str, reference: str = "") -> ToolOutput:
    if reference:
        if not os.path.isfile(reference) or os.path.splitext(reference)[1].lower() not in _IMAGE_EXTS:
            raise ToolError(f"Reference image not found or not an image: {reference}")
    full = _PURPOSES[purpose].format(prompt=prompt.strip().rstrip("."))
    if reference:
        full += " Base the subject on the attached image and keep its proportions, colours and materials."
    path = _keep(run_codex_image(full, reference), prompt, purpose)
    preview = registry.run_on_main(lambda: view_image(path), 60.0)
    return ToolOutput({"file": path, "purpose": purpose, "size": preview.data["original_size"],
                       "note": "Saved on disk. Pass `file` to match_reference to build towards it."}, preview.images)


def _derive_maps(base_path: str, roughness: float) -> Dict[str, str]:
    """Normal and roughness maps from the base colour's luminance. Main thread (uses bpy images)."""
    import numpy as np

    img = bpy.data.images.load(base_path, check_existing=False)
    try:
        w, h = img.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    rgba = buf.reshape(h, w, 4)
    lum = 0.2126 * rgba[..., 0] + 0.7152 * rgba[..., 1] + 0.0722 * rgba[..., 2]
    height = lum.copy()
    for _ in range(2):  # soften pixel noise before taking gradients
        height = (height + np.roll(height, 1, 0) + np.roll(height, -1, 0) + np.roll(height, 1, 1) + np.roll(height, -1, 1)) / 5.0
    dx = (np.roll(height, -1, 1) - np.roll(height, 1, 1)) * 4.0
    dy = (np.roll(height, -1, 0) - np.roll(height, 1, 0)) * 4.0
    length = np.sqrt(dx * dx + dy * dy + 1.0)
    normal = np.stack([-dx / length * 0.5 + 0.5, -dy / length * 0.5 + 0.5, 1.0 / length * 0.5 + 0.5,
                       np.ones_like(length)], axis=-1).astype(np.float32)
    rough_value = np.clip(roughness + (0.5 - lum) * 0.35, 0.04, 1.0)
    rough = np.stack([rough_value, rough_value, rough_value, np.ones_like(rough_value)], axis=-1).astype(np.float32)

    out: Dict[str, str] = {}
    stem = os.path.splitext(base_path)[0]
    for key, data in (("normal", normal), ("roughness", rough)):
        image = bpy.data.images.new(f"_unix_{key}", w, h, alpha=False, is_data=True)
        try:
            image.pixels.foreach_set(np.ascontiguousarray(data).ravel())
            image.filepath_raw, image.file_format = f"{stem}_{key}.png", 'PNG'
            image.save()
        finally:
            bpy.data.images.remove(image)
        out[key] = f"{stem}_{key}.png"
    return out


@tool(
    "generate_texture",
    """Generate a surface texture with AI and build a tiling PBR material from it in one call:
    base colour from the image model, normal and roughness maps derived from it, mirrored
    tiling so repeats have no visible seam. Use it when Poly Haven (asset_search) has nothing
    suitable or the surface is stylised or unusual. Objects need UVs. One tile covers about
    `tile_m` meters of surface.""",
    {
        "prompt": {"type": "string", "description": "The surface, e.g. 'hand-painted mossy cobblestone, stylised'."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Objects to assign the material to."},
        "name": {"type": "string", "description": "Material name (default: derived from the prompt)."},
        "scale": {"type": "number", "description": "UV tiling multiplier (default 1.0; 2.0 = twice as many repeats)."},
        "roughness": {"type": "number", "description": "Average roughness 0-1 (default 0.6)."},
        "metallic": {"type": "number", "description": "Metallic 0-1 (default 0)."},
        "bump": {"type": "number", "description": "Strength of the derived normal map 0-2 (default 0.6)."},
    },
    ["prompt"],
    main_thread=False,
    timeout=480.0,
    mutates=True,
    label="Generated a material",
)
def generate_texture(prompt: str, objects: Optional[List[str]] = None, name: str = "", scale: float = 1.0,
                     roughness: float = 0.6, metallic: float = 0.0, bump: float = 0.6) -> ToolOutput:
    full = _PURPOSES["texture"].format(prompt=prompt.strip().rstrip("."))
    base = _keep(run_codex_image(full), name or prompt, "textures")

    def build() -> ToolOutput:
        targets = [get_object(n) for n in (objects or [])]
        maps = _derive_maps(base, roughness)
        material_name = name or "Gen_" + _slug(prompt)
        out = set_material(name=material_name, objects=[o.name for o in targets], metallic=metallic,
                           textures={"base_color": base, "normal": maps["normal"], "roughness": maps["roughness"]})
        mat = bpy.data.materials[out["material"]]
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        coord = nodes.get("unix_texcoord") or nodes.new("ShaderNodeTexCoord")
        mapping = nodes.get("unix_mapping") or nodes.new("ShaderNodeMapping")
        coord.name, mapping.name = "unix_texcoord", "unix_mapping"
        coord.location, mapping.location = (-1260, 0), (-1040, 0)
        mapping.inputs["Scale"].default_value = (scale, scale, scale)
        links.new(coord.outputs["UV"], mapping.inputs["Vector"])
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.name.startswith("unix_"):
                node.extension = 'MIRROR'  # mirrored repeats meet edge to edge: no seam
                links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        normal_map = nodes.get("unix_normal_map")
        if normal_map is not None:
            normal_map.inputs["Strength"].default_value = float(bump)
        no_uv = [o.name for o in targets if o.type == 'MESH' and not o.data.uv_layers]
        out.update(base_color_file=base, maps=["base_color", "normal", "roughness"], tiling=scale)
        if no_uv:
            out["warning"] = f"No UV layer on: {', '.join(no_uv)}. Unwrap them or the texture will not show correctly."
        preview = view_image(base)
        return ToolOutput(out, preview.images)

    return registry.run_on_main(build, 180.0)
