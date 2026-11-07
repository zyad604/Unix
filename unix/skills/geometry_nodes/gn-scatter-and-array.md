---
name: gn-scatter-and-array
domain: geometry_nodes
when: Many copies of an object are needed: rocks or plants over a surface, rows, grids or rings of parts.
---

# Scatter and array with instances
Copies placed by a node group as instances, with density, seed and size variation under control.

## Do
- Scatter: Distribute Points on Faces into Instance on Points. Density is points per square meter: grass 200 to 2000, pebbles 5 to 50, rocks 0.05 to 1, trees 0.01 to 0.1.
- Compute count = density x surface area first. Keep it under about 100,000 instances; otherwise lower the density or scatter only near the camera.
- Scale 0.7 to 1.3 from Random Value. Rotation from the Distribute node, so instances follow the surface.
- Feed the object through Object Info with `As Instance` on. Its origin lands on the surface, so its origin must be at its base.
- Join Geometry of the input and the instances keeps the surface itself.
- Expose a Seed and change it instead of moving instances by hand.
- Rows: Mesh Line into Instance on Points (code in `gn-expose-parameters`). Grids: `GeometryNodeMeshGrid` as the points. Rings: `GeometryNodeMeshCircle`.
- Realize Instances only when a later node needs real mesh; it multiplies memory by the count.

## Code
```python
tree = bpy.data.node_groups.get("Scatter") or bpy.data.node_groups.new("Scatter", 'GeometryNodeTree')
tree.nodes.clear()
tree.interface.clear()
for name, kind, io in (("Geometry", 'Geometry', 'OUTPUT'), ("Geometry", 'Geometry', 'INPUT'),
                       ("Instance", 'Object', 'INPUT'), ("Density", 'Float', 'INPUT'), ("Seed", 'Int', 'INPUT')):
    tree.interface.new_socket(name, in_out=io, socket_type='NodeSocket' + kind)
new, link = tree.nodes.new, tree.links.new
gin, gout, join = new('NodeGroupInput'), new('NodeGroupOutput'), new('GeometryNodeJoinGeometry')
pts, info = new('GeometryNodeDistributePointsOnFaces'), new('GeometryNodeObjectInfo')
inst, size = new('GeometryNodeInstanceOnPoints'), new('FunctionNodeRandomValue')
info.inputs['As Instance'].default_value = True
size.inputs[2].default_value, size.inputs[3].default_value = 0.7, 1.3        # float Min, Max
for a, b in ((gin.outputs['Geometry'], pts.inputs['Mesh']), (gin.outputs['Density'], pts.inputs['Density']),
             (gin.outputs['Seed'], pts.inputs['Seed']), (gin.outputs['Instance'], info.inputs['Object']),
             (pts.outputs['Points'], inst.inputs['Points']), (info.outputs['Geometry'], inst.inputs['Instance']),
             (pts.outputs['Rotation'], inst.inputs['Rotation']), (size.outputs[1], inst.inputs['Scale']),
             (gin.outputs['Geometry'], join.inputs[0]), (inst.outputs['Instances'], join.inputs[0]),
             (join.outputs[0], gout.inputs[0])):
    link(a, b)

ground, rock = bpy.data.objects["Ground"], bpy.data.objects["Rock"]
mod = ground.modifiers.get("Scatter") or ground.modifiers.new("Scatter", 'NODES')
mod.node_group = tree
ids = {i.name: i.identifier for i in tree.interface.items_tree if i.item_type == 'SOCKET' and i.in_out == 'INPUT'}
mod[ids["Instance"]], mod[ids["Density"]], mod[ids["Seed"]] = rock, 0.5, 7
ground.update_tag()
dg = bpy.context.evaluated_depsgraph_get()
result = {"instances": sum(1 for i in dg.object_instances if i.is_instance and i.parent.original == ground)}
```

## Avoid
- Density set without computing the count: a 500 m terrain at density 50 is 12.5 million instances and Blender stalls.
- Instancing an object whose origin is at its centre: every instance is half buried.
- Identical scale and rotation on every instance: the repeat is obvious in `look`.

Check: `run_python` returns `instances` within 20% of density x area, and `look` (persp, top) shows even cover with no floating or buried instances.
