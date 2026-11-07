---
name: joints-and-contact
domain: modeling
when: Placing parts against each other, or building parts that hinge, slide or plug together.
---

# Joints and contact between parts
Parts that visibly touch, pivot at the right point and move together as one asset.

## Do
- Decide how each pair meets. Resting (lid on box): gap 0. Fixed (leg into top, handle into mug): overlap 1 to 3 mm. Fitted (peg in hole): hole 0.2 to 0.5 mm larger.
- Place from world bounding boxes, not by eye: part minimum = support maximum minus the overlap.
- Bounding boxes measure the cage. A part with a Subdivision modifier is smaller than its box, and a flat part on a curved surface touches at one point. Add overlap until the whole rim is inside and confirm with `look`.
- Keep rigid parts as separate overlapping objects. Fuse with a Boolean union only when one watertight mesh is needed (print).
- A moving part's origin is its pivot: hinge line for a door or lid, axle for a wheel. Move the origin, not the geometry.
- Parent each part to what carries it, keeping its world transform. Test a hinge by rotating it 30 degrees and looking.
- Update the view layer before reading `matrix_world` or `bound_box` of anything just moved.

## Code
```python
def bounds(ob):                                   # world-space min and max corners
    bpy.context.view_layer.update()
    pts = [ob.matrix_world @ Vector(c) for c in ob.bound_box]
    return Vector(map(min, zip(*pts))), Vector(map(max, zip(*pts)))

def rest_on(part, support, overlap=0.002):        # sink part's bottom `overlap` m into support's top
    mw = part.matrix_world.copy()
    mw.translation.z += bounds(support)[1].z - overlap - bounds(part)[0].z
    part.matrix_world = mw

def set_pivot(ob, world_point):                   # move the origin; geometry stays where it is
    bpy.context.view_layer.update()
    mw = ob.matrix_world.copy()
    ob.data.transform(Matrix.Translation(-(mw.inverted() @ Vector(world_point))))
    mw.translation = world_point
    ob.matrix_world = mw

def parent_keep(child, parent):                   # parent without moving the child
    bpy.context.view_layer.update()
    mw = child.matrix_world.copy()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()
    child.matrix_world = mw

box, lid = bpy.data.objects["Chest_Box"], bpy.data.objects["Chest_Lid"]
rest_on(lid, box, overlap=0.0)                    # a lid rests on the rim: contact, no overlap
lo, hi = bounds(lid)
set_pivot(lid, ((lo.x + hi.x) / 2, hi.y, lo.z))   # hinge line: back bottom edge
parent_keep(lid, box)
result = {"gap_m": round(bounds(lid)[0].z - bounds(box)[1].z, 5), "pivot": list(lid.matrix_world.translation)}
```

## Avoid
- Exact face-to-face contact between fixed parts: a dark hairline gap or flicker shows in renders. Overlap them.
- Setting `child.parent` alone when the parent is not at the origin: the child jumps.
- `set_pivot` on mesh data shared by several objects: every user shifts. Check `ob.data.users == 1`.

Check: `run_python` returns `gap_m` between minus the overlap and 0, and `look` (front, right) shows no daylight between parts and the pivot on the hinge line.
