---
name: stl-print-export
domain: export
when: A model is going to a slicer or 3D printer as an STL file.
---

# STL export for 3D printing
One millimetre-scaled, modifier-applied mesh, resting on the bed, measured before it is written.

## Do
- STL stores bare numbers with no unit and slicers read them as millimetres. `export_file` writes Blender units unchanged, so a 0.09 m mug would arrive 0.09 mm tall. Build a copy scaled by 1000 and export that.
- Build the copy from evaluated meshes, so Subdivision, Bevel, Boolean and Solidify are in it, with every part's world transform baked.
- Curves are final in STL: facets must deviate less than 0.05 mm. A 80 mm cylinder needs 64 segments; use Subdivision level 2 or 3 on organic forms.
- Each shell must be closed. Overlapping closed shells are fine, slicers merge them. Open edges, loose faces and inner walls are not; join real intersections with a Boolean union first.
- Minimum wall 1.2 mm for filament printers, 1.0 mm for resin. Minimum raised or engraved detail 0.5 mm.
- Give the model a flat base; the code drops its lowest point to Z = 0. Avoid overhangs steeper than 45° or expect supports.
- Read `size_mm` against the printer bed. 220 x 220 x 250 mm is typical; ask the user if it is close.
- Run `check_mesh` on `Print_Export`, then `export_file` with a `.stl` path and `objects: ["Print_Export"]`, then delete the copy in `run_python` so a 1000x object does not stay in the scene.

## Code
```python
names, scale = ["Mug"], 1000.0                      # meters to millimetres
dg = C.evaluated_depsgraph_get()
bm = bmesh.new()
for ob in (D.objects[n] for n in names):
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    me.transform(Matrix.Scale(scale, 4) @ ev.matrix_world)
    bm.from_mesh(me)
    ev.to_mesh_clear()
lo = Vector(min(v.co[i] for v in bm.verts) for i in range(3))
hi = Vector(max(v.co[i] for v in bm.verts) for i in range(3))
bmesh.ops.translate(bm, verts=bm.verts, vec=(0, 0, -lo.z))        # rest on the bed
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
open_edges = sum(1 for e in bm.edges if not e.is_manifold)
volume_mm3 = abs(bm.calc_volume())
mesh = D.meshes.get("Print_Export") or D.meshes.new("Print_Export")
bm.to_mesh(mesh)
bm.free()
copy = D.objects.get("Print_Export") or D.objects.new("Print_Export", mesh)
if copy.name not in C.scene.collection.all_objects:
    C.scene.collection.objects.link(copy)
result = {"object": copy.name, "size_mm": [round(v, 2) for v in hi - lo], "open_edges": open_edges,
          "volume_cm3": round(volume_mm3 / 1000, 2), "triangles": sum(len(p.vertices) - 2 for p in mesh.polygons)}
# after export_file:  D.meshes.remove(D.meshes["Print_Export"])   # also removes the object
```

## Avoid
- Exporting the original objects: the file is 1000 times too small and the slicer shows nothing or a speck.
- Exporting with `open_edges` above 0: the slicer leaves holes or fills the wrong side.
- Thin decorative parts modelled as single planes: zero thickness cannot print. Give them a Solidify of at least 1.2 mm.

Check: `check_mesh` on `Print_Export` reports `watertight: true` and 0 `non_manifold_edges`; the result's `open_edges` is 0 and `size_mm` matches the intended real size and fits the bed; the `export_file` reply has `bytes` equal to 84 + 50 x `triangles`.
