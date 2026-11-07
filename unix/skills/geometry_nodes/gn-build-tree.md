---
name: gn-build-tree
domain: geometry_nodes
when: Creating or rebuilding a Geometry Nodes group from a script and attaching it to an object.
---

# Build a Geometry Nodes tree by script
A node group built in code, valid link by link, attached as a modifier and producing geometry.

## Do
- Get or create the group by name, then `tree.nodes.clear()` and `tree.interface.clear()` so a re-run rebuilds it instead of stacking nodes.
- Declare the interface first with `tree.interface.new_socket(name, in_out=..., socket_type=...)`. The first output must be a `NodeSocketGeometry` or the modifier shows nothing.
- A scripted tree starts empty: add `NodeGroupInput` and `NodeGroupOutput` yourself.
- Create nodes by `bl_idname` (`GeometryNodeSetPosition`, `GeometryNodeTransform`, `GeometryNodeJoinGeometry`, `ShaderNodeMath`, `ShaderNodeVectorMath`, `FunctionNodeRandomValue`). If unsure of an id or a socket name, use `api_lookup`.
- Set `operation`, `data_type` and `mode` before linking; changing them swaps the sockets.
- Link output to input. Use the socket name when it is unique and the index when names repeat (Math and Vector Math inputs).
- Unlinked inputs take `socket.default_value`.
- Space nodes 200 units apart in x so the user can read the tree.
- Verify from the evaluated object, as in the code.

## Code
```python
def build_inflate(amount=0.01):
    """Push every point `amount` metres along its normal."""
    tree = bpy.data.node_groups.get("Inflate") or bpy.data.node_groups.new("Inflate", 'GeometryNodeTree')
    tree.nodes.clear()
    tree.interface.clear()
    tree.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    tree.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    def node(kind, x, **props):
        n = tree.nodes.new(kind)
        n.location = (x, 0)
        for key, value in props.items(): setattr(n, key, value)
        return n
    gin, gout = node('NodeGroupInput', 0), node('NodeGroupOutput', 800)
    normal = node('GeometryNodeInputNormal', 200)
    scale = node('ShaderNodeVectorMath', 400, operation='SCALE')
    setpos = node('GeometryNodeSetPosition', 600)
    scale.inputs['Scale'].default_value = amount
    tree.links.new(gin.outputs['Geometry'], setpos.inputs['Geometry'])
    tree.links.new(normal.outputs['Normal'], scale.inputs[0])
    tree.links.new(scale.outputs['Vector'], setpos.inputs['Offset'])
    tree.links.new(setpos.outputs['Geometry'], gout.inputs['Geometry'])
    return tree

ob = bpy.data.objects["Crate"]
mod = ob.modifiers.get("Inflate") or ob.modifiers.new("Inflate", 'NODES')
mod.node_group = build_inflate(0.01)
ev = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
result = {"links_valid": all(l.is_valid for l in mod.node_group.links),
          "verts": len(ev.data.vertices), "size": list(ev.dimensions)}
```

## Avoid
- `bpy.data.node_groups.new` on every run: `Inflate.001`, `.002` pile up and the modifier points at an old one.
- `tree.inputs.new` / `tree.outputs.new`: removed in 4.0, raises `AttributeError`. Use `tree.interface`.
- Linking by a socket name that occurs twice: the link lands on the first match and the result is silently wrong.

Check: `run_python` returns `links_valid: true` and `verts` above 0, and `object_info` shows the NODES modifier with its `node_group` set.
