---
name: batch-operations
domain: workflow
when: The same change must be applied to many objects, meshes or materials at once.
---

# Batch Operations
One script that changes every matching object, reports what it skipped, and finishes inside the time limit.

## Do
- Choose targets by name prefix, type or collection. Use the selection only when the user said "the selected objects".
- Dry run first above about 20 objects: return the matched names in `result`, read them, then run the change. Call `save_snapshot` before it.
- Loop over the data API: thousands of objects per second. An operator call per object updates the whole scene each time.
- Group objects by `obj.data`. Mesh edits happen once per mesh, object edits once per object. A mesh shared by objects with different scales cannot have scale applied: skip and report it.
- Skip linked data (`obj.library`, `obj.data.library`); it is read-only.
- Per-element values on big meshes: `me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))`, not a Python loop. Call `me.update()` after.
- `run_python` stops at 120 s by default (`timeout_s` up to 600). Over about 2,000 objects, process 500 per call.
- Return counts: matched, changed, skipped with the reason.

## Code
```python
PREFIX = "Crate_"                                   # apply scale and add one bevel to every match
targets = [o for o in C.scene.objects if o.type == 'MESH' and o.name.startswith(PREFIX)]
groups, changed, skipped = {}, [], {}
for o in targets:
    groups.setdefault(o.data.name, []).append(o)
for me_name, objs in groups.items():
    me = D.meshes[me_name]
    scales = {tuple(round(s, 5) for s in o.scale) for o in objs}
    if me.library or len(scales) > 1 or me.users != len(objs):
        skipped.update({o.name: "linked mesh, or shared at different scales" for o in objs})
        continue
    S = Matrix.Diagonal((*objs[0].scale, 1.0))
    me.transform(S)                                 # once per mesh, not once per object
    if S.determinant() < 0:
        me.flip_normals()                           # negative scale turns faces inside out
    me.update()
    for o in objs:
        for child in o.children:                    # keep children where they are
            child.matrix_parent_inverse = S @ child.matrix_parent_inverse
        o.scale = (1.0, 1.0, 1.0)
        bevel = o.modifiers.get("Bevel") or o.modifiers.new("Bevel", 'BEVEL')
        bevel.width, bevel.segments = 0.01, 2
        changed.append(o.name)
C.view_layer.update()
result = {"matched": len(targets), "changed": len(changed), "skipped": skipped}
```

## Avoid
- Transforming a shared mesh once per object: the scale is applied two or three times and the copies balloon.
- Removing objects while iterating `D.objects` or a collection: skipped items or a crash. Build a list first.
- Adding a modifier without `modifiers.get`: every re-run stacks another Bevel.

Check: `result` shows `changed` plus the number of `skipped` names equal to `matched`, `check_scene` reports no meshes with unapplied scale among the targets, and `look` shows unchanged sizes.
