---
name: roblox-native-vfx
domain: vfx
when: The user asks for an effect to be built inside Roblox Studio itself, out of Roblox's own emitters, beams and scripts, or to be controlled by the game. Not for an effect they asked you to make and then send: that is made in Blender and sent.
---

# Roblox-native VFX
An effect built from Roblox's own emitters, beams, lights and a controller script, checked by looking at Studio.

## Do
- This is for effects the user wants native to Roblox. One they asked you to make and send is made in Blender (`vfx_column`, `vfx_create`) and sent with `send_to_engine`: it arrives as a flipbook of its own frames. Here, build with `roblox_build`.
- One Model. An invisible Ball `Part` at the centre (`Transparency` 1, collisions off) holds every emitter, attachment, beam and the light. Units are studs; R below is the effect's radius.
- Depth comes from layers, inside out: a dark core (Ball, `Neon`, colour near black, 0.6 R across); two `ForceField` shells of different size and colour (1.35 R and 2 R; this material draws moving energy on the silhouette); plasma emitters filling the volume; sparks leaving the surface; one faint halo.
- Emitters: `LockedToPart` true, `Shape` Sphere, `ShapeStyle` Volume (plasma) or Surface with `ShapeInOut` Outward (sparks, `Drag` 3-4, `Speed` 5-11). `LightEmission` 1 glows, 0 gives dark smoke. Size and Transparency are `nseq` curves that start and end invisible. Keep Rate x Lifetime under about 150 particles for the whole effect.
- Electric arcs: `Beam`s between `Attachment` pairs on the shell (`{"ref": id}`), `CurveSize0/1` about R with opposite signs, `Segments` 24, `Width0` 0.15 to `Width1` 0.03, `FaceCamera` true, `LightEmission` 1. Ten or more, not three.
- Textures: only these, they ship with Roblox. Under `rbxasset://textures/particles/`: `forcefield_glow_main.dds` (soft plasma), `forcefield_vortex_main.dds` (swirl), `sparkles_main.dds`, `fire_main.dds`, `fire_sparks_main.dds`, `smoke_main.dds`, `explosion01_core_main.dds`, `explosion01_implosion_main.dds`, `explosion01_shockwave_main.dds`. Also `rbxasset://textures/glow.png` and `sparkle.png`. Never write an `rbxassetid://` number you were not given.
- Motion without code: node `attributes`. `Spin` {"v3": [0, 32, 9]} (degrees per second; give the two shells opposite signs), `Pulse` 0.03-0.07 on parts and the light, `Flicker` about R on every arc. They play in Edit mode and in Play, and the model's `Enabled` attribute stops and starts everything.
- Custom behaviour (triggers, damage, UI): add a `Script` node with `props.Source` (Luau below). Scripts run in Play only.
- Read `refused_by_studio` in the reply and fix every line. Then `roblox_look` (with `focus`, unless the user said to leave their camera), judge it against the request, and rebuild under the same name. At least two rounds.
- Report what you saw in a screenshot, and separately what only happens in Play and is untested.

## Code
```json
{"class": "Part", "name": "Field", "props": {"Shape": "Enum.PartType.Ball", "Size": {"v3": [7, 7, 7]}, "CFrame": {"cf": [0, 7, 0]}, "Transparency": 1, "CanCollide": false},
 "children": [
  {"class": "ParticleEmitter", "name": "Plasma", "props": {"Texture": "rbxasset://textures/particles/forcefield_glow_main.dds", "LockedToPart": true, "Shape": "Enum.ParticleEmitterShape.Sphere", "ShapeStyle": "Enum.ParticleEmitterShapeStyle.Volume", "Rate": 26, "Lifetime": {"range": [0.9, 1.6]}, "Speed": {"range": [0.2, 1.2]}, "RotSpeed": {"range": [-140, 140]}, "LightEmission": 0.9, "Size": {"nseq": [[0, 2], [0.5, 3.8], [1, 1.6]]}, "Transparency": {"nseq": [[0, 1], [0.25, 0.55], [1, 1]]}, "Color": {"cseq": [[0, 1, 0.15, 0.75], [1, 0.45, 0.15, 1]]}}},
  {"class": "Attachment", "name": "A1", "id": "a1", "props": {"Position": {"v3": [3.8, 0, 0]}}},
  {"class": "Attachment", "name": "B1", "id": "b1", "props": {"Position": {"v3": [-1, 2, 3.4]}}},
  {"class": "Beam", "name": "Arc1", "attributes": {"Flicker": 4}, "props": {"Attachment0": {"ref": "a1"}, "Attachment1": {"ref": "b1"}, "CurveSize0": 3.5, "CurveSize1": -3.5, "Segments": 24, "FaceCamera": true, "Width0": 0.15, "Width1": 0.03, "LightEmission": 1, "Color": {"cseq": [[0, 1, 1, 1], [1, 0.3, 0.9, 1]]}}},
  {"class": "PointLight", "name": "Glow", "props": {"Color": {"c3": [0.65, 0.3, 1]}, "Range": 26, "Brightness": 2.4}}]}
```
```lua
local model, RunService = script.Parent, game:GetService("RunService")
local shell = model:WaitForChild("ShellOuter")
local arcs, random, wait = {}, Random.new(), 0
for _, item in ipairs(model:GetDescendants()) do if item:IsA("Beam") then table.insert(arcs, item) end end
local beat = RunService.Heartbeat:Connect(function(dt)
	if model:GetAttribute("Enabled") == false then return end
	shell.CFrame = shell.CFrame * CFrame.Angles(0, 0.55 * dt, 0)        -- inner shell: a negative speed
	wait = wait - dt
	if wait <= 0 then wait = 0.05
		for _, arc in ipairs(arcs) do arc.CurveSize0, arc.CurveSize1 = random:NextNumber(-4, 4), random:NextNumber(-4, 4) end
	end
end)
model:GetAttributeChangedSignal("Enabled"):Connect(function()
	local on = model:GetAttribute("Enabled") ~= false
	for _, item in ipairs(model:GetDescendants()) do
		if item:IsA("ParticleEmitter") or item:IsA("Beam") or item:IsA("PointLight") then item.Enabled = on end
	end
end)
model.Destroying:Connect(function() beat:Disconnect() end)
```

## Avoid
- One glowing ball plus sparkles: it reads as a bubble. Layer core, shells, plasma, arcs, sparks and halo.
- Thick, smooth beams: they read as hoops. Thin, many, and flickering.
- Saying "it will animate" without a controller script, or claiming a look you have not seen in `roblox_look`.

Check: `roblox_build` returns no `refused_by_studio` lines, and a `roblox_look` screenshot shows a dark core, coloured shells, arcs and sparks that match the request.
