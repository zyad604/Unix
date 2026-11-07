---
name: camera-framing
domain: camera
when: A camera exists and the subject must fill the frame correctly: fitting, margins, aspect ratio, off-centre composition.
---

# Frame the subject
The camera moved so the subject fills a chosen share of the frame, with the result measured in frame coordinates.

## Do
- Fix resolution and aspect first; the fit depends on them. 1920 x 1080 general, 1080 x 1080 product, 1080 x 1350 portrait or social, 2048 x 858 cinematic. Tall subject: use a portrait frame instead of a wider lens.
- Fit with `cam.camera_fit_coords(depsgraph, coords)`: it keeps the camera's rotation and returns the location where the points just touch the frame edge.
- Then back off from the subject centre by a margin: 1.15 for a hero subject (fills about 85%), 1.4 for a subject with room around it, 2 to 3 when the setting matters.
- Full figures: 5 to 8% headroom, feet inside the frame. Portraits: eyes on the upper third.
- Off-centre composition: use `shift_x`, not rotation. Shift is in units of the longer frame side; +1/6 puts a centred subject on the left third line, -1/6 on the right. Use margin 1.6 or more so it stays inside. Leave the empty space on the side the subject faces.
- Vertical thirds: `shift_y`, positive moves the subject down. On a 16:9 frame 1/6 of the height is 0.094.
- Measure, do not guess: `world_to_camera_view` gives each point as 0 to 1 across the frame.

## Code
```python
from bpy_extras.object_utils import world_to_camera_view
scene, cam, names, margin = C.scene, C.scene.camera, ["Subject"], 1.15
dg = C.evaluated_depsgraph_get()
pts = []
for ob in (D.objects[n].evaluated_get(dg) for n in names):
    pts += [ob.matrix_world @ Vector(c) for c in ob.bound_box]
centre = sum(pts, Vector()) / len(pts)
cam.data.shift_x = cam.data.shift_y = 0.0
for _ in range(3):                      # repeat: a Track To re-aims after each move
    loc, _scale = cam.camera_fit_coords(dg, [v for p in pts for v in p])
    cam.location = centre + (Vector(loc) - centre) * margin
    C.view_layer.update()
    dg = C.evaluated_depsgraph_get()
uv = [world_to_camera_view(scene, cam, p) for p in pts]
xs, ys = [p.x for p in uv], [p.y for p in uv]
result = {"x": [round(min(xs), 3), round(max(xs), 3)], "y": [round(min(ys), 3), round(max(ys), 3)],
          "fill": round(max(max(xs) - min(xs), max(ys) - min(ys)), 3),
          "inside": min(xs + ys) >= 0 and max(xs + ys) <= 1 and all(p.z > 0 for p in uv)}
```

## Avoid
- Framing before the resolution is final: changing 16:9 to 1:1 afterwards crops the sides.
- Fitting at margin 1.0: the bounding box touches the edge and the subject looks cramped.
- Turning the camera to push the subject off-centre: verticals lean and the perspective changes. Shift instead.

Check: the result has `inside` true and `fill` between 0.6 and 0.9 (or the value the brief asks for), and `look` with `use_scene_camera: true` shows nothing important touching the frame edge.
