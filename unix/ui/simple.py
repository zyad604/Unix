# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Beginner and Advanced: how much of Blender is on screen, and one-click playback.

Beginner shows the viewport, the Unix island, a top bar of five short menus (File, Edit, Render,
Window, Help) and one viewport toolbar (add, select / move / rotate / scale, render engine and samples,
shading, camera, playback, export); the free camera (``fly``) moves around it.
Advanced is all of Blender. In the Unix app the two are the ``Zen`` and ``Studio``
workspaces of the start-up file; in plain Blender, Beginner maximises the 3D viewport.

Playback matters because effects are animated shaders and simulations: in Solid view, or with
the timeline stopped, a fire looks like a frozen shape. ``unix.play`` switches the viewport
to a shaded view, loops the effect's own frame range and starts the timeline.
"""

from __future__ import annotations

from typing import Optional

import bpy
from bpy.props import EnumProperty
from bpy.types import Menu, Operator

from ..core import engine
from . import island

ZEN, STUDIO = "Zen", "Studio"
_original = {}


def level() -> str:
    saved = island.settings.get("level")
    if saved in ("beginner", "advanced"):
        return saved
    return "beginner" if ZEN in bpy.data.workspaces else "advanced"


def _window(context) -> Optional[bpy.types.Window]:
    wm = context.window_manager
    return context.window or (wm.windows[0] if wm and wm.windows else None)


def _viewport(screen) -> Optional[bpy.types.Area]:
    areas = [a for a in screen.areas if a.type == 'VIEW_3D']
    return max(areas, key=lambda a: a.width * a.height) if areas else None


def _dress(screen, beginner: bool) -> None:
    """Show or hide Blender's own controls around the 3D viewports of a screen."""
    for area in screen.areas:
        if area.type != 'VIEW_3D':
            continue
        space = area.spaces.active
        space.show_region_header = True          # in Beginner it is Unix's own short toolbar (_draw_view_header)
        space.show_region_tool_header = not beginner
        if beginner:
            space.show_region_toolbar = False
            space.show_region_ui = False
            space.show_gizmo = True
            space.show_gizmo_navigate = True     # the orbit ball stays: it is how a beginner looks around
    screen.show_statusbar = not beginner


def apply_level(window, new_level: str) -> None:
    beginner = new_level == "beginner"
    island.settings["level"] = new_level
    island.save_layout()
    if beginner:                                # the wheel zooms to where the mouse points, as in game editors
        inputs = bpy.context.preferences.inputs
        inputs.use_zoom_to_mouse = inputs.use_mouse_depth_navigate = True
    zen, studio = bpy.data.workspaces.get(ZEN), bpy.data.workspaces.get(STUDIO)
    if zen is not None and studio is not None:
        target = zen if beginner else studio
        for screen in target.screens:
            _dress(screen, beginner)
        if window.workspace != target:
            window.workspace = target
        return
    screen = window.screen                      # plain Blender: one screen, viewport maximised for beginners
    area = _viewport(screen)
    if area is None:
        return
    try:
        if beginner and not screen.show_fullscreen:
            with bpy.context.temp_override(window=window, screen=screen, area=area):
                bpy.ops.screen.screen_full_area()
        elif not beginner and screen.show_fullscreen:
            with bpy.context.temp_override(window=window, screen=screen, area=screen.areas[0]):
                bpy.ops.screen.back_to_previous()
    except RuntimeError:
        pass
    _dress(window.screen, beginner)


class UNIX_OT_ui_level(Operator):
    """Beginner: just the viewport and Unix. Advanced: all of Blender"""
    bl_idname = "unix.ui_level"
    bl_label = "Beginner or Advanced"

    level: EnumProperty(items=(("beginner", "Beginner", "Only the viewport and Unix"),
                               ("advanced", "Advanced", "The full Blender interface")))

    def execute(self, context):
        window = _window(context)
        if window is None:
            return {'CANCELLED'}
        apply_level(window, self.level)
        for area in window.screen.areas:
            area.tag_redraw()
        return {'FINISHED'}


