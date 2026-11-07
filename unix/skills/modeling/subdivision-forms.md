---
name: subdivision-forms
domain: modeling
when: Building rounded or organic forms: vessels, cushions, bodies, handles, anything with curved surfaces.
---

# Subdivision forms
A low quad cage plus a live Subdivision modifier that gives a smooth, controllable curved form.

## Do
- Build the cage from the fewest quads that hold the shape: 8 to 12 sides around a round section, 100 to 500 faces for a prop.
- Objects round about an axis (bottle, vase, bowl, knob): lathe a (radius, z) profile with `bmesh.ops.spin`. Blobby forms: start from a cube, not a UV sphere.
- Levels: 2 in the viewport, 2 or 3 at render. Each level multiplies faces by 4; stay under about 100k final faces per object.
- Hold an edge with the FLOAT EDGE attribute `crease_edge` at 0.6 to 0.9 (1.0 is razor sharp), or with a support loop 5 to 15% of the face width from the edge.
- Thin walls: Solidify before Subdivision. Symmetric forms: Mirror first, with `use_clip=True`.
- The surface shrinks inside the cage: the vase below loses 14% of its widest radius. `dimensions` still reports the cage, so measure evaluated vertices and widen the cage to hit the target.
- Shade smooth. N-gons only as flat end caps.

## Code
```python
profile = [(0.045, 0.0), (0.06, 0.02), (0.075, 0.12), (0.04, 0.22), (0.03, 0.27), (0.04, 0.30)]  # (radius, z) m
me = bpy.data.meshes.get("Vase") or bpy.data.meshes.new("Vase")
bm = bmesh.new()
crease = bm.edges.layers.float.new("crease_edge")       # make layers before geometry
vs = [bm.verts.new((r, 0, z)) for r, z in profile]
es = [bm.edges.new(pair) for pair in zip(vs, vs[1:])]
bmesh.ops.spin(bm, geom=vs + es, cent=(0, 0, 0), axis=(0, 0, 1), angle=math.tau, steps=12, use_merge=True)
bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
rim = [e for e in bm.edges if e.is_boundary and all(v.co.z < 1e-6 for v in e.verts)]
bmesh.ops.contextual_create(bm, geom=rim)               # flat n-gon base cap
for e in rim: e[crease] = 0.8
bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
if sum(f.normal.xy.dot(f.calc_center_median().xy) for f in bm.faces) < 0:   # open mesh: force outward
    bmesh.ops.reverse_faces(bm, faces=bm.faces)
for f in bm.faces: f.smooth = True
bm.to_mesh(me); bm.free()
ob = bpy.data.objects.get("Vase") or bpy.data.objects.new("Vase", me)
if not ob.users_collection: bpy.context.scene.collection.objects.link(ob)
wall = ob.modifiers.get("Wall") or ob.modifiers.new("Wall", 'SOLIDIFY')
wall.thickness, wall.offset = 0.004, -1.0               # 4 mm, grown inward
sub = ob.modifiers.get("Subdivision") or ob.modifiers.new("Subdivision", 'SUBSURF')
sub.levels, sub.render_levels = 2, 3
ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
result = {"cage_faces": len(me.polygons), "final_faces": len(ev.data.polygons),
          "radius": max(v.co.xy.length for v in ev.data.vertices), "height": max(v.co.z for v in ev.data.vertices)}
```

## Avoid
- Triangles or 6+ edge poles on a curved area: visible pinch or star in the highlight. Move them to flat caps.
- Subdivision level 4 or more to hide a faceted cage: slow scene, same lumps. Add cage loops instead.
- Creating a bmesh layer after the geometry: existing vert and edge references go stale and the script errors.

Check: `check_mesh` on the cage reports 0 `tris` and `ngons` only for flat caps, `run_python` returns the target radius and height within 2%, and `look` (front, persp) shows a smooth silhouette with no pinches or facets.
