---
name: scripting-patterns
domain: workflow
when: Before writing any run_python script that creates or edits objects, or after a script failed.
---

# Scripting Patterns
A `run_python` script that works the first time and can be re-run without duplicating anything.

## Do
- Data API first: `D.meshes.new` + `bmesh` + `D.objects.new`, then assign properties. `bpy.ops.mesh.primitive_*_add` and `bpy.ops.transform.*` depend on selection, mode and the 3D cursor.
- Get-or-create by name. `D.objects.new("X", ...)` returns `X.001` when the name is taken: read `obj.name` back.
- A new object is invisible until linked to a collection. Link once; a second `link` raises `RuntimeError`.
- After setting `location`, `rotation_euler`, `scale` or `parent`, call `C.view_layer.update()` before reading `matrix_world`, `dimensions` or `bound_box`.
- Operator with no data equivalent: be in object mode and pass the objects through `C.temp_override(...)`. Exception: `bpy.ops.object.mode_set` ignores the override and acts on `C.view_layer.objects.active`; set that for real and return to `'OBJECT'` before the script ends.
- Modifier results are not in `obj.data`. Read them from `obj.evaluated_get(C.evaluated_depsgraph_get())`.
- References die. Re-read `obj.data.vertices` after a mode switch; re-fetch by name after removing data or an undo. Carry names, not objects, between `run_python` calls.
- On `AttributeError` or `TypeError`, call `api_lookup` (`BevelModifier`, `ops.object.shade_smooth_by_angle`) before the next attempt.

## Code
```python
def get_collection(name):
    col = D.collections.get(name) or D.collections.new(name)
    if not C.scene.user_of_id(col):                  # not in this scene yet, at any depth
        C.scene.collection.children.link(col)
    return col

def get_mesh_object(name, col):
    obj = D.objects.get(name) or D.objects.new(name, D.meshes.new(name))
    if obj.name not in col.objects:
        col.objects.link(obj)
    return obj

obj = get_mesh_object("Crate_Body", get_collection("Crate"))
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(obj.data); bm.free()                      # replaces the geometry: re-runs do not stack
obj.location = (0.0, 0.0, 0.5)
bevel = obj.modifiers.get("Bevel") or obj.modifiers.new("Bevel", 'BEVEL')
bevel.width, bevel.segments = 0.02, 3
C.view_layer.update()                                # matrix_world is stale until this

if C.mode != 'OBJECT':
    bpy.ops.object.mode_set(mode='OBJECT')
with C.temp_override(active_object=obj, object=obj, selected_objects=[obj], selected_editable_objects=[obj]):
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(30))   # user's selection is untouched

me = obj.evaluated_get(C.evaluated_depsgraph_get()).data           # mesh after modifiers
result = {"object": obj.name, "base_verts": len(obj.data.vertices), "final_verts": len(me.vertices),
          "world_z": round(obj.matrix_world.translation.z, 3)}
```

## Avoid
- Reading `matrix_world` before `view_layer.update()`: the object lands at the world origin or a child is offset.
- Keeping vertex or face references across edit mode: garbage coordinates or a crash.
- `bpy.ops` inside a loop over many objects: every call updates the whole scene, so 500 objects take minutes.

Check: run the script twice with `run_python`; both replies return the same `result` and the second lists no `created` names in `changes`.
