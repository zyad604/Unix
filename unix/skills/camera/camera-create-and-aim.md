---
name: camera-create-and-aim
domain: camera
when: A shot needs a camera: creating one, choosing the focal length, placing it at the right distance and keeping it aimed at the subject.
---

# Create and aim a camera
A named camera with the right lens, at a distance that fits the subject, held on target by a Track To constraint.

## Do
- Pick the lens by subject, sensor width 36 mm: interiors 18 to 24 mm; architecture and landscapes 24 to 35; vehicles and full figures 50 to 85; portraits 85 to 105; products 85 to 135. Use 50 when nothing dictates.
- Distance comes from the lens, not by eye: `radius / sin(half_angle) x 1.1`, using the bounding-sphere radius and the narrower view angle. The subject then fits from any direction.
- Default view: 35° round from the front, 15° above. Portraits: at the subject's eye height. Products: 10 to 25° above. Architecture: 1.6 m high and level, then reframe with `cam.data.shift_y`, never by tilting.
- Aim with a Track To constraint on an Empty: move the Empty to re-aim, move the camera freely. For a one-off aim use `cam.rotation_euler = (point - cam.location).to_track_quat('-Z', 'Y').to_euler()`.
- Clipping: `clip_start` = distance / 100 (not below 0.01), `clip_end` at least 10 x distance.
- Set `scene.camera` only when the task is a shot; say so in the reply.
- Tighten the frame afterwards with `camera-framing`.

## Code
```python
scene, names, lens = C.scene, ["Subject"], 50.0
pts = [o.matrix_world @ Vector(c) for o in (D.objects[n] for n in names) for c in o.bound_box]
lo, hi = Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))
centre, radius = (lo + hi) / 2, (hi - lo).length / 2
target = D.objects.get("Cam_Target") or D.objects.new("Cam_Target", None)
cam = D.objects.get("Cam_Main") or D.objects.new("Cam_Main", D.cameras.new("Cam_Main"))
for ob in (target, cam):
    if ob.name not in scene.collection.all_objects:
        scene.collection.objects.link(ob)
target.location, target.empty_display_size = centre, radius / 4
aspect = scene.render.resolution_x / scene.render.resolution_y
half = math.atan(18.0 / lens * min(aspect, 1 / aspect))     # narrower half view angle
dist = 1.1 * radius / math.sin(half)
az, el = math.radians(35), math.radians(15)                 # round from -Y, above the horizon
cam.location = centre + dist * Vector((math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)))
cam.data.lens, cam.data.sensor_width = lens, 36.0
cam.data.clip_start, cam.data.clip_end = max(0.01, dist / 100), max(100.0, dist * 10)
aim = cam.constraints.get("Aim") or cam.constraints.new('TRACK_TO')
aim.name, aim.target, aim.track_axis, aim.up_axis = "Aim", target, 'TRACK_NEGATIVE_Z', 'UP_Y'
scene.camera = cam
C.view_layer.update()
m = cam.matrix_world
error = (-m.col[2].to_3d()).angle(centre - m.translation)
result = {"camera": cam.name, "distance_m": round(dist, 3), "aim_error_deg": round(math.degrees(error), 3),
          "roll_deg": round(math.degrees(math.asin(max(-1, min(1, m.col[0].z)))), 3)}
```

## Avoid
- A wide lens pushed close to a small subject: bulging perspective. Step back and use a longer lens.
- Setting `rotation_euler` on a camera that has a Track To: the constraint overrides it and nothing changes.
- The default `clip_start` of 0.1 m on a close-up of a small object: its near side is cut away.

Check: the result has `aim_error_deg` under 0.5 and `roll_deg` 0, and `look` with `use_scene_camera: true` shows the whole subject, upright, inside the frame.
