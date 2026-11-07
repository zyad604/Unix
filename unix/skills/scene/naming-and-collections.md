---
name: naming-and-collections
domain: scene
when: Objects have just been created or imported, or the outliner is a flat list, and things need proper names and collections.
---

# Naming and collections
Every object named for what it is and filed in a collection per asset, with lights, cameras and helpers kept apart.

## Do
- Object names: `Asset_Part_Position` in PascalCase joined by underscores: `Table_Top`, `Table_Leg_FL`, `Door_Handle_L`. Number sets as `_01`, `_02`. Keep names under 40 characters.
- Mesh data takes its object's name. Exporters write data names, and `Cube.014` in a game engine helps nobody.
- Materials are named for the substance, not the object, because they are shared: `Oak_Varnished`, `Steel_Brushed`.
- Never leave `.001` suffixes or a primitive's default name. `check_scene` flags `Cube`, `Sphere`, `Cylinder`, `Plane`, `Empty`, `Light`, `Sun`, `Area`, `Point`, `Spot`, `Camera` and the like, with or without a number.
- Assigning a name that is taken makes Blender append `.001` silently. Read the name back.
- One collection per asset, named for the asset. Shared collections: `Lights`, `Cameras`, `Set` for floor, walls and backdrop, `Helpers` for references, cutters and guide Empties.
- At most three levels deep. Nothing but collections directly under the Scene Collection.
- Hide helpers through their collection (`collection.hide_render = True`, `hide_viewport` to match). Setting only one flag on an object makes `check_scene` report a visibility mismatch.
- Rename only what you made or were asked to tidy.

## Code
```python
scene = C.scene
renames = {"Cube": "Table_Top", "Cylinder": "Table_Leg_FL"}        # old name -> new name
groups = {"Table": ["Table_Top", "Table_Leg_FL"]}                  # collection -> members
by_type = {'LIGHT': "Lights", 'CAMERA': "Cameras"}
clashes = []
for old, new in renames.items():
    ob = D.objects.get(old)
    if ob and new not in D.objects:
        ob.name = new
        if ob.data and ob.data.users == 1:
            ob.data.name = new
    elif ob:
        clashes.append(new)

def file_in(ob, col_name):
    col = D.collections.get(col_name) or D.collections.new(col_name)
    if col not in scene.collection.children_recursive:
        scene.collection.children.link(col)
    for other in ob.users_collection:
        other.objects.unlink(ob)
    col.objects.link(ob)

for col_name, members in groups.items():
    for name in members:
        file_in(D.objects[name], col_name)
for ob in list(scene.collection.objects):
    if ob.type in by_type:
        file_in(ob, by_type[ob.type])
result = {"clashes": clashes, "loose": [o.name for o in scene.collection.objects],
          "collections": {c.name: [o.name for o in c.objects] for c in scene.collection.children_recursive}}
```

## Avoid
- Linking to the new collection without unlinking from the old: the object shows up twice in the outliner.
- Names that describe shape instead of role (`Box_Long`): nobody can find the table leg.
- Renaming by prefix in a loop without checking for clashes: half the set ends in `.001` again.

Check: `check_scene` lists no default-named objects, and the result's `clashes` and `loose` are both empty.
