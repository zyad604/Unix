---
name: performance-audit
domain: workflow
when: The viewport or renders are slow, the file is heavy, or an asset must meet a triangle budget.
---

# Performance Audit
A ranked list of what makes the scene heavy, then the cheapest fix for each.

## Do
- Measure evaluated triangles. `scene_info` and `check_scene` count base meshes only: no modifiers, no instances.
- Loop over `iter(dg.object_instances)`. A plain `for x in dg.object_instances:` is wrapped in `list()` by `run_python`, and instance structs do not survive that (`ReferenceError`).
- Budgets: the viewport stays fluid under about 5 M evaluated triangles (2 to 20 M by GPU). Real-time prop 1k to 10k, hero asset 20k to 100k.
- Subdivision is the usual cause: each level multiplies faces by 4. Keep `levels` (viewport) at 1 to 2 and `render_levels` at 2 to 3.
- Identical meshes in separate datablocks: share one with `obj.data = first.data`. Above about 100 copies use a collection instance (`empty.instance_type = 'COLLECTION'`, `empty.instance_collection = col`) or Geometry Nodes instances.
- Viewport-only relief: `obj.display_type = 'BOUNDS'` on heavy background objects, or `scene.render.use_simplify = True` with `simplify_subdivision = 2`. Tell the user; leave `simplify_subdivision_render` alone.
- Textures: 4096 px only for hero close-ups, 2048 for normal props, 1024 for background.

## Code
```python
dg = C.evaluated_depsgraph_get()
tri = lambda me: len(me.loops) - 2 * len(me.polygons)      # triangle count without triangulating
cost = {}                                                  # owner -> [evaluated triangles, copies]
for inst in iter(dg.object_instances):                     # real objects and every instance
    ob = inst.object
    if ob.type == 'MESH' and ob.data:
        owner = inst.parent.original.name if inst.is_instance else ob.original.name
        c = cost.setdefault(owner, [0, 0])
        c[0] += tri(ob.data); c[1] += 1
heavy = sorted(cost.items(), key=lambda kv: -kv[1][0])[:10]
twins = {}                                                 # same counts, separate datablocks
for me in D.meshes:
    if me.users and len(me.polygons) >= 500:
        twins.setdefault((len(me.vertices), len(me.polygons)), []).append(me.name)
result = {
    "total_tris": sum(c[0] for c in cost.values()),
    "heaviest": [{"name": n, "tris": c[0], "copies": c[1]} for n, c in heavy],
    "subsurf": {o.name: [m.levels, m.render_levels] for o in C.scene.objects
                for m in o.modifiers if m.type == 'SUBSURF'},
    "duplicate_meshes": [names for names in twins.values() if len(names) > 1],
    "big_images": [[i.name, *i.size] for i in D.images if max(i.size) > 2048]}
```

## Avoid
- Trusting base mesh counts: a 5,000-face mesh with subdivision level 4 draws 1.28 M faces.
- Applying or decimating to "optimise": detail is lost for good. Lower a modifier level first; decimate only a copy, when asked.
- Replacing duplicates that only look alike: `duplicate_meshes` matches counts, not shape. Compare dimensions first.

Check: re-run the audit with `run_python`; `total_tris` is under the stated budget, and `look` shows no visible loss on the changed objects.
