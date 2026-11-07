# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""UI registration: properties, preferences, operators, panel, keymap, overlay."""

from __future__ import annotations

import bpy
from bpy.props import BoolProperty, EnumProperty, StringProperty
from bpy.types import AddonPreferences

from ..agent.runner import find_claude
from ..core import DEFAULT_MODEL, EFFORTS, MODELS
from . import island, operators, overlay, panel, simple

ROOT_PACKAGE = __package__.rpartition(".")[0]
_keymaps = []


class UnixPreferences(AddonPreferences):
    bl_idname = ROOT_PACKAGE

    claude_path: StringProperty(
        name="Claude CLI",
        description="Path to the claude executable. Leave empty to find it automatically",
        subtype='FILE_PATH',
    )
    rules: StringProperty(
        name="Global rules",
        description="Standing instructions sent with every chat in every file, e.g. "
                    "'Low-poly style. Name objects in PascalCase.' Per-file rules live in the panel's rules button",
    )
    snapshots: BoolProperty(
        name="Snapshot before each turn",
        description="Save a copy of the .blend before Unix first changes the scene in a turn (newest 20 kept). "
                    "Needed for the restore button on your messages",
        default=True,
    )
    lock_viewport: BoolProperty(
        name="Lock viewport edits while working",
        description="Block clicks and edit keys in the 3D viewport during a run so your edits cannot collide "
                    "with Unix's. Navigation still works",
        default=True,
    )
    show_hud: BoolProperty(
        name="Unix island",
        description="Show the floating Unix chat island in the 3D viewport",
        default=True,
        update=lambda self, context: island.settings.__setitem__("visible", self.show_hud),
    )
    free_camera: BoolProperty(
        name="Free camera",
        description="Hold the right mouse button to look around and W A S D / E Q to fly, the wheel for speed, Shift "
                    "for slow, like a game editor. In Beginner the keys fly without the button too and F frames the "
                    "selection. A right click that does not move still opens the context menu",
        default=True,
    )
    ai_images: BoolProperty(
        name="AI images (OpenAI via Codex)",
        description="Let Unix generate concept images, reference sheets and textures through your Codex CLI login",
        default=True,
    )
    online_assets: BoolProperty(
        name="Online assets, reference photos and recorded motion",
        description="Let Unix search and download free CC0 HDRIs, textures and models from polyhaven.com, look up "
                    "freely licensed photos of real things on Wikimedia Commons to model from, and fetch recordings of "
                    "real human motion from the CMU motion capture library to animate characters with",
        default=True,
    )
    auto_check: BoolProperty(
        name="Check the scene after every turn",
        description="When Unix finishes a turn that changed the scene, measure it for flickering surfaces, objects "
                    "standing inside each other and objects hanging in the air, and send Unix back to fix what is found",
        default=True,
    )
    auto_review: BoolProperty(
        name="Review realistic work against real photos",
        description="When you ask for something realistic, Unix is sent back after its first version to compare it "
                    "with photos of the real thing and improve it (up to two more passes). Slower, and much closer",
        default=True,
    )
    team_work: BoolProperty(
        name="Build as a team",
        description="In Agent mode, a build with several parts (a house and its garden, a room and its furniture) is "
                    "split between two to four agents that work at the same time, each on one part. Faster and more "
                    "detailed; uses more of your Claude or Codex allowance while it runs",
        default=True,
    )
    roblox_link: BoolProperty(
        name="Roblox Studio link",
        description="Keep a small plugin in Roblox Studio's plugin folder and a local connection open, so "
                    "Unix can build models and effects straight into the place you have open",
        default=True,
    )
    roblox_meshes: EnumProperty(
        name="Meshes sent to Roblox",
        description="Roblox keeps a mesh only as an asset on your Roblox account: without the upload it looks right "
                    "while you edit and turns into a checkered box in Play. Boxes, cylinders, balls, wedges and "
                    "effects are native Roblox parts and always stay",
        items=[('ASK', "Ask me", "Ask the first time a send has meshes that need uploading"),
               ('UPLOAD', "Upload to my account", "Always upload them, so what is sent is part of the game"),
               ('PREVIEW', "Preview only", "Never upload: meshes are for looking at in Edit mode")],
        default='ASK',
    )
    unrestricted_scripts: BoolProperty(
        name="Unrestricted scripts",
        description="Let Unix's scripts use file deletion, subprocess, network and file-open operators. "
                    "Off is safer: the guard refuses those and Unix uses its file tools instead",
        default=False,
    )

    def draw(self, context):
        layout = self.layout
        found = find_claude()
        row = layout.row()
        row.alert = not (found or self.claude_path)
        row.label(text=f"Claude CLI: {self.claude_path or found or 'not found — npm i -g @anthropic-ai/claude-code'}",
                  icon='CHECKMARK' if (found or self.claude_path) else 'ERROR')
        layout.prop(self, "claude_path")
        layout.prop(self, "rules")
        col = layout.column(align=True)
        col.prop(self, "snapshots")
        col.prop(self, "lock_viewport")
        col.prop(self, "auto_check")
        col.prop(self, "auto_review")
        col.prop(self, "team_work")
        col.prop(self, "show_hud")
        col.prop(self, "free_camera")
        col.prop(self, "online_assets")
        col.prop(self, "ai_images")
        col.prop(self, "roblox_link")
        col.prop(self, "unrestricted_scripts")
        layout.prop(self, "roblox_meshes")
        row = layout.row(align=True)
        for kind in ("snapshots", "exports", "renders", "chats", "assets"):
            row.operator("unix.open_folder", text=kind.capitalize()).kind = kind
        layout.operator("unix.apply_theme", icon='COLOR')
        layout.label(text="Ask from the viewport: Ctrl+Shift+Space. Sign in once with `claude auth login`.")


