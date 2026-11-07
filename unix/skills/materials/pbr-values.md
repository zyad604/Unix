---
name: pbr-values
domain: materials
when: Choosing base colour, metallic and roughness for a real-world surface on the Principled BSDF.
---

# PBR values
Gives measured starting values so materials read as the right substance under any light.

## Do
- Metallic is 0 or 1. Metals get a bright base colour (0.5-1.0); everything else is 0.
- Non-metal base colour stays between 0.02 (charcoal) and 0.9 (fresh snow). Pure black or white breaks lighting.
- Colours here and in `set_material` are linear. Convert hex or colour-picker values first: sRGB 0.5 is linear 0.214.
- Roughness: 0.05-0.2 polished, 0.3-0.5 satin, 0.6-0.9 matte. Vary it by 0.1 across large surfaces.
- Leave IOR at 1.5 and Specular IOR Level at 0.5 for all opaque solids.

| Material | Base colour (linear) | Metallic | Roughness |
|---|---|---|---|
| Steel, iron | 0.56 0.57 0.58 | 1 | 0.35 |
| Aluminium | 0.91 0.92 0.92 | 1 | 0.30 |
| Chrome | 0.55 0.55 0.55 | 1 | 0.05 |
| Gold | 1.00 0.77 0.34 | 1 | 0.20 |
| Copper | 0.95 0.64 0.54 | 1 | 0.25 |
| Brass | 0.91 0.78 0.42 | 1 | 0.30 |
| Glossy plastic | any, max 0.8 | 0 | 0.25 |
| Rubber, tyre | 0.02 0.02 0.02 | 0 | 0.80 |
| Varnished oak | 0.40 0.22 0.10 | 0 | 0.40 |
| Concrete | 0.50 0.50 0.48 | 0 | 0.85 |
| Red brick | 0.35 0.12 0.08 | 0 | 0.80 |
| White wall paint | 0.80 0.80 0.78 | 0 | 0.60 |
| Glazed ceramic | 0.85 0.85 0.82 | 0 | 0.10 |
| Cotton fabric | any, max 0.8 | 0 | 0.90 |

## Code
```python
def lin(c):                              # sRGB 0-1 channel -> linear
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

PRESETS = {                              # name: (linear rgb, metallic, roughness)
    "Steel": ((0.56, 0.57, 0.58), 1.0, 0.35),
    "Oak_Varnished": ((0.40, 0.22, 0.10), 0.0, 0.40),
    "Plastic_Red": (tuple(lin(v / 255) for v in (200, 30, 30)), 0.0, 0.25),   # from hex C81E1E
}
for name, (rgb, metallic, rough) in PRESETS.items():
    mat = D.materials.get(name) or D.materials.new(name)
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED')
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Metallic"].default_value = metallic
    bsdf.inputs["Roughness"].default_value = rough
    mat.diffuse_color = (*rgb, 1.0)      # colour shown in solid view
result = {n: [round(c, 3) for c in v[0]] for n, v in PRESETS.items()}
```

## Avoid
- Metallic 0.5 on a clean surface: it reads as neither metal nor paint.
- Pasting a hex colour as linear: the material renders washed out and too bright.

Check: `check_materials` reports no issues, and `look` with `shading: rendered` shows metals reflecting and non-metals not.
