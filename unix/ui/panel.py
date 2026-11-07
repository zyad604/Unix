# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""The chat panel (3D Viewport > Sidebar > Unix).

Stock Blender has no rich-text widget, so the transcript is boxes of word-wrapped
labels: user bubbles on the right, Unix's replies full width, tool calls folded into
"N steps" groups with friendly results, the live plan above, the composer at the bottom.
"""

from __future__ import annotations

import os
import re
import time
from typing import List

import bpy
import bpy.utils.previews
from bpy.types import Panel

from ..core import engine
from ..tools import interact
from . import operators

MAX_MESSAGES = 14
MAX_PREVIEWS = 60
_MD = re.compile(r"(\*\*|__|`)")
_STATUS = {"idle": ("Ready", 'CHECKMARK'), "working": ("Working", 'SORTTIME'), "waiting": ("Needs your answer", 'QUESTION')}
_previews = None


def wrap(text: str, width: int) -> List[str]:
    """Greedy word wrap that keeps blank lines and turns markdown bullets into dots."""
    lines: List[str] = []
    for raw in _MD.sub("", text).splitlines():
        raw = raw.rstrip()
        if not raw:
            lines.append("")
            continue
        indent = ""
        stripped = raw.lstrip()
        if stripped.startswith(("- ", "* ")):
            stripped, indent = "•  " + stripped[2:], "    "
        elif stripped.startswith("#"):
            stripped = stripped.lstrip("# ").upper()
        current = ""
        for word in stripped.split(" "):
            if current and len(current) + 1 + len(word) > width:
                lines.append(current)
                current = indent + word
            else:
                current = f"{current} {word}" if current else word
        lines.append(current)
    return lines


def _chars(context) -> int:
    scale = context.preferences.system.ui_scale
    return max(24, int((context.region.width - 50 * scale) / (6.3 * scale)))


def _text_block(layout, text: str, width: int, alert: bool = False, muted: bool = False) -> None:
    col = layout.column(align=True)
    col.scale_y = 0.78
    col.alert = alert
    col.enabled = not muted
    for line in wrap(text, width):
        col.label(text=line if line else " ")


def _thumbnail(path: str) -> int:
    """Icon id for an image file, loaded on first use (0 when it cannot be shown)."""
    global _previews
    if _previews is None or not os.path.isfile(path):
        return 0
    if path not in _previews:
        if len(_previews) >= MAX_PREVIEWS:
            _previews.clear()
        try:
            _previews.load(path, path, 'IMAGE')
        except Exception:
            return 0
    return _previews[path].icon_id


def _draw_steps(layout, part, message_index: int, part_index: int, width: int, show_images: bool) -> None:
    steps = part["steps"]
    running = next((s for s in steps if s["status"] == "running"), None)
    failed = sum(1 for s in steps if s["status"] == "error")
    title = f"{len(steps)} step{'s' if len(steps) != 1 else ''}"
    if running:
        title += f"  ·  {running['label']}…"
    elif failed:
        title += f"  ·  {failed} failed"
    row = layout.row(align=True)
    op = row.operator("unix.toggle_steps", text=title, emboss=False,
                      icon='DOWNARROW_HLT' if part.get("open") else 'RIGHTARROW')
    op.message, op.part = message_index, part_index
    if part.get("open"):
        col = layout.column(align=True)
        col.scale_y = 0.78
        for step in steps:
            glyph = "■" if step["acted"] else "○"
            line = f"{glyph}  {step['label']}"
            if step["detail"]:
                line += f": {step['detail']}"
            if step["status"] == "running":
                line += " …"
            elif step.get("result"):
                line += f"  —  {step['result']}"
            sub = col.row()
            sub.alert = step["status"] == "error"
            sub.label(text=line[:width + 6])
            if step["status"] == "error" and step["summary"]:
                for extra in wrap(step["summary"], width - 4)[:3]:
                    col.label(text="      " + extra)
    if show_images:  # what Unix last saw
        images = next((s["images"] for s in reversed(steps) if s.get("images")), [])
        icons = [i for i in (_thumbnail(p) for p in images[:2]) if i]
        if icons:
            grid = layout.row(align=True)
            for icon in icons:
                grid.template_icon(icon_value=icon, scale=5.0)


def _draw_message(layout, message, index: int, width: int, newest: bool) -> None:
    if message["role"] == "user":
        split = layout.split(factor=0.18)
        side = split.column()
        if message.get("snapshot") and not engine.busy and os.path.isfile(message["snapshot"]):
            side.operator("unix.restore", text="", icon='LOOP_BACK', emboss=False).message = index
        else:
            side.label(text="")
        box = split.box()
        _text_block(box, message["text"], int(width * 0.8))
        count = len(message.get("attachments") or [])
        if count:
            row = box.row()
            row.scale_y = 0.7
            row.enabled = False
            row.label(text=f"{count} attachment{'s' if count != 1 else ''}", icon='FILE_IMAGE')
        return
    box = layout.box()
    parts = message["parts"]
    if not parts:
        box.label(text=engine.activity or "Thinking…", icon='SORTTIME')
    last_steps = max((i for i, p in enumerate(parts) if p["type"] == "steps"), default=-1)
    for part_index, part in enumerate(parts):
        if part["type"] == "steps":
            _draw_steps(box, part, index, part_index, width, show_images=newest and part_index == last_steps)
        else:
            _text_block(box, part["text"], width, alert=part["type"] == "error", muted=part["type"] == "note")
    if message.get("meta"):
        row = box.row()
        row.scale_y = 0.7
        row.enabled = False
        row.label(text=message["meta"])


class UNIX_PT_chat(Panel):
    bl_label = "Unix"
    bl_idname = "UNIX_PT_chat"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Unix"

    def draw_header(self, context):
        if _previews is not None and "logo" in _previews:
            self.layout.label(text="", icon_value=_previews["logo"].icon_id)

    def draw_header_preset(self, context):
        row = self.layout.row(align=True)
        row.operator("unix.new_chat", text="", icon='ADD', emboss=False)
        row.menu("UNIX_MT_history", text="", icon='TIME')
        row.menu("UNIX_MT_copy", text="", icon='COPYDOWN')
        row.operator("unix.edit_rules", text="", icon='TEXT', emboss=False)
        row.operator("unix.open_folder", text="", icon='FILE_FOLDER', emboss=False).kind = "snapshots"
        row.operator("unix.apply_theme", text="", icon='COLOR', emboss=False)
        row.operator("unix.toggle_hud", text="", icon='WINDOW', emboss=False)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        width = _chars(context)

        text, icon = _STATUS.get(engine.status, _STATUS["idle"])
        row = layout.row()
        if engine.busy:
            text = f"{text}  ·  {int(time.monotonic() - engine.started_at)}s"
        row.label(text=text, icon=icon)
        if engine.busy and engine.activity:
            sub = layout.row()
            sub.scale_y = 0.75
            sub.enabled = False
            sub.label(text=engine.activity[:width])

        if engine.restored_from:
            box = layout.box()
            _text_block(box, "You are viewing a restored copy. Your project file on disk is unchanged.", width)
            box.operator("unix.save_back", text=f"Save back to {os.path.basename(engine.restored_from)}", icon='FILE_TICK')

        plan = interact.get_plan()
        if plan:
            box = layout.box()
            done = sum(1 for s in plan if s["status"] == "done")
            box.label(text=f"Plan  ·  {done}/{len(plan)}", icon='PRESET')
            col = box.column(align=True)
            col.scale_y = 0.78
            for step in plan:
                glyph = {"done": 'CHECKBOX_HLT', "in_progress": 'PLAY', "pending": 'CHECKBOX_DEHLT'}[step["status"]]
                r = col.row()
                r.enabled = step["status"] != "done"
                r.label(text=step["title"][:width], icon=glyph)

        if not engine.messages:
            box = layout.box()
            _text_block(box, "Tell Unix what to build or change. It reads your scene, works in small steps, "
                             "looks at the result and checks it before answering.", width)
            col = box.column(align=True)
            col.scale_y = 0.78
            col.enabled = False
            for hint in ("•  a low-poly pine tree, 3 m tall", "•  make the selected chair wider and add armrests",
                         "•  light this for a product shot with a real HDRI", "•  find an oak texture and put it on the table",
                         "•  check my meshes and fix the problems"):
                col.label(text=hint)
        else:
            hidden = len(engine.messages) - MAX_MESSAGES
            if hidden > 0:
                row = layout.row()
                row.enabled = False
                row.label(text=f"{hidden} earlier messages hidden")
            last = len(engine.messages) - 1
            for index, message in list(enumerate(engine.messages))[-MAX_MESSAGES:]:
                _draw_message(layout, message, index, width, newest=index == last)

        question = interact.pending_question()
        if question:
            box = layout.box()
            box.alert = True
            _text_block(box, question["text"], width)
            for option in question["options"]:
                box.operator("unix.answer", text=option).option = option
            row = box.row(align=True)
            row.prop(wm, "unix_answer", text="")
            row.operator("unix.answer", text="", icon='CHECKMARK').option = ""

        if engine.can_continue and not engine.busy:
            layout.operator("unix.continue_run", icon='PLAY')

        layout.separator(factor=0.4)
        col = layout.column(align=True)
        attached = operators.attachments(wm)
        if attached:
            row = col.row(align=True)
            row.scale_y = 0.85
            names = ", ".join(os.path.basename(p) for p in attached[:3]) + (" …" if len(attached) > 3 else "")
            row.label(text=names[:width - 4], icon='FILE_IMAGE')
            row.operator("unix.clear_attachments", text="", icon='X', emboss=False)
        row = col.row(align=True)
        row.scale_y = 1.5
        row.enabled = not engine.busy
        row.prop(wm, "unix_prompt", text="", icon='GREASEPENCIL')
        row.operator("unix.attach", text="", icon='ADD')
        row = col.row(align=True)
        row.scale_y = 1.25
        row.prop(wm, "unix_model", text="")
        row.prop(wm, "unix_effort", text="")
        if engine.busy:
            row.operator("unix.stop", text="Stop", icon='SNAP_FACE')
        else:
            row.operator("unix.send", text="Send", icon='PLAY')


classes = (UNIX_PT_chat,)


def register() -> None:
    global _previews
    _previews = bpy.utils.previews.new()
    logo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logo.png")
    if os.path.isfile(logo):
        _previews.load("logo", logo, 'IMAGE')


def unregister() -> None:
    global _previews
    if _previews is not None:
        bpy.utils.previews.remove(_previews)
        _previews = None
