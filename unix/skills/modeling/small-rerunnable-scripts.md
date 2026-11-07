---
name: small-rerunnable-scripts
domain: modeling
when: Before writing the first build script of a task, or after a script failed halfway or created duplicates.
---

# Small, re-runnable build scripts
Scripts that can run twice and leave the same scene, so a failed step is fixed by editing and re-running.

## Do
- One step per `run_python`: one part, one modifier stack or one operation, under about 80 lines. Split anything longer.
- Parameters first, as named constants in meters. Change a constant and re-run; do not patch the result with a second script.
- Get or create every datablock by name: mesh, object, collection, modifier, material, node group.
- Refill mesh data in place with `bm.to_mesh(existing_mesh)`. The object keeps its modifiers, materials, parent and any transform the user gave it.
- Never rely on the active object or the selection. Look objects up by name, and read `ob.name` back: Blender appends `.001` when a name is taken.
- Free every bmesh in `finally`, so a failed build does not leak it.
- Do not loop in Python over more than about 100,000 elements. Use `foreach_get` / `foreach_set`, a modifier or geometry nodes.
- Return names, sizes and counts in `result` and compare them with the `changes` block.

## Code
```python
def ensure_collection(name):
    col = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    if col.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(col)
    return col

def ensure_mesh_object(name, col, build):
    """Create or refresh a mesh object. `build(bm)` fills an empty bmesh."""
    me = bpy.data.meshes.get(name) or bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        build(bm)
        bm.to_mesh(me)                      # replaces the geometry, keeps the datablock and its users
    finally:
        bm.free()
    ob = bpy.data.objects.get(name) or bpy.data.objects.new(name, me)
    if ob.name not in col.objects:
        col.objects.link(ob)
    return ob

def ensure_modifier(ob, name, kind):
    return ob.modifiers.get(name) or ob.modifiers.new(name, kind)

RADIUS, HEIGHT = 0.04, 0.09                 # metres
col = ensure_collection("Mug")
body = ensure_mesh_object("Mug_Body", col, lambda bm: bmesh.ops.create_cone(
    bm, cap_ends=True, segments=32, radius1=RADIUS, radius2=RADIUS, depth=HEIGHT,
    matrix=Matrix.Translation((0, 0, HEIGHT / 2))))
ensure_modifier(body, "Bevel", 'BEVEL').width = 0.002
result = {"object": body.name, "size": list(body.dimensions), "modifiers": [m.name for m in body.modifiers]}
```

## Avoid
- `bpy.data.objects.new` without a lookup: every run adds `Mug_Body.001`, `.002`, stacked in one place.
- Deleting and recreating an object to change it: its modifiers, materials and parenting are lost.
- One 200-line script for a whole asset: it fails near the end and leaves the scene half built.

Check: run the script twice with `run_python`; the second reply's `changes` lists nothing under `created`, and `scene_info` shows no `.001` names.
