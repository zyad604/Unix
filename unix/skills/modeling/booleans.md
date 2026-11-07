---
name: booleans
domain: modeling
when: Cutting holes, slots or recesses, or fusing or intersecting two volumes.
---

# Booleans
A clean cut or union made by a live Boolean modifier with named cutters that can still be moved.

## Do
- Both meshes must be closed volumes with scale (1, 1, 1). Run `check_mesh` on the cutter first: `watertight: true`.
- Push the cutter through the surface by at least 1 mm on each side. Never leave cutter faces coplanar with target faces.
- Use `solver='EXACT'`. Use `api_lookup` on `BooleanModifier` before choosing another solver; names differ between versions.
- Keep the modifier live. Name cutters `<Asset>_Cut_<n>`, put them in a `<Asset>_Cutters` collection, parent them to the target, set `display_type='WIRE'` and `hide_render=True`.
- Several cutters: one modifier with `operand_type='COLLECTION'`, not one modifier each.
- Round cutters: 32 segments under 20 mm diameter, 48 to 64 above.
- Order: Boolean before Bevel, so the bevel rounds the new edges. No Subdivision after a boolean.
- `check_mesh` reads the base mesh, not the modifier result. Verify the cut from the evaluated mesh as below.

## Code
```python
target = bpy.data.objects["Plate"]                 # closed mesh, scale 1
R, LENGTH = 0.006, 0.05                            # hole radius; cutter longer than the plate is thick
col = bpy.data.collections.get("Plate_Cutters") or bpy.data.collections.new("Plate_Cutters")
if col.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(col)
for i, (x, y) in enumerate([(-0.04, 0.0), (0.04, 0.0)]):
    name = f"Plate_Cut_{i}"
    me = bpy.data.meshes.get(name) or bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=32, radius1=R, radius2=R, depth=LENGTH)
    bm.to_mesh(me); bm.free()
    cut = bpy.data.objects.get(name) or bpy.data.objects.new(name, me)
    if cut.name not in col.objects: col.objects.link(cut)
    cut.parent, cut.location = target, (x, y, 0)   # position in the target's local space
    cut.display_type, cut.hide_render = 'WIRE', True
mod = target.modifiers.get("Holes") or target.modifiers.new("Holes", 'BOOLEAN')
mod.operation, mod.solver = 'DIFFERENCE', 'EXACT'
mod.operand_type, mod.collection = 'COLLECTION', col

ev = target.evaluated_get(bpy.context.evaluated_depsgraph_get())
bm = bmesh.new(); bm.from_mesh(ev.data)
result = {"volume_m3": bm.calc_volume(), "faces": len(bm.faces),
          "open_edges": sum(1 for e in bm.edges if not e.is_manifold)}
bm.free()
```

## Avoid
- Coplanar or barely touching faces: the cut flickers, leaves slivers or does nothing. Overlap by 1 mm or more.
- An open or inside-out cutter: the result is empty or inverted.
- Applying the modifier and deleting the cutter: the cut can no longer be moved. Apply only on a print or export copy.

Check: `run_python` reports `open_edges` 0 and a volume lower than before by the cutter volume inside the target, and `look` shows the cut with clean edges.
