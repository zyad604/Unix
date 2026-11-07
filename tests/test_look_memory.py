# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""A rendered look that would not fit in the computer's memory is shown solid with textures instead (5 Nov: rendered
looks of a 3,000-object scene took the last memory and Windows went down). Run: blender -b --factory-startup --python
tests/test_look_memory.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_lookmem_")
from unix.tools import registry, vision

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:400]}"))
    if not cond:
        fails.append(label)


left = vision.memory_left_gb()
check("free memory is measured on Windows", os.name != "nt" or (left is not None and left > 0), left)
bpy.ops.mesh.primitive_cube_add()
bpy.context.object.data.materials.append(bpy.data.materials.new("Red"))
check("a small scene needs little", vision._rendered_need_gb([bpy.context.object])[0] < 2.0)

res = registry.call("look", {"shading": "rendered", "size": 256})
data = res["content"][0]["text"]
check("a small rendered look renders", not res["isError"] and '"shading": "rendered"' in data.replace('":"', '": "'), data[:300])

vision.memory_left_gb = lambda: 3.0        # a computer near the end of its memory
res = registry.call("look", {"shading": "rendered", "size": 256})
data = res["content"][0]["text"]
check("with no room it is solid and says why", not res["isError"] and "rendered_skipped" in data and "solid" in data, data[:400])
check("...and the scene's engine and shading are put back", bpy.context.scene.display.shading.color_type != 'TEXTURE',
      bpy.context.scene.display.shading.color_type)
real_need = vision._rendered_need_gb
vision._rendered_need_gb = lambda objs: (17.0, 10.0)        # the 5 Nov scene: 10 GB of it textures
vision.memory_left_gb = lambda: 18.0
res = registry.call("look", {"shading": "rendered", "size": 256})
data = res["content"][0]["text"]
check("textures that would not fit either: plain colours", not res["isError"] and "plain colours" in data, data[:400])
vision._rendered_need_gb = lambda objs: (17.0, 1.0)
res = registry.call("look", {"shading": "rendered", "size": 256})
check("textures that fit: solid with textures", "with textures" in res["content"][0]["text"], res["content"][0]["text"][:300])
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
