---
name: product-backdrop-lighting
domain: lighting
when: Shooting a product or small object on a clean seamless background (packshot, catalogue, hero image).
---

# Product lighting on a seamless backdrop
A curved sweep behind the product and three large soft lights, built to the product's size and turned to the camera.

## Do
- Set the camera first: 85 mm, 10 to 25° above the product. The sweep and lights turn to face it.
- Sweep: width 6 x product size, floor depth 4 x, wall height 4 x, fillet radius 1 x. The curve hides the floor-to-wall line. Product stands 1.6 x size in from the front edge.
- Give the sweep a matt material with `set_material`: base colour (0.8, 0.8, 0.8), roughness 0.6. Never pure white (1, 1, 1): it clips first.
- Lights are Area lights sized 2 to 3 x the product: big sources give the long soft highlights that read as "studio".
- Top light: above and slightly behind, 3 x size wide. It draws the top edge and lights the sweep.
- Key: 55° to one side, 30° up, 2 x size wide. Fill: other side, low, half the key.
- Watts follow distance: 10 x d² per light. Small products need little: a 0.3 m product takes 3 to 8 W per light.
- Glossy or metal products show the lights as reflections. Add world Background strength 0.3 (0.2 to 0.5) of plain grey so the dark side reflects something.
- For a pure white ground, raise the top light, not the material colour.

## Code
```python
pts = [o.matrix_world @ Vector(c) for o in (D.objects[n] for n in ["Product"]) for c in o.bound_box]
lo, hi = Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))
size, centre, foot = max(hi - lo), (lo + hi) / 2, Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
cam = C.scene.camera
v = cam.matrix_world.translation - centre if cam else Vector((0, -1, 0))
turn = Matrix.Rotation(math.atan2(v.x, -v.y), 4, 'Z')      # local -Y faces the camera
W, R, back, H = 6 * size, size, 2.4 * size, 4 * size
prof = [(-1.6 * size, 0.0)] + [(back - R + R * math.sin(t), R - R * math.cos(t))
        for t in (i * math.pi / 32 for i in range(17))] + [(back, H)]
n = len(prof)
me = D.meshes.get("Backdrop") or D.meshes.new("Backdrop")
me.clear_geometry()
me.from_pydata([(x, y, z) for x in (-W / 2, W / 2) for y, z in prof], [],
               [(i, n + i, n + i + 1, i + 1) for i in range(n - 1)])
me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
drop = D.objects.get("Backdrop") or D.objects.new("Backdrop", me)
drop.matrix_world = Matrix.Translation(foot) @ turn

def soft(name, offset, width, gain):                        # offset in product sizes
    ob = D.objects.get(name) or D.objects.new(name, D.lights.new(name, 'AREA'))
    ob.location = centre + (turn @ Vector(offset)) * size
    ob.rotation_euler = (centre - ob.location).to_track_quat('-Z', 'Y').to_euler()
    ob.data.size, ob.data.energy = width * size, 10 * gain * (ob.location - centre).length_squared
    return ob

made = [drop, soft("Top", (0, 0.6, 2.5), 3, 1), soft("Key", (2.0, -1.4, 1.4), 2, 1), soft("Fill", (-2.2, -1.2, 0.5), 2, 0.5)]
for ob in made:
    if ob.name not in C.scene.collection.all_objects:
        C.scene.collection.objects.link(ob)
result = {"size_m": round(size, 3), "watts": {o.name: round(o.data.energy, 1) for o in made[1:]}}
```

## Avoid
- A flat floor plane and a separate wall: a hard horizon line cuts through the product.
- Small lights (under 1 x product size): pin highlights and hard shadows, the opposite of a packshot.

Check: `look` with `shading: "rendered"` and `use_scene_camera: true` shows only backdrop behind the product with no horizon line, a soft contact shadow under it, and no clipped white on the product.
