---
name: character-animation
domain: animation
when: A rigged character or creature must move - walk, run, idle, jump, attack, gesture, any body motion - or the user says an animation looks wrong.
---

# Character animation
Motion a viewer believes: recorded where a recording exists, keyed pose to pose where it does not, and watched before it is called done.

## Do
- The rig comes first: `check_rig` clean and a pose test you looked at. Bad weights make every move look broken.
- A move a person makes (walk, run, sidestep, back up, turn, start, stop, jump, sit, climb, fight, dance): `motion_search` with one to three plain words, then `retarget_motion(target, clip, name)`. Do not type gait maths.
- A clip often holds several things in a row. Retarget it whole once, `look_motion`, then take the part you need with `start_s` and `end_s`. Judge it from the front too: a recording can carry a tilted head, curled toes or a hunch; then take another clip.
- Game cycle: `loop: true, in_place: true` (an idle also `min_cycle_s: 3`). Read `cycle.match`; if it is poor, take another window or clip.
- Read `recorded_move`: it says which way the recording goes, seen from the character. Name the action for that, or set `mirror: true` for the other side.
- A start or a stop: `blend_from: "Idle"` and `blend_to: "Run_Forward"`, so the actions join. No recording goes the way you need (a run to the side or back): `turn_deg` turns a good one to head that way.
- `check_motion` reports arms through a broad body or its clothes: `arms_out_deg` 8-20.
- No recording fits (a creature, a machine, a stylised move): key pose to pose. Extremes first (contact, passing, highest, lowest), then the poses between them.
- Offset the parts: hips lead, chest follows 2-3 frames later, head and hands later still. Nothing starts or stops on one frame.
- Weight: hips drop after a foot lands and rise as it pushes off. A heavy start sinks and leans 6-10 frames before it goes, and overshoots before it settles.
- Feet are positions: drive them with IK targets and hold a planted target still. Turn bones about world axes (code), not their own.
- One action per move, named for what it is, `use_fake_user = True`. A loop keys its first pose again on the frame after the last.
- After every action: `look_motion`, then `check_motion`. Fix what they show before making the next one.
- At the end, leave the action the user will want first on the rig, with the scene's frame range set to it, and tell them to press Play.

## Code
```python
rig = D.objects.get("Rig") or D.objects.new("Rig", D.armatures.new("Rig"))
if rig.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(rig)
if not rig.data.bones:                                  # stand-in bones; normally the rig already exists
    C.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for name, head, tail in (("Chest", (0, 0, 1.2), (0, 0, 1.5)), ("Arm.L", (0.2, 0, 1.45), (0.2, 0, 1.0)),
                             ("Arm.R", (-0.2, 0, 1.45), (-0.2, 0, 1.0))):
        eb = rig.data.edit_bones.new(name)
        eb.head, eb.tail = head, tail
    bpy.ops.object.mode_set(mode='OBJECT')

def turn(bone, frame, axis, degrees):                   # turn a bone about a WORLD axis, from its rest pose
    pb = rig.pose.bones[bone]
    rest = rig.matrix_world.to_quaternion() @ rig.data.bones[bone].matrix_local.to_quaternion()
    pb.rotation_mode = 'QUATERNION'
    pb.rotation_quaternion = rest.inverted() @ Quaternion(axis, math.radians(degrees)) @ rest
    pb.keyframe_insert("rotation_quaternion", frame=frame, group=bone)

N = 24                                                  # one cycle; frame N + 1 is frame 1 again
action = D.actions.new("Arm_Swing")
action.use_fake_user = True
rig.animation_data_create().action = action
for frame, swing in ((1, 28), (N // 2 + 1, -28), (N + 1, 28)):   # the two extremes, and the first one again
    turn("Arm.L", frame, (1, 0, 0), swing)              # about world X: forward and back
    turn("Arm.R", frame, (1, 0, 0), -swing)             # the other side is half a cycle away
    turn("Chest", frame, (0, 0, 1), -swing / 5)         # the chest counters the arms
action.use_frame_range, action.use_cyclic = True, True
action.frame_start, action.frame_end = 1, N + 1
C.scene.frame_set(C.scene.frame_current)
result = {"action": action.name, "frames": [1, N + 1], "then": "look_motion and check_motion"}
```

## Avoid
- A sine wave on every bone: it reads as a machine, and leans and reaches the wrong way as soon as the direction changes.
- Judging a move from one `look`: every pose can be fine in a move that is wrong.
- Leaving arms at the rest pose's angle: resting arms hang beside the body, a hand's width from the hip.

Check: `check_motion` returns `issues: []` for every action, and each `look_motion` strip reads as the move it is named for.
