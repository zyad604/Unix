---
name: depth-of-field
domain: camera
when: A shot needs background blur, selective focus, or a guarantee that the whole subject is sharp.
---

# Depth of field
Focus locked to an Empty, an f-stop chosen on purpose, and the sharp zone reported in meters.

## Do
- Decide the intent first. All sharp: products, architecture, technical views. Selective focus: portraits, hero details. No intent: leave `use_dof` off.
- Focus on an Empty, not the mesh: `focus_object` uses the object's origin, which is often at its feet. Put the Empty on the near eye, the label, or one third into the product's depth.
- `focus_distance` is measured along the view axis, not as straight-line distance, and is ignored while `focus_object` is set.
- F-stop: portraits f/1.8 to 2.8; full figures and groups f/4 to 5.6; products f/11 to 32. Close-ups of small things have a thin sharp zone: an 85 mm lens 0.9 m away at f/2.8 keeps only 2 cm sharp.
- Blur follows real size. Check the scene is at real-world scale before tuning the f-stop.
- Read the result. For an all-sharp shot set the f-stop to `fstop_all_sharp` or higher. For selective focus stay well below it and keep the focus point on the feature that matters.
- `aperture_blades` 0 gives round highlights; 6 to 9 gives polygons.
- `look` with `shading: "rendered"` shows the blur. In Cycles, blurred areas need about twice the samples.

## Code
```python
scene, cam, names, fstop = C.scene, C.scene.camera, ["Subject"], 4.0
pts = [o.matrix_world @ Vector(c) for o in (D.objects[n] for n in names) for c in o.bound_box]
focus = D.objects.get("Cam_Focus") or D.objects.new("Cam_Focus", None)
if focus.name not in scene.collection.all_objects:
    scene.collection.objects.link(focus)
focus.location = sum(pts, Vector()) / len(pts)        # then move it to the feature that must be sharp
focus.empty_display_size = 0.05
dof = cam.data.dof
dof.use_dof, dof.focus_object, dof.aperture_fstop = True, focus, fstop
C.view_layer.update()
inv = cam.matrix_world.inverted()
s = -(inv @ focus.matrix_world.translation).z         # focus distance along the view axis
depths = [-(inv @ p).z for p in pts]
f = cam.data.lens / 1000                              # focal length in meters
coc = cam.data.sensor_width / 1000 / 1200             # acceptable blur: 0.03 mm on a 36 mm sensor
H = f * f / (fstop * coc)
near, far = s * H / (H + s), s * H / (H - s) if H > s else None
need = f * f * max(abs(d - s) / d for d in depths) / (coc * s)
result = {"focus_m": round(s, 3), "sharp_from_m": round(near, 3),
          "sharp_to_m": round(far, 3) if far else "infinity",
          "subject_depth_m": [round(min(depths), 3), round(max(depths), 3)],
          "fstop_all_sharp": round(need, 1)}
```

## Avoid
- `focus_object` set to the subject mesh: the floor at its origin is sharp and the face is soft.
- f/1.4 to 2.8 on a product close-up: one sliver is sharp and it reads as a miniature.
- Changing lens or camera distance and keeping the old f-stop: the sharp zone moves and changes size. Re-run and re-read the result.

Check: for an all-sharp shot the result's `sharp_from_m` and `sharp_to_m` enclose `subject_depth_m`; for selective focus `look` with `shading: "rendered"` and `use_scene_camera: true` shows the focus feature crisp and the background visibly soft.
