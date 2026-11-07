# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Engine tests without model calls: prompt assembly, chat archive, restore, step labels.
Run: blender -b --factory-startup --python tests/test_engine.py"""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix import core
from unix.core import engine, _friendly_result, _step_detail
from unix.tools import files, script, _util

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")  # keep test data out of the user's config
engine.start()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:500]}"))
    if not cond:
        fails.append(label)


# -- prompt assembly ---------------------------------------------------------
cube = bpy.data.objects["Cube"]
bpy.context.view_layer.objects.active = cube
cube.select_set(True)
tmp = tempfile.mkdtemp()
ref, model = os.path.join(tmp, "ref.png"), os.path.join(tmp, "chair.glb")
open(ref, "wb").close(); open(model, "wb").close()
prompt = engine._compose("make it taller", [ref, model])
check("context: selection", "active: Cube" in prompt and "selected (1): Cube" in prompt, prompt)
check("context: attachments", "view_image" in prompt and "import_file" in prompt and ref in prompt, prompt)
prompt = engine._compose("a red chair", [], "[Mode: 3D] Build this as a 3D object.")
check("mode instruction leads the prompt", prompt.startswith("[Mode: 3D]") and "a red chair" in prompt, prompt)
engine._scene_after_turn, engine._scene_file = script._snapshot(), bpy.data.filepath
cube.location.x = 2.0
bpy.data.objects.remove(bpy.data.objects["Light"])
bpy.context.view_layer.update()
prompt = engine._compose("and now?", [])
check("context: user changes since last turn", "the user changed since your last turn" in prompt
      and "modified Cube" in prompt and "deleted Light" in prompt, prompt)
paths = engine._write_session_files("- Low-poly only")
system = open(paths["system"], encoding="utf-8").read()
check("system prompt: rules + skills + version", "- Low-poly only" in system and "{{" not in system
      and bpy.app.version_string in system, system[-400:])
mcp = json.load(open(paths["mcp"]))
check("mcp config has token", mcp["mcpServers"]["unix"]["headers"]["Authorization"].startswith("Bearer "))

# -- friendly step text ------------------------------------------------------
check("friendly run_python", _friendly_result("run_python", json.dumps({"ok": True, "changes": {"created": ["a", "b"], "modified": ["c"]}}), False) == "2 created · 1 modified")
check("friendly script error", _friendly_result("run_python", json.dumps({"ok": False, "error": "x"}), False) == "script error")
check("friendly check", _friendly_result("check_mesh", json.dumps({"checked": 3, "with_issues": 0}), False) == "3 checked, all clean")
check("friendly look with image suffix", _friendly_result("look", json.dumps({"views": [1, 2]}) + " [+2 images]", False) == "2 views")
check("friendly failure", _friendly_result("look", "boom", True) == "failed")
check("step detail", _step_detail("import_file", {"path": r"C:\a\b\chair.glb"}) == "chair.glb"
      and _step_detail("skill_load", {"names": ["booleans", "clean-topology"]}) == "booleans, clean-topology")

# -- copy to clipboard ---------------------------------------------------------
engine.messages = [
    {"role": "user", "text": "rig the hands", "attachments": [], "snapshot": "", "mode": "agent"},
    {"role": "assistant", "meta": "59s", "parts": [
        {"type": "steps", "open": False, "steps": [
            {"name": "run_python", "label": "Ran a script", "detail": "Build bones", "acted": True, "status": "ok",
             "summary": '{"ok":true}', "result": "1 created", "images": [],
             "input": {"code": "arm = D.armatures.new('Rig')\nresult = 1", "purpose": "Build bones"}},
            {"name": "rig_bind", "label": "Bound the mesh to the rig", "detail": "Rig", "acted": True, "status": "error",
             "summary": "boom", "result": "failed", "images": [], "input": {"armature": "Rig", "meshes": ["Hands"]}}]},
        {"type": "text", "text": "I rigged both hands."}]}]
check("copy reply: just the answer", engine.transcript("reply") == "I rigged both hands.", engine.transcript("reply"))
chat = engine.transcript("chat")
check("copy chat: both sides and step labels, no scripts", "You:\nrig the hands" in chat and "1. Ran a script: Build bones  ->  1 created" in chat
      and "D.armatures" not in chat, chat)
work = engine.transcript("work")
check("copy work: scripts, arguments and results", "arm = D.armatures.new('Rig')" in work and '"meshes": ["Hands"]' in work
      and "result: boom" in work and "2. Bound the mesh to the rig: Rig  ->  failed" in work, work)
long_args = core._trim_input({"code": "x" * 30000, "n": 1})
check("stored tool input is capped", len(long_args["code"]) < 20200 and long_args["n"] == 1)

# -- chat archive ------------------------------------------------------------
engine.messages = [{"role": "user", "text": "build a table\nwith four legs", "attachments": [], "snapshot": ""},
                   {"role": "assistant", "meta": "5s", "parts": [{"type": "text", "text": "Done."},
                    {"type": "steps", "open": False, "steps": [{"name": "look", "label": "Looked", "detail": "", "acted": False,
                                                                 "status": "running", "summary": "", "result": "", "images": []}]}]}]
engine.session_id = "sess-1"
first_id = engine.chat_id
engine.save_chat()
engine.new_chat()
check("new chat is empty with a new id", engine.messages == [] and engine.chat_id != first_id and engine.session_id is None)
chats = engine.list_chats()
check("archive lists the chat with a title", len(chats) == 1 and chats[0]["title"] == "build a table" and chats[0]["turns"] == 1, chats)
check("open chat restores it", engine.open_chat(first_id) is None and engine.session_id == "sess-1" and len(engine.messages) == 2)
check("stale running step is closed", engine.messages[1]["parts"][1]["steps"][0]["status"] == "error")
check("open missing chat is an error", engine.open_chat("nope") is not None)

# -- restore -----------------------------------------------------------------
project = os.path.join(tmp, "project.blend")
bpy.ops.wm.save_as_mainfile(filepath=project)
snap = files.snapshot("before_turn")
engine.messages = [{"role": "user", "text": "delete everything", "attachments": [], "snapshot": snap},
                   {"role": "assistant", "meta": "", "parts": [{"type": "text", "text": "Deleted."}]},
                   {"role": "user", "text": "thanks", "attachments": [], "snapshot": ""},
                   {"role": "assistant", "meta": "", "parts": []}]
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
size_before = os.path.getsize(project)
check("restore without snapshot is refused", engine.restore(2) is not None)
error = engine.restore(0)
check("restore brings the scene back", error is None and "Cube" in bpy.data.objects, error)
check("restore rewinds the chat", len(engine.messages) == 1 and engine.messages[0]["role"] == "assistant"
      and engine.pending_prompt == "delete everything" and engine.session_id is None, engine.messages)
check("restore opens a copy, not the snapshot or the project", bpy.data.filepath not in (snap, project)
      and "restored" in bpy.data.filepath and os.path.isfile(snap), bpy.data.filepath)
check("project file on disk untouched", os.path.getsize(project) == size_before and engine.restored_from == project)

engine.shutdown()
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
