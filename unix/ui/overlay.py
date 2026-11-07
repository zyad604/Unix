# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Viewport halo: a soft purple frame around the 3D view while Unix is working."""

from __future__ import annotations

import math
import time

import bpy
import gpu
from gpu_extras.batch import batch_for_shader

from ..core import engine

ACCENT = (0.60, 0.36, 0.95)
_handle = None


def _draw() -> None:
    if not engine.busy:
        return
    region = bpy.context.region
    if region is None:
        return
    w, h = region.width, region.height
    scale = bpy.context.preferences.system.ui_scale
    pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 2.4)
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    gpu.state.blend_set('ALPHA')
    shader.bind()
    bands = 5
    for i in range(bands):  # stacked thin frames fade inward
        t = int((i + 1) * 2 * scale)
        alpha = (0.34 + 0.22 * pulse) * (1.0 - i / bands) ** 2
        verts = [(0, 0), (w, 0), (w, t), (0, t),               # bottom
                 (0, h - t), (w, h - t), (w, h), (0, h),        # top
                 (0, 0), (t, 0), (t, h), (0, h),                # left
                 (w - t, 0), (w, 0), (w, h), (w - t, h)]        # right
        indices = [(q, q + 1, q + 2) for q in (0, 4, 8, 12)] + [(q, q + 2, q + 3) for q in (0, 4, 8, 12)]
        shader.uniform_float("color", (*ACCENT, alpha))
        batch_for_shader(shader, 'TRIS', {"pos": verts}, indices=indices).draw(shader)
    gpu.state.blend_set('NONE')


def register() -> None:
    global _handle
    if _handle is None and not bpy.app.background:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_draw, (), 'WINDOW', 'POST_PIXEL')


def unregister() -> None:
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
