---
name: engine-and-samples
domain: render
when: The user wants a final render or asks about quality, noise or render time, and the engine, samples and denoising must be set.
---

# Engine, samples and denoising
The right engine for the job, set to numbers that give a clean image in bounded time.

## Do
- Change render settings only when the task is a render, and say what you changed.
- EEVEE for previews, stylised work and animation. Cycles for glass and liquids, skin, interiors lit by bounce light, emissive meshes that must light the scene, and product finals.
- The EEVEE engine id is `'BLENDER_EEVEE_NEXT'` on 4.2 to 4.5 and `'BLENDER_EEVEE'` on 5.x and on 4.1 and older. Assigning the wrong one raises TypeError.
- EEVEE: `taa_render_samples` 64 (16 draft, 128 with depth of field or soft shadows). Turn on `use_raytracing` for reflections of nearby objects; the default is off.
- Cycles stills: 256 samples (64 draft, 512 to 1024 for interiors), adaptive sampling at 0.01 (0.05 draft), OpenImageDenoise on. The factory default of 4096 samples is far too slow.
- Cycles bounces: 8 total, diffuse 4, glossy 4, transmission 8, volume 0. Stacked glass needs transmission 12. `sample_clamp_indirect` 10 removes fireflies. Caustics off.
- Use the GPU only if one is configured in Preferences; read it, never change it.
- `render_image` caps samples at `max_samples` (default 128). Pass `max_samples: 256` for a final, and test first at `resolution_percentage: 50`, about four times faster.
- `look` always renders with EEVEE. Judge Cycles-only effects with `render_image`.

## Code
```python
scene, engine, final = C.scene, 'CYCLES', True           # engine: 'CYCLES' or 'EEVEE'
if engine == 'EEVEE':
    try:
        scene.render.engine = 'BLENDER_EEVEE_NEXT'       # 4.2 to 4.5
    except TypeError:
        scene.render.engine = 'BLENDER_EEVEE'            # 5.x, 4.1 and older
    scene.eevee.taa_render_samples = 64 if final else 16
    scene.eevee.use_raytracing = True
    samples = scene.eevee.taa_render_samples
else:
    scene.render.engine = 'CYCLES'
    cy = scene.cycles
    cy.samples = 256 if final else 64
    cy.use_adaptive_sampling, cy.adaptive_threshold = True, 0.01 if final else 0.05
    cy.use_denoising, cy.denoiser = True, 'OPENIMAGEDENOISE'
    cy.max_bounces, cy.diffuse_bounces, cy.glossy_bounces = 8, 4, 4
    cy.transmission_bounces, cy.volume_bounces = 8, 0
    cy.sample_clamp_indirect = 10.0
    cy.caustics_reflective = cy.caustics_refractive = False
    prefs = C.preferences.addons['cycles'].preferences
    cy.device = 'GPU' if prefs.compute_device_type != 'NONE' else 'CPU'
    samples = cy.samples
result = {"engine": scene.render.engine, "samples": samples,
          "device": scene.cycles.device if scene.render.engine == 'CYCLES' else "GPU",
          "resolution": [scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage]}
```

## Avoid
- Raising samples to fix noise before turning on denoising: four times the samples only halves the noise.
- Old EEVEE properties (`use_bloom`, `use_ssr`, `use_soft_shadows`): they no longer exist and raise AttributeError.
- Cycles with the default 12 transmission and 4096 samples on a simple scene: minutes spent for no visible gain.

Check: `render_image` with `max_samples` equal to the result's `samples` returns the expected `engine`, and the image shows no grain in the shadows and no bright single-pixel fireflies.
