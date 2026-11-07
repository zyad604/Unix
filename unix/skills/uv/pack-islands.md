---
name: pack-islands
domain: uv
when: UV islands exist and must be laid out in the 0-1 tile without overlap, for one object or several sharing one texture.
---

# Pack islands
Equalises island scale and packs one or more meshes into a single 0-1 layout with correct padding.

## Do
- Run `uv.average_islands_scale` before packing so every island has the same texel density.
- Pack with `margin_method='FRACTION'`. `margin` is the gap to the tile edge; the gap between two islands is twice that. For N pixels between islands on a T pixel texture use `N / (2 * T)`.
- Padding between islands: 8 px at 1024, 16 px at 2048, 32 px at 4096. Less bleeds across islands in mipmaps.
- Use `rotate_method='CARDINAL'` (90 degree steps) on hard-surface so straight edges stay on the pixel grid; `'ANY'` packs tighter for organic islands.
- Use `shape_method='AABB'` when the result must be verified: island boxes never interlock, so `check_uv` overlap counts are exact. `'CONCAVE'` fills about 10 points more of the tile on irregular islands but raises false `overlap_candidates`.
- To share one texture, select all the objects and enter edit mode together; the pack treats them as one layout.
- Stack islands only on purpose (mirrored or repeated parts) and never for a bake. Add `merge_overlap=True` so stacks move as one.

## Code
```python
names = ["Crate", "Barrel"]            # meshes sharing one texture, scale applied
tex, pad_px = 2048, 16                 # texture size, pixels between islands
objs = [D.objects[n] for n in names]
if C.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
# Operators: need the objects selected, one active, edit mode, faces and UVs selected.
for o in C.view_layer.objects: o.select_set(False)
for o in objs: o.select_set(True)
C.view_layer.objects.active = objs[0]
bpy.ops.object.mode_set(mode='EDIT')   # multi-object edit
bpy.ops.mesh.select_all(action='SELECT')
bpy.ops.uv.select_all(action='SELECT') # pack moves selected UVs only
bpy.ops.uv.average_islands_scale()
bpy.ops.uv.pack_islands(rotate=True, rotate_method='CARDINAL', shape_method='AABB',
                        margin_method='FRACTION', margin=pad_px / (2 * tex))
bpy.ops.object.mode_set(mode='OBJECT')
result = {"objects": names, "margin": pad_px / (2 * tex)}
```

## Avoid
- Packing with no UVs selected: the operator returns FINISHED and moves nothing.
- Packing objects one at a time for a shared texture: each fills 0-1 alone and they overlap each other.
- Packing before averaging scale: small parts get more pixels than large ones.

Check: `check_uv` on every packed object reports 0% outside 0-1, `overlap_candidates` 0, 0 `zero_area_uv_faces`, and `texel_density_px_per_m` (pass `texture_size`) within 10% across the objects.
