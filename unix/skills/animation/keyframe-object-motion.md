---
name: keyframe-object-motion
domain: animation
when: An object's location, rotation or scale must change over time and you are about to write keyframes by script.
---

# Keyframe object motion
Transform keyframes with deliberate timing and easing, written so they work on Blender 4.4+ slotted actions.

## Do
- Read fps and frame range from `scene_info`. Frames = seconds x fps (24 film, 30 web and games). Change fps or range only when the animation is the task, and report it.
- Set the value, then `obj.keyframe_insert(path, index=, frame=)`. It creates the action, slot and F-curves correctly on every version.
- Key only the channels that move (`index=2` is Z), on whole frames.
- Read F-curves through the slot's channelbag. `action.fcurves` is a legacy proxy in 4.4/4.5 and is removed in 5.0.
- A key's `interpolation` shapes the segment after it. Constant-speed or mechanical: `LINEAR`. Start-and-stop: `BEZIER` with `AUTO_CLAMPED` handles. Snap: `CONSTANT`.
- Timing at 24 fps: quick move 6-8 frames, normal 12-16, heavy 24-48. Hold 4-8 frames before and after so the move reads.
- Rotation is radians. For a full turn key `math.tau`, not 0.
- Call `fc.update()` after editing keyframe points, and finish with `C.scene.frame_set(C.scene.frame_current)`. Until then the object sits at the last keyed value and `look` shows the wrong pose.

## Code
```python
ob = D.objects["Cube"]                              # object to animate

def fcurves(idb):                                   # slotted actions (4.4+), legacy fallback
    ad = idb.animation_data
    act = ad.action
    if not hasattr(act, "layers"):
        return list(act.fcurves)
    bags = [s.channelbag(ad.action_slot) for l in act.layers for s in l.strips]
    return [fc for bag in bags if bag for fc in bag.fcurves]

x0 = ob.location.x
for frame, x in ((1, x0), (9, x0), (25, x0 + 2.0)):   # hold 8 frames, then move 2 m in 16
    ob.location.x = x
    ob.keyframe_insert("location", index=0, frame=frame)
for fc in fcurves(ob):
    for kp in fc.keyframe_points:
        kp.interpolation = 'BEZIER'
        kp.handle_left_type = kp.handle_right_type = 'AUTO_CLAMPED'
    fc.update()
C.scene.frame_set(C.scene.frame_current)            # re-evaluate: the object returns to the pose of this frame
result = {"action": ob.animation_data.action.name,
          "curves": [[fc.data_path, fc.array_index, len(fc.keyframe_points)] for fc in fcurves(ob)]}
```

## Avoid
- `action.fcurves` and `action.fcurves.new()`: they still work in 4.5 through the legacy proxy, then raise AttributeError on 5.0.
- `AUTO` handles on a hold: the curve drifts past the held value between two equal keys (0.07 m on this move). Use `AUTO_CLAMPED`.
- Assigning a value on a keyed channel without keying it: it snaps back on the next frame change.

Check: `object_info` shows the action with the intended frame range; `C.scene.frame_set(n)` in `run_python` then `look` at the first and last key shows the start and end poses. Restore the frame after.
