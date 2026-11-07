---
name: rigid-attachments
domain: rigging
when: A hard object must follow one bone without deforming - a weapon in a hand, helmet, armour plate, wheel, tool or any machine part.
---

# Rigid attachments
Hard parts that follow one bone exactly, stay where they were placed and keep their shape.

## Do
- Rigid means one bone and no deformation: bone-parent the object. No Armature modifier, no vertex groups.
- Place the part first, with the rig in its rest pose or the grip pose. Save `matrix_world` before parenting and assign it back after, so the part does not jump.
- Bone-parent space sits at the bone's tail, not its head, with Y along the bone.
- Data API: `parent = rig`, `parent_type = 'BONE'`, `parent_bone = name`, clear `matrix_parent_inverse`, restore `matrix_world`. The part's location is then its offset from the bone tail.
- Many parts: one dict of part name to bone name, one loop.
- Part that changes owner during a shot (sword from hip to hand): no bone parent. One `CHILD_OF` constraint per owner with `target = rig`, `subtarget = bone`, `inverse_matrix = (rig.matrix_world @ pose_bone.matrix).inverted()`. Key `influence` 1 and 0 on adjacent frames; only one constraint at 1.
- Engine needs one skinned mesh instead: join the part to the body and weight all its vertices 1.0 to that bone.

## Code
```python
attach = {"Cube": "Hand.R"}                              # part object -> bone
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if not rig.data.bones:                                   # stand-in rig; normally from build-armature
    C.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    eb = rig.data.edit_bones.new("Hand.R")
    eb.head, eb.tail = (-0.6, 0, 1.4), (-0.7, 0, 1.4)
    bpy.ops.object.mode_set(mode='OBJECT')
C.view_layer.update()
result = {}
for part_name, bone_name in attach.items():
    part = D.objects[part_name]
    if bone_name not in rig.data.bones:
        raise KeyError(f"no bone {bone_name} in {rig.name}")
    world = part.matrix_world.copy()
    for mod in [m for m in part.modifiers if m.type == 'ARMATURE']:
        part.modifiers.remove(mod)                       # rigid parts must not be skinned
    part.parent, part.parent_type, part.parent_bone = rig, 'BONE', bone_name
    part.matrix_parent_inverse = Matrix.Identity(4)
    C.view_layer.update()                                # parent matrix must be current before restoring
    part.matrix_world = world
    C.view_layer.update()
    result[part_name] = {"bone": bone_name, "offset_from_bone_tail": [round(v, 4) for v in part.location],
                         "moved_m": round((part.matrix_world.translation - world.translation).length, 6)}
```

## Avoid
- Parenting without restoring `matrix_world`: the part's world coordinates are re-read in bone space and it jumps (1.45 m in a test).
- Skinning a rigid part (Armature modifier, blended weights): the hard surface bends at joints.
- A misspelled `parent_bone`: no Python error; the part follows the armature object, not the bone.

Check: `result` shows `moved_m` 0 for every part; after rotating the bone 60 degrees in `run_python`, `look` shows the part still seated in place relative to the body, no gap; reset the pose.
