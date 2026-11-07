---
name: hard-surface-projection
domain: uv
when: A boxy or mechanical mesh needs UVs fast, either unique islands for a bake or world-scale UVs for a tiling texture.
---

# Hard-surface projection
Gives flat-sided meshes UVs without hand-placed seams: Smart UV Project for unique islands, cube projection for tiling.

## Do
- Apply scale first; both operators read local coordinates.
- Use Smart UV Project when the texture is unique to the object (bakes, atlases). It splits by face angle and packs into 0-1.
- `angle_limit` is in radians. Use 66 degrees; range 45-80. Lower gives more islands and less stretch.
- Set `margin_method='FRACTION'` and `island_margin` 0.004 (about 16 px between islands at 2048).
- If Smart UV Project returns more than about 50 islands (bevels make slivers), raise the angle to 80 or load `seams-and-unwrap`.
- Use cube projection for tiling textures on walls, floors and crates. `cube_size` is metres per UV tile: 2.0 repeats the texture every 2 m on every object, so separate meshes line up.
- Cube projection stacks all six sides and leaves 0-1. That is correct for tiling and unusable for baking.
- Both operators act on selected faces only, so you can project one face set and unwrap the rest.

## Code
```python
def project(obj, mode="smart", tile_m=1.0):
    # Operators: need the object active and selected, edit mode, faces selected.
    if C.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
    for o in C.view_layer.objects: o.select_set(False)
    obj.select_set(True); C.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    if mode == "smart":      # unique islands inside 0-1
        bpy.ops.uv.smart_project(angle_limit=math.radians(66), margin_method='FRACTION',
                                 island_margin=0.004, area_weight=0.0, scale_to_bounds=False)
    else:                    # tiling: one UV tile per tile_m metres
        bpy.ops.uv.cube_project(cube_size=tile_m, correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    return len(obj.data.uv_layers.active.data)

result = {"Crate": project(D.objects["Crate"], "smart"),
          "Wall": project(D.objects["Wall"], "cube", tile_m=2.0)}
```

## Avoid
- Passing `angle_limit=66`: read as 66 radians, every face lands in one projection and side faces collapse to lines.
- `scale_to_bounds=True`: stretches the layout along one axis to fill the tile (1.2x on a 3 x 1 x 0.5 m box). Textures look squashed and `check_uv` does not flag it.
- Baking onto cube-projected UVs: the sides overwrite each other.

Check: `check_uv` after smart project reports 0% outside 0-1, `overlap_candidates` 0 and spread below 2.0; after cube projection, `texel_density_px_per_m` equals `texture_size / tile_m` within 5% (overlap and outside-0-1 issues are expected there).
