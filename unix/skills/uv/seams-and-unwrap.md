---
name: seams-and-unwrap
domain: uv
when: A curved or organic mesh needs UVs for an image texture or a bake and a box projection would stretch it.
---

# Seams and unwrap
Marks seams with bmesh and unwraps the mesh into a few low-stretch islands.

## Do
- Apply scale first. The unwrap reads local coordinates, and `check_uv` cannot see the stretch that unapplied scale causes (`check_mesh` flags the scale).
- Plan cuts so every part can lie flat: a cylinder needs both rim loops plus one line down the side; a ball-like form needs one meridian; a limb needs one line along its hidden side.
- Put seams on edges sharper than 60 degrees, on the back or underside, and where the material changes. Keep them off faces the camera sees.
- Aim for 1-5 islands on a simple prop and under 20 on a complex one. Every extra island costs padding.
- Use `method='ANGLE_BASED'` for mechanical shapes and `'MINIMUM_STRETCH'` (Blender 4.3+) for organic ones.
- The operator needs edit mode and unwraps selected faces only, so select all faces.
- Unwrap after mirror, bevel and boolean decisions are final. New geometry gets no usable UVs.

## Code
```python
obj = D.objects["Barrel"]                  # scale already applied
me = obj.data
if C.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
bm = bmesh.new(); bm.from_mesh(me)
for e in bm.edges:
    sharp = len(e.link_faces) == 2 and e.calc_face_angle(0.0) > math.radians(60)
    back = all(abs(v.co.x) < 1e-4 and v.co.y > 0 for v in e.verts)   # one line down the back
    e.seam = sharp or back
seams = sum(e.seam for e in bm.edges)
bm.to_mesh(me); bm.free()
# Operator: needs the object active and selected, edit mode, all faces selected.
for o in C.view_layer.objects: o.select_set(False)
obj.select_set(True); C.view_layer.objects.active = obj
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.uv.unwrap(method='ANGLE_BASED', margin_method='FRACTION', margin=0.004)
bpy.ops.object.mode_set(mode='OBJECT')
result = {"seams": seams, "uv_layer": me.uv_layers.active.name}
```

## Avoid
- Unwrapping a closed mesh with no seams: the solver fails with only a console warning and leaves the old UVs, so `check_uv` shows the wrong island count or a spread above 4x.
- Seams that do not form a full cut (a rim loop with a gap): the island cannot flatten and stretches near the gap.
- Calling `bpy.ops.uv.unwrap` in object mode: `RuntimeError` from a failed poll.

Check: `check_uv` reports the island count you planned, `density_spread_max_over_min` below 2.0, 0 `zero_area_uv_faces` and 0% outside 0-1.
