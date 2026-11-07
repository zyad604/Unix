---
name: grease-pencil
domain: workflow
when: The task needs drawn strokes in 3D: outlines, annotations, guide lines, 2D-style linework.
---

# Grease Pencil
A Grease Pencil object with a layer, a keyframe and strokes placed by script.

## Do
- Blender 4.3 to 4.5 use Grease Pencil v3: object type `'GREASEPENCIL'`, data created in `D.grease_pencils_v3`. Older `gpencil` code (`frame.strokes.new()`) does not apply. On 5.x confirm the collection name with `api_lookup("BlendData")`.
- Structure: data, then layer, then frame, then `frame.drawing`, then strokes, then points. A layer with no frame at or before the current frame shows nothing.
- Add strokes with `drawing.add_strokes([n1, n2, ...])`, one point count per stroke. Then set every point's `position` (object space, meters), `radius` (meters) and `opacity`. New points have radius 0 and opacity 0.
- Radius: 0.002 to 0.005 m for fine lines on a 1 m subject, 0.01 to 0.02 m for bold outlines.
- A stroke is invisible without a Grease Pencil material. Create a normal material, call `D.materials.create_gpencil_data(mat)`, set `mat.grease_pencil.color`, append it to the data, and set `stroke.material_index`.
- Closed shapes: `stroke.cyclic = True`. Filled shapes: also `mat.grease_pencil.show_fill = True` and set `fill_color`.
- Re-runs: clear with `drawing.remove_strokes()` before adding, or strokes pile up.
- Unsure about a property: `api_lookup("GreasePencilDrawing")`.

## Code
```python
gp = D.grease_pencils_v3.get("Outline") or D.grease_pencils_v3.new("Outline")
obj = D.objects.get("Outline") or D.objects.new("Outline", gp)
if not obj.users_collection:
    C.scene.collection.objects.link(obj)
ink = D.materials.get("Outline_Ink") or D.materials.new("Outline_Ink")
if not ink.is_grease_pencil:
    D.materials.create_gpencil_data(ink)
ink.grease_pencil.color = (0.02, 0.02, 0.02, 1.0)
if ink.name not in gp.materials:
    gp.materials.append(ink)
layer = gp.layers.get("Lines") or gp.layers.new("Lines")
frame = layer.get_frame_at(1) or layer.frames.new(1)
drawing = frame.drawing
drawing.remove_strokes()                            # a re-run replaces the strokes
ring = [(0.5 * math.cos(i * math.tau / 32), 0.0, 0.5 + 0.5 * math.sin(i * math.tau / 32)) for i in range(32)]
drawing.add_strokes([len(ring)])
stroke = drawing.strokes[0]
stroke.cyclic, stroke.material_index = True, 0
for point, co in zip(stroke.points, ring):
    point.position, point.radius, point.opacity = co, 0.005, 1.0
C.view_layer.update()
result = {"type": obj.type, "strokes": len(drawing.strokes), "points": len(stroke.points),
          "dimensions": [round(d, 3) for d in obj.dimensions]}
```

## Avoid
- `D.grease_pencils.new()` on 4.3 to 4.5: that is legacy annotation data, and `D.objects.new` raises `RuntimeError: ID type 'GREASEPENCIL' is not valid for an object`.
- Skipping `radius` and `opacity`, or using pixel values like 20: the stroke is invisible, or 40 m wide.
- No material slot: the stroke exists in `result` but nothing appears in `look`.

Check: `run_python` returns `strokes` 1 and dimensions near 1.0 x 0.01 x 1.0 m, and `look(targets=["Outline"], views=["front"])` shows a closed dark ring.
