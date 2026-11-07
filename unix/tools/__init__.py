# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Unix tools. Importing this package registers every tool on ``registry``."""

from .registry import registry, tool, ToolOutput, ToolError, Pending  # noqa: F401


def load_all() -> None:
    """Import the bpy-backed tool modules (call from add-on register)."""
    from . import scene, script, vision, checks, materials, files, assets, layout, reference, references, generate, engines, studio, rigging, limbs, organic, knowledge, interact, motion, retarget, team, goal  # noqa: F401
    import importlib.util
    if importlib.util.find_spec(__name__ + ".vfx") is not None:
        from . import vfx, vfx_column  # noqa: F401
