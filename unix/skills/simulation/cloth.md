---
name: cloth
domain: simulation
when: Fabric must drape, hang or fall over other objects: tablecloths, flags, curtains, sheets.
---

# Cloth
A baked cloth simulation that drapes over its colliders without passing through them.

## Do
- Mesh: an even grid of quads, 0.02 to 0.05 m per quad. A 1.4 m tablecloth needs 30 to 60 cuts per side. Too coarse cannot fold; scale must be (1, 1, 1).
- Start the cloth 0.02 to 0.1 m above the collider, not touching it.
- Every object the cloth touches needs a Collision modifier. Set `obj.collision.thickness_outer` to 0.01 to 0.02 m.
- Material presets (mass kg per vertex, tension = compression, bending): silk 0.15, 5, 0.05. Cotton 0.3, 15, 0.5. Denim 1.0, 40, 10. Leather 0.4, 80, 150.
- `quality` 5 is the default; use 8 to 12 when cloth passes through colliders. Collision `distance_min` 0.01 to 0.015 m.
- Self collision makes the bake 2 to 3 times slower. Turn it on only for cloth that folds on itself, with `self_distance_min` under half a quad edge.
- Pin with a vertex group: `settings.vertex_group_mass = "Pin"` (weight 1 holds the vertex).
- Stack order: Cloth first, then Subdivision (level 1 to 2) and Solidify for thickness.
- Bake a short range (40 to 100 frames), then move to the frame that looks settled.

## Code
```python
scene = C.scene
cloth, table = D.objects["Tablecloth"], D.objects["Table_Top"]
if table.modifiers.get("Collision") is None:
    table.modifiers.new("Collision", 'COLLISION')
table.collision.thickness_outer = 0.01
mod = cloth.modifiers.get("Cloth") or cloth.modifiers.new("Cloth", 'CLOTH')
s = mod.settings
s.quality, s.mass = 8, 0.3                           # cotton
s.tension_stiffness = s.compression_stiffness = 15.0
s.bending_stiffness = 0.5
mod.collision_settings.distance_min = 0.01
mod.collision_settings.use_self_collision = False
cache = mod.point_cache
cache.frame_start, cache.frame_end = 1, 40
with C.temp_override(scene=scene, active_object=cloth, object=cloth, point_cache=cache):
    if cache.is_baked:
        bpy.ops.ptcache.free_bake()                  # settings changed: the old bake is stale
    bpy.ops.ptcache.bake(bake=True)
scene.frame_set(40)
ev = cloth.evaluated_get(C.evaluated_depsgraph_get())
zs = [(ev.matrix_world @ v.co).z for v in ev.data.vertices]
result = {"baked": cache.is_baked, "frame": scene.frame_current,
          "min_z": round(min(zs), 3), "max_z": round(max(zs), 3)}
```

## Avoid
- No Collision modifier on the table: the cloth falls straight through.
- Cloth starting inside or touching the collider: it sticks, jitters or shoots away on frame 2.
- Reading `cloth.data.vertices` for the result: that is the rest shape. Use the evaluated object.

Check: `run_python` returns `baked: true`, `max_z` within 0.03 m above the collider top and `min_z` below it, and `look` shows folds hanging at the edges with no table poking through.
