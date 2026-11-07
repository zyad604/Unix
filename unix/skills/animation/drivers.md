---
name: drivers
domain: animation
when: One property must always follow another - a part controlled by a slider, wheels that turn with travel, a value linked across objects.
---

# Drivers
A property computed live from other properties, valid and safe to open on any machine.

## Do
- Use a driver for a fixed relationship. Use keyframes for motion that has its own timing.
- `fc = obj.driver_add(path, index)` returns the driver F-curve and reuses an existing one. Clear its variables before rebuilding, so a re-run does not stack them.
- Variable types: `SINGLE_PROP` (any property by `data_path`, e.g. `'["open"]'` for a custom property), `TRANSFORMS` (one channel of an object or bone: set `transform_type` and `transform_space`), `LOC_DIFF` (distance between two objects), `ROTATION_DIFF` (angle between two).
- User slider: create the custom property, set its range, then `obj.update_tag()`. Map the 0-1 range in the expression.
- Rolling wheel: a `TRANSFORMS` variable `dist` reading the car body's `LOC_Y` in `WORLD_SPACE`, expression `-dist / radius` on the wheel's X rotation.
- Keep the expression simple: variables, numbers, arithmetic, `frame`, and math functions (`sin`, `cos`, `min`, `max`, `clamp`, `radians`, `sqrt`, `abs`). Simple expressions run without Python and work with Auto Run Scripts off.
- Units: rotations are radians, lengths meters.
- Confirm `driver.is_valid`, and tell the user the property is now driven: it can no longer be keyed or edited by hand.

## Code
```python
door = D.objects["Cube"]                            # hinged part: origin on the hinge, Z up
door["open"] = door.get("open", 0.0)                # the user's slider, 0 closed to 1 open
door.id_properties_ui("open").update(min=0.0, max=1.0)
door.update_tag()                                   # drivers cannot see a new property without this
fc = door.driver_add("rotation_euler", 2)           # reuses the driver if it exists
drv = fc.driver
drv.type = 'SCRIPTED'
for v in list(drv.variables):
    drv.variables.remove(v)
var = drv.variables.new()
var.name, var.type = "open", 'SINGLE_PROP'
var.targets[0].id, var.targets[0].data_path = door, '["open"]'
drv.expression = "open * radians(95)"               # full swing 95 degrees
C.view_layer.update()
result = {"valid": drv.is_valid, "simple": drv.is_simple_expression,
          "angle_deg": round(math.degrees(door.rotation_euler.z), 2)}
```

## Avoid
- `bpy.` or attribute access in the expression: `is_simple_expression` is False and, with Auto Run Scripts off, the driver errors and `is_valid` goes False.
- A variable named after a function the expression calls (`sin`, `min`): "'float' object is not callable".
- Changing a custom property by script without `obj.update_tag()`: the driver does not see it. If the property was missing when the driver first ran, `is_valid` stays False until the expression is assigned again.
- Degrees in the expression: 90 read as radians is 14 turns.

Check: in `run_python` set the source to a known value (`door["open"] = 1.0`, `door.update_tag()`, `C.view_layer.update()`) and read the driven value (95 degrees) with `driver.is_valid` True; then restore the source.
