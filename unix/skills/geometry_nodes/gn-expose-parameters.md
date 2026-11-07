---
name: gn-expose-parameters
domain: geometry_nodes
when: A node group needs controls on the modifier (count, size, seed, object) or a script must set those controls.
---

# Expose and set Geometry Nodes parameters
A node group with named, ranged inputs, and modifier values set by name from a script.

## Do
- Expose 3 to 8 inputs: what the user will tune (count, spacing, size, seed, density). Leave the rest as node defaults.
- Create each with `tree.interface.new_socket`, then set `default_value`, `min_value`, `max_value` and `subtype` (`DISTANCE`, `ANGLE`, `FACTOR`). Put the unit in the name where no subtype fits.
- Socket types: `NodeSocketFloat`, `Int`, `Bool`, `Vector`, `Color`, `Object`, `Collection`, `Material`, `Geometry`.
- The modifier stores values under the socket identifier (`Socket_2`), not the name. Find it through `interface.items_tree`.
- Match the type. An Int input raises `TypeError` on a float: pass `int(value)`. An Object input takes the object datablock.
- After writing a value call `ob.update_tag()`; the modifier does not re-evaluate by itself.
- Rebuilding the interface gives new identifiers and resets the values on existing modifiers. Set every input after building, in the same script.

## Code
```python
tree = bpy.data.node_groups.get("Linear_Array") or bpy.data.node_groups.new("Linear_Array", 'GeometryNodeTree')
tree.nodes.clear(); tree.interface.clear()
def socket(name, kind, in_out='INPUT', **props):
    s = tree.interface.new_socket(name, in_out=in_out, socket_type=kind)
    for key, value in props.items(): setattr(s, key, value)
socket("Geometry", 'NodeSocketGeometry')
socket("Geometry", 'NodeSocketGeometry', in_out='OUTPUT')
socket("Count", 'NodeSocketInt', default_value=5, min_value=1, max_value=500)
socket("Spacing", 'NodeSocketFloat', subtype='DISTANCE', default_value=0.5, min_value=0.0)
new = tree.nodes.new
gin, gout = new('NodeGroupInput'), new('NodeGroupOutput')
line, xyz = new('GeometryNodeMeshLine'), new('ShaderNodeCombineXYZ')
inst, real = new('GeometryNodeInstanceOnPoints'), new('GeometryNodeRealizeInstances')
for a, b in ((gin.outputs['Count'], line.inputs['Count']), (gin.outputs['Spacing'], xyz.inputs['X']),
             (xyz.outputs['Vector'], line.inputs['Offset']), (line.outputs['Mesh'], inst.inputs['Points']),
             (gin.outputs['Geometry'], inst.inputs['Instance']), (inst.outputs['Instances'], real.inputs['Geometry']),
             (real.outputs['Geometry'], gout.inputs['Geometry'])):
    tree.links.new(a, b)
def set_input(mod, name, value):
    item = next(i for i in mod.node_group.interface.items_tree
                if i.item_type == 'SOCKET' and i.in_out == 'INPUT' and i.name == name)
    mod[item.identifier] = value            # the key is "Socket_N", not the display name
    mod.id_data.update_tag()

ob = bpy.data.objects["Fence_Post"]
mod = ob.modifiers.get("Array") or ob.modifiers.new("Array", 'NODES')
mod.node_group = tree
set_input(mod, "Count", 8); set_input(mod, "Spacing", 0.4)
ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
result = {"size": list(ev.dimensions), "verts": len(ev.data.vertices)}
```

## Avoid
- `mod["Count"] = 8`: it creates an unrelated custom property and the input does not change.
- Hard-coding `Socket_2`: identifiers change when the interface is rebuilt.
- Exposing every node value: the panel becomes unreadable and inputs get wrong ranges.

Check: `run_python` returns a `size` that follows the inputs (here X = post width + 7 x 0.4 m) and changes when one input changes; `object_info` shows the NODES modifier with its `node_group`.
