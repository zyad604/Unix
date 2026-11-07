# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Image to 3D by modelling: compare the build against a reference image.

There is no mesh-generation model here. The agent looks at the reference, builds the
object from geometry, then calls ``match_reference`` to see the reference and its own
render side by side and correct the differences. Analysis by synthesis: it converges in
a few rounds for hard-surface and furniture-like subjects.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import bpy

from ._util import data_dir
from .registry import ToolError, ToolOutput, tool
from .vision import _IMAGE_EXTS, look

PANEL_HEIGHT = 640
GAP = 12


def _load_pixels(path: str, height: int):
    """(H, W, 4) float array of the image scaled to ``height`` rows, top row first."""
    import numpy as np

    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            raise ToolError(f"Blender could not read the image: {path}")
        new_w = max(1, int(round(w * height / h)))
        img.scale(new_w, height)
        buf = np.empty(new_w * height * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    arr = buf.reshape(height, new_w, 4)[::-1].copy()  # Blender stores rows bottom-up
    alpha = arr[..., 3:4]
    arr[..., :3] = arr[..., :3] * alpha + (1.0 - alpha) * 0.18  # flatten transparency on dark grey
    arr[..., 3] = 1.0
    return arr


@tool(
    "match_reference",
    """Image-to-3D check: render the object and return ONE image with the user's reference on
    the left and your render on the right, same height, so you can compare silhouette,
    proportions, part placement, colours and materials directly. Call it after the blockout and
    after each correction round; list the concrete differences you see, fix the largest first,
    and stop when the remaining differences are small. Pick the view that matches the angle
    of the reference photo, or aim the scene camera to match it and pass use_scene_camera. With
    view 'front' and a photo on a plain background it also MEASURES the match: how much of the
    real outline the model covers, where it is missing or sticks out, and whether its openings
    are as big as the real ones. Act on those numbers.""",
    {
        "image": {"type": "string", "description": "Absolute path of the reference image."},
        "targets": {"type": "array", "items": {"type": "string"}, "description": "Objects that make up the build. Omit for everything."},
        "view": {"type": "string", "enum": ["persp", "persp_back", "front", "back", "left", "right", "top"],
                 "description": "Auto-framed view closest to the reference angle (default persp)."},
        "use_scene_camera": {"type": "boolean", "description": "Render through the scene camera instead of an auto view."},
        "shading": {"type": "string", "enum": ["solid", "rendered"], "description": "solid to judge form (default), rendered to judge materials and light."},
    },
    ["image"],
    timeout=420.0,
    label="Compared with the reference",
)
def match_reference(image: str, targets: Optional[List[str]] = None, view: str = "persp",
                    use_scene_camera: bool = False, shading: str = "solid") -> ToolOutput:
    import numpy as np

    image = bpy.path.abspath(image)
    if not os.path.isfile(image):
        raise ToolError(f"Reference image not found: {image}")
    if os.path.splitext(image)[1].lower() not in _IMAGE_EXTS:
        raise ToolError("The reference must be an image file.")
    if not targets and not use_scene_camera:        # frame the work, not the studio backdrop around it
        targets = [o.name for o in bpy.context.scene.objects
                   if o.type in {'MESH', 'CURVE', 'FONT', 'SURFACE', 'META'} and not o.hide_render and not o.hide_get()
                   and not o.name.startswith("Unix_")] or None
    shot = look(targets=targets, views=[view], shading=shading, use_scene_camera=use_scene_camera,
                isolate=False, size=PANEL_HEIGHT)
    left = _load_pixels(image, PANEL_HEIGHT)
    right = _load_pixels(shot.images[0], PANEL_HEIGHT)
    gap = np.full((PANEL_HEIGHT, GAP, 4), 1.0, dtype=np.float32)
    sheet = np.concatenate([left, gap, right], axis=1)[::-1]  # back to bottom-up
    height, width = sheet.shape[:2]
    out_img = bpy.data.images.new("_unix_match", width, height, alpha=False)
    out = os.path.join(data_dir("looks"), f"match_{time.strftime('%H%M%S')}.jpg")
    try:
        out_img.pixels.foreach_set(np.ascontiguousarray(sheet).ravel())
        out_img.filepath_raw, out_img.file_format = out, 'JPEG'
        out_img.save()
    finally:
        bpy.data.images.remove(out_img)
    data: Dict[str, Any] = {
        "layout": "LEFT = reference, RIGHT = your render",
        "view": "scene camera" if use_scene_camera else view, "shading": shading,
        "compare": ["overall silhouette and aspect ratio", "relative size and position of each part",
                    "curvature, taper and edge softness", "missing or extra parts", "colour and material per part"],
    }
    if view == "front" and not use_scene_camera:        # measured, not judged: the outline and its openings against the photo
        from . import references
        try:
            numbers = references.outline_numbers(image, [bpy.data.objects[n] for n in (targets or []) if n in bpy.data.objects])
        except Exception:
            numbers = None
        if numbers:
            data["outline_measured"] = numbers
    return ToolOutput(data, [out])
