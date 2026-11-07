---
name: terrain-heightfield
domain: terrain
when: The scene needs natural ground: hills, a landscape backdrop, or an uneven ground plane for an outdoor shot.
---

# Terrain from a noise heightfield
A real-size terrain grid with believable relief, a flat pad where things stand, and a slope attribute for materials and scattering.

## Do
- Size it for the shot: 50 to 200 m around a subject, 1 to 5 km for a backdrop. Extend it past the camera's view so no edge shows.
- Vertex spacing: 0.25 to 0.5 m near the camera, 1 to 2 m mid-ground, 5 to 10 m for a backdrop. Stay under 250,000 vertices (500 x 500).
- Relief: widest feature 1/3 to 1/2 of the tile, 5 to 7 octaves. Height is 10 to 25% of the feature width for hills, 30 to 50% for mountains.
- Normalise the noise so the height range is the number you chose.
- Flatten a pad where objects stand and blend it out over 10 to 20 m.
- Store slope in degrees as a point attribute: grass below 25, rock above 35, scatter only under 30.
- Shade smooth. No Subdivision modifier: it melts the fine octaves. Detail under 0.5 m goes in the material.

## Code
```python
SIZE, RES = 200.0, 129                    # metres across, vertices per side (1.56 m spacing)
FEATURE, HEIGHT, SEED = 80.0, 16.0, 3     # widest hill, valley-to-peak height, variation
PAD_R, PAD_BLEND = 8.0, 12.0              # flat pad radius at the origin, blend width
me = bpy.data.meshes.get("Terrain") or bpy.data.meshes.new("Terrain")
bm = bmesh.new()
bm.loops.layers.uv.new("UVMap")
bmesh.ops.create_grid(bm, x_segments=RES - 1, y_segments=RES - 1, size=SIZE / 2, calc_uvs=True)
noise = lambda co: mathutils.noise.fractal(Vector((co.x / FEATURE, co.y / FEATURE, SEED * 7.3)), 1.0, 2.0, 6)
raw = [noise(v.co) for v in bm.verts]
lo, k = min(raw), HEIGHT / (max(raw) - min(raw))               # normalise to exactly HEIGHT
pad = (noise(Vector((0, 0, 0))) - lo) * k                      # ground level under the pad
for v, h in zip(bm.verts, raw):
    t = min(1.0, max(0.0, (v.co.xy.length - PAD_R) / PAD_BLEND))
    v.co.z = ((h - lo) * k - pad) * t * t * (3 - 2 * t)        # smoothstep; the pad ends at Z = 0
for f in bm.faces: f.smooth = True
bm.to_mesh(me); bm.free()
slope = me.attributes.get("slope") or me.attributes.new("slope", 'FLOAT', 'POINT')
degs = [math.degrees(math.acos(max(-1.0, min(1.0, v.normal.z)))) for v in me.vertices]
slope.data.foreach_set("value", degs)
ob = bpy.data.objects.get("Terrain") or bpy.data.objects.new("Terrain", me)
if not ob.users_collection: bpy.context.scene.collection.objects.link(ob)
zs = [v.co.z for v in me.vertices]
result = {"verts": len(me.vertices), "z_range": [round(min(zs), 2), round(max(zs), 2)],
          "max_slope_deg": round(max(degs), 1)}
```

## Avoid
- Height above half the feature width: spikes and slopes over 60 degrees that read as noise, not land.
- A single noise octave: smooth blobs with no ridges.
- A terrain that ends inside the frame: a straight edge shows on the horizon.

Check: `run_python` returns `z_range` spanning HEIGHT and `max_slope_deg` under 45 for hills, and `look` (persp, and the scene camera if there is one) shows varied relief, a flat pad at the origin and no visible edge.
