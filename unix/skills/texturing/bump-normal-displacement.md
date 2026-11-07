---
name: bump-normal-displacement
domain: texturing
when: A surface needs relief (grain, pores, weave, bricks, bark) beyond what its mesh has.
---

# Bump, normal and displacement
Chooses the cheapest relief method that holds up at the viewing distance and wires it by script.

## Do
- Choose by depth. Under 2 mm and never on the silhouette (grain, pores, scratches, weave): Bump. A baked or supplied normal map: Normal Map node. 5 mm and deeper, or visible on the outline (bricks, bark, cobbles): displacement.
- Bump: height texture into Bump, Bump into the Principled Normal. Set `Distance` to the real depth in metres, 0.0002-0.002, and keep Strength at 1.0.
- Bump only shows when features are small: 1 mm depth needs a texture Scale of 100 or more per metre.
- Normal maps: image set to Non-Color, into a Normal Map node, Strength 1.0 (0.5-2.0). `set_material` with `textures: {"normal": path}` does this. Blender expects OpenGL (Y up) maps.
- To combine them, plug the Normal Map output into the Bump node's Normal input.
- Displacement (Cycles and EEVEE): Displacement node into the Material Output, `mat.displacement_method = 'BOTH'`, Scale in metres (0.005-0.1). Midlevel 0.0 for procedural height, 0.5 for image height maps.
- Displacement needs vertices: edge length at most half the feature size. Add a Simple subdivision modifier, levels 4-6.
- Shader displacement does not export. For export use a Displace modifier or bake a normal map.

## Code
```python
obj = D.objects["Wall"]
mat = obj.active_material
nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
out = next(n for n in nt.nodes if n.type == 'OUTPUT_MATERIAL' and n.is_active_output)
def node(name, kind):                    # get-or-create: safe to re-run
    n = nt.nodes.get(name) or nt.nodes.new(kind)
    n.name = n.label = name
    return n
co = node("unix_coord", "ShaderNodeTexCoord")
fine = node("unix_fine", "ShaderNodeTexNoise"); fine.inputs["Scale"].default_value = 300.0
big = node("unix_big", "ShaderNodeTexVoronoi"); big.inputs["Scale"].default_value = 8.0
bump = node("unix_bump", "ShaderNodeBump")
bump.inputs["Distance"].default_value = 0.001          # 1 mm of fine relief
disp = node("unix_disp", "ShaderNodeDisplacement")
disp.inputs["Scale"].default_value = 0.03              # 3 cm of real relief
disp.inputs["Midlevel"].default_value = 0.0
for a, b in ((co.outputs["Object"], fine.inputs["Vector"]), (co.outputs["Object"], big.inputs["Vector"]),
             (fine.outputs[0], bump.inputs["Height"]), (bump.outputs["Normal"], bsdf.inputs["Normal"]),
             (big.outputs[0], disp.inputs["Height"]), (disp.outputs["Displacement"], out.inputs["Displacement"])):
    nt.links.new(a, b)
mat.displacement_method = 'BOTH'                       # default 'BUMP' moves no geometry
sub = obj.modifiers.get("Detail_Subdiv") or obj.modifiers.new("Detail_Subdiv", 'SUBSURF')
sub.subdivision_type = 'SIMPLE'; sub.levels = sub.render_levels = 6
result = {"displacement_method": mat.displacement_method, "subdiv_levels": sub.levels}
```

## Avoid
- Displacement with the default method: the outline stays flat however high the Scale.
- Displacement on a low-poly mesh: the surface shades bumpy but the outline stays straight.
- A normal map left as sRGB: shading tilts everywhere; `check_materials` flags it.

Check: `check_materials` reports no issues, and a `look` with `shading: rendered` at a grazing angle shows a broken outline where displacement was used and no faceting.
