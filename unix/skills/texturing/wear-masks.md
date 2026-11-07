---
name: wear-masks
domain: texturing
when: A clean material should look used: chipped paint on edges, exposed metal, dirt or dust.
---

# Wear masks
Builds an edge-wear mask that renders in EEVEE and Cycles and blends paint into bare metal with it.

## Do
- Edge wear is a 0-1 mask that blends paint into bare metal on convex edges.
- Do not build it from Geometry Pointiness or the Bevel node: both are Cycles-only, so `look` shows nothing. Store convexity in a colour attribute (code below).
- The mask is per vertex. Bevel worn edges with 2 or more segments first, or wear smears across whole faces.
- Multiply the mask by Noise (Scale 30-60) and tighten it with a ColorRamp whose stops sit 0.05-0.15 apart: chips, not a gradient.
- Drive base colour (paint to steel 0.56 0.57 0.58), Metallic (0 to 1) and Roughness (0.5 to 0.3) from the one mask.
- Dust on top faces: Geometry Normal Z through a Map Range 0.5-1.0, mixing a lighter colour at 20-40%.
- Dirt in hollows: bake AO with `bake-to-image` and use it inverted as the mask.
- Keep the worn area to 5-15% of the surface.
- Address a Mix node set to Color by index: A `inputs[6]`, B `inputs[7]`, result `outputs[2]`.

## Code
```python
obj = D.objects["Crate"]; me = obj.data
bm = bmesh.new(); bm.from_mesh(me)
wear = [max(0.0, min(1.0, max([e.calc_face_angle_signed(0.0) for e in v.link_edges] or [0.0]) / math.radians(45)))
        for v in bm.verts]               # 1.0 on convex edges, 0.0 on flats and hollows
bm.free()
attr = me.color_attributes.get("wear") or me.color_attributes.new("wear", 'FLOAT_COLOR', 'POINT')
attr.data.foreach_set("color", [c for w in wear for c in (w, w, w, 1.0)])
mat = obj.active_material; nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
def node(name, kind):
    n = nt.nodes.get(name) or nt.nodes.new(kind)
    n.name = n.label = name
    return n
src = node("unix_wear_attr", "ShaderNodeAttribute"); src.attribute_name = "wear"
noise = node("unix_wear_noise", "ShaderNodeTexNoise"); noise.inputs["Scale"].default_value = 40.0
mul = node("unix_wear_mul", "ShaderNodeMath"); mul.operation = 'MULTIPLY'
ramp = node("unix_wear_ramp", "ShaderNodeValToRGB")
ramp.color_ramp.elements[0].position, ramp.color_ramp.elements[1].position = 0.30, 0.40
mix = node("unix_wear_mix", "ShaderNodeMix"); mix.data_type = 'RGBA'
mix.inputs[6].default_value = bsdf.inputs["Base Color"].default_value[:]   # A: paint
mix.inputs[7].default_value = (0.56, 0.57, 0.58, 1.0)                      # B: bare steel
for a, b in ((src.outputs["Fac"], mul.inputs[0]), (noise.outputs[0], mul.inputs[1]), (mul.outputs[0], ramp.inputs[0]),
             (ramp.outputs[0], mix.inputs[0]), (mix.outputs[2], bsdf.inputs["Base Color"]), (ramp.outputs[0], bsdf.inputs["Metallic"])):
    nt.links.new(a, b)
result = {"worn_verts": sum(w > 0.5 for w in wear), "verts": len(wear)}
```

## Avoid
- Wear on an unbevelled 8-vertex cube: every vertex is an edge, so the whole object turns to metal.
- Raw convexity as the mask, without noise: even stripes along every edge that look airbrushed.
- Changing the mesh after computing the attribute: the mask no longer matches. Recompute it.

Check: a `look` with `shading: rendered` shows bare metal only along convex edges, broken into chips, covering well under a quarter of the surface; `check_materials` reports no issues.
