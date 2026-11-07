---
name: world-hdri-and-sky
domain: lighting
when: Lighting an outdoor scene, or any scene that needs ambient light and reflections from an HDRI or a procedural sky.
---

# World lighting: HDRI or sky
A world node tree built by script: an HDRI with rotation control, or a Sky Texture with a matching Sun light.

## Do
- Pick one source. HDRI: real reflections, fastest route to believable product and vehicle shots. Sky plus Sun: full control of time of day, crisp shadows.
- HDRI: load the .exr or .hdr with `import_file`, then wire the image by name. With no file to hand, Blender ships eight: `bpy.utils.system_resource('DATAFILES', path='studiolights/world')` returns the folder (`studio.exr`, `city.exr`, `forest.exr`, `sunset.exr` ...). Good light, blurry as a backdrop.
- HDRI strength 1.0 (0.5 to 2). Turn it with the Mapping Z rotation until the shadow falls where the shot needs it.
- Sky: the Sky Texture sun disc lights Cycles only, and at strength 1.0 it clips. Turn the disc off, set Background strength 0.06 (0.03 deep shadows, 0.1 hazy) and add a Sun light on the same direction, so `look` and Cycles agree.
- Sun light strength 4 (2 hazy, 6 harsh noon), `angle` 0.5° (3 to 10° for soft, hazy shadows).
- Elevation: 60 to 75° noon, 30 to 45° mid-morning, 5 to 12° golden hour with sun colour (1.0, 0.6, 0.35). Rotation 0 puts the sun at +Y; positive turns it towards +X.
- Light the subject from the front quarter: sun 30 to 60° off the camera axis.

## Code
```python
scene, elev, azim = C.scene, 35.0, 120.0     # sun elevation and rotation, degrees
world = scene.world or D.worlds.new("World")
scene.world, world.use_nodes = world, True
nt = world.node_tree
nt.nodes.clear()
out, bg = nt.nodes.new('ShaderNodeOutputWorld'), nt.nodes.new('ShaderNodeBackground')
nt.links.new(bg.outputs['Background'], out.inputs['Surface'])
hdri = D.images.get("studio.exr")            # None -> procedural sky
if hdri:
    tex, mapping = nt.nodes.new('ShaderNodeTexEnvironment'), nt.nodes.new('ShaderNodeMapping')
    tex.image = hdri
    mapping.inputs['Rotation'].default_value[2] = math.radians(azim)
    nt.links.new(nt.nodes.new('ShaderNodeTexCoord').outputs['Generated'], mapping.inputs['Vector'])
    nt.links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
else:
    tex = nt.nodes.new('ShaderNodeTexSky')   # 4.x default model is 'NISHITA'
    e, a = math.radians(elev), math.radians(azim)
    tex.sun_disc, tex.sun_elevation, tex.sun_rotation = False, e, a
    sun = D.objects.get("Sun_Key") or D.objects.new("Sun_Key", D.lights.new("Sun_Key", 'SUN'))
    if sun.name not in scene.collection.all_objects:
        scene.collection.objects.link(sun)
    to_sun = Vector((math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), math.sin(e)))
    sun.location = 10 * to_sun               # position is cosmetic, rotation is the light
    sun.rotation_euler = (-to_sun).to_track_quat('-Z', 'Y').to_euler()
    sun.data.energy, sun.data.angle = 4.0, math.radians(0.5)
bg.inputs['Strength'].default_value = 1.0 if hdri else 0.06
nt.links.new(tex.outputs['Color'], bg.inputs['Color'])
result = {"source": hdri.name if hdri else "sky", "strength": round(bg.inputs['Strength'].default_value, 3)}
```

## Avoid
- Sky Texture at strength 1.0 with the sun disc on: Cycles renders white, EEVEE shows no sun at all.
- A Sun light on top of a sunny HDRI: two shadows per object.
- World light indoors in EEVEE: it passes through walls. Use `interior-and-night`.

Check: `look` with `shading: "rendered"` shows one clear shadow direction, a sunlit side that is not pure white and a shadow side that is not black.
