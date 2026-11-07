---
name: edit-existing-mesh
domain: modeling
when: Changing the shape of a mesh that already exists in the scene, especially one the user made or imported.
---

# Edit an existing mesh
A targeted change to existing geometry that keeps its UVs, materials, modifiers and name.

## Do
- `object_info` first: note modifiers, UV layers, material slots, shape keys and `data_users`. Call `save_snapshot` before a destructive edit of the user's mesh.
- If `data_users` is above 1 the edit changes every linked duplicate. Say so, or edit a copy made with `me.copy()`.
- Edit in Object mode with `bmesh.new()`, `from_mesh`, `to_mesh`. If `ob.mode == 'EDIT'`, use `bmesh.from_edit_mesh(me)` and `bmesh.update_edit_mesh(me)` instead.
- Pick elements by geometry, never by index: face normal against a direction (dot above 0.9), centre position, bounding-box side.
- bmesh coordinates are local. Convert world points with `ob.matrix_world.inverted() @ p`.
- Edit the base cage and leave modifiers in place. Under a Mirror modifier, touch only the source half.
- With shape keys present, move vertices only. Do not add or remove geometry without asking.
- Faces made by `inset_region` and `extrude_face_region` inherit the UVs and material of the faces they came from.

## Code
```python
ob = bpy.data.objects["Block"]                 # Object mode; adds a 5 mm recess in the top
me = ob.data
RIM, DEPTH = 0.01, 0.005
up = (ob.matrix_world.inverted().to_3x3() @ Vector((0, 0, 1))).normalized()   # world up, in local space
bm = bmesh.new()
bm.from_mesh(me)
top = [f for f in bm.faces if f.normal.dot(up) > 0.9]
bmesh.ops.inset_region(bm, faces=top, thickness=RIM, use_even_offset=True)
ext = bmesh.ops.extrude_face_region(bm, geom=top)
new_verts = [g for g in ext["geom"] if isinstance(g, bmesh.types.BMVert)]
bmesh.ops.translate(bm, verts=new_verts, vec=-DEPTH * up)
bmesh.ops.delete(bm, geom=top, context='FACES')    # the old faces would be left inside
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
open_edges = sum(1 for e in bm.edges if not e.is_manifold)
bm.to_mesh(me)
bm.free()
me.update()
result = {"faces": len(me.polygons), "open_edges": open_edges, "uv_layers": [u.name for u in me.uv_layers],
          "materials": [m.name if m else None for m in me.materials]}
```

## Avoid
- Selecting by stored vertex or face indices: the second run edits the wrong faces.
- Reading `ob.data` while the object is in Edit mode: you get the mesh as it was before the user's edits.
- Deleting and rebuilding the object to change it: modifiers, materials, UVs, parenting and the user's name are lost.

Check: `object_info` shows the same UV layers, material slots and modifiers as before, and `check_mesh` reports no new non-manifold edges or duplicate verts.
