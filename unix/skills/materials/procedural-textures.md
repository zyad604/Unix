---
name: procedural-textures
domain: materials
when: A surface needs pattern or variation (wood grain, brushed metal, plaster, speckle) and no image texture is available.
---

# Procedural textures
Builds a node texture by script that drives base colour, roughness and bump from one pattern.

## Do
- Use Object texture coordinates: units are metres, no UVs needed, and Scale means features per metre.
- Chain: Texture Coordinate, Mapping, texture, then a ColorRamp for colour and a Map Range for roughness.
- Never feed raw 0-1 noise into Roughness. Remap it to a band about 0.15 wide, such as 0.25-0.40.
- Noise Scale: 2-10 for blotches, 30-80 for plaster grain, 200-400 for fine grain. Detail 2-8.
- Brushed metal: stretch Noise with Mapping Scale (300, 300, 3) so streaks run along local Z; Metallic 1, roughness 0.25-0.45, bump depth 0.2 mm.
- Wood: Wave Texture, Scale 6-12, Distortion 4-8, Detail 3; Mapping Scale (1, 0.1, 1) puts the grain along local Y. Colours from 0.15 0.07 0.03 to 0.45 0.26 0.12.
- Rotate the Mapping node, not the object, to turn the grain.
- Procedural nodes do not export. Load `bake-to-image` before `export_file`.

## Code
```python
def build(name, tex, scale, dark, light, rough, metallic=0.0, depth_m=0.001, **tex_inputs):
    mat = D.materials.get(name) or D.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()                     # fully procedural: rebuild, never stack duplicates
    kinds = dict(co="TexCoord", mp="Mapping", tx=tex, ramp="ValToRGB", rng="MapRange",
                 bump="Bump", bsdf="BsdfPrincipled", out="OutputMaterial")
    N = {k: nt.nodes.new("ShaderNode" + v) for k, v in kinds.items()}
    for i, n in enumerate(N.values()): n.location = (220 * i, 0)
    N["mp"].inputs["Scale"].default_value = scale
    for key, value in tex_inputs.items(): N["tx"].inputs[key.replace("_", " ")].default_value = value
    stops = N["ramp"].color_ramp.elements
    stops[0].color, stops[1].color = (*dark, 1.0), (*light, 1.0)
    N["rng"].inputs["To Min"].default_value, N["rng"].inputs["To Max"].default_value = rough
    N["bump"].inputs["Distance"].default_value = depth_m      # relief depth in metres
    N["bsdf"].inputs["Metallic"].default_value = metallic
    fac = next(o for o in N["tx"].outputs if o.type == 'VALUE')     # scalar output of any texture
    for a, b in ((N["co"].outputs["Object"], N["mp"].inputs["Vector"]), (N["mp"].outputs["Vector"], N["tx"].inputs["Vector"]),
                 (fac, N["ramp"].inputs[0]), (fac, N["rng"].inputs["Value"]), (fac, N["bump"].inputs["Height"]),
                 (N["ramp"].outputs["Color"], N["bsdf"].inputs["Base Color"]), (N["rng"].outputs[0], N["bsdf"].inputs["Roughness"]),
                 (N["bump"].outputs["Normal"], N["bsdf"].inputs["Normal"]), (N["bsdf"].outputs["BSDF"], N["out"].inputs["Surface"])):
        nt.links.new(a, b)
    return mat.name

result = [
    build("Steel_Brushed", "TexNoise", (300, 300, 3), (0.50, 0.51, 0.52), (0.60, 0.61, 0.62), (0.25, 0.45), 1.0, 0.0002, Scale=1.0, Detail=4.0),
    build("Oak_Grain", "TexWave", (1, 0.1, 1), (0.15, 0.07, 0.03), (0.45, 0.26, 0.12), (0.35, 0.50), Scale=8.0, Distortion=6.0, Detail=3.0),
    build("Plaster", "TexNoise", (1, 1, 1), (0.62, 0.60, 0.56), (0.80, 0.79, 0.75), (0.75, 0.90), depth_m=0.003, Scale=50.0, Detail=8.0),
]
```

## Avoid
- Generated coordinates on a long object: the pattern stretches with the bounding box.
- Re-running a script that adds nodes without clearing or reusing them: stacked duplicate nodes.

Check: `check_materials` reports no issues, and a `look` with `shading: rendered` shows the pattern at the intended size with no stretching.
