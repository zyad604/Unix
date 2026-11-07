---
name: set-material-or-nodes
domain: materials
when: Deciding whether a material needs only `set_material` or a scripted node tree, or extending a material that already exists.
---

# set_material or a node script
Picks the cheapest correct way to make a material and extends existing ones without breaking them.

## Do
- Use `set_material` for every uniform or image-textured surface. One call sets base colour, metallic, roughness, emission, alpha, transmission and IOR, wires texture files with the right colour space, and assigns to objects.
- Reuse the material name to update it in place; every object sharing it changes together.
- Use `run_python` only for what `set_material` cannot do: procedural nodes, mixes and masks, Coat, Sheen and Subsurface inputs, EEVEE material flags, per-face slots.
- Create with `set_material` first, then extend by script. Keep one Principled BSDF wired to the output: exporters read only that, and `check_materials` flags a material without one.
- Find nodes by type (`n.type == 'BSDF_PRINCIPLED'`), not by the name "Principled BSDF".
- Give every node you add a name and get-or-create by it, so a re-run changes values instead of adding copies.
- Several materials on one mesh: append slots, then set `polygon.material_index` per face.
- Name materials after the substance (`Oak_Varnished`), never `Material.001`. Share one material across all objects of that substance.

## Code
```python
obj = D.objects["Mug"]
mat = D.materials["Ceramic_White"]       # made earlier with set_material
nt = mat.node_tree
bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
def node(name, kind):                    # get-or-create: safe to re-run
    n = nt.nodes.get(name) or nt.nodes.new(kind)
    n.name = n.label = name
    return n
noise = node("unix_rough_noise", "ShaderNodeTexNoise")
noise.inputs["Scale"].default_value = 30.0
rng = node("unix_rough_range", "ShaderNodeMapRange")
rng.inputs["To Min"].default_value, rng.inputs["To Max"].default_value = 0.08, 0.20
nt.links.new(noise.outputs[0], rng.inputs["Value"])
nt.links.new(rng.outputs[0], bsdf.inputs["Roughness"])
# Second material on the top faces: add a slot, then index faces into it.
rim = D.materials.get("Ceramic_Blue") or D.materials.new("Ceramic_Blue")
rim.use_nodes = True
if obj.data.materials.find(rim.name) < 0: obj.data.materials.append(rim)
slot = obj.data.materials.find(rim.name)
top = max(v.co.z for v in obj.data.vertices) - 0.005
for p in obj.data.polygons:
    if p.center.z > top: p.material_index = slot
result = {"slots": [m.name for m in obj.data.materials], "nodes": len(nt.nodes)}
```

## Avoid
- Rebuilding a whole node tree by script for a plain colour: more code, more failure points, same result as one `set_material` call.
- `nodes.clear()` on a material that `set_material` textured: the image nodes and their colour-space setup are gone.
- A new material per object for the same substance: later edits miss some objects, and `check_materials` lists the leftovers as unused.

Check: `check_materials` reports no issues (no empty slots, every material has a Principled BSDF, no unused materials you created), and `object_info` shows the slots you intended.
