---
name: stylised-effect-shaders
domain: vfx
when: Building a glowing, animated emissive material by script for an effect the presets do not cover.
---

# Stylised effect shaders
An animated emissive material: scrolling noise through a colour ramp, with a fresnel rim and a frame driver.

## Do
- Drive animation with a driver on a Mapping node's Location, not keyframes: `socket.driver_add("default_value", 2)` then `fc.driver.expression = "frame * -0.08"`. The expression uses `frame` and the safe maths subset (`sin`, `cos`, `pi`), so it needs no auto-run of scripts.
- Feed noise from Object coordinates (TexCoord.Object -> Mapping -> Noise), so the pattern rides with the object and loops with the scroll.
- Keep Emission Strength modest, 1.2-2.5. Blender's AgX view transform desaturates bright emission toward white, so high strengths wash colour out.
- Put the colour in a ColorRamp: a dark or black first stop gives the noise contrast; 3-5 stops read as fire, energy or plasma. Set `scale` 3-5 and `detail` 5-6 on the noise.
- For a rim/forcefield glow, multiply a Fresnel Fac into the strength so edges light up and the centre stays dim.
- Pulse brightness with a Value node driven by `"1.5+0.8*sin(frame*0.25)"` feeding the strength.
- Mirror the look into `mat.diffuse_color` so solid-shaded `look` previews are not grey.

## Code
```python
ob = D.objects["Cube"]
mat = D.materials.new("FX_Glow"); mat.use_nodes = True
nt = mat.node_tree; nt.nodes.clear()
out = nt.nodes.new("ShaderNodeOutputMaterial")
emit = nt.nodes.new("ShaderNodeEmission"); emit.inputs["Strength"].default_value = 1.8
ramp = nt.nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.elements[0].color = (0.0, 0.0, 0.0, 1.0)
ramp.color_ramp.elements[1].color = (1.0, 0.4, 0.05, 1.0)
noise = nt.nodes.new("ShaderNodeTexNoise"); noise.inputs["Scale"].default_value = 4.0
noise.inputs["Detail"].default_value = 6.0
mapping = nt.nodes.new("ShaderNodeMapping"); coord = nt.nodes.new("ShaderNodeTexCoord")
nt.links.new(coord.outputs["Object"], mapping.inputs["Vector"])
nt.links.new(mapping.outputs["Vector"], noise.inputs["Vector"])
nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
nt.links.new(ramp.outputs["Color"], emit.inputs["Color"])
nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
fc = mapping.inputs["Location"].driver_add("default_value", 2)
fc.driver.expression = "frame * -0.08"
mat.diffuse_color = (1.0, 0.4, 0.05, 1.0)
ob.data.materials.clear(); ob.data.materials.append(mat)
result = {"material": mat.name, "driver": fc.driver.expression}
```

## Avoid
- Emission Strength 6+: AgX turns the colour to flat white; drop to ~2 and deepen the ramp.
- Generated coordinates for a moving object: the texture swims; use Object coordinates.
- A python-function driver: it is blocked unless auto-run is on. Keep expressions to `frame` plus `sin`/`cos`.

Check: `look` with `shading: "rendered"` shows coloured, non-uniform glow, and stepping two frames in `run_python` with `scene.frame_set` visibly moves the pattern.
