---
name: clean-topology
domain: modeling
when: A mesh was imported, joined, booleaned or hand-built and must now shade, subdivide, unwrap or export without faults.
---

# Clean topology
A mesh with applied scale, no doubles, no loose or degenerate geometry and outward normals.

## Do
- Run `check_mesh` first and fix only what it reports, in this order: scale, duplicate verts, degenerate faces, loose geometry, triangles, normals.
- Apply scale through the data: transform the mesh by the scale matrix, then set scale to 1. Skip this when `me.users > 1` or the object has children, and ask.
- Merge distance: 0.0001 m by default, 0.00001 m on parts under 50 mm. Never more than a tenth of the smallest feature.
- Quads on anything that subdivides or deforms. Triangles and n-gons are fine on flat faces of rigid parts.
- Join triangles to quads only on meshes that arrived triangulated; use a 40 degree limit so real creases survive.
- Poles: 3 and 5 edge poles go on flat areas, away from curvature. `poles_6plus` should be 0 except at cap centres.
- Keep neighbouring quads within about 1:4 in size and aspect; long slivers shade badly.

## Code
```python
ob = bpy.data.objects["Crate"]
me = ob.data
if any(abs(s - 1.0) > 1e-6 for s in ob.scale) and me.users == 1 and not ob.children:
    me.transform(Matrix.Diagonal((*ob.scale, 1.0)))       # bake scale into the mesh
    ob.scale = (1, 1, 1)
bm = bmesh.new()
bm.from_mesh(me)
before = (len(bm.verts), len(bm.faces))
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.0001)
bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=1e-6)
bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)          # outward on closed meshes; do it before joining
tris = [f for f in bm.faces if len(f.verts) == 3]
bmesh.ops.join_triangles(bm, faces=tris, angle_face_threshold=math.radians(40),
                         angle_shape_threshold=math.radians(40))
bm.to_mesh(me)
result = {"verts": [before[0], len(bm.verts)], "faces": [before[1], len(bm.faces)],
          "tris_left": sum(1 for f in bm.faces if len(f.verts) == 3)}
bm.free()
me.update()
```

## Avoid
- A large merge distance to "fix" gaps: thin features collapse and UVs smear. Find the gap instead.
- Recalculating normals on an open mesh and trusting it: some faces stay flipped and render dark. Close the mesh or flip those faces by hand.
- Cleaning a mesh that has shape keys: vertex count changes and the keys break. Ask first.

Check: `check_mesh` reports an empty `issues` list for the object, with `duplicate_verts` 0 and `zero_area_faces` 0.
