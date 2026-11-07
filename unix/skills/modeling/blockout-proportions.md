---
name: blockout-proportions
domain: modeling
when: Starting any new object or asset, before detail, bevels or materials.
---

# Blockout and proportions
A named, parented set of 5 to 15 plain volumes at real size, with the silhouette right before any detail exists.

## Do
- Fix the overall size in meters first (chair seat 0.45 high, table 0.75 high, door 2.0 x 0.9, mug 0.09 high). Write every part size as a named constant derived from it.
- One box or cylinder per major mass. No bevels, no subdivision, no part smaller than about 5% of the longest dimension.
- Put the size in the mesh, not in object scale. Origin at the centre of each part's base; the asset stands on Z = 0.
- Cylinders: 16 segments in blockout, 32 to 64 when finished.
- Link every part to one collection named for the asset and parent it to a root empty at the base centre.
- Inset supports from the outer edge by a named constant (20 to 50 mm on furniture) so later bevels do not expose them.
- `look` from front, right and persp. Judge silhouette and ratios only. Fix by editing constants and re-running.

## Code
```python
W, D, H, TOP, LEG, INSET = 1.2, 0.7, 0.75, 0.04, 0.05, 0.03   # metres
col = bpy.data.collections.get("Table") or bpy.data.collections.new("Table")
if col.name not in bpy.context.scene.collection.children:
    bpy.context.scene.collection.children.link(col)

def box(name, size, base):
    """Box with scale 1 and its origin at the centre of the bottom face."""
    me = bpy.data.meshes.get(name) or bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.Translation((0, 0, 0.5)))
    bmesh.ops.scale(bm, vec=size, verts=bm.verts)
    bm.to_mesh(me); bm.free()
    ob = bpy.data.objects.get(name) or bpy.data.objects.new(name, me)
    if ob.name not in col.objects: col.objects.link(ob)
    ob.location = base
    return ob

root = bpy.data.objects.get("Table_Root") or bpy.data.objects.new("Table_Root", None)
if root.name not in col.objects: col.objects.link(root)
parts = [box("Table_Top", (W, D, TOP), (0, 0, H - TOP))]
for tag, sx, sy in (("FL", -1, -1), ("FR", 1, -1), ("BL", -1, 1), ("BR", 1, 1)):   # front is -Y
    x, y = sx * (W / 2 - INSET - LEG / 2), sy * (D / 2 - INSET - LEG / 2)
    parts.append(box("Table_Leg_" + tag, (LEG, LEG, H - TOP), (x, y, 0)))
for ob in parts: ob.parent = root
bpy.context.view_layer.update()
pts = [ob.matrix_world @ Vector(c) for ob in parts for c in ob.bound_box]
result = {"size": [round(max(p[i] for p in pts) - min(p[i] for p in pts), 4) for i in range(3)]}
```

## Avoid
- Sizing with `ob.scale`: `check_mesh` reports unapplied scale and bevels come out uneven.
- Detailing one part before every mass exists: proportions drift and the detail is redone.
- Guessed sizes: the asset looks wrong beside anything else. State the size you chose.

Check: `run_python` returns `size` within 2% of the target and `look` (front, right, persp) shows every mass touching its neighbour, none floating.
