---
name: game-ready-export
domain: export
when: A static model is going to a game engine or real-time viewer as .glb or .fbx.
---

# Game-ready glTF or FBX export
A model with baked transforms, triangulated output, clean names and engine-safe materials, checked in the exported file.

## Do
- Format: .glb unless the engine demands .fbx. `export_file` converts Z-up to Y-up and applies modifiers in the file only.
- Scale: 1 unit = 1 m. Confirm real size with `object_info` before anything else.
- Put the asset root at the world origin with the model's base at Z = 0 and its front facing -Y.
- Bake rotation and scale into every mesh so each exports with scale (1, 1, 1). Do it with the data API, as below, not with operators.
- Triangulate with a modifier named `Export_Triangulate`: the file gets triangles, the scene keeps quads. The engine would triangulate anyway, and differently from the baker.
- Names: object and mesh data share one name; no `.001` suffixes; Unreal collision hulls are `UCX_<Mesh>_01`, LODs end in `_LOD0`, `_LOD1`.
- Materials: Principled BSDF only. Image textures or plain values on Base Color, Metallic, Roughness, Normal (through a Normal Map node), Emission, Alpha. Procedural nodes are not exported; bake them first. One material per prop, three at most.
- Budgets: mobile prop 500 to 3,000 triangles; desktop prop 3,000 to 15,000; hero or character 20,000 to 80,000. Textures 1024 or 2048, power of two.
- Run `check_mesh` and `check_materials`, then `export_file` with `objects` set to the result's `objects`. Passing only the root exports an empty node.

## Code
```python
root = D.objects["Chair_Root"]                     # asset root; everything under it is exported
parts = [root] + list(root.children_recursive)
meshes = [o for o in parts if o.type == 'MESH' and not o.find_armature()]
for ob in meshes:
    if ob.data.users > 1:
        ob.data = ob.data.copy()                   # linked duplicates must be unique before baking
    loc, rot, scale = ob.matrix_basis.decompose()
    bake = Matrix.LocRotScale(None, rot, scale)    # rotation and scale go into the mesh
    ob.data.transform(bake, shape_keys=True)
    if bake.determinant() < 0:
        ob.data.flip_normals()                     # negative scale turns faces inside out
    ob.matrix_basis = Matrix.Translation(loc)
    for child in ob.children:
        child.matrix_parent_inverse = bake @ child.matrix_parent_inverse
    ob.data.name = "tmp_" + ob.name               # two passes, so names can swap without clashing
    tri = ob.modifiers.get("Export_Triangulate") or ob.modifiers.new("Export_Triangulate", 'TRIANGULATE')
    tri.quad_method, tri.min_vertices = 'SHORTEST_DIAGONAL', 4
for ob in meshes:
    ob.data.name = ob.name
C.view_layer.update()
dg = C.evaluated_depsgraph_get()
result = {"objects": [o.name for o in parts],
          "triangles": sum(len(o.evaluated_get(dg).data.polygons) for o in meshes),
          "materials": sorted({s.material.name for o in meshes for s in o.material_slots if s.material}),
          "root_at_origin": root.matrix_world.translation.length < 1e-4,
          "numbered_names": [o.name for o in parts if o.name[-4:-3] == "." and o.name[-3:].isdigit()]}
```

## Avoid
- Unapplied scale: the engine shows the wrong size, and negative scale shows faces inside out.
- Baking scale and not looking again: Bevel and Solidify widths change with it. `look` afterwards.
- Baking a rigged mesh this way: it breaks the bind pose. The code skips meshes that have an armature.

Check: the `export_file` reply for the .glb has `file_contains.meshes` equal to the number of mesh objects and `materials` equal to the result's material count; `check_mesh` reports scale [1, 1, 1] for every part; the result's `numbered_names` is empty and `triangles` is inside the budget.
