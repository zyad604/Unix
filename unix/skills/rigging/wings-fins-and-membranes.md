---
name: wings-fins-and-membranes
domain: rigging
when: A wing, fin, frill, webbed foot, ear, cape or other thin sheet stretched between spars must fold and flap.
---

# Wings, fins and membranes
Spar bones from the wing's wrist to each tip, and a membrane that stretches between them when the wing folds.

## Do
- `find_limbs` (whole creature: `root` at the pelvis, `smallest: 0.01`). A wing shows up as a `fan`: its `limbs` are the spar tips, `origin` is where the spars spread from (the wing's wrist), `stem` is the wing arm.
- Wing arm: `rig_chain` on the stem with `at` from `narrow_points`. No stem (a flat wing with no thick arm): `points` from shoulder to elbow to the fan's `origin` with `centre: true`. In a sheet, `centre` finds the middle of the thickness and does not slide sideways.
- Each spar: `{limb: id, start: 'origin', names: [...], parent: <last arm bone>}`. Two bones for a short spar, three or four for a long one, so it can bow.
- Feathered wing with no separate tips: three chains by `points` (arm, hand, primaries) with `centre: true`.
- Bat or dragon wing membrane: `rig_bind` method `auto`. Skin between two spars shares both bones; that is correct and `check_rig` does not count it as bleed.
- Bones of one fan share a roll: local Z swings a spar within the wing's plane (folding), local X lifts it out of the plane (flapping, cupping).
- Test the fold (code): spars rotate on Z toward the arm by growing angles. `look` from above the wing, then reset.
- Membrane spikes when folded: more spar bones, or add a Corrective Smooth modifier after the Armature (factor 0.5, 10 repeats).

## Code
```python
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if not rig.data.bones:                                   # stand-in spars; normally from rig_chain
    C.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for i, y in enumerate((0.3, -0.2, -0.7)):
        b = rig.data.edit_bones.new(f"Spar{i + 1}.1.L")
        b.head, b.tail = (1.0, 0, 0), (2.0, y, 0)
        b.align_roll((0, 0, 1))
    bpy.ops.object.mode_set(mode='OBJECT')
spars = sorted(pb.name for pb in rig.pose.bones if pb.name.startswith("Spar") and ".1" in pb.name)
fold = {}
for i, name in enumerate(spars):
    pb = rig.pose.bones[name]
    pb.rotation_mode = 'XYZ'
    angle = 20.0 * (i + 1)                               # outer spars travel further, like a closing fan
    pb.rotation_euler = (0, 0, math.radians(angle))      # flip the sign if the wing opens instead
    fold[name] = angle
C.view_layer.update()
result = {"folded_deg": fold, "reset": "set rotation_euler back to (0, 0, 0) after looking"}
```

## Avoid
- Spar bones starting at the membrane's scallops (`at` from 0): the inner half of the membrane then follows nothing. Start at the `origin`.
- Nearest-bone weights on a membrane: each strip follows one spar and the sheet creases down the middle.
- One bone per spar on a long wing: it cannot bow, and the fold looks like a paper fan.

Check: `check_rig` returns `issues: []`; a `look` of the folded wing shows the membrane gathering between the spars with no spikes or tears.
