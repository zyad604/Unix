---
name: glass-emission-alpha
domain: materials
when: A surface must refract (glass, water, gems), glow (screens, neon, lamps) or be see-through (cutouts, fades).
---

# Glass, emission and alpha
Sets up refracting, glowing and transparent materials so they work in both EEVEE and Cycles.

## Do
- Glass: Transmission Weight 1.0, Roughness 0.0 (frosted 0.2-0.5), Metallic 0, base colour 0.9-1.0 or a light tint. IOR: water 1.33, window glass 1.45, crown glass 1.52, diamond 2.42.
- Glass needs thickness. Model panes as solids 3-6 mm thick with normals out; a single-sided plane refracts like an endless block.
- EEVEE refracts only when `mat.use_raytrace_refraction` and `scene.eevee.use_raytracing` are both True; with either off the glass shows only the world colour. `set_material` sets neither. The scene flag is a render setting: tell the user you enabled it.
- Emission: set Emission Color and Emission Strength. Strength 1-5 for screens and indicator LEDs, 5-20 for neon and light strips, 20-100 for bare bulbs.
- Emissive surfaces light their surroundings in Cycles. In EEVEE they do so only with `scene.eevee.use_raytracing` on and the emitter in view; otherwise put a real light of the same colour next to them.
- Alpha is for cutouts and fades, not glass: it does not bend light.
- `surface_render_method`: `'DITHERED'` (default) for texture cutouts such as leaves and grilles, sorts correctly; `'BLENDED'` for a smooth uniform fade at alpha 0.2-0.6. `set_material` with `alpha` below 1 picks `'BLENDED'`.

## Code
```python
def principled(name):
    mat = D.materials.get(name) or D.materials.new(name)
    mat.use_nodes = True
    return mat, next(n for n in mat.node_tree.nodes if n.type == 'BSDF_PRINCIPLED').inputs

mat, inp = principled("Glass_Clear")
inp["Base Color"].default_value = (1.0, 1.0, 1.0, 1.0)
inp["Transmission Weight"].default_value = 1.0
inp["Roughness"].default_value = 0.0
inp["IOR"].default_value = 1.45
mat.use_raytrace_refraction = True       # EEVEE: needed for refraction...
C.scene.eevee.use_raytracing = True      # ...together with this render setting (report it)

mat, inp = principled("Neon_Pink")
inp["Emission Color"].default_value = (1.0, 0.1, 0.4, 1.0)
inp["Emission Strength"].default_value = 15.0

mat, inp = principled("Ghost_Fade")
inp["Alpha"].default_value = 0.4
mat.surface_render_method = 'BLENDED'    # 'DITHERED' for texture cutouts
result = ["Glass_Clear", "Neon_Pink", "Ghost_Fade"]
```

## Avoid
- Glass made with Alpha: no refraction and no reflections on the far side; it looks like plastic film.
- Glass in EEVEE without both raytracing flags: the world colour fills it and nothing behind is visible.
- Emission Strength above 100 to light a room: the source clips to white. Add a light instead.

Check: `check_materials` reports no issues, and a `look` with `shading: rendered` shows objects behind the glass distorted through it and the emitter brighter than anything lit.
