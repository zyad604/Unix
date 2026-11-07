---
name: build-armature
domain: rigging
when: You are building a skeleton by script from known proportions - a new character you modelled, a mechanism, a prop. For an existing or imported model load measured-skeleton instead.
---

# Build an armature
A named bone hierarchy with joints at the mesh's real pivots, ready for weights and constraints.

## Do
- An existing model's limbs, fingers, tail or wings: do not guess from coordinates. `find_limbs` measures them and `rig_chain` builds on the measurement (skill `measured-skeleton`). Use this skill for what you place by design.
- Positions are WORLD space (`obj.matrix_world @ v.co`). `transform_warning` in `object_info` means local coordinates are not world coordinates.
- Place heads at real pivots (hip, knee, ankle; shoulder, elbow, wrist; each finger joint).
- Keep the armature object at the world origin, rotation 0, scale 1, so bone coordinates equal world coordinates.
- One `Root` bone on the floor at the origin; every other bone descends from it. A humanoid at 1.75 m: hips 0.95 m, knee 0.50 m, ankle 0.08 m, shoulder 1.45 m.
- Pre-bend hinge joints 1-3 cm toward the way they fold (knee forward, elbow back). IK needs it to pick a direction.
- `use_connect = True` only when the child's head sits on the parent's tail (chains). Otherwise keep the offset with `use_connect = False`.
- Name sides `.L` / `.R` and build the right side by negating X. Set `use_deform = False` on Root and control bones. One roll per chain: `eb.align_roll((0, -1, 0))`.
- Edit bones exist only in Edit Mode: make the armature active, `mode_set(mode='EDIT')`, build, return to Object Mode. Do not keep `EditBone` references afterwards.

## Code
```python
spec = [  # name, head, tail, parent, connected
    ("Root",    (0, 0, 0),          (0, 0.2, 0),        None,      False),
    ("Hips",    (0, 0, 0.95),       (0, 0, 1.10),       "Root",    False),
    ("Spine",   (0, 0, 1.10),       (0, 0, 1.40),       "Hips",    True),
    ("Thigh.L", (0.10, 0, 0.95),    (0.10, -0.02, 0.50), "Hips",   False),
    ("Shin.L",  (0.10, -0.02, 0.50), (0.10, 0, 0.08),   "Thigh.L", True),
]
spec += [(n[:-1] + "R", (-h[0], h[1], h[2]), (-t[0], t[1], t[2]), p[:-1] + "R" if p.endswith(".L") else p, c)
         for n, h, t, p, c in spec if n.endswith(".L")]            # mirror the left side across X
arm = D.armatures.get("Rig") or D.armatures.new("Rig")
rig = D.objects.get("Rig") or D.objects.new("Rig", arm)
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if C.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
C.view_layer.objects.active = rig                 # mode_set acts on the active object
bpy.ops.object.mode_set(mode='EDIT')
for name, head, tail, parent, connect in spec:
    eb = arm.edit_bones.get(name) or arm.edit_bones.new(name)
    eb.head, eb.tail = head, tail
    eb.parent = arm.edit_bones[parent] if parent else None
    eb.use_connect = connect
    eb.align_roll((0, -1, 0))
bpy.ops.object.mode_set(mode='OBJECT')
arm.bones["Root"].use_deform = False
result = {b.name: [b.parent.name if b.parent else None, round(b.length, 3), b.use_connect]
          for b in arm.bones}
```

## Avoid
- A bone with head equal to tail: Blender deletes zero-length bones on leaving Edit Mode, silently.
- `arm.edit_bones.new()` in Object Mode: RuntimeError "not in edit mode, cannot add an editbone".
- `use_connect = True` on an offset child: its head snaps to the parent's tail and the joint moves.
- Skipping the visual check: `look` on the rig draws solid orange bones inside a see-through mesh. A skeleton beside the model, or at a right angle to it, means local coordinates were used as world coordinates.

Check: `look` at the rig from two views shows every orange bone running inside its part of the mesh; `result` shows the intended parent and a length above 0.01 m for each bone.
