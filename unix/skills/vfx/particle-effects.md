---
name: particle-effects
domain: vfx
when: Scripting a one-shot burst, a trail or falling debris with legacy particles and force fields.
---

# Particle effects for VFX
A legacy EMITTER particle system tuned for a burst, a trail or debris, shaped by a force field.

## Do
- Burst: `frame_start` and `frame_end` within 1-3 frames of each other, `count` 100-300, `lifetime` 30-50, `lifetime_random` 0.3. All particles leave at once and die together.
- Trail: `frame_end` far from `frame_start` (the whole shot), low `count` per frame, short `lifetime` 10-20, and parent the emitter to the moving object so points drop behind it.
- Debris: `render_type = 'OBJECT'` with an instanced mesh, `particle_size = 1.0`, `size_random` 0.5, `effector_weights.gravity = 1.0`, and `use_rotations = True` so chunks tumble.
- Speed is `normal_factor` (m/s along the emitter normals) plus `factor_random`; 2-5 reads as an energetic burst.
- Hide the emitter mesh from the render with `emitter.show_instancer_for_render = False`; instances still render. Hide the instance source with `hide_render = hide_viewport = True`.
- Shape motion with a force field: add one with `bpy.ops.object.effector_add(type='TURBULENCE')` (tumbling), `'WIND'` (drift) or `'VORTEX'` (swirl), then set `field.strength` 1-5. An empty's `.field` is None until the operator adds it.
- Simulate in frame order with `scene.frame_set` from `frame_start`; read particles from the evaluated object, never the original.

## Code
```python
import bmesh
me = D.meshes.new("Debris"); bm = bmesh.new()
bmesh.ops.create_cube(bm, size=0.12); bm.to_mesh(me); bm.free()
deb = D.objects.new("Debris", me); C.scene.collection.objects.link(deb)
deb.hide_render = deb.hide_viewport = True
em = D.objects["Cube"]
ps = em.modifiers.new("Burst", 'PARTICLE_SYSTEM').particle_system.settings
ps.type, ps.count = 'EMITTER', 150
ps.frame_start, ps.frame_end, ps.lifetime = 1, 3, 45
ps.lifetime_random, ps.normal_factor, ps.factor_random = 0.3, 4.0, 2.0
ps.render_type, ps.instance_object = 'OBJECT', deb
ps.particle_size, ps.size_random = 1.0, 0.5
ps.use_rotations, ps.rotation_mode = True, 'VEL'
ps.effector_weights.gravity = 1.0
em.show_instancer_for_render = False
bpy.ops.object.effector_add(type='TURBULENCE', location=(0, 0, 2))  # adds the field object
turb = C.active_object
turb.field.strength, turb.field.size = 3.0, 1.5
for f in range(1, 26):
    C.scene.frame_set(f)
dg = C.evaluated_depsgraph_get()
psys = em.evaluated_get(dg).particle_systems["Burst"]
result = {"alive_at_25": sum(p.alive_state == 'ALIVE' for p in psys.particles)}
```

## Avoid
- Jumping `scene.frame_set` straight to a late frame: the cache is empty and few or no particles exist. Step in order.
- Reading `em.particle_systems[...].particles` on the original object: it is empty; use the evaluated object.
- Leaving the emitter mesh visible: it renders as a solid blob around the burst.

Check: `run_python` returns `alive_at_25` near half the count, and `look` with `shading: "rendered"` on a mid-life frame shows the debris spread and lit.
