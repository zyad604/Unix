---
name: asset-root-and-parenting
domain: scene
when: A multi-part asset must move, rotate, scale, duplicate or export as one unit.
---

# Asset root and parenting
One root Empty at the asset's base, every part parented to it without moving, all filed in the asset's collection.

## Do
- Root: an Empty named `<Asset>_Root`, placed at the centre of the asset's footprint at its lowest Z. Root location (x, y, 0) then means "standing on the ground at x, y".
- Root rotation (0, 0, 0) and scale (1, 1, 1) when you parent. Afterwards move, turn and scale the asset through the root only, and scale it uniformly.
- Parent only top-level parts. Parts that already have a parent inside the asset keep it: handle to door, door to cabinet, cabinet to root. Two or three levels is enough.
- Keep parts in place by setting `matrix_parent_inverse` to the root's inverted world matrix. Update the view layer first, or a new root still reports an identity matrix.
- Each part's origin belongs where it pivots: door on the hinge line, wheel on the axle, lid on its back edge. Move an origin to world point `P` without moving the mesh: `local = ob.matrix_world.inverted() @ P`, then `ob.data.transform(Matrix.Translation(-local))` and `ob.matrix_world = ob.matrix_world @ Matrix.Translation(local)`.
- Root and parts share one collection named for the asset.
- Size the Empty to a quarter of the asset so it is easy to pick and does not clutter.

## Code
```python
scene, asset, names = C.scene, "Table", ["Table_Top", "Table_Leg_FL", "Table_Leg_FR"]
parts = [D.objects[n] for n in names]
pts = [o.matrix_world @ Vector(c) for o in parts for c in o.bound_box]
lo, hi = Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))
col = D.collections.get(asset) or D.collections.new(asset)
if col not in scene.collection.children_recursive:
    scene.collection.children.link(col)
root = D.objects.get(asset + "_Root")
if root is None:
    root = D.objects.new(asset + "_Root", None)
    root.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)
    root.empty_display_type, root.empty_display_size = 'PLAIN_AXES', max(hi - lo) / 4
    col.objects.link(root)
C.view_layer.update()                               # root.matrix_world is identity until this runs
before = {o.name: o.matrix_world.copy() for o in parts}
inverse = root.matrix_world.inverted()
for ob in parts:
    if ob.parent is None:
        ob.parent, ob.matrix_parent_inverse = root, inverse
    for other in ob.users_collection:
        other.objects.unlink(ob)
    col.objects.link(ob)
C.view_layer.update()
drift = max((o.matrix_world.translation - before[o.name].translation).length for o in parts)
result = {"root": root.name, "root_location": [round(v, 4) for v in root.location],
          "children": [o.name for o in root.children], "max_drift_m": round(drift, 6)}
```

## Avoid
- `child.parent = root` alone: the child jumps by the root's offset, turn and scale.
- Non-uniform scale on the root: rotated children shear.
- Parenting the camera, lights or floor to the asset root: they follow when the asset moves.

Check: the result's `max_drift_m` is 0 and `children` lists every top-level part; `object_info` on the root shows scale [1, 1, 1].
