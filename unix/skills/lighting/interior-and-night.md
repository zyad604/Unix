---
name: interior-and-night
domain: lighting
when: Lighting a room through its windows, a room at night with lamps, or a night exterior.
---

# Interior and night lighting
Motivated light: sun or moon through the window, a fill light in each opening, warm lamps, and a dim world.

## Do
- Keep world lighting dim: strength 0.1 (0.05 to 0.2). EEVEE, and so `look`, lets world light pass through walls; a bright world makes every room flat. The code feeds Background strength from Light Path "Is Camera Ray", so the camera still sees a bright sky through the window by day.
- Day: Sun strength 4 (3 to 6), colour (1.0, 0.95, 0.9), aimed through the window so its patch lands on the floor or a wall.
- One Area light in every window: the size of the opening, 0.05 m inside, facing in, 50 W per m² of window (30 to 100), colour (0.8, 0.9, 1.0). It stands in for sky light and bounce.
- Night: world colour (0.25, 0.45, 1.0). The moon is a Sun light at 0.3 (0.1 to 0.5), colour (0.55, 0.7, 1.0). Window fill drops to 5 W per m².
- Lamps carry a night scene: Point light, `shadow_soft_size` 0.05 to 0.1 m, colour (1.0, 0.75, 0.5), or (1.0, 0.6, 0.3) for candles and street lamps. Ceiling light in a 4 x 5 m room: 100 W (60 to 150). In general 20 x d² W, d being the distance to the surface it lights.
- Put each lamp where a fitting is, 0.2 m clear of the mesh. Make the bulb or shade glow with `set_material` (`emission_strength` 5 to 20); the light object does the lighting.
- Night needs contrast: warm pools against cool dark. Leave a third of the frame dark.

## Code
```python
scene, night, win_w, win_h = C.scene, False, 1.5, 1.2           # window width, height
win, inward = Vector((0.0, 2.5, 1.5)), Vector((0.0, -1.0, 0.0))  # window centre; direction into the room
world = scene.world or D.worlds.new("World")
scene.world, world.use_nodes = world, True
nt = world.node_tree
bg = next(n for n in nt.nodes if n.type == 'BACKGROUND')
bg.inputs['Color'].default_value = (0.25, 0.45, 1.0, 1) if night else (0.6, 0.75, 1.0, 1)
view = nt.nodes.get("View_Boost") or nt.nodes.new('ShaderNodeMath')
view.name, view.operation = "View_Boost", 'MULTIPLY_ADD'         # lights get 0.1, camera sees 1.0 by day
view.inputs[1].default_value, view.inputs[2].default_value = (0.0 if night else 0.9), 0.1
path = nt.nodes.get("Light Path") or nt.nodes.new('ShaderNodeLightPath')
nt.links.new(path.outputs['Is Camera Ray'], view.inputs[0])
nt.links.new(view.outputs[0], bg.inputs['Strength'])

def light(name, kind, watts, loc, rgb, aim, **props):
    ob = D.objects.get(name) or D.objects.new(name, D.lights.new(name, kind))
    if ob.name not in scene.collection.all_objects:
        scene.collection.objects.link(ob)
    ob.location, ob.data.energy, ob.data.color = loc, watts, rgb
    for k, v in props.items():
        setattr(ob.data, k, v)
    ob.rotation_euler = (Vector(aim) - Vector(loc)).to_track_quat('-Z', 'Y').to_euler()

light("Sun_Window", 'SUN', 0.3 if night else 4.0, win - 3 * inward + Vector((0, 0, 3)),
      (0.55, 0.7, 1.0) if night else (1.0, 0.95, 0.9), win)
light("Window_Fill", 'AREA', (5 if night else 50) * win_w * win_h, win + 0.05 * inward,
      (0.8, 0.9, 1.0), win + inward, shape='RECTANGLE', size=win_w, size_y=win_h)
if night:
    light("Lamp_Ceiling", 'POINT', 100, (0, 0, 2.3), (1.0, 0.75, 0.5), (0, 0, 0), shadow_soft_size=0.08)
result = {"night": night, "lights": sorted(o.name for o in scene.objects if o.type == 'LIGHT')}
```

## Avoid
- `light.cycles.is_portal = True` on window lights: a portal emits nothing in Cycles, and EEVEE ignores the flag and renders a light, so the engines disagree.
- Lighting night by dimming everything evenly: the image is just underexposed. Keep lamps at full exposure.

Check: `look` with `shading: "rendered"` and `use_scene_camera: true` shows a window patch or lamp pool, walls away from it darker but readable, and no pure-white wall.
