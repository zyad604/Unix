---
name: realistic-from-reference
domain: modeling
when: The user wants a real-world object to look realistic or accurate, or says a result looks fake, blocky or wrong.
---

# Realistic objects from real references
A model that reads as the real thing in a close-up: true outline, real parts, three levels of form, more than one surface.

## Do
- Look before building: `reference_search` for the exact thing. Keep one square-on photo on a plain background and two or three from other angles. Note the real size, the parts, and for every feature which side and which end it is on.
- Start from the real outline: `trace_reference` on the square-on photo at real size. Keep that solid hidden as the measuring stick.
- Big parts: `profile_solid` in photo fractions, each a loose region drawn around the part and running outside the silhouette. It follows the true outline; your lines only mark where parts meet. Give each its real thickness and a rounded section: `round_m` 20% of the thickness for a machined block, 35% for a cast or moulded body, 50% for a grip, handle or limb (an oval).
- Parts differ in width, and along their own length too: `width_profile` tapers a stock, a blade, a handle. Never give every part the same thickness.
- Round parts (barrels, tubes, rods, wheels, knobs) are `lathe_solid` between two points on the photo, with `bore_m` when the real one is hollow and `radii` for steps and collars.
- Medium forms next: steps, recesses, openings and grooves, cut with boolean cutters placed at fractions read off the photo.
- Small forms last: every button, lever, sight and rib is its own `profile_solid`, placed so it shows (toward the front-view viewer is -Y). For each distinctive one, `reference_search` a close-up first ('<thing> <part>') and build what it shows.
- Check every side with `look_around`, and the outline with `match_reference(view='front')`, which measures overlap and openings.
- Surface: parts placed by the photo carry its surface already. Give `material` for roughness and metal (0.25 polished, 0.5 painted, 0.8 moulded); use `project_reference` only for meshes made another way.

## Code
```python
body = bpy.data.objects["Body_Upper"]            # a part that needs grooves, slots or vents cut into it
count, first_x, pitch = 7, 0.060, 0.004          # read off the photo: how many, where the first is, how far apart
depth, width = 0.0012, 0.0018                    # groove depth into the side, groove width along the part
zs = [(body.matrix_world @ v.co).z for v in body.data.vertices]
ys = [(body.matrix_world @ v.co).y for v in body.data.vertices]
mesh = bpy.data.meshes.new("Body_Grooves")
bm = bmesh.new()
for k in range(count):
    for side in (min(ys), max(ys)):              # both sides of the part
        made = bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=(width, depth * 2, (max(zs) - min(zs)) * 0.9), verts=made["verts"])
        bmesh.ops.translate(bm, vec=(first_x + k * pitch, side, (min(zs) + max(zs)) / 2), verts=made["verts"])
bm.to_mesh(mesh)
bm.free()
cutter = bpy.data.objects.new("Body_Grooves", mesh)
bpy.context.scene.collection.objects.link(cutter)
cutter.hide_render = cutter.hide_viewport = True  # the cutter only cuts
cut = body.modifiers.new("Grooves", 'BOOLEAN')
cut.operation, cut.object, cut.solver = 'DIFFERENCE', cutter, 'EXACT'
result = {"grooves": count * 2, "modifier": cut.name}
```

## Avoid
- Slabs: parts of one thickness with soft edges. Right from the side, a stack of boxes from the end.
- Features placed from memory: they land on the wrong side or the wrong end. Read each position off a photo.
- Drawing a big part's outline by hand: it comes out wrong and gets rebuilt. Let it follow the traced outline.
- Leaving real parts to the photo texture: a sight or a lever that is only painted on is missing from every other side.

Check: `look_around` shows no side that reads as stacked boxes, and `match_reference` with the traced photo and `view='front'` reports an outline overlap of 95% or more with openings as large as in the photo.
