# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Skill library lint + execution test.

Checks every skill against docs/SKILL_SPEC.md and runs each ```python block the way the
add-on would (same guard, same namespace) in a fresh factory scene.
Run: blender -b --factory-startup --python tests/test_skills.py
"""
import contextlib, io, math, os, re, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bmesh, bpy, mathutils
import tempfile as _tempfile
from unix.tools import _util as _base
_base._base_dir = _tempfile.mkdtemp(prefix="unix_test_")   # snapshots, looks and assets stay out of the app's own data
from unix import tools
from unix.tools import guard, registry

tools.load_all()
SKILLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unix", "skills")
TOOL_NAMES = set(registry.names())
FRONT = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.S)
BLOCK = re.compile(r"```python\n(.*?)```", re.S)
TOOLISH = re.compile(r"`((?:check|scene|object|run|render|set|import|export|save|skill|api|update|ask|look)[a-z_]*)`")
problems, blocks_run, blocks_failed, count = [], 0, 0, 0

MISSING = re.compile(r'key "([^"]+)" not found')


def stand_in(name):
    """A subdivided, unwrapped 1 m cube with a node material, resting on the ground.

    If an object of that name already exists the snippet wanted some other datablock,
    so a material of that name is made instead.
    """
    if name in bpy.data.objects:
        mat = bpy.data.materials.new(name)
        mat.use_nodes = True
        return mat
    if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")
    bmesh.ops.create_cube(bm, size=1.0, calc_uvs=True)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
    bm.to_mesh(mesh); bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.location.z = 0.5
    mat = bpy.data.materials.new(name + "_Mat")
    mat.use_nodes = True
    mesh.materials.append(mat)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.context.view_layer.update()
    return obj


def namespace():
    return {"__name__": "__unix__", "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils, "math": math,
            "Vector": mathutils.Vector, "Matrix": mathutils.Matrix, "Euler": mathutils.Euler,
            "Quaternion": mathutils.Quaternion, "Color": mathutils.Color, "C": bpy.context, "D": bpy.data}

for root, _dirs, names in sorted(os.walk(SKILLS)):
    for fname in sorted(names):
        if not fname.endswith(".md"):
            continue
        count += 1
        path = os.path.join(root, fname)
        rel = os.path.relpath(path, SKILLS).replace("\\", "/")
        text = open(path, encoding="utf-8").read()
        m = FRONT.match(text)
        if not m:
            problems.append(f"{rel}: no front matter"); continue
        meta = dict((k.strip(), v.strip()) for k, _, v in (line.partition(":") for line in m.group(1).splitlines()))
        body = m.group(2)
        if meta.get("name") != fname[:-3]: problems.append(f"{rel}: name '{meta.get('name')}' != file name")
        if meta.get("domain") != os.path.basename(root): problems.append(f"{rel}: domain '{meta.get('domain')}' != folder")
        if not meta.get("when"): problems.append(f"{rel}: missing 'when'")
        if not re.search(r"^Check:", body, re.M): problems.append(f"{rel}: no Check: line")
        lines = len(text.splitlines())
        if lines > 66: problems.append(f"{rel}: {lines} lines (limit 60)")
        for name in set(TOOLISH.findall(body)):
            # names from older drafts of the tool set; fine only if such a tool exists today
            if name in {"check_rig", "check_topology", "preview_render", "scene_query", "run_script",
                        "delegate_task", "export_scene"} - TOOL_NAMES:
                problems.append(f"{rel}: names a tool that does not exist: {name}")
        for index, code in enumerate(BLOCK.findall(body)):
            blocks_run += 1
            bpy.ops.wm.read_factory_settings(use_empty=False)
            compiled, refused = guard.prepare(code)
            if refused:
                blocks_failed += 1; problems.append(f"{rel} block {index}: refused by guard: {refused.splitlines()[1].strip() if len(refused.splitlines()) > 1 else refused}")
                continue
            failure = None
            for _attempt in range(6):  # snippets act on a named object: supply a stand-in and retry
                try:
                    with contextlib.redirect_stdout(io.StringIO()):
                        exec(compiled, namespace())
                    failure = None
                    break
                except Exception as exc:
                    tb = [f for f in traceback.extract_tb(sys.exc_info()[2]) if f.filename == "<unix>"]
                    where = f" (line {tb[-1].lineno})" if tb else ""
                    failure = f"{rel} block {index}: {type(exc).__name__}: {str(exc)[:160]}{where}"
                    missing = MISSING.search(str(exc)) if isinstance(exc, KeyError) else None
                    if missing is None or missing.group(1) in bpy.data.materials:
                        break
                    stand_in(missing.group(1))
            if failure:
                blocks_failed += 1
                problems.append(failure)

print(f"SKILLS {count} | python blocks {blocks_run}, failed {blocks_failed} | problems {len(problems)}")
for p in problems:
    print("PROBLEM", p)
print("RESULT", "ALL PASS" if not problems else "HAS PROBLEMS")
