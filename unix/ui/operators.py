# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Chat operators: send, stop, history, attachments, rules, restore, viewport lock."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import bpy
from bpy.props import BoolProperty, CollectionProperty, IntProperty, StringProperty
from bpy.types import Menu, Operator, OperatorFileListElement

from ..core import engine
from ..tools import interact
from ..tools._util import data_dir

ROOT_PACKAGE = __package__.rpartition(".")[0]
RULES_TEXT = "Unix Rules"
SEPARATOR = "|"
_RULES_TEMPLATE = """# Unix rules for this file. One instruction per line; lines starting with # are ignored.
# Examples:
# Everything is low-poly and flat shaded.
# Target engine is Unity: keep assets under 10k triangles and name them PascalCase.
"""


def preferences(context):
    addon = context.preferences.addons.get(ROOT_PACKAGE)
    return addon.preferences if addon else None


def apply_preferences(context) -> str:
    """Push preference values into the engine. Returns the Claude CLI path override."""
    from ..tools import assets, generate, script
    prefs = preferences(context)
    if prefs is None:
        return ""
    script.set_unrestricted(prefs.unrestricted_scripts)
    assets.settings["enabled"] = prefs.online_assets
    generate.settings["enabled"] = prefs.ai_images
    engine.settings["snapshots"] = prefs.snapshots
    engine.settings["auto_check"] = prefs.auto_check
    engine.settings["auto_review"] = prefs.auto_review
    engine.settings["team"] = prefs.team_work
    return bpy.path.abspath(prefs.claude_path) if prefs.claude_path else ""


def collect_rules(context) -> str:
    """Global rules (preferences) followed by this file's rules (a text datablock)."""
    lines = []
    prefs = preferences(context)
    if prefs is not None and prefs.rules.strip():
        lines.append(prefs.rules.strip())
    text = bpy.data.texts.get(RULES_TEXT)
    if text is not None:
        lines += [l.body.strip() for l in text.lines if l.body.strip() and not l.body.lstrip().startswith("#")]
    return "\n".join(f"- {line}" for line in lines)[:6000]


def attachments(wm) -> list:
    return [p for p in wm.unix_attachments.split(SEPARATOR) if p]


def _send(context, text: str, mode: str = "agent", auto: bool = False):
    from . import island
    wm = context.window_manager
    prefs = preferences(context)
    instruction = island.MODE_PROMPTS.get(mode, "")
    if auto:
        instruction = (instruction + "\n" if instruction else "") + (
            "[Auto mode] Do not ask the user questions. Decide open choices yourself and list them in your summary.")
    error = engine.send(text, model=wm.unix_model, claude_bin=apply_preferences(context),
                        attachments=attachments(wm), rules=collect_rules(context),
                        mode=mode, instruction=instruction, effort=wm.unix_effort)
    if error is None:
        wm.unix_prompt = ""
        wm.unix_attachments = ""
        if not bpy.app.background and (prefs is None or prefs.lock_viewport):
            try:
                bpy.ops.unix.viewport_lock('INVOKE_DEFAULT')
            except RuntimeError:
                pass  # no window context (e.g. called from a timer); the lock is optional
    return error


class UNIX_OT_send(Operator):
    """Send the message to Unix"""
    bl_idname = "unix.send"
    bl_label = "Send"

    def execute(self, context):
        error = _send(context, context.window_manager.unix_prompt)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        return {'FINISHED'}


class UNIX_OT_continue(Operator):
    """Ask Unix to carry on from where the last run stopped"""
    bl_idname = "unix.continue_run"
    bl_label = "Continue"

    def execute(self, context):
        error = _send(context, "Continue from where you stopped. Check the scene first, then finish the task.")
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        return {'FINISHED'}


