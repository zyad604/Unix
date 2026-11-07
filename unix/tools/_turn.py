# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Per-turn state shared by tools: one undo step per agent turn.

Blender's undo is pushed once before the first scene change of a turn and once at the
end, so a single Ctrl+Z reverts everything the agent did for that message.
"""

from __future__ import annotations

from typing import Callable, Optional

import bpy

from ._util import ui_context

_state = {"active": False, "mutated": False, "label": "", "mode": "OBJECT"}
# Set by the engine: runs once per turn just before the first scene change (file snapshot).
before_first_mutation: Optional[Callable[[], None]] = None


def _push(message: str) -> None:
    if bpy.app.background:
        return
    try:
        with ui_context():
            bpy.ops.ed.undo_push(message=message)
    except Exception:
        pass


def _marks() -> dict:
    """Every object's place and size in a form that can be compared later."""
    out = {}
    for obj in bpy.data.objects:
        count = len(obj.data.vertices) if obj.type == 'MESH' else 0
        out[obj.name] = (count, tuple(round(v, 5) for row in obj.matrix_world for v in row))
    return out


def begin(label: str = "") -> None:
    try:
        mode = bpy.context.mode
    except Exception:
        mode = 'OBJECT'
    _state.update(active=True, mutated=False, label=" ".join(label.split())[:40], mode=mode, before=_marks())


def touched() -> Optional[set]:
    """Names of the objects this turn made or changed, or None when no turn is running (then
    nothing is known to be the user's own, and a caller may treat everything as its to change)."""
    if not _state.get("active") or "before" not in _state:
        return None
    before = _state["before"]
    return {name for name, mark in _marks().items() if before.get(name) != mark}


def mutated() -> bool:
    return bool(_state.get("mutated"))


def mark_mutation() -> None:
    """Call before a tool changes the scene."""
    if _state["mutated"]:
        return
    _state["mutated"] = True
    if before_first_mutation is not None:
        try:
            before_first_mutation()
        except Exception:
            pass
    _push("Before Unix: " + (_state["label"] or "agent turn"))


def end() -> bool:
    """Main thread, when the turn finishes. Returns True if the scene was changed."""
    mutated = _state["mutated"]
    if mutated:
        if _state.get("mode") == 'OBJECT':  # leave the user in the mode they started in
            from ._util import ensure_object_mode
            ensure_object_mode()
        _push("Unix: " + (_state["label"] or "agent turn"))
    _state.update(active=False, mutated=False)
    return mutated
