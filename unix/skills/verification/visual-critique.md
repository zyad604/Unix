---
name: visual-critique
domain: verification
when: You have a `look` or `render_image` result and must decide whether the work is good and what to fix first.
---

# Visual critique
A ranked list of visible faults, each tied to a cause and a fix, judged the way an art director would.

## Do
- Read in this order and fix the first failure before going on: silhouette, proportion, contact, shading, materials, lighting, framing. Detail cannot rescue wrong big shapes.
- Silhouette: would the outline alone name the object? A box or blob means primary forms are missing. Rebuild the blockout; do not add detail.
- Proportion: compare ratios with the real thing. Chair seat 0.45 m of 0.9 m total; table 0.75 m; door 2.0 x 0.9 m; adult 1.75 m, about 7.5 heads. Thickness is the usual fault: table tops 2-4 cm, chair legs 3-5 cm, walls 10-20 cm.
- Contact: every part touches its support. A gap or light line under a part means floating; a part cutting through another means intersecting. Measure with `object_info` world bounds and move by the difference.
- Shading: faceted curves mean flat shading (`mesh.shade_smooth()` then `mesh.set_sharp_from_angle(angle=math.radians(30))`). Dark or inside-out patches mean flipped normals (`check_mesh`). Speckled flicker means two coplanar faces. Pinches mean n-gons or poles under subdivision.
- Edges: razor edges with no highlight read as CG. Bevel 1-3 mm on small objects, 5-10 mm on furniture.
- Materials (rendered): everything looking plastic means roughness left at 0.5. Polished 0.1-0.3, satin 0.4-0.6, matte 0.7-0.9. Base colour between 0.04 and 0.9, never pure black or white.
- Lighting (rendered): the subject separates from the background, one clear key, a shadow that grounds it. Flat: raise key-to-fill to 3:1. Burnt-out white or crushed black: lower energy or exposure.
- Framing (camera): subject fills 60-80 % of the frame, uncropped, horizon level.

## Code
```text
look(targets=[asset], views=["persp","front","right"])        silhouette, proportion, contact
look(targets=[part], views=["front"], isolate=true)           a part hidden behind others
look(targets=[asset], views=["persp"], shading="rendered")    shading, materials, light
look(use_scene_camera=true, shading="rendered")               framing of the real shot
object_info(name=part)                                        world bounds, to measure a gap or overlap
```

## Avoid
- Judging from `persp` alone: perspective hides gaps and unequal lengths. Orthographic `front` and `right` show them.
- Calling it fine because the checks passed: checks measure topology, not looks.
- Polishing materials or light on a wrong silhouette: the rebuild throws that work away.

Check: a second `look` from the same views after the fixes shows each listed fault gone; any that remain are named in the reply.
