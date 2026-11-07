# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""export_file with chosen objects: they export from a scene of their own (fast in a big world) and a glTF
takes only the actions made for them, not those of another rig with the same bones. No engine needed.
Run: blender -b --factory-startup --python tests/test_export_alone.py"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_export_")  # keep test data out of the user's config
from unix import tools
tools.load_all()
from unix.tools import files, registry

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 40


def creature(name, x):
    """A rig of two bones with two actions keyed on it, and a mesh skinned to it."""
    bpy.ops.object.armature_add(location=(x, 0, 0))
    rig = bpy.context.active_object
    rig.name = f"CR_{name}_Rig"
    bpy.ops.object.mode_set(mode='EDIT')
    tail = rig.data.edit_bones.new("Head")
    tail.head, tail.tail, tail.parent = (0, 0, 1), (0, 0, 1.5), rig.data.edit_bones["Bone"]
    bpy.ops.object.mode_set(mode='OBJECT')
    rig.animation_data_create()
    for clip, swing in (("Idle", 0.2), ("Run", 1.0)):
        action = bpy.data.actions.new(f"{name}_{clip}")
        action.use_fake_user = True
        rig.animation_data.action = action
        for frame, angle in ((1, 0.0), (20, swing), (40, 0.0)):
            bone = rig.pose.bones["Head"]
            bone.rotation_mode = 'XYZ'
            bone.rotation_euler = (angle, 0, 0)
            bone.keyframe_insert("rotation_euler", frame=frame)
    rig.animation_data.action = bpy.data.actions[f"{name}_Idle"]
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(x, 0, 1.2))
    body = bpy.context.active_object
    body.name = f"CR_{name}"
    group = body.vertex_groups.new(name="Head")
    group.add(range(len(body.data.vertices)), 1.0, 'REPLACE')
    body.modifiers.new("Armature", 'ARMATURE').object = rig
    return rig, body


rig_a, body_a = creature("Fluffalo", 0)
rig_b, body_b = creature("Clucker", 5)
slots = {a.name: [s.identifier for s in a.slots] for a in bpy.data.actions}
check("each creature's actions have a slot named for its rig", slots.get("Fluffalo_Run") == ["OBCR_Fluffalo_Rig"]
      and slots.get("Clucker_Idle") == ["OBCR_Clucker_Rig"], slots)

from unix.tools import motion
check("a rig's actions are its own: another rig's with the same bones are not (sending sampled 13 x 130)",
      sorted(motion.actions_for(rig_a)) == ["Fluffalo_Idle", "Fluffalo_Run"], motion.actions_for(rig_a))

# a big world around them: what made one creature's export take minutes
bpy.ops.mesh.primitive_cube_add(size=1)
seed = bpy.context.active_object
for i in range(1500):
    copy = seed.copy()
    copy.location = (i % 40 * 3.0, i // 40 * 3.0 + 20.0, 0)
    copy.animation_data_create()
    scene.collection.objects.link(copy)
before_scenes = len(bpy.data.scenes)
for o in bpy.context.view_layer.objects:
    o.select_set(False)
seed.select_set(True)
bpy.context.view_layer.objects.active = seed

began = time.monotonic()
result = registry.call("export_file", {"path": "Creatures/CR_Fluffalo.glb", "objects": ["CR_Fluffalo", "CR_Fluffalo_Rig"]})
seconds = time.monotonic() - began
check("the export works", not result["isError"], result["content"][0]["text"][:400])
import json
summary = json.loads(result["content"][0]["text"]) if not result["isError"] else {}
contains = summary.get("file_contains") or {}
check("the file has this creature's two actions only, not the other rig's", contains.get("animations") == 2, contains)
check("and its own mesh and skin", contains.get("meshes") == 1 and contains.get("skins") == 1, contains)
check(f"quickly, in a 1,500-object world ({seconds:.1f} s)", seconds < 20.0, seconds)
check("the scene of its own is gone afterwards", len(bpy.data.scenes) == before_scenes and "_unix_export" not in bpy.data.scenes,
      [s.name for s in bpy.data.scenes])
check("the user's scene, selection and active object are as they were", bpy.context.scene == scene and seed.select_get()
      and not rig_a.select_get() and bpy.context.view_layer.objects.active == seed)
check("the exporter's action filter is put back (none was set)", not hasattr(bpy.types.Scene, "gltf_action_filter"))
check("the rig keeps its action", rig_a.animation_data.action.name == "Fluffalo_Idle")

hidden = bpy.data.collections.new("Creature Clucker")         # a creature kept out of the scene, as a kit
for obj in (rig_b, body_b):
    for owner in list(obj.users_collection):
        owner.objects.unlink(obj)
    hidden.objects.link(obj)
result = registry.call("export_file", {"path": "Creatures/CR_Clucker.glb", "objects": ["CR_Clucker", "CR_Clucker_Rig"]})
kit = json.loads(result["content"][0]["text"]) if not result["isError"] else {}
check("objects in no scene at all export too, with their own actions", not result["isError"]
      and (kit.get("file_contains") or {}).get("animations") == 2, result["content"][0]["text"][:300])
check("and the user's scene is still the one shown", bpy.context.scene == scene and len(bpy.data.scenes) == before_scenes)

result = registry.call("export_file", {"path": "Everything.glb"})
whole = json.loads(result["content"][0]["text"]) if not result["isError"] else {}
check("exporting everything still works and takes every action", not result["isError"]
      and (whole.get("file_contains") or {}).get("animations", 0) >= 2, result["content"][0]["text"][:300])

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
