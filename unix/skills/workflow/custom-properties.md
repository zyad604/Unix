---
name: custom-properties
domain: workflow
when: An asset needs a stored parameter, a tag to find it again, or one value that drives several objects.
---

# Custom Properties
Named values stored on an object that scripts can read back and drivers can follow.

## Do
- Store parameters on the asset's root object, not on every part: `root["top_height"] = 0.75`. Floats, ints, bools, strings and short lists are allowed.
- Use real units in the name or description: meters, degrees, kilograms.
- Give numeric properties limits with `id_properties_ui(name).update(min=, max=, description=)` so the user gets a usable slider.
- Tag what you build (`obj["unix_asset"] = "desk"`) and find it later with `o.get("unix_asset")`. `obj["key"]` raises `KeyError` when missing; `obj.get("key")` returns `None`.
- A driver reads a property through a `SINGLE_PROP` variable with data path `'["top_height"]'`. Create the property before the driver.
- After changing a property by script, call `obj.update_tag()`, then `C.view_layer.update()`. Drivers do not react without the tag.
- Keep driver expressions to plain arithmetic on variables (`h`, `h * 0.5 + 0.02`). These run without the user's Python auto-run permission; function calls do not.
- `object_info` lists an object's custom properties: use it to read them back.

## Code
```python
root = D.objects["Desk_Root"]
root["top_height"] = 0.75                           # meters
root.id_properties_ui("top_height").update(min=0.6, max=1.2, soft_min=0.6, soft_max=1.2,
                                           description="Height of the desk top in meters")
root["unix_asset"] = "desk"
root.update_tag()

top = D.objects["Desk_Top"]
top.driver_remove("location", 2)                    # re-run safe: no stacked drivers
drv = top.driver_add("location", 2).driver
var = drv.variables.new()
var.name, var.type = "h", 'SINGLE_PROP'
var.targets[0].id = root
var.targets[0].data_path = '["top_height"]'
drv.expression = "h"                                # set last: this validates the driver
C.view_layer.update()
result = {"driver_valid": drv.is_valid, "top_z": round(top.location.z, 4),
          "tagged": [o.name for o in D.objects if o.get("unix_asset") == "desk"]}
```

## Avoid
- Creating the driver before the property exists: `is_valid` stays `False` and the channel reads 0 until the expression is assigned again.
- Changing the property and reading the driven value at once: it still shows the old value. Tag and update first.
- Driving `location` on an object the user moves by hand: their edit snaps back. Drive a child or say so.

Check: `run_python` returns `driver_valid: true` and `top_z` equal to the property; `object_info` on the root lists `top_height`.
