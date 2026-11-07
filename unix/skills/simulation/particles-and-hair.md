---
name: particles-and-hair
domain: simulation
when: Many copies must be scattered over a surface, or points must be emitted over time (sparks, debris, rain).
---

# Particles and Hair
A legacy particle system used either as a static scatter of instances or as an animated emitter.

## Do
- Scatter (rocks, grass, bolts): `type = 'HAIR'` with `render_type = 'OBJECT'`. Hair is static, needs no cache and exists on every frame.
- Instance scale is `particle_size * hair_length`. `hair_length` defaults to 4.0: set it to 1.0 so `particle_size` is the scale. Vary with `size_random` 0.3 to 0.6.
- The source object's location and rotation are ignored; its scale multiplies in, so apply it. Hide the source with `hide_viewport = hide_render = True`: the instances still draw and render.
- Typical counts: 200 to 2,000 for props, 5,000 to 50,000 for grass. Check triangles: count times instance triangles.
- Emitter (sparks, debris): `type = 'EMITTER'`; set `frame_start`, `frame_end`, `lifetime` in frames and `normal_factor` in m/s (1 to 5). Gravity scale is `effector_weights.gravity` (1 = 9.81 m/s2).
- Judge an emitter on a frame inside its life: step to it with `scene.frame_set` in order from `frame_start`, as below.
- New work that needs control per point (density maps, alignment, collisions avoided) is better done with Geometry Nodes: Distribute Points on Faces into Instance on Points. See the `geometry_nodes` skills.

## Code
```python
def particle_settings(obj, name):
    mod = obj.modifiers.get(name) or obj.modifiers.new(name, 'PARTICLE_SYSTEM')
    return mod.particle_system.settings

ground, rock, nozzle = D.objects["Ground"], D.objects["Rock"], D.objects["Nozzle"]
sc = particle_settings(ground, "Scatter")           # static scatter
sc.type, sc.count = 'HAIR', 300
sc.use_advanced_hair = True
sc.render_type, sc.instance_object = 'OBJECT', rock
sc.hair_length, sc.particle_size, sc.size_random = 1.0, 0.5, 0.5
sc.use_rotations, sc.rotation_mode = True, 'NOR'
sc.phase_factor_random = 2.0                        # random spin around the normal

em = particle_settings(nozzle, "Spray")             # animated emitter
em.type, em.count = 'EMITTER', 1000
em.frame_start, em.frame_end, em.lifetime = 1, 50, 50
em.normal_factor, em.factor_random = 3.0, 0.5
for f in range(1, 26):                              # simulate in order up to frame 25
    C.scene.frame_set(f)
dg = C.evaluated_depsgraph_get()
spray = nozzle.evaluated_get(dg).particle_systems["Spray"]
result = {"scatter_instances": sum(1 for i in dg.object_instances
                                   if i.is_instance and i.parent.original == ground),
          "alive_at_25": sum(p.alive_state == 'ALIVE' for p in spray.particles)}
```

## Avoid
- Leaving `hair_length` at 4.0: every instance is four times too big.
- Reading `obj.particle_systems[...].particles` on the original object: it is empty. Use the evaluated object.
- Judging an emitter on frame 1: nothing is born yet, so `look` shows an empty scene.

Check: `run_python` returns `scatter_instances` equal to the count and `alive_at_25` near half the emitter count, and `look` shows instances sitting on the surface at a believable size.
