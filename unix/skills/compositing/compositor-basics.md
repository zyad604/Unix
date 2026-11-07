---
name: compositor-basics
domain: compositing
when: The user wants bloom or glow, a colour grade or a vignette on the rendered image.
---

# Compositor Basics
A small post-processing chain on the render: bloom, colour balance, vignette.

## Do
- Add it last and say so: it changes the user's output, and every `look` render passes through it too.
- Blender 4.x: set `scene.use_nodes = True` and build in `scene.node_tree`. Keep the existing Render Layers and Composite nodes; insert between them.
- Blender 5.x: `scene.node_tree` is gone; the tree is `scene.compositing_node_group` with a Group Output node. Call `api_lookup("Scene")` first.
- Set values on input sockets, found by `identifier`. Names repeat: Color Balance has two inputs named "Lift".
- Bloom first: `glare_type = 'BLOOM'`, threshold 1.0 so only emissive or overexposed pixels glow, strength 0.3 to 1.0, size 0.4 to 0.7.
- Then grade: `'LIFT_GAMMA_GAIN'`, colours within 0.95 to 1.05 of white. More reads as a tint.
- Vignette last: an ellipse mask 0.85 of the frame, blurred by 10 % of the image width in pixels, multiplied in at factor 0.2 to 0.5.
- Name added nodes `FX_...` so a re-run updates them. Switch one off with `node.mute = True`.

## Code
```python
scene = C.scene
scene.use_nodes = True
tree = scene.node_tree
def node(idname, name, x, y=400):
    n = tree.nodes.get(name) or tree.nodes.new(idname)
    n.name, n.location = name, (x, y)
    return n
def sock(n, identifier):
    return next(s for s in n.inputs if s.identifier == identifier)
src, out = (next(n for n in tree.nodes if n.type == t) for t in ('R_LAYERS', 'COMPOSITE'))
glare = node("CompositorNodeGlare", "FX_Bloom", 300)
glare.glare_type = 'BLOOM'
sock(glare, "Highlights Threshold").default_value = 1.0
sock(glare, "Strength").default_value = 0.6
sock(glare, "Size").default_value = 0.5
grade = node("CompositorNodeColorBalance", "FX_Grade", 500)
grade.correction_method = 'LIFT_GAMMA_GAIN'
sock(grade, "Color Lift").default_value = (0.98, 0.99, 1.03, 1.0)   # cool shadows
sock(grade, "Color Gain").default_value = (1.04, 1.01, 0.96, 1.0)   # warm highlights
mask = node("CompositorNodeEllipseMask", "FX_Vignette_Mask", 300, 100)
sock(mask, "Size").default_value = (0.85, 0.85)
soft = node("CompositorNodeBlur", "FX_Vignette_Blur", 500, 100)
sock(soft, "Size").default_value = [0.1 * scene.render.resolution_x * scene.render.resolution_percentage / 100] * 2
mix = node("CompositorNodeMixRGB", "FX_Vignette", 700)
mix.blend_type, mix.inputs[0].default_value = 'MULTIPLY', 0.4
for a, b in ((src.outputs["Image"], sock(glare, "Image")), (glare.outputs["Image"], sock(grade, "Image")),
             (grade.outputs["Image"], mix.inputs[1]), (mask.outputs["Mask"], sock(soft, "Image")),
             (soft.outputs["Image"], mix.inputs[2]), (mix.outputs["Image"], out.inputs["Image"])):
    tree.links.new(a, b)
result = {"nodes": sorted(n.name for n in tree.nodes if n.name.startswith("FX_")), "links": len(tree.links)}
```

## Avoid
- `inputs["Lift"]` on Color Balance: it returns the float "Base Lift", and assigning a colour raises `TypeError`.
- Bloom threshold below 0.5: the whole image glows and looks foggy.
- A blur size copied from another resolution: it is in pixels, so at 4K the vignette edge turns hard.

Check: `look(shading="rendered", use_scene_camera=true)` or `render_image` shows glow around bright areas only and corners darker than the centre; `run_python` returns five `FX_` nodes.
