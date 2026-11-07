---
name: hard-surface-bevel
domain: modeling
when: Finishing a manufactured or mechanical part: cases, furniture, tools, panels, anything with flat faces and crisp edges.
---

# Hard-surface finish with a live modifier stack
A sharp low-poly base mesh that renders with even rounded edges and flat faces, and stays editable.

## Do
- Keep the base mesh sharp and simple, scale (1, 1, 1). Modifiers do the finishing and stay live.
- Stack order, top to bottom: Mirror, Solidify, Boolean, Bevel, Weighted Normal. Reorder with `ob.modifiers.move(from_index, to_index)`.
- Bevel by angle: `limit_method='ANGLE'`, `angle_limit` 30 degrees. Width is 1 to 2% of the part's smallest dimension: 0.5 to 1 mm on a handheld device, 2 to 3 mm on an appliance, 5 to 10 mm on furniture.
- Segments: 3 for a round edge, 2 at distance, 1 for a chamfer or a game asset.
- To bevel chosen edges only: `limit_method='WEIGHT'` plus a FLOAT attribute `bevel_weight_edge` on the EDGE domain (1.0 = full width).
- Shade every face smooth and put Weighted Normal after Bevel: flats stay flat, bevels catch the light. Blender 4.1+ has no `use_auto_smooth`.
- Detail under about 1 mm (seams, knurling, text) goes in a normal map, not in geometry.

## Code
```python
ob = bpy.data.objects["Case_Body"]            # sharp base mesh, scale 1
me = ob.data
me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))

def mod(name, kind):
    return ob.modifiers.get(name) or ob.modifiers.new(name, kind)

bev = mod("Bevel", 'BEVEL')
bev.limit_method, bev.angle_limit = 'ANGLE', math.radians(30)
bev.width, bev.segments, bev.profile = 0.002, 3, 0.5
bev.miter_outer, bev.harden_normals = 'MITER_ARC', True
wn = mod("WeightedNormal", 'WEIGHTED_NORMAL')
wn.keep_sharp, wn.weight = True, 50

names = [m.name for m in ob.modifiers]        # Bevel and WeightedNormal go last, in that order
for name in ("Bevel", "WeightedNormal"):
    ob.modifiers.move(names.index(name), len(names) - 1)
    names = [m.name for m in ob.modifiers]

ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
result = {"stack": names, "base_verts": len(me.vertices), "final_verts": len(ev.data.vertices)}
```

## Avoid
- Width larger than the shortest edge allows: `clamp_overlap` shrinks the bevel there and edges look uneven. Lower the width or remove the short edge.
- Bevel on unapplied non-uniform scale: the bevel is wider along one axis.
- Subdivision after Bevel on flat-faced parts: flats bulge and shading ripples. For rounded forms use `subdivision-forms`.

Check: `object_info` lists Bevel directly before WeightedNormal as the last two modifiers, and `look` (persp) shows an even highlight on every edge with no gradient across flat faces.
