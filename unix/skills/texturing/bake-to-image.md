---
name: bake-to-image
domain: texturing
when: Procedural nodes, ambient occlusion or other shading must become an image texture, usually before export.
---

# Bake to image
Bakes one map with Cycles into a packed image with the right colour space.

## Do
- Bake only onto unique UVs: run `check_uv` first and require 0% outside 0-1 and no overlaps.
- The bake target is the selected, active Image Texture node in every material on the object. Leave it unlinked while baking.
- Colour space: create colour maps (DIFFUSE, EMIT) as sRGB and data maps (AO, ROUGHNESS, NORMAL) with `is_data=True`.
- `type='DIFFUSE'` with `pass_filter={'COLOR'}` gives base colour without lighting. Metallic surfaces bake black there: set Metallic to 0 for the bake.
- Size 1024 for props, 2048 for hero assets. Margin 8 px at 1024, 16 px at 2048.
- Samples: 32 for AO (range 16-64); 1-4 for colour, roughness and normal, which have no noise.
- The bake blocks Blender. Pass `timeout_s` 300 or more for 2048 and above.
- Restore the render engine and sample count afterwards.
- Call `image.pack()`; an unpacked baked image is lost when the file closes. Do not call `image.save()`.
- Wire the result afterwards: colour into Base Color, AO multiplied over base colour at 0.5-1.0.

## Code
```python
obj = D.objects["Crate"]; size = 1024
kind = 'AO'                              # or 'DIFFUSE', 'ROUGHNESS', 'NORMAL', 'EMIT'
name = f"{obj.name}_{kind}"
img = D.images.get(name) or D.images.new(name, size, size, alpha=False, is_data=kind not in {'DIFFUSE', 'EMIT'})
for slot in obj.material_slots:          # target = selected, active image node
    nt = slot.material.node_tree
    node = nt.nodes.get("unix_bake") or nt.nodes.new("ShaderNodeTexImage")
    node.name = node.label = "unix_bake"
    node.image = img
    for n in nt.nodes: n.select = False
    node.select = True; nt.nodes.active = node
sc = C.scene
saved = (sc.render.engine, sc.cycles.samples)
sc.render.engine = 'CYCLES'; sc.cycles.samples = 32 if kind == 'AO' else 4
# Operator: needs object mode and the object selected and active.
if C.mode != 'OBJECT': bpy.ops.object.mode_set(mode='OBJECT')
for o in C.view_layer.objects: o.select_set(False)
obj.select_set(True); C.view_layer.objects.active = obj
try:
    status = bpy.ops.object.bake(type=kind, pass_filter={'COLOR'}, margin=size // 128, use_clear=True)
finally:
    sc.render.engine, sc.cycles.samples = saved
if 'FINISHED' in status: img.pack()      # store inside the .blend
result = {"status": list(status), "image": img.name, "colorspace": img.colorspace_settings.name, "packed": img.packed_file is not None}
```

## Avoid
- No active image node in one of the materials: the bake returns `CANCELLED` without raising and the image stays black.
- Baking into an image that is linked into the same shader: circular dependency, and the map comes out black.
- Overlapping UVs: islands overwrite each other and the map shows patches of the wrong surface.

Check: `result` shows `status: ["FINISHED"]`, `packed: true` and the expected colour space; after wiring, `check_materials` reports no issues and `look` with `shading: rendered` shows the map on the surface with no seams or black patches.
