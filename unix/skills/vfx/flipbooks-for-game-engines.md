---
name: flipbooks-for-game-engines
domain: vfx
when: Exporting an effect's animation as a sprite-sheet texture for a game engine.
---

# Flipbooks for game engines
Turn a VFX effect into one RGBA sprite sheet with `vfx_flipbook`, sized and framed for an engine.

## Do
- Reach for a flipbook when a real-time engine needs the effect as a texture (particles, projectiles, impacts, UI FX) rather than as Blender geometry.
- Choose `frames` by motion: a quick impact 8-16, fire or smoke 16-32, a slow loop 32-64. More frames are smoother but a bigger texture.
- Keep the whole sheet a power of two: `cell_px` x `columns` and `cell_px` x `rows` should each land on 256, 512, 1024 or 2048. 16 frames at 256 in a 4x4 grid is 1024x1024.
- A square-ish grid (`columns` near the square root of `frames`) wastes the least texture; override `columns` only when the engine wants a single row or a fixed layout.
- Loop-friendly presets (fire, smoke, rain, snow, magic_swirl, energy_shield) tile seamlessly; one-shots (explosion, shockwave, sparks, dust_burst) should run to their faded end so the last cell is empty.
- `view` 'front' suits most effects; 'top' suits ground rings and splashes; 'camera' bakes the real shot.
- The sheet is transparent (RGBA) with the first frame top-left, filling left to right, top to bottom.

## Engine import
- Unity: import as Sprite (2D/UI), Multiple, slice by cell count; play via a flipbook animation or the Shader Graph Flipbook node.
- Unreal: import as Texture, drive a material with the SubUV / Texture Sampler and a Flipbook/SubImage index over columns x rows.
- Godot: use AnimatedSprite2D with a SpriteFrames sheet, or a CanvasItem shader stepping UVs over the grid.
- Roblox: ImageLabel with a sprite sheet, stepping ImageRectOffset by ImageRectSize across the cells.

## Code
```python
import math
frames, cell = 16, 256
cols = rows = max(1, round(math.sqrt(frames)))
sheet = (cols * cell, rows * cell)
pow2 = {128, 256, 512, 1024, 2048, 4096}
scene = C.scene
scene.render.fps = 24
fps_suggestion = max(1, round(frames * scene.render.fps / 96))  # 96-frame loop -> fps
result = {"grid": [cols, rows], "sheet_px": list(sheet),
          "power_of_two": sheet[0] in pow2 and sheet[1] in pow2,
          "fps_suggestion": fps_suggestion}
```

## Avoid
- Non power-of-two sheets: many engines pad or refuse to mipmap them. Match `cell_px` x grid to a power of two.
- Too few frames for a fast effect: it strobes. Add frames or slow the playback fps.
- Baking a one-shot over too short a range so the last cells still show the effect; let it fade fully.

Check: `vfx_flipbook` returns a `file` whose sheet is `columns*cell_px` by `rows*cell_px`, and the returned preview shows the frames stepping in order with a transparent background.
