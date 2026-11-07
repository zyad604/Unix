---
name: turntable-and-camera-moves
domain: animation
when: The user wants a turntable, an orbit around an asset, or a simple animated camera move such as a push-in.
---

# Turntable and camera moves
A looping 360 degree orbit, or a clean eased camera move, that keeps the subject in frame on every frame.

## Do
- Choose what turns. Orbit the camera when the subject sits in a set or on a floor. Rotate the subject's root instead when lights and backdrop should stay fixed (product shots).
- Orbit through a pivot empty at the subject's bounding-box centre. Parent the camera to it and key only the pivot's Z rotation: 0 at frame 1, `math.tau` at frame N+1, `LINEAR`, `frame_end = N`.
- Length: 5-10 s (N = 120-240 at 24 fps), 36-72 degrees per second.
- Lens 50-85 mm. Elevation 10-20 degrees above the centre.
- Distance from the bounding sphere, not from one view: `radius / sin(half_fov) x 1.05`, using the narrower frame side, so nothing leaves frame as it turns.
- Push-in or pull-out: key camera location at two frames only, Bezier `AUTO_CLAMPED`, 3-5 s, travel 10-30 % of the distance. Aim with a `TRACK_TO` constraint (`TRACK_NEGATIVE_Z`, `UP_Y`) at an empty instead of keying rotation.
- Make a new camera (`Turntable_Cam`). Setting `scene.camera` and the frame range changes the user's shot: do it only when the turntable is the task, and report it.

## Code
```python
subject, N, elev = D.objects["Cube"], 120, math.radians(15)       # asset root; 5 s at 24 fps
parts = [o for o in (subject, *subject.children_recursive) if o.type == 'MESH']
pts = [o.matrix_world @ Vector(c) for o in parts for c in o.bound_box]
centre = Vector([(min(p[i] for p in pts) + max(p[i] for p in pts)) / 2 for i in range(3)])
radius = max((p - centre).length for p in pts)
pivot = D.objects.get("Turntable_Pivot") or D.objects.new("Turntable_Pivot", None)
cam = D.objects.get("Turntable_Cam") or D.objects.new("Turntable_Cam", D.cameras.new("Turntable_Cam"))
for ob in (pivot, cam):
    if ob.name not in C.scene.collection.all_objects:
        C.scene.collection.objects.link(ob)
rd = C.scene.render
half_fov = math.atan(cam.data.sensor_width / 2 / cam.data.lens
                     * min(rd.resolution_x, rd.resolution_y) / max(rd.resolution_x, rd.resolution_y))
dist = radius / math.sin(half_fov) * 1.05
pivot.location, cam.parent = centre, pivot
cam.location = (0, -dist * math.cos(elev), dist * math.sin(elev))
cam.rotation_euler = (math.pi / 2 - elev, 0, 0)                   # looks at the pivot
for frame, angle in ((1, 0.0), (N + 1, math.tau)):
    pivot.rotation_euler.z = angle
    pivot.keyframe_insert("rotation_euler", index=2, frame=frame)
ad = pivot.animation_data                                         # 4.4+ slotted action, legacy fallback
bag = ad.action.layers[0].strips[0].channelbag(ad.action_slot) if hasattr(ad.action, "layers") else ad.action
for kp in bag.fcurves[0].keyframe_points:
    kp.interpolation = 'LINEAR'
C.scene.camera, C.scene.frame_start, C.scene.frame_end = cam, 1, N
C.scene.frame_set(C.scene.frame_current)                          # re-evaluate the pose for this frame
result = {"camera": cam.name, "distance_m": round(dist, 3), "frames": N, "lens_mm": cam.data.lens}
```

## Avoid
- `frame_end = N + 1`: frame 1 shows twice and the loop stutters.
- Bezier on the orbit: it eases to a stop at the seam.
- Distance fitted to one view: the long side of the subject swings out of frame at 90 degrees.

Check: `look` with `use_scene_camera` at frames 1, N/4+1 and N/2+1 (`C.scene.frame_set` in `run_python`, then restore) shows the whole subject, centred, filling 45-85 % of the frame.
