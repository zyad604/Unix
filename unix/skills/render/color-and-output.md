---
name: color-and-output
domain: render
when: Before a final render: choosing the view transform, exposure, resolution, transparency and file format.
---

# Colour management and output format
A view transform that suits the image, exposure set in stops, and an output format that keeps what the user needs.

## Do
- View transform: `'AgX'` for photoreal work; it holds bright highlights without clipping. `'Standard'` for flat, toon or emissive colours that must match a hex value exactly. `'Khronos PBR Neutral'` for product shots: a lit material keeps its base colour and highlights still roll off softly.
- AgX is not colour-exact: a red of (0.8, 0.05, 0.05) comes out paler and less saturated.
- Look adds contrast. Names differ per transform: `'AgX - Medium High Contrast'` under AgX, `'Medium High Contrast'` under the others. A wrong name raises TypeError.
- Exposure is in stops: +1 doubles the light. Fix light ratios first, then trim exposure within -1.5 to +1.5. Leave gamma at 1.
- Resolution: 1920 x 1080 general, 2048 x 2048 product, 3840 x 2160 only on request. Keep `resolution_percentage` at 100.
- Format: PNG RGB 8-bit for delivery. PNG RGBA with `film_transparent` for cut-outs. PNG 16-bit for print or smooth gradients. JPEG for previews only.
- `render_image` takes PNG or JPEG from the path extension. Colour mode, depth and transparency come from the scene, so set them here first.
- With a transparent film in Cycles, keep the ground shadow by setting `is_shadow_catcher = True` on the floor object.

## Code
```python
scene, transform, look, transparent = C.scene, 'AgX', 'None', False
vs = scene.view_settings
vs.view_transform = transform               # 'AgX', 'Standard' or 'Khronos PBR Neutral'
for name in ('AgX - ' + look, look, 'None'):   # AgX look names carry a prefix
    try:
        vs.look = name
        break
    except TypeError:
        pass
vs.exposure, vs.gamma = 0.0, 1.0
r = scene.render
r.resolution_x, r.resolution_y, r.resolution_percentage = 1920, 1080, 100
r.film_transparent = transparent
im = r.image_settings
im.file_format = 'PNG'
im.color_mode = 'RGBA' if transparent else 'RGB'
im.color_depth, im.compression = '8', 15       # '16' for print or gradients
result = {"view_transform": vs.view_transform, "look": vs.look, "exposure": vs.exposure,
          "resolution": [r.resolution_x, r.resolution_y], "transparent": r.film_transparent,
          "format": [im.file_format, im.color_mode, im.color_depth]}
```

## Avoid
- Rendering to a .jpg and then expecting alpha: switching the format to JPEG resets `color_mode` to RGB and it stays RGB afterwards. Re-run this code before a transparent PNG.
- Fixing a dark render with +3 exposure: noise and banding rise with it. Add light.
- Judging brand colours under AgX: they will not match the swatch.

Check: `render_image` to a .png path reports the requested `resolution`; for a cut-out the result shows `transparent` true and `format` with `RGBA`, and the returned image has no background behind the subject.
