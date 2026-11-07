---
name: hands-and-fingers
domain: rigging
when: A hand, paw or foot needs finger or toe bones, or finger bones exist but sit outside the fingers or bend the wrong way.
---

# Hands and fingers
Three bones per finger inside each finger, knuckles inside the palm, and a curl that closes the hand.

## Do
- `find_limbs` reports a hand as a fan of kind `hand` with `fingers` (thumb, index, middle, ring, pinky) and the arm it sits on as `stem`. A foot looks the same: big toe = thumb.
- Arm first: `rig_chain` on the stem or trunk with `at` from its `narrow_points`, the last bone ending at 1.0 (inside the palm). Name it `Hand.L`.
- Each finger: `{limb: 'A.index', names: [three names], parent: 'Hand.L'}` and no `at`. The first knuckle is placed inside the palm, the thumb's root near the wrist.
- Different joint count (a two-bone toe, a four-bone claw): pass `at`, e.g. `[-0.25, 0.4, 1.0]`. Below 0 reaches back into the palm.
- Knuckles look wrong in the close-up: rebuild that chain with your own `at` (default `[-0.28, 0.33, 0.69, 1.0]`).
- Bind with `rig_bind`. Nails and claws that are loose pieces follow the finger automatically.
- Test: curl every finger (code), `look` at the hand with `focus_bones`, then reset the pose.
- `check_rig` reporting `bleed` between two fingers means their bones are not centred, or the fingers touch: `rig_bind` with `method: 'nearest'`.

## Code
```python
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if not rig.data.bones:                                   # stand-in finger; normally from rig_chain
    C.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    last = None
    for i in range(3):
        b = rig.data.edit_bones.new(f"Index.0{i + 1}.L")
        b.head, b.tail, b.parent = (0, 0.03 * i, 0), (0, 0.03 * (i + 1), 0), last
        last = b
    bpy.ops.object.mode_set(mode='OBJECT')
fingers = ("Index", "Middle", "Ring", "Pinky")
curled = []
for pb in rig.pose.bones:
    if pb.name.startswith(fingers):
        pb.rotation_mode = 'XYZ'
        pb.rotation_euler = (math.radians(45), 0, 0)     # local X: positive curls toward the palm
        curled.append(pb.name)
    elif pb.name.startswith("Thumb") and ".01" not in pb.name:
        pb.rotation_mode = 'XYZ'
        pb.rotation_euler = (math.radians(25), 0, 0)
        curled.append(pb.name)
C.view_layer.update()
result = {"curled": curled, "reset": "set rotation_euler back to (0, 0, 0) after looking"}
```

## Avoid
- Framing the whole character to judge fingers: they are a few pixels. Use `focus_bones`.
- A curl that opens the hand: `palm_side_known` was false. Use negative angles in your poses.
- Leaving the test pose on: `rig_bind` resets it, but an export would bake it.

Check: `check_rig` returns `issues: []`; a `look` close-up of the curled hand shows every finger bending at three places with no neighbour dragged along.
