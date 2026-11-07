# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Unix application template: what turns Blender into the Unix app.

- enables the bundled ``unix`` add-on;
- replaces the splash with a level chooser (Beginner or Advanced);
- adds the brand and the Beginner / Advanced switch to the top bar.

The splash artwork (``splash.png``) and the start-up file (``startup.blend``, with the
Zen and Studio workspaces) sit next to this file and are picked up by Blender itself.
"""

import os

import bpy
import bpy.utils.previews
from bpy.props import EnumProperty
from bpy.types import Menu, Operator

ZEN, STUDIO = "Zen", "Studio"
_original_splash = None
_icons = None


def _logo() -> int:
    return _icons["logo"].icon_id if _icons is not None and "logo" in _icons else 0


class UNIX_OT_set_mode(Operator):
    """Beginner: just you, the viewport and Unix. Advanced: the full Blender workspace"""
    bl_idname = "unix.set_mode"
    bl_label = "Switch Mode"

    mode: EnumProperty(items=((ZEN, "Beginner", "A clean viewport with the Unix island"),
                              (STUDIO, "Advanced", "The full Blender interface")))

    def execute(self, context):
        if hasattr(bpy.types, "UNIX_OT_ui_level"):      # the add-on also hides Blender's own controls
            return bpy.ops.unix.ui_level(level="beginner" if self.mode == ZEN else "advanced")
        workspace = bpy.data.workspaces.get(self.mode)
        if workspace is None:
            self.report({'WARNING'}, f"This file has no '{self.mode}' workspace. Use File > New > Unix.")
            return {'CANCELLED'}
        context.window.workspace = workspace
        return {'FINISHED'}


class WM_MT_splash(Menu):
    bl_label = "Splash"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'EXEC_DEFAULT'
        layout.emboss = 'PULLDOWN_MENU'
        split = layout.split()

        col = split.column()
        col.label(text="How much of Blender do you want to see?")
        col.scale_y = 1.3
        col.operator("unix.set_mode", text="Beginner   ·   just the viewport and Unix", icon='LIGHT_SUN').mode = ZEN
        col.operator("unix.set_mode", text="Advanced   ·   all of Blender", icon='WORKSPACE').mode = STUDIO
        col.separator()
        col.operator_context = 'INVOKE_DEFAULT'
        col.operator("wm.open_mainfile", text="Open…", icon='FILE_FOLDER')

        col = split.column()
        col.label(text="Recent files")
        col.scale_y = 1.1
        try:
            if not col.template_recent_files(rows=5):
                col.label(text="Nothing yet")
        except (AttributeError, TypeError):
            col.operator_context = 'INVOKE_DEFAULT'
            col.operator("wm.open_mainfile", text="Open…", icon='FILE_FOLDER')

        layout.separator()
        row = layout.row()
        row.enabled = False
        row.label(text="Click the Unix island in the viewport, or press Ctrl+Shift+Space, and say what to build.")
        layout.separator()


def _draw_brand(self, context):
    if context.region.alignment == 'RIGHT':
        return
    row = self.layout.row(align=True)
    row.label(text="Unix", icon_value=_logo())
    current = context.workspace.name if context.workspace else ""
    row.operator("unix.set_mode", text="Beginner", depress=current == ZEN).mode = ZEN
    row.operator("unix.set_mode", text="Advanced", depress=current != ZEN).mode = STUDIO
    row.separator()


def _enable_addon():
    """Templates register before Blender sets up its add-on paths, so this runs a moment later."""
    import addon_utils
    if "unix" not in bpy.context.preferences.addons:
        addon_utils.enable("unix", default_set=True, persistent=True)
    _first_run_theme()
    return None


def _first_run_theme():
    """Apply the Unix theme once. After that the user's own theme choice is left alone."""
    try:
        marker = os.path.join(bpy.utils.user_resource('CONFIG', path="unix", create=True), ".theme_applied")
        if os.path.exists(marker):
            return
        import unix
        theme = os.path.join(os.path.dirname(unix.__file__), "theme", "Unix.xml")
        wm = bpy.context.window_manager
        if os.path.isfile(theme) and wm.windows:
            with bpy.context.temp_override(window=wm.windows[0]):
                bpy.ops.script.execute_preset(filepath=theme, menu_idname="USERPREF_MT_interface_theme_presets")
            bpy.ops.wm.save_userpref()
            open(marker, "w").close()
    except Exception as exc:
        print(f"[Unix] could not apply the theme: {exc!r}")


def register():
    global _original_splash, _icons
    bpy.app.timers.register(_enable_addon, first_interval=0.1, persistent=True)

    _icons = bpy.utils.previews.new()
    logo = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logo.png")
    if os.path.isfile(logo):
        _icons.load("logo", logo, 'IMAGE')

    bpy.utils.register_class(UNIX_OT_set_mode)
    _original_splash = getattr(bpy.types, "WM_MT_splash", None)
    if _original_splash is not None:
        bpy.utils.unregister_class(_original_splash)
    bpy.utils.register_class(WM_MT_splash)
    bpy.types.TOPBAR_HT_upper_bar.prepend(_draw_brand)


def unregister():
    global _original_splash, _icons
    bpy.types.TOPBAR_HT_upper_bar.remove(_draw_brand)
    bpy.utils.unregister_class(WM_MT_splash)
    if _original_splash is not None:
        bpy.utils.register_class(_original_splash)
        _original_splash = None
    bpy.utils.unregister_class(UNIX_OT_set_mode)
    if _icons is not None:
        bpy.utils.previews.remove(_icons)
        _icons = None
