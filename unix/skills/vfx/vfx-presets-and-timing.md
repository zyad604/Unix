---
name: vfx-presets-and-timing
domain: vfx
when: Adding fire, smoke, an explosion, sparks, magic, a shockwave, a shield, rain, snow or dust.
---

# VFX presets and timing
Pick the right `vfx_create` preset and give it believable timing and layering.

## Do
- Match the request to a preset: flame/torch -> fire; chimney/steam -> smoke; blast -> explosion; impact/weld -> sparks; spell/portal -> magic_swirl; ground pulse -> shockwave; force field -> energy_shield; weather -> rain or snow; footstep/landing -> dust_burst.
- No preset fits and it rises, jets or turns about an axis (whirl, tornado, plume, geyser, jet flame, beam, vortex): `vfx_column`. Set the silhouette first with `profile` (funnel: narrow at 0, wide at 1; jet: wide then tapering to the tip; pillar: flat). Then the motion: a whirl has `swirl` 0.3-1, `twist` 1-3, `hollow` 0.3-0.7 and `sway` 0.2-0.4; a plume or a jet has none of them, only `rise_m_s`. `carried` 200-600 adds embers or debris. Look, change two or three numbers, look again.
- Fire and explosion are simulated by default (`quality: 'realistic'`): a real gas simulation, baked in about a minute. `detail` 72 is the default, 96 is sharp and takes a few minutes, 128 is for final shots. `quality: 'fast'` is an instant stylised stand-in: use it only when asked for speed or a cartoon look.
- Set `size_m` to the real size: a campfire is ~0.6, a bonfire ~1.5, a muzzle flash ~0.2, a storm plane 20+.
- Timing at 24 fps: an explosion flash lasts 2-4 frames, its smoke 20-40; a shockwave pulse 12-24 frames; a spark burst dies in 30-48; dust settles in 30-45. Continuous effects (fire, smoke, rain, snow, magic_swirl, energy_shield) loop, so `duration_frames` only sets how long they play.
- Layer a blast: explosion + shockwave + sparks + dust_burst at one `location` and `start_frame`, same `size_m`. Build them in that order so the flash reads first.
- `start_frame` offsets per layer by 0-2 frames keep it from looking synthetic.
- Colour temperature: warm fire (1.0, 0.4, 0.05); cold magic (0.4, 0.5, 1.0); toxic (0.3, 1.0, 0.2). Leave `color` unset for the natural look.
- `intensity` scales brightness and particle count together; stay in 0.5-2.0 for a plausible result.

## Code
```python
scene = C.scene
scene.render.fps = 24
fps = scene.render.fps
# frame counts for a one-shot blast, derived from seconds so they track the fps
timings = {"flash": max(2, round(3 / 24 * fps)), "shockwave": round(0.6 * fps),
           "spark_life": round(1.8 * fps), "dust_settle": round(1.6 * fps)}
# make room on the timeline for the longest layer before building effects
scene.frame_end = max(scene.frame_end, scene.frame_start + 120)
result = {"fps": fps, "frames": timings, "frame_end": scene.frame_end}
```

## Avoid
- A 1-frame explosion flash: it flickers and reads as a glitch. Use 2-4.
- Judging fire from one still frame, or telling the user it is done without saying how to watch it: effects move only while the timeline plays (Play on the island).
- One giant effect for a big blast: layer flash, wave, sparks and dust instead of scaling one up.
- Scenery round an effect that was asked for on its own. Give it a plain ground, a dark world if it glows (daylight for smoke and dust) and a camera that holds all of it; the effect is the picture.
- Leaving `duration_frames` short on a one-shot so it is cut off mid-fade; let it run to the faded end.

Check: `vfx_create` returns a `frame_range` that fits inside `scene.frame_end`, then `look` with `shading: "rendered"` on the returned root shows the effect mid-action.
