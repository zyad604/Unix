---
name: image-to-3d-by-modelling
domain: reference
when: The user attaches an image and wants that object built in 3D.
---

# Image to 3D by modelling
A clean, editable model that matches the reference image in silhouette, proportions and materials.

## Do
- Open the image with `view_image`. Write down: the parts, the viewing angle, and each part's size as a fraction of the whole (seat at 45% of total height, legs 8% of width).
- Fix one real-world dimension first (a chair is about 0.85 m tall, a mug 0.09 m) and derive every other size from the fractions.
- Block out every part with primitives in one step, named and parented under one root. No detail yet.
- Call `match_reference` with the view closest to the photo angle. List the three largest differences and fix only those. Repeat; three rounds is usual, five is the limit.
- Proportion errors come first, then part positions, then curvature and taper, then bevels and small parts.
- Sample colours from the image for `set_material`; use `asset_material` when the surface is a recognisable real material.
- Hidden sides: assume symmetry for man-made objects and say so in the reply.
- Organic subjects (faces, animals, cloth folds) will only be approximated by this method. Say that up front.

## Code
```python
# Blockout from measured fractions: total height fixed, parts derived from it.
H = 0.85                                    # real-world height chosen for the subject
parts = {                                   # name: (width, depth, height, z of base) as fractions of H
    "Ref_Seat": (0.55, 0.55, 0.08, 0.45),
    "Ref_Back": (0.55, 0.06, 0.47, 0.53),
}
root = bpy.data.objects.get("Ref_Root") or bpy.data.objects.new("Ref_Root", None)
if root.name not in bpy.context.scene.collection.objects:
    bpy.context.scene.collection.objects.link(root)
made = {}
for name, (w, d, h, z) in parts.items():
    old = bpy.data.objects.get(name)
    if old:
        bpy.data.objects.remove(old)
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bmesh.ops.scale(bm, vec=(w * H, d * H, h * H), verts=bm.verts)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    obj.parent = root
    obj.location = (0.0, 0.0, (z + h / 2) * H)
    made[name] = [round(w * H, 3), round(d * H, 3), round(h * H, 3)]
bpy.data.objects["Ref_Back"].location.y = 0.245 * H
result = {"height_m": H, "parts": made}
```

## Avoid
- Detailing before the blockout matches: every later fix moves the detail too.
- Comparing from the wrong angle: a front view against a three-quarter photo hides depth errors.
- Chasing pixel-perfect likeness: stop when the remaining differences are small and name them.

Check: `match_reference` shows the same silhouette and part layout on both sides, and `check_mesh` reports no issues.