class UNIX_OT_play(Operator):
    """Play or stop the animation. Effects loop over their own frames, in a shaded view so they are visible"""
    bl_idname = "unix.play"
    bl_label = "Play"

    action: EnumProperty(items=(("TOGGLE", "Toggle", ""), ("START", "Start", ""), ("STOP", "Stop", "")), default="TOGGLE")

    def execute(self, context):
        window = _window(context)
        if window is None:
            return {'CANCELLED'}
        screen = window.screen
        playing = screen.is_animation_playing
        override = dict(window=window, screen=screen)
        area = _viewport(screen)
        if area is not None:
            override["area"] = area
            override["region"] = next((r for r in area.regions if r.type == 'WINDOW'), None)
        if self.action == "STOP" or (self.action == "TOGGLE" and playing):
            if playing:
                with context.temp_override(**override):
                    bpy.ops.screen.animation_cancel(restore_frame=False)
            return {'FINISHED'}
        if playing:
            return {'FINISHED'}
        scene = context.scene
        effects = [o for o in scene.objects if "unix_start" in o and "unix_end" in o]
        if effects:
            def chosen(obj):
                try:
                    return obj.select_get() or any(c.select_get() for c in obj.children_recursive)
                except RuntimeError:
                    return False
            picked = [o for o in effects if chosen(o)] or effects
            start = min(int(o["unix_start"]) for o in picked)
            end = max(int(o["unix_end"]) for o in picked)
            scene.use_preview_range = True       # loops the effect without touching the scene's own range
            scene.frame_preview_start, scene.frame_preview_end = start, end
            scene.frame_set(start)
            for view in (a for a in screen.areas if a.type == 'VIEW_3D'):
                shading = view.spaces.active.shading
                if shading.type in {'SOLID', 'WIREFRAME'}:   # glow and fire shaders do not show in Solid view
                    if scene.render.engine == 'CYCLES':
                        shading.type = 'MATERIAL'
                        shading.use_scene_lights = shading.use_scene_world = True
                    else:
                        shading.type = 'RENDERED'
            self.report({'INFO'}, f"Playing frames {start}-{end} on a loop. Click Stop, or press Esc.")
        with context.temp_override(**override):
            bpy.ops.screen.animation_play()
        return {'FINISHED'}


class UNIX_MT_file(Menu):
    """The handful of file commands a beginner needs"""
    bl_idname = "UNIX_MT_file"
    bl_label = "File"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_AREA'
        layout.operator("wm.read_homefile", text="New", icon='FILE_NEW')
        layout.operator("wm.open_mainfile", text="Open…", icon='FILE_FOLDER')
        layout.menu("TOPBAR_MT_file_open_recent")
        layout.separator()
        layout.operator("wm.save_mainfile", text="Save", icon='FILE_TICK')
        layout.operator("wm.save_as_mainfile", text="Save As…")
        layout.separator()
        layout.menu("TOPBAR_MT_file_import", icon='IMPORT')
        layout.menu("TOPBAR_MT_file_export", icon='EXPORT')
        layout.separator()
        layout.operator("render.render", text="Render Image", icon='RENDER_STILL').write_still = False
        layout.operator("render.render", text="Render Animation", icon='RENDER_ANIMATION').animation = True
        layout.separator()
        layout.operator("unix.ui_level", text="Show all of Blender (Advanced)", icon='WORKSPACE').level = "advanced"
        layout.operator("wm.quit_blender", text="Quit", icon='QUIT')


class UNIX_MT_edit(Menu):
    bl_idname = "UNIX_MT_edit"
    bl_label = "Edit"

    def draw(self, context):
        layout = self.layout
        layout.operator("ed.undo", text="Undo", icon='LOOP_BACK')
        layout.operator("ed.redo", text="Redo", icon='LOOP_FORWARDS')
        layout.menu("TOPBAR_MT_undo_history")
        layout.separator()
        layout.operator("object.duplicate_move", text="Duplicate", icon='DUPLICATE')
        layout.operator("object.delete", text="Delete", icon='TRASH').use_global = False
        layout.separator()
        layout.operator("screen.userpref_show", text="Preferences…", icon='PREFERENCES')


