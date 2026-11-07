---
name: seamless-loops
domain: animation
when: Motion must repeat without a visible seam - a spin, bob, sway, pulse, idle or any clip played on repeat.
---

# Seamless loops
An N-frame clip whose last frame flows into its first, with F-curves that keep cycling outside the keys.

## Do
- Pick the loop length N first: 48-96 frames for ambient motion at 24 fps, 24-32 for a walk, 16-20 for a run.
- Key the start pose at frame 1 and the identical pose at frame N+1, then set `frame_end = N`. Frame N+1 is the copy of frame 1 and must not be rendered.
- Continuous spin: two keys, 0 and `math.tau` x whole turns, `LINEAR`.
- Oscillation (bob, sway, pulse): keys on the extremes at 1, N/2+1 and N+1, `BEZIER`. Typical bob 0.02-0.1 m, sway 2-8 degrees.
- Add a Cycles modifier to every curve: `fc.modifiers.new('CYCLES')`. Default mode `REPEAT`; use `REPEAT_OFFSET` for motion that accumulates (a wheel, a walk moving forward).
- Several loops in one clip: every period must divide N (N = 120 allows 120, 60, 40, 30, 24).
- Renders and exports use the keyed frame range, not the modifier. The keys alone must already loop.

## Code
```python
ob, N = D.objects["Cube"], 96                       # object to loop; 4 s at 24 fps

def fcurves(idb):                                   # slotted actions (4.4+), legacy fallback
    ad = idb.animation_data
    act = ad.action
    if not hasattr(act, "layers"):
        return list(act.fcurves)
    bags = [s.channelbag(ad.action_slot) for l in act.layers for s in l.strips]
    return [fc for bag in bags if bag for fc in bag.fcurves]

z0 = ob.location.z
keys = [("location", 1, z0), ("location", N // 2 + 1, z0 + 0.05), ("location", N + 1, z0),
        ("rotation_euler", 1, 0.0), ("rotation_euler", N + 1, math.tau)]
for path, frame, value in keys:                     # bob 5 cm and spin one turn, both on Z
    getattr(ob, path)[2] = value
    ob.keyframe_insert(path, index=2, frame=frame)
for fc in fcurves(ob):
    for kp in fc.keyframe_points:
        kp.interpolation = 'LINEAR' if fc.data_path == "rotation_euler" else 'BEZIER'
    if not any(m.type == 'CYCLES' for m in fc.modifiers):
        fc.modifiers.new('CYCLES')
    fc.update()
C.scene.frame_start, C.scene.frame_end = 1, N
C.scene.frame_set(C.scene.frame_current)            # re-evaluate the pose for the current frame
result = {"loop_frames": N, "curves": [[fc.data_path, fc.array_index] for fc in fcurves(ob)]}
```

## Avoid
- `frame_end = N + 1`: frame 1 plays twice and the loop stutters at every repeat.
- First and last key typed separately: a 0.001 difference shows as a pop. Reuse one variable.
- A spin ending at 359 degrees, or Bezier spin keys with no Cycles modifier: the speed dips to near zero at the seam.

Check: in `run_python`, every curve gives `fc.evaluate(1) == fc.evaluate(N + 1)` and `fc.evaluate(2) == fc.evaluate(N + 2)` (spin: equal modulo `math.tau`); `scene_info` shows frames 1 to N.
