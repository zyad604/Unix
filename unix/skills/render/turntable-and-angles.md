---
name: turntable-and-angles
domain: render
when: The user wants a turntable, a 360 spin, or the same subject rendered from several angles.
---

# Turntable and multi-angle output
A pivot Empty that spins the subject (or the camera) one full turn, usable for an animation or for evenly spaced stills.

## Do
- Frame and light the first view before building the spin.
- Spin the subject for products and characters: light and background stay fixed to the camera and highlights travel over the surface. Spin the camera for environments and dioramas, where the light belongs to the scene.
- Pivot at the subject's centre on the ground. Parent only top-level objects, with `matrix_parent_inverse` set so nothing jumps.
- One turn is keyed from frame 1 to frame N + 1 with linear interpolation and the range ends at N, so the loop has no repeated frame. N = 96 at 24 fps is a 4 s spin; 120 to 240 for a slow hero spin.
- Stills: `render_image` renders the current frame only. For K evenly spaced views pick N divisible by K, set the frame to `1 + i * N / K` with `scene.frame_set` in `run_python`, then call `render_image` with a numbered path such as `chair_045.png`. K = 8 (45° steps) for a review set, 24 or 36 for a web 360 viewer.
- A full animation is one `render_image` per frame. Past about 36 frames, leave the keyed scene for the user to render with Render Animation. Say that you changed the frame range.
- For a quick check of all sides with no setup, use `look` with `views: ["front", "right", "back", "left"]`.

## Code
```python
scene, names, frames, spin_camera = C.scene, ["Subject"], 96, False
pts = [o.matrix_world @ Vector(c) for o in (D.objects[n] for n in names) for c in o.bound_box]
lo, hi = Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))
pivot = D.objects.get("Turntable") or D.objects.new("Turntable", None)
if pivot.name not in scene.collection.all_objects:
    scene.collection.objects.link(pivot)
    pivot.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)
pivot.rotation_euler = (0, 0, 0)
C.view_layer.update()                          # pivot.matrix_world must be current
movers = [scene.camera] if spin_camera else [D.objects[n] for n in names]
for ob in movers:
    if ob.parent is None:
        ob.parent, ob.matrix_parent_inverse = pivot, pivot.matrix_world.inverted()
pivot.keyframe_insert("rotation_euler", index=2, frame=1)
pivot.rotation_euler.z = math.tau
pivot.keyframe_insert("rotation_euler", index=2, frame=frames + 1)
anim = pivot.animation_data
slot = getattr(anim, "action_slot", None)      # slotted actions: 4.4 and later
fcurves = anim.action.layers[0].strips[0].channelbag(slot).fcurves if slot else anim.action.fcurves
for fc in fcurves:
    for key in fc.keyframe_points:
        key.interpolation = 'LINEAR'
scene.frame_start, scene.frame_end = 1, frames
scene.frame_set(1)
result = {"pivot": pivot.name, "spinning": [o.name for o in movers if o.parent == pivot],
          "frames": frames, "degrees_per_frame": round(360 / frames, 3)}
```

## Avoid
- Keys at frames 1 and N with the range ending at N: the first and last frames are the same pose and the loop stutters.
- Default Bezier keys: the spin eases in and out instead of turning at a constant speed.
- Parenting the floor or backdrop to the pivot: the ground turns with the product.

Check: after `scene.frame_set(1 + frames // 4)` in `run_python`, `object_info` on the pivot shows `rotation_deg` Z = 90, and `look` with `use_scene_camera: true` shows the subject a quarter turn round and still fully in frame.