class UNIX_MT_render(Menu):
    bl_idname = "UNIX_MT_render"
    bl_label = "Render"

    def draw(self, context):
        layout = self.layout
        layout.operator_context = 'INVOKE_DEFAULT'
        layout.operator("render.render", text="Render Image", icon='RENDER_STILL')
        layout.operator("render.render", text="Render Animation", icon='RENDER_ANIMATION').animation = True
        layout.operator("render.view_show", text="View Render", icon='IMAGE')
        layout.separator()
        layout.prop(context.scene.render, "engine", text="")


class UNIX_MT_window(Menu):
    bl_idname = "UNIX_MT_window"
    bl_label = "Window"

    def draw(self, context):
        layout = self.layout
        layout.operator("wm.window_fullscreen_toggle", icon='FULLSCREEN_ENTER')
        layout.operator("unix.toggle_hud", text="Show or Hide the Unix Chat", icon='HIDE_OFF')
        layout.separator()
        layout.operator("unix.ui_level", text="Show all of Blender (Advanced)", icon='WORKSPACE').level = "advanced"


class UNIX_MT_help(Menu):
    bl_idname = "UNIX_MT_help"
    bl_label = "Help"

    def draw(self, context):
        layout = self.layout
        layout.label(text="Moving around", icon='VIEW_PAN')
        for line in ("Hold right mouse  ·  look around", "W A S D  ·  fly", "E / Q  ·  up / down",
                     "Shift  ·  slow     wheel while holding right mouse  ·  speed", "F  ·  frame the selection",
                     "Wheel  ·  zoom to the mouse     middle mouse  ·  orbit"):
            layout.label(text="      " + line)
        layout.separator()
        layout.operator("wm.url_open", text="Blender Manual", icon='HELP').url = "https://docs.blender.org/manual/en/4.5/"


class UNIX_MT_export(Menu):
    bl_idname = "UNIX_MT_export"
    bl_label = "Export"

    def draw(self, context):
        layout = self.layout
        layout.operator("unix.engine_tab", text="Send to a Game Engine…", icon='EXPORT')
        layout.separator()
        layout.menu("TOPBAR_MT_file_export", text="Export a File", icon='FILE')


class UNIX_OT_engine_tab(Operator):
    """Open the Unix chat on its Engine tab: send the selection to Roblox Studio, Unreal, Unity or Godot"""
    bl_idname = "unix.engine_tab"
    bl_label = "Send to a Game Engine"

    def execute(self, context):
        island.settings.update(visible=True, collapsed=False, mode="engine", scroll=0.0, notice="")
        island.refresh_targets()
        island.save_layout()
        for area in context.screen.areas if context.screen else ():
            area.tag_redraw()
        return {'FINISHED'}


def _draw_left(self, context):
    if level() != "beginner":
        return _original["left"](self, context)
    row = self.layout.row(align=True)
    for menu in ("UNIX_MT_file", "UNIX_MT_edit", "UNIX_MT_render", "UNIX_MT_window", "UNIX_MT_help"):
        row.menu(menu)


def _draw_right(self, context):
    if level() != "beginner":
        return _original["right"](self, context)
    if _original.get("switch"):                 # plain Blender: the switch sits on the right (the app's top bar has its own)
        _switch(self.layout)


def _switch(layout) -> None:
    row = layout.row(align=True)
    row.scale_x = 1.15
    current = level()
    row.operator("unix.ui_level", text="Beginner", depress=current == "beginner").level = "beginner"
    row.operator("unix.ui_level", text="Advanced", depress=current == "advanced").level = "advanced"


def _draw_switch(self, context):
    """Beginner / Advanced buttons for plain Blender, in Advanced (Beginner has them on the right)."""
    if context.region.alignment == 'RIGHT' or level() == "beginner":
        return
    _switch(self.layout)
    self.layout.separator()


TOOLS = (("builtin.select_box", 'RESTRICT_SELECT_OFF'), ("builtin.move", 'EMPTY_ARROWS'),
         ("builtin.rotate", 'FILE_REFRESH'), ("builtin.scale", 'FULLSCREEN_ENTER'))