class UNIX_OT_quick_prompt(Operator):
    """Ask Unix from anywhere in the 3D viewport"""
    bl_idname = "unix.quick_prompt"
    bl_label = "Ask Unix"

    prompt: StringProperty(name="", description="What should Unix do?", options={'SKIP_SAVE'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=520)

    def draw(self, context):
        col = self.layout.column()
        col.activate_init = True
        col.prop(self, "prompt", text="")

    def execute(self, context):
        error = _send(context, self.prompt)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        return {'FINISHED'}


class UNIX_OT_stop(Operator):
    """Stop the current run. Changes made so far stay in the scene (Ctrl+Z reverts them)"""
    bl_idname = "unix.stop"
    bl_label = "Stop"

    def execute(self, context):
        engine.stop_turn()
        return {'FINISHED'}


class UNIX_OT_new_chat(Operator):
    """Start a fresh conversation (the scene is not changed; the current chat is kept in History)"""
    bl_idname = "unix.new_chat"
    bl_label = "New Chat"

    def execute(self, context):
        engine.new_chat()
        return {'FINISHED'}


class UNIX_OT_open_chat(Operator):
    """Reopen this conversation and continue it"""
    bl_idname = "unix.open_chat"
    bl_label = "Open Chat"
    bl_options = {'INTERNAL'}

    chat_id: StringProperty()

    def execute(self, context):
        error = engine.open_chat(self.chat_id)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        return {'FINISHED'}


class UNIX_MT_history(Menu):
    bl_idname = "UNIX_MT_history"
    bl_label = "Chat History"

    def draw(self, context):
        layout = self.layout
        chats = engine.list_chats()
        if not chats:
            layout.label(text="No saved chats yet")
            return
        today = time.strftime("%Y-%m-%d")
        for chat in chats[:25]:
            day = time.strftime("%Y-%m-%d", time.localtime(chat["updated"]))
            when = time.strftime("%H:%M", time.localtime(chat["updated"])) if day == today else day[5:]
            current = chat["id"] == engine.chat_id
            op = layout.operator("unix.open_chat", text=f"{when}   {chat['title']}",
                                 icon='RADIOBUT_ON' if current else 'RADIOBUT_OFF')
            op.chat_id = chat["id"]


class UNIX_OT_set_model(Operator):
    """Choose which model drives Unix"""
    bl_idname = "unix.set_model"
    bl_label = "Set Model"
    bl_options = {'INTERNAL'}

    model: StringProperty()

    def execute(self, context):
        context.window_manager.unix_model = self.model
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class UNIX_MT_models(Menu):
    bl_idname = "UNIX_MT_models"
    bl_label = "Model"

    def draw(self, context):
        from ..core import MODELS
        current = context.window_manager.unix_model
        family = ""
        for ident, label, description in MODELS:
            if family and label.split()[0] != family:
                self.layout.separator()            # Sonnets, then Opuses
            family = label.split()[0]
            op = self.layout.operator("unix.set_model", text=f"{label}   ·   {description}",
                                      icon='RADIOBUT_ON' if ident == current else 'RADIOBUT_OFF')
            op.model = ident


class UNIX_OT_copy(Operator):
    """Copy to the clipboard"""
    bl_idname = "unix.copy"
    bl_label = "Copy"

    scope: StringProperty(default="reply")

    def execute(self, context):
        text = engine.transcript(self.scope)
        if not text:
            self.report({'WARNING'}, "There is nothing to copy yet.")
            return {'CANCELLED'}
        context.window_manager.clipboard = text
        what = {"reply": "Unix's last reply", "chat": "the whole chat", "work": "everything Unix did, with scripts"}
        self.report({'INFO'}, f"Copied {what.get(self.scope, 'text')} ({len(text):,} characters)")
        return {'FINISHED'}


class UNIX_MT_copy(Menu):
    bl_idname = "UNIX_MT_copy"
    bl_label = "Copy"

    def draw(self, context):
        layout = self.layout
        layout.operator("unix.copy", text="Unix's last reply", icon='COPYDOWN').scope = "reply"
        layout.operator("unix.copy", text="What it did  (steps, scripts and results)", icon='TEXT').scope = "work"
        layout.operator("unix.copy", text="The whole chat", icon='DOCUMENTS').scope = "chat"


class UNIX_OT_set_effort(Operator):
    """Choose how hard the model thinks before acting"""
    bl_idname = "unix.set_effort"
    bl_label = "Set Effort"
    bl_options = {'INTERNAL'}

    effort: StringProperty()

    def execute(self, context):
        context.window_manager.unix_effort = self.effort
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class UNIX_MT_effort(Menu):
    bl_idname = "UNIX_MT_effort"
    bl_label = "Effort"

    def draw(self, context):
        from ..core import EFFORTS
        current = context.window_manager.unix_effort
        for ident, label, description in EFFORTS:
            op = self.layout.operator("unix.set_effort", text=f"{label}   ·   {description}",
                                      icon='RADIOBUT_ON' if ident == current else 'RADIOBUT_OFF')
            op.effort = ident


class UNIX_OT_attach(Operator):
    """Attach reference images or 3D files to the next message"""
    bl_idname = "unix.attach"
    bl_label = "Attach Files"

    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    filter_glob: StringProperty(default="*.png;*.jpg;*.jpeg;*.webp;*.exr;*.hdr;*.tif;*.tiff;*.bmp;*.tga;"
                                        "*.glb;*.gltf;*.fbx;*.obj;*.stl;*.ply;*.usd;*.usdc;*.usda;*.usdz;*.blend",
                                options={'HIDDEN'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        wm = context.window_manager
        current = attachments(wm)
        for item in self.files:
            path = os.path.join(self.directory, item.name)
            if os.path.isfile(path) and path not in current:
                current.append(path)
        wm.unix_attachments = SEPARATOR.join(current[:10])
        return {'FINISHED'}


class UNIX_OT_clear_attachments(Operator):
    """Remove the attached files from the next message"""
    bl_idname = "unix.clear_attachments"
    bl_label = "Clear Attachments"

    def execute(self, context):
        context.window_manager.unix_attachments = ""
        return {'FINISHED'}


class UNIX_OT_edit_rules(Operator):
    """Open this file's standing rules for Unix (a text block saved with the .blend)"""
    bl_idname = "unix.edit_rules"
    bl_label = "Edit Rules"

    def execute(self, context):
        text = bpy.data.texts.get(RULES_TEXT)
        if text is None:
            text = bpy.data.texts.new(RULES_TEXT)
            text.write(_RULES_TEMPLATE)
        shown = False
        for area in context.screen.areas:
            if area.type == 'TEXT_EDITOR':
                area.spaces[0].text = text
                shown = True
                break
        self.report({'INFO'}, "Rules opened in the Text Editor." if shown else
                    f"Rules are in the text block '{RULES_TEXT}': open a Text Editor (Scripting tab) to edit them.")
        return {'FINISHED'}


class UNIX_OT_answer(Operator):
    """Answer Unix's question"""
    bl_idname = "unix.answer"
    bl_label = "Answer"

    option: StringProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        wm = context.window_manager
        answer = self.option or wm.unix_answer.strip()
        if not answer:
            self.report({'WARNING'}, "Type an answer or pick an option.")
            return {'CANCELLED'}
        interact.answer_question(answer)
        wm.unix_answer = ""
        return {'FINISHED'}


class UNIX_OT_toggle_steps(Operator):
    """Show or hide the tool calls in this group"""
    bl_idname = "unix.toggle_steps"
    bl_label = "Toggle Steps"
    bl_options = {'INTERNAL'}

    message: IntProperty()
    part: IntProperty()

    def execute(self, context):
        try:
            part = engine.messages[self.message]["parts"][self.part]
            part["open"] = not part.get("open", False)
        except (IndexError, KeyError):
            pass
        return {'FINISHED'}


class UNIX_OT_restore(Operator):
    """Put the scene back to how it was before this message and rewind the chat to it.
Your project file on disk is not changed: a restored copy is opened"""
    bl_idname = "unix.restore"
    bl_label = "Restore the scene to before this message?"
    bl_options = {'INTERNAL'}

    message: IntProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        error = engine.restore(self.message)
        if error:
            self.report({'WARNING'}, error)
            return {'CANCELLED'}
        context.window_manager.unix_prompt = engine.pending_prompt
        return {'FINISHED'}


class UNIX_OT_save_back(Operator):
    """Save the restored scene over your original project file"""
    bl_idname = "unix.save_back"
    bl_label = "Overwrite the original project file with this restored version?"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        target = engine.restored_from
        if not target:
            self.report({'WARNING'}, "There is no original file path. Use File > Save As.")
            return {'CANCELLED'}
        bpy.ops.wm.save_as_mainfile(filepath=target)
        engine.restored_from = ""
        return {'FINISHED'}


class UNIX_OT_open_folder(Operator):
    """Open a Unix data folder in the file manager"""
    bl_idname = "unix.open_folder"
    bl_label = "Open Folder"

    kind: StringProperty(default="snapshots")

    def execute(self, context):
        allowed = {"snapshots", "exports", "renders", "looks", "chats", "assets"}
        path = data_dir(self.kind if self.kind in allowed else "snapshots")
        try:
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606
            else:
                subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])
        except OSError as exc:
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class UNIX_OT_apply_theme(Operator):
    """Switch Blender to the Unix charcoal and purple theme (Preferences > Themes can switch back)"""
    bl_idname = "unix.apply_theme"
    bl_label = "Apply Unix Theme"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "theme", "Unix.xml")
        if not os.path.isfile(path):
            self.report({'ERROR'}, "Theme file is missing from the add-on.")
            return {'CANCELLED'}
        bpy.ops.script.execute_preset(filepath=path, menu_idname="USERPREF_MT_interface_theme_presets")
        return {'FINISHED'}


