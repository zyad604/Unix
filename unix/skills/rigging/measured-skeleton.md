---
name: measured-skeleton
domain: rigging
when: Any existing model needs a skeleton - a character, creature, hand, tail, tentacle, plant or machine - before you place a single bone.
---

# Measured skeleton
Bones that run through the middle of every part, placed from measurements instead of guesses.

## Do
- `object_info` first. A `transform_warning` means local coordinates are not world coordinates; the tools below work in world space either way.
- `find_limbs` on the mesh. Whole body: pass `root` at the pelvis (or chest) and `smallest: 0.01` so fingers and toes count. One part (an arm, a tail): no arguments.
- Read the result: `trunk` and `stems` are the long runs (spine, arms, legs, neck, tail), `limbs` end in a tip, `fans` group short limbs that spread from one place (hands, feet, claws, wing spars).
- Joints of a long run sit at its `narrow_points`: near the middle is the elbow or knee, the last is the wrist or ankle. Pass them as `at` fractions to `rig_chain`. A stem starts on the body's surface: begin arms and legs at `-0.1` so the first joint is inside the body.
- No natural joints (tail, neck, tentacle, spine): give only names. Bones are spaced evenly. Tail 6-10, neck 3-5, spine 4-6, tentacle 8-12.
- Parts `find_limbs` cannot see (jaw, ear, eyelid, cape, strap): `rig_chain` with `points` and `centre: true`.
- Build every chain in one `rig_chain` call, parents before children. Name sides `.L` (+X) and `.R`.
- Then add one `Root` and hang the loose chains on it (code), `rig_bind`, `check_rig`, and `look` with `focus_bones` on every small group.
- Fix what `check_rig` reports by calling `rig_chain` again with better `at` values: existing bones move. Then `rig_bind` again.

## Code
```python
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
C.view_layer.objects.active = rig                       # mode_set acts on the active object
bpy.ops.object.mode_set(mode='EDIT')
eb = rig.data.edit_bones
if not eb:                                              # stand-in chains; normally from rig_chain
    for name, x in (("Arm.L", 0.4), ("Arm.R", -0.4)):
        b = eb.new(name)
        b.head, b.tail = (x, 0, 1.4), (x * 2, 0, 1.4)
loose = [b for b in eb if b.parent is None and b.name != "Root"]
floor = min(min(b.head.z, b.tail.z) for b in eb)
span = max((b.head - c.head).length for b in eb for c in eb) or 1.0
root = eb.get("Root") or eb.new("Root")
root.head, root.tail = (0, 0, floor), (0, 0.15 * span, floor)
for b in loose:
    b.parent, b.use_connect = root, False               # keep each chain where it was measured
bpy.ops.object.mode_set(mode='OBJECT')
rig.data.bones["Root"].use_deform = False               # a control: it must not take weights
result = {"root_children": [b.name for b in rig.data.bones["Root"].children], "bones": len(rig.data.bones)}
```

## Avoid
- Reading vertex coordinates and guessing joints: small bones land outside their part and get no weights.
- Building bones before `find_limbs`, or after moving the mesh: `rig_chain` refuses a stale measurement.
- `use_connect = True` when hanging chains on Root: every chain head snaps to the root's tail.

Check: `check_rig` returns `issues: []`, and `look` with `focus_bones` shows each orange bone running inside its own part.
