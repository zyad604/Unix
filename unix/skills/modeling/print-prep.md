---
name: print-prep
domain: modeling
when: The user wants a model 3D printed (FDM or resin) or exported as STL for a slicer.
---

# 3D-print preparation
A separate `<Name>_Print` copy: one watertight solid in millimetre units, flat on the build plate, checked for wall thickness and overhangs.

## Do
- Confirm the final size in mm and the process. Assume FDM with a 0.4 mm nozzle if not told.
- Fuse separate parts with a Boolean union (`booleans`). Intersecting shells are not one solid.
- Bake modifiers and transforms into a new `_Print` mesh. Never apply them on the original.
- STL has no units and slicers read millimetres, while `export_file` writes scene units. The code scales the copy by 1000, so `check_mesh` reports its volume in mm3; hide it after export.
- Minimum wall: FDM 1.2 mm (0.8 absolute), resin 1.0 mm. Minimum raised or engraved detail: FDM 0.5 mm, resin 0.3 mm.
- Clearance between mating parts: FDM 0.2 to 0.3 mm per side, resin 0.1 mm.
- Faces more than 45 degrees past vertical need supports. Put the largest flat face down or replace the overhang with a 45 degree chamfer.
- Cylinders over 20 mm across need 64 or more segments.

## Code
```python
src = bpy.data.objects["Bracket"]
name = src.name + "_Print"
dg = bpy.context.evaluated_depsgraph_get()
me = bpy.data.meshes.new_from_object(src.evaluated_get(dg))     # modifiers baked; source untouched
me.transform(Matrix.Scale(1000.0, 4) @ src.matrix_world)        # world transform baked; 1 unit = 1 mm
ob = bpy.data.objects.get(name) or bpy.data.objects.new(name, me)
old, ob.data, me.name = ob.data, me, name
if old != me: bpy.data.meshes.remove(old)
if not ob.users_collection: src.users_collection[0].objects.link(ob)
bm = bmesh.new()
bm.from_mesh(me)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=0.001)        # 1 micron
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
zmin = min(v.co.z for v in bm.verts)
bmesh.ops.translate(bm, verts=bm.verts, vec=(0, 0, -zmin))      # sit on the build plate
tree = mathutils.bvhtree.BVHTree.FromBMesh(bm)
walls = [tree.ray_cast(f.calc_center_median() - f.normal * 1e-4, -f.normal)[3] for f in bm.faces]
over = sum(f.calc_area() for f in bm.faces if f.normal.z < -0.7071 and f.calc_center_median().z > 0.01)
size = [max(v.co[i] for v in bm.verts) - min(v.co[i] for v in bm.verts) for i in range(3)]
result = {"object": name, "size_mm": [round(s, 2) for s in size], "volume_cm3": round(bm.calc_volume() / 1000, 3),
          "open_edges": sum(1 for e in bm.edges if not e.is_manifold),
          "min_wall_mm": round(min(w for w in walls if w), 3), "overhang_mm2": round(over, 1)}
bm.to_mesh(me)
bm.free()
```

## Avoid
- Exporting the scene-scale object: the slicer shows a 0.06 mm speck for a 60 mm part.
- Trusting `min_wall_mm` alone: it samples face centres only. Look at thin fins and tips yourself.
- Zero-thickness surfaces (planes, open shells): the slicer drops them. Add Solidify first.

Check: `check_mesh` on `<Name>_Print` reports `watertight: true` and no issues, `size_mm` matches the requested size, `min_wall_mm` meets the minimum, then `export_file` to `.stl` succeeds.
