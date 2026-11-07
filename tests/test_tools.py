# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tool tests. Run: blender -b --factory-startup --python tests/test_tools.py"""
import json, os, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import tempfile as _tempfile
from unix.tools import _util as _base
_base._base_dir = _tempfile.mkdtemp(prefix="unix_test_")   # snapshots, looks and assets stay out of the app's own data
from unix import tools
from unix.tools import registry, interact
tools.load_all()
interact.interactive["enabled"] = False
fails = []

def call(tool_name, **args):
    res = registry.call(tool_name, args)
    text = res["content"][0]["text"]
    imgs = sum(1 for c in res["content"] if c["type"] == "image")
    return res["isError"], text, imgs

def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {detail[:400]}"))
    if not cond: fails.append(label)

print("TOOLS", registry.names())
err, text, _ = call("scene_info"); d = json.loads(text)
check("scene_info", not err and d["counts"].get("MESH") == 1 and d["objects"][0]["name"], text)
err, text, _ = call("object_info", name="Cube"); check("object_info", not err and json.loads(text)["mesh"]["verts"] == 8, text)
err, text, _ = call("object_info", name="cub"); check("object_info suggests", err and "Did you mean" in text, text)

code = '''
import bmesh
me = bpy.data.meshes.new("TableTop"); ob = bpy.data.objects.new("TableTop", me)
bpy.context.scene.collection.objects.link(ob)
bm = bmesh.new(); bmesh.ops.create_cube(bm, size=1.0); bm.to_mesh(me); bm.free()
ob.scale = (1.2, 0.7, 0.04); ob.location = (0, 0, 0.75)
for o in bpy.data.objects:
    if o.name == "Cube": bpy.data.objects.remove(o)
print("made top"); result = {"dims": ob.dimensions}
'''
err, text, _ = call("run_python", code=code, purpose="table top"); d = json.loads(text)
check("run_python ok", not err and d["ok"] and d["changes"].get("created") == ["TableTop"] and d["changes"].get("deleted") == ["Cube"] and "made top" in d["stdout"], text)
err, text, _ = call("run_python", code="x = 1\nbpy.data.objects['Nope'].location.x = 2"); d = json.loads(text)
check("run_python traceback", (not d["ok"]) and "line 2" in d["error"] and "KeyError" in d["error"], text)
err, text, _ = call("run_python", code="import subprocess\nsubprocess.run(['calc'])"); check("guard import", err and "not allowed" in text, text)
err, text, _ = call("run_python", code="import os\nos.remove('x')"); check("guard call", err and "os.remove" in text, text)
err, text, _ = call("run_python", code="bpy.ops.wm.read_factory_settings()"); check("guard wm op", err, text)
err, text, _ = call("run_python", code="def f(:\n  pass"); check("syntax error", err and "SyntaxError" in text, text)
err, text, _ = call("run_python", code="while True:\n    pass", timeout_s=5); d = json.loads(text) if not err else {}
check("deadline stops loop", d.get("ok") is False and "ScriptTimeout" in d.get("error", ""), text)
err, text, _ = call("run_python", code="result = [o.name for o in bpy.data.objects]"); check("alive after timeout", json.loads(text)["ok"], text)
err, text, _ = call("run_python", code="x", bogus=1); check("arg validation", err and "unknown argument" in text, text)

err, text, _ = call("check_mesh"); d = json.loads(text)
top = next(o for o in d["objects"] if o["object"] == "TableTop")
check("check_mesh", top["watertight"] and top["quads"] == 6 and any("unapplied scale" in i for i in top["issues"]), text)
err, text, _ = call("check_uv", texture_size=2048); check("check_uv", not err and "objects" in json.loads(text), text)
err, text, _ = call("set_material", name="Oak", objects=["TableTop"], base_color=[0.35, 0.2, 0.08], roughness=0.6)
check("set_material", not err and json.loads(text)["assigned_to"] == ["TableTop"], text)
err, text, _ = call("check_materials"); check("check_materials", not err and json.loads(text)["issues"] == [], text)
err, text, _ = call("check_scene"); check("check_scene", not err and "issues" in json.loads(text), text)

err, text, imgs = call("look", views=["persp", "front", "top"]); check("look solid x3", not err and imgs == 3, text)
err, text, imgs = call("look", shading="rendered", use_scene_camera=True, size=512); check("look rendered camera", not err and imgs == 1, text)
cam_before = bpy.context.scene.camera.name; eng = bpy.context.scene.render.engine
check("look restores state", not any(o.name.startswith("_unix") for o in bpy.data.objects) and bpy.context.scene.render.resolution_x == 1920, f"{eng} {bpy.context.scene.render.resolution_x}")

out = os.path.join(bpy.app.tempdir, "t.glb")
err, text, _ = call("export_file", path=out, objects=["TableTop"]); d = json.loads(text) if not err else {}
check("export glb", not err and d["file_contains"]["meshes"] == 1 and d["file_contains"]["materials"] == 1, text)
err, text, _ = call("import_file", path=out); check("import glb", not err and json.loads(text)["count"] >= 1, text)
err, text, _ = call("save_snapshot", label="test"); check("save_snapshot", not err and os.path.exists(json.loads(text)["snapshot"]) and bpy.data.filepath == "", text)

err, text, _ = call("skill_list"); d = json.loads(text); check("skill_list", not err and len(d["skills"]) > 50, text[:200])
err, text, _ = call("skill_load", names=[d["skills"][0]["name"], "nope-skill"]); check("skill_load", not err and "# SKILL:" in text and "not found" in text, text[:200])
err, text, _ = call("api_lookup", query="ops.mesh.bevel"); check("api_lookup op", not err and "offset" in text, text[:300])
err, text, _ = call("api_lookup", query="BevelModifier"); check("api_lookup type", not err and "segments" in text, text[:300])
err, text, _ = call("api_lookup", query="solidify"); check("api_lookup search", not err and "Solidify" in text, text[:300])
err, text, _ = call("update_plan", steps=[{"title": "a", "status": "done"}, {"title": "b", "status": "pending"}]); check("update_plan", not err and interact.get_plan()[0]["status"] == "done", text)
err, text, _ = call("ask_user", question="?"); check("ask_user headless", err and "No user" in text, text)
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