class UNIX_OT_send_to_engine(Operator):
    """Send the selected objects straight into a game engine project (no AI involved)"""
    bl_idname = "unix.send_to_engine"
    bl_label = "Send to Engine"

    engine: StringProperty()
    project: StringProperty(options={'SKIP_SAVE'})
    drag: BoolProperty(options={'SKIP_SAVE'})

    def execute(self, context):
        from . import island
        from ..tools import engines
        from ..tools.registry import ToolError
        project = self.project
        if self.engine == "roblox" and not self.drag:
            try:
                started = engines.send_roblox(None, "", project)
            except ToolError as exc:
                if str(exc) != "no_link":
                    island.settings["notice"] = str(exc)
                    self.report({'WARNING'}, str(exc))
                    return {'CANCELLED'}
                started = None
            if started is not None:
                started.can_ask = True      # the button is the user: the upload question may always be put to them
                island.settings["notice"] = f"Sending to Roblox Studio ({started.session['place']})…"

                def drive(sender=started):      # one piece per tick: Blender stays responsive
                    try:
                        report = sender.step()
                    except ToolError as exc:
                        island.settings["notice"] = str(exc)
                        return None
                    if report is None:
                        done = sender.total - len(sender.todo)
                        island.settings["notice"] = f"Sending to Roblox Studio: {done} of {sender.total}…"
                        return 0.05
                    built = report["built"]
                    what = ", ".join(f"{count} {kind}" for kind, count in built.items() if count)
                    lines = [f"Sent {report['asset']} to Roblox Studio ({report['place']}): {what}. "
                             f"It is in front of the Studio camera."]
                    lines += [report[key] for key in ("meshes_kept", "detail", "animation", "not_transferred") if key in report]
                    if "effects_as_made" in report:
                        lines.append("The effect in Roblox is the one made here, played as a flipbook of its own frames.")
                    if "effects_not_as_made" in report:
                        lines.append(report["effects_not_as_made"].split(" Tell the user")[0] + " Send again and choose Upload "
                                     "to get the effect as it looks here.")
                    if "preview_only" in report:
                        lines.append(report["preview_only"].split(" Tell the user")[0] + " Send again and choose Upload to "
                                     "keep them in the game.")
                    lines += ["Problem: " + line for line in report.get("problems", [])[:4]]
                    engine.messages.append({"role": "assistant", "meta": f"{report['triangles']:,} triangles · {report['seconds']}s",
                                            "parts": [{"type": "text", "text": "\n".join(lines)}]})
                    engine.save_chat()
                    island.settings["notice"] = ""
                    return None

                bpy.app.timers.register(drive, first_interval=0.05)
                self.report({'INFO'}, "Sending to Roblox Studio…")
                return {'FINISHED'}
            island.settings["notice"] = engines.link_help()
            project = ""
        try:
            result = engines.send(self.engine, None, "", project)
        except ToolError as exc:
            island.settings["notice"] = str(exc)
            self.report({'WARNING'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # exporter errors
            island.settings["notice"] = f"Export failed: {exc}"
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        island.settings["notice"] = ""
        name = os.path.splitext(os.path.basename(result["file"]))[0]
        verb = "Updated" if result["updated_existing"] else "Sent"
        lines = [f"{verb} {name} in {result['engine']}: {result['in_engine']}", result["next"]]
        if self.drag or self.engine in engines.FOLDERLESS:
            lines.append("Drag tile: " + engines.show_tile(result["file"]) + ".")
        lines += ["Warning: " + w for w in result.get("warnings", [])]
        engine.messages.append({"role": "assistant", "meta": f"{result['triangles']:,} triangles · {result['bytes'] / 1e6:.1f} MB",
                                "parts": [{"type": "text", "text": "\n".join(lines)}]})
        engine.save_chat()
        self.report({'INFO'}, lines[0])
        return {'FINISHED'}


class UNIX_OT_toggle_hud(Operator):
    """Show or hide the Unix island in the viewport"""
    bl_idname = "unix.toggle_hud"
    bl_label = "Toggle Viewport Card"

    def execute(self, context):
        from . import island
        prefs = preferences(context)
        if prefs is not None:
            prefs.show_hud = not prefs.show_hud
        else:
            island.settings["visible"] = not island.settings["visible"]
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


class UNIX_OT_viewport_lock(Operator):
    """While Unix works, block edits in the 3D viewport so they cannot collide with its changes.
Navigation (orbit, pan, zoom, view keys) still works, and so do the panel and Stop"""
    bl_idname = "unix.viewport_lock"
    bl_label = "Unix Viewport Lock"
    bl_options = {'INTERNAL'}

    _BLOCKED = {'LEFTMOUSE', 'RIGHTMOUSE', 'G', 'R', 'S', 'E', 'I', 'X', 'DEL', 'TAB', 'BACK_SPACE',
                'J', 'P', 'M', 'H', 'D', 'F', 'K', 'B', 'C', 'A'}
    _timer = None
    _active = False

    def invoke(self, context, event):
        cls = type(self)
        if cls._active or context.window is None:
            return {'CANCELLED'}
        cls._active = True
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        return {'RUNNING_MODAL'}

    def _finish(self, context):
        type(self)._active = False
        if self._timer is not None:
            context.window_manager.event_timer_remove(self._timer)
            self._timer = None

    @staticmethod
    def _over_viewport(context, event) -> bool:
        x, y = event.mouse_x, event.mouse_y
        for area in context.window.screen.areas:
            if area.type != 'VIEW_3D' or not (area.x <= x < area.x + area.width and area.y <= y < area.y + area.height):
                continue
            from . import island
            for region in area.regions:  # sidebar, toolbar and header overlap the main region
                inside = region.x <= x < region.x + region.width and region.y <= y < region.y + region.height
                if inside and region.type != 'WINDOW' and region.width > 1 and region.height > 1:
                    return False
                if inside and region.type == 'WINDOW' and island.hit(region.as_pointer(), x - region.x, y - region.y):
                    return False  # the island (and its Stop button) stays clickable during a run
            return True
        return False

    def modal(self, context, event):
        if not engine.busy:
            self._finish(context)
            return {'FINISHED', 'PASS_THROUGH'}
        if event.type == 'Z' and (event.ctrl or event.oskey) and event.value == 'PRESS':
            return {'RUNNING_MODAL'}  # undo mid-run would pull the scene out from under the agent
        if event.type in self._BLOCKED and event.value in {'PRESS', 'CLICK', 'DOUBLE_CLICK', 'CLICK_DRAG'} \
                and self._over_viewport(context, event):
            return {'RUNNING_MODAL'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        self._finish(context)


classes = (
    UNIX_OT_send, UNIX_OT_continue, UNIX_OT_quick_prompt, UNIX_OT_stop, UNIX_OT_new_chat,
    UNIX_OT_open_chat, UNIX_MT_history, UNIX_OT_set_model, UNIX_MT_models, UNIX_OT_copy, UNIX_MT_copy, UNIX_OT_set_effort, UNIX_MT_effort, UNIX_OT_attach, UNIX_OT_clear_attachments, UNIX_OT_edit_rules,
    UNIX_OT_answer, UNIX_OT_toggle_steps, UNIX_OT_restore, UNIX_OT_save_back, UNIX_OT_open_folder,
    UNIX_OT_apply_theme, UNIX_OT_send_to_engine, UNIX_OT_toggle_hud, UNIX_OT_viewport_lock,
)
