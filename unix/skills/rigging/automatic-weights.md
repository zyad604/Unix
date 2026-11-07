---
name: automatic-weights
domain: rigging
when: A mesh must deform with an armature - binding with automatic weights, or repairing a bind that left vertices unweighted.
---

# Automatic weights
Every vertex of the mesh weighted to deform bones, with a fallback when bone heat fails.

## Do
- Prepare first: armature scale 1, bones inside the volume, control bones set to `use_deform = False`, no inward normals.
- Bind with the `rig_bind` tool (armature, meshes). It keeps the mesh where it is, bakes its world rotation and scale, adds the modifier, and fills from the nearest bones where bone heat fails. Parenting by script is how a mesh jumps away from correctly placed bones.
- Meshes split into touching pieces (texture seams, separate arm segments) are fine: `rig_bind` gives touching vertices identical weights. Do not merge vertices to fix a tear; that changes the model's normals and UVs.
- After any script that edits weights (smoothing, limiting, painting), run `rig_bind` with `method: 'keep'`: smoothing works per piece and reopens seams.
- The code below is the manual route, for repairs: the operator returns FINISHED even when bone heat fails, so count unweighted vertices yourself.
- Still failing, or mechanical parts: weight each leftover vertex 1.0 to its nearest bone (code). Right for rigid pieces; soft joints need a blend across two bones.
- Keep the Armature modifier first, above Subdivision, so the cage deforms and is then smoothed.
- Game export: at most 4 bones per vertex, weights summing to 1. With the mesh active: `vertex_group_limit_total(limit=4)`, then `vertex_group_normalize_all(lock_active=False)`.

## Code
```python
ob = D.objects["Cube"]                                   # mesh to skin
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if not rig.data.bones:                                   # stand-in rig; normally from build-armature
    C.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, z0, z1 in (("Lower", -0.9, 0.0), ("Upper", 0.0, 0.9)):
        eb = rig.data.edit_bones.new(name)
        eb.head, eb.tail = (0, 0, z0), (0, 0, z1)
    bpy.ops.object.mode_set(mode='OBJECT')
for o in C.selected_objects:
    o.select_set(False)
ob.select_set(True)
rig.select_set(True)
C.view_layer.objects.active = rig                        # parent_set: mesh selected, armature active
bpy.ops.object.parent_set(type='ARMATURE_AUTO')
empty = [v.index for v in ob.data.vertices if not any(g.weight > 0 for g in v.groups)]
bones = [b for b in rig.data.bones if b.use_deform]
to_mesh = ob.matrix_world.inverted() @ rig.matrix_world
def distance(p, b):                                      # point to bone segment, in mesh space
    a, c = to_mesh @ b.head_local, to_mesh @ b.tail_local
    return (p - a.lerp(c, max(0.0, min(1.0, (p - a).dot(c - a) / (c - a).length_squared)))).length
for i in empty:                                          # bone heat left these unweighted
    near = min(bones, key=lambda b: distance(ob.data.vertices[i].co, b))
    (ob.vertex_groups.get(near.name) or ob.vertex_groups.new(name=near.name)).add([i], 1.0, 'REPLACE')
ob.modifiers.move([m.type for m in ob.modifiers].index('ARMATURE'), 0)
result = {"bone_heat_failed_on": len(empty), "groups": [g.name for g in ob.vertex_groups],
          "modifiers": [m.type for m in ob.modifiers]}
```

## Avoid
- Trusting the operator's return value: a failed bind looks fine until the first pose, when unweighted parts stay behind.
- `vertex_group_normalize_all()` with defaults: the active group stays locked and sums stay off 1.0.
- Leaving `use_deform` on control bones (`Foot_IK`): they get vertex groups and take weight from the real joints.

Check: `check_rig` returns `issues: []` (bones inside the mesh, 0 unweighted vertices, no seam opens, no bleed), and `look` at the rig shows the orange bones inside the model.
