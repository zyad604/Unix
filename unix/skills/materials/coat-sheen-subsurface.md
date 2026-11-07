---
name: coat-sheen-subsurface
domain: materials
when: Shading car paint, lacquer, wet surfaces, cloth, skin, wax or marble, where base colour and roughness alone look wrong.
---

# Coat, sheen and subsurface
Adds the second layer of the Principled BSDF that layered and translucent surfaces need.

## Do
- `set_material` does not expose these inputs. Create the material with it, then set them by script.
- Car paint and lacquer: Coat Weight 1.0, Coat Roughness 0.03 (0.0-0.1), Coat IOR 1.5. Under it, base Roughness 0.4 and Metallic 1 for metallic paint, Metallic 0 for solid paint.
- Wet surface: Coat Weight 1.0, Coat Roughness 0.0, and darken the base colour by 30-50%.
- Cloth: Sheen Weight 1.0 for velvet and felt, 0.3-0.5 for cotton; Sheen Roughness 0.3-0.5; Roughness 0.8-1.0.
- Skin, wax, marble, leaves: Subsurface Weight 1.0. Subsurface Scale is the depth light travels in metres: skin 0.005-0.02, wax 0.02-0.05, marble 0.01-0.03. The default 0.05 makes a real-size head look like a candle.
- Subsurface Radius sets depth per colour channel; keep 1.0, 0.2, 0.1 for skin (red travels furthest), use 1, 1, 1 for neutral materials.
- Subsurface needs real scale. Check dimensions with `object_info` first.
- Anisotropic 0.5-0.9 stretches highlights on brushed metal, in Cycles only.

## Code
```python
def layered(name, **inputs):
    mat = D.materials.get(name) or D.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    for key, value in inputs.items():            # Coat_Weight -> "Coat Weight"
        bsdf.inputs[key.replace("_", " ")].default_value = value
    return mat.name

result = [
    layered("Car_Paint_Red", Base_Color=(0.45, 0.01, 0.01, 1.0), Metallic=1.0, Roughness=0.4,
            Coat_Weight=1.0, Coat_Roughness=0.03),
    layered("Velvet_Blue", Base_Color=(0.02, 0.04, 0.25, 1.0), Roughness=0.9,
            Sheen_Weight=1.0, Sheen_Roughness=0.4),
    layered("Skin_Light", Base_Color=(0.80, 0.52, 0.42, 1.0), Roughness=0.45,
            Subsurface_Weight=1.0, Subsurface_Radius=(1.0, 0.2, 0.1), Subsurface_Scale=0.01),
]
```

## Avoid
- Pre-4.0 input names (`Clearcoat`, `Subsurface`, `Sheen`): `KeyError` on Blender 4.x.
- Subsurface on a model built at the wrong scale: a 20 m "head" looks like plastic, a 2 cm one glows through.
- Coat on a rough matte surface such as concrete: it reads as shrink-wrap.

Check: `check_materials` reports no issues, and a `look` with `shading: rendered` shows a sharp highlight over the paint, a soft rim on the cloth and soft shadow edges on the skin.
