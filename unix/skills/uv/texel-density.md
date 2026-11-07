---
name: texel-density
domain: uv
when: Choosing a texture size, or making several objects in one shot look equally sharp.
---

# Texel density
Picks a pixels-per-metre target, sizes the texture for it and scales UVs to hit it.

## Do
- Pick one density for the whole scene: 256 px/m background, 512 px/m mid-ground and third-person props, 1024 px/m first-person and hero props, 2048 px/m and up for close-ups.
- Size a unique texture from surface area: `size = density * sqrt(area_m2 / 0.7)`, rounded up to a power of two (0.7 is a typical pack fill). A 1 m cube (6 m2) at 512 px/m needs 1499, so 2048.
- If that exceeds 4096, split the object over two materials or drop the density one step. Do not scale unique UVs past 0-1.
- For tiling textures UVs may leave 0-1: scale them until the density matches (code below).
- Keep every object in a shot within 0.8x to 1.25x of the target. A 2x mismatch reads as blurry next to sharp.
- Unseen faces (undersides, backs) may run at half density.
- Apply scale first, and work in object mode: `uv_layers.active.data` is empty in edit mode.

## Code
```python
obj = D.objects["Wall"]                # scale applied, object mode
tex, target = 2048, 512.0              # texture pixels, wanted pixels per metre
me = obj.data
uvs = me.uv_layers.active.data
ratios = []
for p in me.polygons:
    pts = [uvs[i].uv for i in p.loop_indices]
    area = abs(sum(pts[i].x * pts[i - 1].y - pts[i - 1].x * pts[i].y for i in range(len(pts)))) / 2
    if area > 1e-12 and p.area > 1e-12:
        ratios.append(math.sqrt(area / p.area))
ratios.sort()
current = ratios[len(ratios) // 2] * tex   # median px/m, the figure check_uv reports
k = target / current
centre = Vector((0.5, 0.5))
for d in uvs:                              # scale about the tile centre
    d.uv = (d.uv - centre) * k + centre
me.update()
result = {"before_px_m": round(current, 1), "uv_scale": round(k, 3), "after_px_m": target}
```

## Avoid
- Judging density from texture size alone: a 4096 map on a 20 m wall is 205 px/m, blurrier than a 1024 map on a 1 m crate.
- Scaling UVs of a baked or painted object: the image no longer lines up.
- Mixed densities inside one object: `check_uv` flags a spread above 4x.

Check: `check_uv` with `texture_size` reports `texel_density_px_per_m` within 10% of the target on every object, and the highest and lowest object differ by less than 1.25x.
