---
name: ik-and-constraints
domain: rigging
when: A limb should be posed by moving its end (foot, hand, claw, piston), or a bone must aim at, copy or be limited by something.
---

# IK and bone constraints
A two-bone limb driven by a target and a pole, solving without twisting the rest pose.

## Do
- IK needs a pre-bent joint: 1-3 cm toward the fold in the rest pose. A straight chain has no bend direction.
- Controls are extra bones in the same armature: `Foot_IK` at the ankle, `Knee_Pole` 0.3-0.6 m in front of the knee (behind an elbow). Parent them to `Root`, never to the limb, with `use_deform = False`.
- Put the IK constraint on the last limb bone (shin, forearm). `chain_count = 2` for a limb; 0 runs the whole chain to the root and drags the hips.
- Target and pole are the armature object plus a `subtarget` bone name.
- Pole angle depends on bone roll. Try -90, 0, 90 and 180 degrees and keep the one that leaves the thigh at its rest matrix.
- Hinge joints: `lock_ik_y = lock_ik_z = True` on the shin or forearm, when local X is the bend axis.
- Other jobs: `COPY_ROTATION` (foot follows its control), `DAMPED_TRACK` (aim one axis: eyes, pistons, turrets), `LIMIT_ROTATION` with `owner_space = 'LOCAL'` (real joint range). The stack runs top to bottom.
- Constraints go on `rig.pose.bones[...]` in Object Mode. Only creating bones needs Edit Mode.

## Code
```python
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
arm = rig.data
spec = [  # name, head, tail, parent, deform   (only missing bones are created)
    ("Root",        (0, 0, 0),             (0, 0.2, 0),            None,      False),
    ("Thigh.L",     (0.1, 0, 0.95),        (0.1, -0.02, 0.50),     "Root",    True),
    ("Shin.L",      (0.1, -0.02, 0.50),    (0.1, 0, 0.08),         "Thigh.L", True),
    ("Foot_IK.L",   (0.1, 0, 0.08),        (0.1, -0.15, 0.08),     "Root",    False),
    ("Knee_Pole.L", (0.1, -0.5, 0.50),     (0.1, -0.6, 0.50),      "Root",    False)]
C.view_layer.objects.active = rig                  # mode_set acts on the active object
bpy.ops.object.mode_set(mode='EDIT')
for name, head, tail, parent, deform in spec:
    if name not in arm.edit_bones:
        eb = arm.edit_bones.new(name)
        eb.head, eb.tail, eb.use_deform = head, tail, deform
        eb.parent = arm.edit_bones[parent] if parent else None
bpy.ops.object.mode_set(mode='OBJECT')
shin = rig.pose.bones["Shin.L"]
ik = shin.constraints.get("IK") or shin.constraints.new('IK')
ik.target, ik.subtarget, ik.chain_count = rig, "Foot_IK.L", 2
ik.pole_target, ik.pole_subtarget = rig, "Knee_Pole.L"
rest, errors = arm.bones["Thigh.L"].matrix_local, {}
for deg in (-90, 0, 90, 180):                      # the right angle leaves the rest pose untouched
    ik.pole_angle = math.radians(deg)
    posed = rig.evaluated_get(C.evaluated_depsgraph_get()).pose.bones["Thigh.L"].matrix
    errors[deg] = round(sum(row.length for row in (posed - rest)), 5)
ik.pole_angle = math.radians(min(errors, key=errors.get))
shin.lock_ik_y = shin.lock_ik_z = True             # the knee is a hinge
result = {"pole_angle_deg": min(errors, key=errors.get), "rest_error_by_angle": errors}
```

## Avoid
- A perfectly straight chain: the solver does not bend it and the foot stays put when the target moves closer.
- A pole angle left untested: the whole leg twists 90 degrees the moment the pole is assigned.
- IK target parented to the limb it controls: the target moves with the chain and the foot never lands on it.

Check: `result` shows one angle with error under 0.001; in `run_python`, moving `Foot_IK` 0.2 m up puts the evaluated shin tail within 1 mm of it and the knee moves toward the pole. Reset the pose after.
