---
name: scene-cleanup
domain: scene
when: The user asks to clean up or tidy a messy scene, or `check_scene` reports stacked duplicates, default names or orphan data.
---

# Clean up a messy scene
Stacked duplicates removed, copied materials merged, empty collections and orphan data gone, and a list of what still needs a real name.

## Do
- Clean only on request: this deletes things. Call `save_snapshot` first and report every removed name.
- Run `check_scene` first; its issues are the work list.
- Stacked duplicates: meshes with the same vertex count, world location and dimensions, to 1 mm. Keep the first by name, delete the rest. Never delete one that has children.
- Copied materials: `Steel.001` merges into `Steel` only when both node trees have the same nodes, values and images. An edited copy is a different material; leave it.
- Remove collections that hold no objects at any depth.
- Purge orphans last, with `bpy.data.orphans_purge(do_recursive=True)`, so data freed by the earlier steps goes too. Data with a fake user is kept on purpose.
- Default names cannot be fixed blind. For each name in the result's `default_named`, inspect it with `object_info` or `look`, then rename it for what it is (see `naming-and-collections`).
- Leave unapplied scale to `game-ready-export` or the modelling skills; baking it here can change modifier results.

## Code
```python
scene, removed, merged, seen = C.scene, [], [], {}
for ob in sorted((o for o in scene.objects if o.type == 'MESH'), key=lambda o: o.name):
    key = (len(ob.data.vertices), tuple(round(v, 3) for v in ob.matrix_world.translation),
           tuple(round(v, 3) for v in ob.dimensions))
    if key in seen and not ob.children:
        removed.append(ob.name)
        D.objects.remove(ob, do_unlink=True)
    else:
        seen.setdefault(key, ob.name)

def signature(mat):
    return [(n.bl_idname, getattr(getattr(n, "image", None), "name", ""),
             [list(i.default_value) if i.type in {'RGBA', 'VECTOR'} else i.default_value
              for i in n.inputs if hasattr(i, "default_value")]) for n in mat.node_tree.nodes] if mat.node_tree else []

for mat in list(D.materials):
    base = D.materials.get(mat.name[:-4]) if mat.name[-4:-3] == "." and mat.name[-3:].isdigit() else None
    if base and signature(base) == signature(mat):
        mat.user_remap(base)
        merged.append(mat.name)
empty = [c.name for c in D.collections if not c.all_objects]
for name in empty:
    D.collections.remove(D.collections[name])
purged = D.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
defaults = {"Cube", "Sphere", "Icosphere", "Cylinder", "Cone", "Torus", "Plane", "Circle", "Grid", "Suzanne",
            "Empty", "Light", "Point", "Sun", "Spot", "Area", "Camera", "BezierCurve", "Text", "Armature"}
result = {"removed_duplicates": removed, "merged_materials": merged, "removed_collections": empty,
          "purged_datablocks": purged, "default_named": [o.name for o in scene.objects if o.name.split(".")[0] in defaults]}
```

## Avoid
- Purging before removing duplicates: their meshes and materials are still in use and stay behind.
- Deleting "duplicates" by name pattern (`.001`) alone: array-like copies at other positions are real objects.
- Purging in a file where the user parks unused materials or node groups without a fake user: they are deleted. Ask first if `check_scene` shows many orphans.

Check: `check_scene` reports no "identical objects" issue and an empty `orphan_data`; once the names in `default_named` are fixed it reports no default-named objects.