def _draw_view_header(self, context):
    """Beginner's viewport toolbar: what you reach for while building, and nothing else."""
    if level() != "beginner":
        return _original["view"](self, context)
    layout, scene, space = self.layout, context.scene, context.space_data
    row = layout.row()
    row.ui_units_x = 6.0            # a header squeezes labels next to a spacer down to "Add Obje…"
    if context.mode == 'OBJECT':
        row.menu("VIEW3D_MT_add", text="Add Objects", icon='ADD')
    else:
        row.operator("object.mode_set", text="Back to Objects", icon='OBJECT_DATAMODE').mode = 'OBJECT'
    row = layout.row(align=True)
    tool = context.workspace.tools.from_space_view3d_mode(context.mode, create=False) if context.workspace else None
    current = tool.idname if tool else ""
    for idname, icon in TOOLS:
        row.operator("wm.tool_set_by_id", text="", icon=icon, depress=current == idname).name = idname

    layout.separator_spacer()
    row = layout.row(align=True)
    row.prop(scene.render, "engine", text="")
    if scene.render.engine == 'CYCLES':
        row.prop(scene.cycles, "samples", text="Samples")
    elif scene.render.engine == 'BLENDER_EEVEE_NEXT':
        row.prop(scene.eevee, "taa_render_samples", text="Samples")
    layout.prop(space.shading, "type", text="", expand=True)
    rv3d = context.region_data
    layout.operator("view3d.view_camera", text="Camera", icon='OUTLINER_OB_CAMERA',
                    depress=rv3d is not None and rv3d.view_perspective == 'CAMERA')
    row = layout.row(align=True)
    row.operator("screen.frame_jump", text="", icon='REW').end = False
    playing = context.screen is not None and context.screen.is_animation_playing
    row.operator("unix.play", text="", icon='PAUSE' if playing else 'PLAY')
    row.operator("screen.frame_jump", text="", icon='FF').end = True
    layout.separator_spacer()
    row = layout.row()
    row.ui_units_x = 4.5
    row.menu("UNIX_MT_export", text="Export", icon='EXPORT')


def startup() -> None:
    """Open in the level the user left it in (Beginner the first time in the app), after start-up and after
    every file opened: a file brings its own screens, and in plain Blender (no Zen and Studio workspaces, how
    foxa (2).exe opens without its launcher) Beginner had only been applied on a click."""
    if bpy.app.background:
        return
    if (ZEN not in bpy.data.workspaces or STUDIO not in bpy.data.workspaces) and island.settings.get("level") is None:
        return                                  # plain Blender, nothing chosen yet: leave Blender as it is
    wm = bpy.context.window_manager
    if wm and wm.windows:
        apply_level(wm.windows[0], level())


def after_turn() -> None:
    """Start playback when Unix has just built an effect, so the user sees it move."""
    if bpy.app.background or not engine.messages:
        return
    last = engine.messages[-1]
    if last.get("role") != "assistant":
        return
    built = any(step.get("name") == "vfx_create" and step.get("status") != "error"
                for part in last.get("parts", []) if part.get("type") == "steps" for step in part["steps"])
    wm = bpy.context.window_manager
    if built and wm and wm.windows:
        try:
            with bpy.context.temp_override(window=wm.windows[0]):
                bpy.ops.unix.play(action='START')
        except RuntimeError:
            pass


classes = (UNIX_OT_ui_level, UNIX_OT_play, UNIX_MT_file, UNIX_MT_edit, UNIX_MT_render, UNIX_MT_window, UNIX_MT_help,
           UNIX_MT_export, UNIX_OT_engine_tab)


def register() -> None:
    bar = getattr(bpy.types, "TOPBAR_HT_upper_bar", None)
    if bar is None or bpy.app.background:
        return
    _original.update(left=bar.draw_left, right=bar.draw_right, view=bpy.types.VIEW3D_HT_header.draw)
    bar.draw_left, bar.draw_right = _draw_left, _draw_right
    bpy.types.VIEW3D_HT_header.draw = _draw_view_header
    if bpy.context.preferences.app_template != "Unix":
        bar.prepend(_draw_switch)
        _original["switch"] = True


def unregister() -> None:
    bar = getattr(bpy.types, "TOPBAR_HT_upper_bar", None)
    if bar is None or not _original:
        return
    bar.draw_left, bar.draw_right = _original["left"], _original["right"]
    bpy.types.VIEW3D_HT_header.draw = _original["view"]
    if _original.get("switch"):
        bar.remove(_draw_switch)
    _original.clear()