_classes = (UnixPreferences, *operators.classes, *panel.classes, *simple.classes)


def register() -> None:
    wm_type = bpy.types.WindowManager
    wm_type.unix_prompt = StringProperty(name="Message", description="What should Unix build or change?",
                                          options={'SKIP_SAVE'})
    wm_type.unix_answer = StringProperty(name="Answer", description="Your answer to Unix's question",
                                          options={'SKIP_SAVE'})
    wm_type.unix_attachments = StringProperty(name="Attachments", options={'SKIP_SAVE', 'HIDDEN'})
    wm_type.unix_model = EnumProperty(name="Model", description="Which model drives Unix",
                                       items=MODELS, default=DEFAULT_MODEL)
    wm_type.unix_effort = EnumProperty(name="Effort", description="How hard the model thinks before acting",
                                        items=EFFORTS, default="medium")
    for cls in _classes:
        bpy.utils.register_class(cls)
    panel.register()
    overlay.register()
    island.register()
    simple.register()
    addon = bpy.context.preferences.addons.get(ROOT_PACKAGE)
    if addon is not None:
        island.settings["visible"] = addon.preferences.show_hud
    keyconfig = bpy.context.window_manager.keyconfigs.addon
    if keyconfig is not None:
        keymap = keyconfig.keymaps.new(name="3D View", space_type='VIEW_3D')
        item = keymap.keymap_items.new("unix.quick_prompt", 'SPACE', 'PRESS', ctrl=True, shift=True)
        _keymaps.append((keymap, item))


def unregister() -> None:
    for keymap, item in _keymaps:
        keymap.keymap_items.remove(item)
    _keymaps.clear()
    simple.unregister()
    island.unregister()
    overlay.unregister()
    panel.unregister()
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    for name in ("unix_prompt", "unix_answer", "unix_attachments", "unix_model", "unix_effort"):
        if hasattr(bpy.types.WindowManager, name):
            delattr(bpy.types.WindowManager, name)
