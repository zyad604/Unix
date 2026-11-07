---
name: three-point-studio
domain: lighting
when: Lighting one subject (character, bust, prop, vehicle) with a controlled key, fill and rim.
---

# Three-point studio lighting
A key, fill and rim rig, sized to the subject, placed relative to the camera and aimed at the subject.

## Do
- Set the camera first. Light angles are measured from the camera direction.
- Measure the subject: `size` is its largest bounding-box dimension. Put lights at `d` = 2.5 x size (range 2 to 4).
- Use Area lights. Blender watts are radiant power, not bulb ratings. At view exposure 0 an Area light needs about 10 x d² W to expose mid-grey (range 5 to 20): 60 W for a 1 m prop at 2.5 m, 200 W for a 1.8 m figure at 4.5 m. Point and Spot lights need about 3x that.
- Key: 45° to one side (30 to 60), 40° up (25 to 50), width 1 x size. Width 0.25 x size gives hard shadows, 2 x size very soft ones.
- Fill: other side, 50° round, 10° up, width 2 x size, key / 4 (key / 2 flat, key / 8 moody).
- Rim: behind the subject, diagonally opposite the key, 50° up, width 0.5 x size, 1.5 x key (1 to 3).
- Warm key (1.0, 0.93, 0.85) against cool fill (0.85, 0.92, 1.0) reads as natural.
- Set the world Background strength to 0 to 0.1 so the rig controls the contrast.

## Code
```python
names = ["Subject"]                                   # objects to light
pts = [o.matrix_world @ Vector(c) for o in (D.objects[n] for n in names) for c in o.bound_box]
lo, hi = Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))
centre, size = (lo + hi) / 2, max(hi - lo)
d, cam = 2.5 * size, C.scene.camera
v = cam.matrix_world.translation - centre if cam else Vector((0, -1, 0))
base = math.atan2(v.x, -v.y)                          # camera azimuth round Z
col = D.collections.get("Lights") or D.collections.new("Lights")
if col.name not in C.scene.collection.children:
    C.scene.collection.children.link(col)

def area(name, az, el, watts, width, rgb):            # az: degrees to camera-right
    ob = D.objects.get(name) or D.objects.new(name, D.lights.new(name, 'AREA'))
    if ob.name not in col.objects:
        col.objects.link(ob)
    ob.data.shape, ob.data.size, ob.data.energy, ob.data.color = 'SQUARE', width, watts, rgb
    a, e = base + math.radians(az), math.radians(el)
    ob.location = centre + d * Vector((math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e)))
    ob.rotation_euler = (centre - ob.location).to_track_quat('-Z', 'Y').to_euler()
    return round(watts, 1)

key = 10 * d * d
result = {"distance_m": round(d, 2),
          "Key": area("Key", 45, 40, key, size, (1.0, 0.93, 0.85)),
          "Fill": area("Fill", -50, 10, key / 4, 2 * size, (0.85, 0.92, 1.0)),
          "Rim": area("Rim", -135, 50, key * 1.5, 0.5 * size, (1.0, 1.0, 1.0))}
```

## Avoid
- Typing bulb wattages (1000 W at 2 m): the subject clips to white.
- Equal key and fill: no shadow side, the form looks flat.
- A rim on the camera side of the subject: it lights the front and separates nothing.

Check: `look` with `shading: "rendered"` and `use_scene_camera: true` shows a brighter key side, a shadow side that is darker but not black, a bright rim edge on the shadow side, and no pure-white patches.
