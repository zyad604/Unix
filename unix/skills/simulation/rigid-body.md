---
name: rigid-body
domain: simulation
when: Solid objects must fall, collide, stack, tumble or be knocked over.
---

# Rigid Body
A rigid body simulation that runs from frame 1 and leaves objects resting where physics puts them.

## Do
- Prepare each mesh: scale (1, 1, 1), origin at the centre of the volume. The origin is the centre of mass.
- Create the world once with `bpy.ops.rigidbody.world_add()`; it has no data API. Its collection starts as `None`: create one.
- Linking an object to the world collection creates `obj.rigid_body`. No per-object operator is needed.
- `'ACTIVE'` bodies move, `'PASSIVE'` bodies only collide. For a body moved by keyframes use `'PASSIVE'` with `kinematic = True`.
- Shapes: `'BOX'` and `'SPHERE'` are fastest and most stable. `'CONVEX_HULL'` for other convex forms. `'MESH'` only on passive concave objects such as bowls and terrain.
- Mass in kilograms: mug 0.3, wooden crate 5 to 20. Friction 0.5 (0.2 ice to 0.9 rubber). Restitution 0 to 0.2 for wood and stone, 0.8 for a ball.
- `'MESH'` shapes rest 0.04 m above surfaces (the default `collision_margin`); set it to 0.001. `'BOX'` leaves no gap. For objects under 0.2 m or fast ones, raise `substeps_per_frame` from 10 to 30.
- Start with no overlaps: leave at least 1 mm between bodies or they explode apart on frame 2.
- The sim advances frame by frame from the cache start. Step every frame in order, then stay on the frame to show.

## Code
```python
scene = C.scene
if scene.rigidbody_world is None:
    bpy.ops.rigidbody.world_add()
world = scene.rigidbody_world
if world.collection is None:
    world.collection = D.collections.new("RigidBodyWorld")
world.substeps_per_frame, world.solver_iterations = 10, 10

def make_body(obj, kind, shape, mass=1.0):
    if obj.name not in world.collection.objects:
        world.collection.objects.link(obj)          # this creates obj.rigid_body
    body = obj.rigid_body
    body.type, body.collision_shape, body.mass = kind, shape, mass
    body.friction, body.restitution = 0.5, 0.1
    return body

make_body(D.objects["Floor"], 'PASSIVE', 'BOX')
crates = [o for o in scene.objects if o.name.startswith("Crate_")]
for o in crates:
    make_body(o, 'ACTIVE', 'BOX', mass=10.0)
END = 60
for f in range(world.point_cache.frame_start, END + 1):    # in order, no skipped frames
    scene.frame_set(f)
result = {o.name: [round(v, 3) for v in o.matrix_world.translation] for o in crates}
result["frame"] = scene.frame_current
```

## Avoid
- Unapplied or non-uniform scale: the collision shape does not match the mesh and objects hover or sink.
- `'MESH'` shape on active bodies: jitter, tunnelling and a slow sim.
- Jumping straight to frame 60 with `frame_set`: nothing has moved, because no frames were simulated.

Check: `run_python` returns each crate's Z at its resting height (half its height above the surface below, within 0.01 m), and `look` at the end frame shows no floating or interpenetrating bodies.
