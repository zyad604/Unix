---
name: organic-characters
domain: modeling
when: A character, creature, monster, animal or any living body must be modelled, above all when the user says realistic.
---

# Organic characters
One continuous, detailed body that holds up in a rendered close-up, not a figure made of visible primitives.

## Do
- Ask first with `ask_user`, one question at a time, up to three, each with options: the look (photoreal, game-ready, stylised), the specifics (build, age, clothing, wounds), and the pose or use. Skip what the message already says.
- Get a target: `generate_image(purpose='reference_sheet')` of the described character, front, side and back. Open it, and compare with `match_reference` after each stage. If image generation is not available, work from the user's image or description and keep going.
- Proportions for an adult: 7.5 heads tall; shoulders 2 heads wide; elbows at the waist; wrists at the crotch; knees at 2 heads from the floor. At 1.75 m a head is 0.23 m.
- Block out with many overlapping parts that follow anatomy, not one capsule per limb: ribcage, belly, pelvis; tapered thigh, knee, calf, ankle; skull, jaw, brow ridge, nose, ears; palm and five fingers. Overlap neighbours by a quarter of their width.
- `fuse_meshes` the blockout into one body. Voxel size: height / 250, or a third of a finger's thickness when hands matter.
- Shape the fused mesh by script: move vertices near a point with a smooth falloff to sink eye sockets and cheeks, raise cheekbones, collarbones, ribs and knuckles.
- Surface detail: two Displace modifiers (code), large soft forms then fine wrinkles; damage and rot as a third, masked by a vertex group.
- Skin: `generate_texture`, then by script set its Image Texture nodes to `projection = 'BOX'` with `projection_blend = 0.3`, fed by Object coordinates (the fused mesh has no UVs). Subsurface weight 0.1-0.3, roughness 0.5-0.7. Eyes, teeth, nails, hair and clothes are separate meshes.
- Rig with `measured-skeleton`, pose it, light it (`asset_hdri`, `light_rig`), and `look(shading='rendered')` at the face and hands up close.
- Loop: at least three look-and-fix rounds. Then show it and ask the user what to change.

## Code
```python
body = D.objects["Body"]                                  # the fused mesh
size = max(body.dimensions)
layers = (("Forms", 'CLOUDS', size * 0.12, size * 0.010),  # name, texture, feature size, depth
          ("Wrinkles", 'VORONOI', size * 0.012, size * 0.0025))
for name, kind, scale, depth in layers:
    tex = D.textures.get("Body_" + name) or D.textures.new("Body_" + name, kind)
    tex.noise_scale = scale
    mod = body.modifiers.get(name) or body.modifiers.new(name, 'DISPLACE')
    mod.texture, mod.texture_coords = tex, 'LOCAL'
    mod.strength, mod.mid_level = depth, 0.5              # mid 0.5: pushes out and in equally, volume stays
rig = next((m for m in body.modifiers if m.type == 'ARMATURE'), None)
if rig is not None:                                       # deform first, then detail rides on the pose
    body.modifiers.move(list(body.modifiers).index(rig), 0)
result = {"modifiers": [m.name for m in body.modifiers], "depths_m": [round(l[3], 4) for l in layers]}
```

## Avoid
- Handing back spheres and capsules with "not realistic yet". A blockout is a stage: fuse, shape, detail, texture and light before you report.
- Smooth or Subdivision on separate pieces: each piece shrinks on its own and the joints open.
- Displace strength above 2% of the body's height: the silhouette turns to noise.
- One flat skin colour: real skin varies. Redder at nose, ears, knuckles; paler and blotched for a corpse.

Check: a `look(shading='rendered')` close-up of the head and of a hand shows one continuous surface with no primitive shapes or seams, and `check_mesh` on the body reports `watertight: true`.
