# Unix

An AI 3D artist inside Blender. You type what you want; it reads your scene, builds in small steps, looks at its own work, checks it and reports back. It runs on your own Claude CLI or Codex CLI logins, with no API keys, no other server and no account of its own.

Status: v0.1, working end to end in Blender 4.5 LTS. Original code, GPL-3.0-or-later.

## The app

`Unix.lnk` (in this folder) starts the standalone app: `app/Unix/Unix.exe`.

It is a portable application built on the Blender 4.5 engine: its own executable, icon and splash, the Unix add-on built in, the purple theme, and its own settings in `app/Unix/portable`, separate from any Blender you have installed.

- **Beginner**: only the viewport, the Unix island and a three-item top bar (File, Undo, Redo). Blender's own headers, toolbars, side panels and status bar are hidden. Switch with the Beginner / Advanced buttons above the island or in the top bar.
- **The island**: click the text box and type; Enter sends, Shift+Enter adds a line, Esc leaves the box. Tabs pick the agent mode: **Agent** (anything), **3D** (model an object, or attach a photo of one), **Image** (generate an image), **Material** (surface the selected objects), **VFX** (fire, explosions, magic, weather, and sprite sheets of them), **Engine** (one-click send of the selection to Unity, Unreal, Godot or Roblox), **Library** (find free HDRIs, materials and models). **Attach** adds reference files, and **Ctrl+V** attaches a picture you copied (a screenshot, "Copy image" in a browser) or files copied in Explorer, with the mouse over the island or while typing, **Model** switches between Claude models and Codex, **Effort** sets how hard it thinks (Low is fastest, Max is most thorough), the **Auto** switch stops it asking questions, and **Send** becomes **Stop** during a run. History, New and **Copy** are at the top right (Copy offers Unix's last reply, everything it did with its scripts and results, or the whole chat); the mouse wheel scrolls the conversation.
- **Goals**: type `/goal` and then what you want, in full. Unix works on it round after round, for up to 24 hours, without asking you anything. When a round says it is finished, a fresh agent that did not build it checks the scene against your words, and only its yes ends the goal; what it finds missing goes back into the work. A failed round (a usage limit, the network) waits and tries again. The goal's line above the text box shows the round, the time and the cost. Stop (or `/goal stop`) ends it; `/goal status` says where it stands; `/goal continue` picks up a goal that was cut off when Blender closed.
- **Move and size it**: drag the top strip (or the status pill) to move the island, drag the grip in its top-right corner to change its width and height, and use **Size – +** (or Ctrl + mouse wheel over it) to make everything smaller or bigger. The layout is remembered.
- **Game engines**: the Engine tab lists the engine projects found on this computer, open editors first. Click one and the selected objects are exported with that engine's format, scale and axes into the project (Unity `Assets/Unix`, Unreal `Content/Unix`, Godot `res://unix`), where the engine imports them. Sending the same asset again updates it in place. Roblox Studio has no project folder, so it gets a live link instead: Unix keeps a small plugin in Studio, and what you send is built inside the place you have open. Without the plugin you get a file plus a **drag tile**: a small window you drag into Studio, or into any other application.
- **Advanced**: the full Blender interface, with the Unix panel in the sidebar (`N`).
- **Play**: the button next to the status pill plays the animation on a loop in a shaded view. Effects only move while it plays, and it starts by itself after Unix builds one.
- Switch modes from the splash or the buttons at the left of the top bar.

Rebuild it with `python scripts/build_app.py`. After editing the add-on or the template, `python scripts/build_app.py --sync` copies the changes into the app.

The window title still reads "Blender", because that text is compiled into the engine.

## Giving it to people (the installer)

`python scripts/build_release.py` builds `dist/Unix-Setup-<version>.exe` (about 370 MB). It takes only the engine and the add-on from `app/`: none of your settings, chats, snapshots, exports or projects. The name is `NAME` at the top of the script, and the logo comes from `scripts/make_unix_logo.py`.

- Double-clicking the file installs the app for that Windows user, with no admin rights needed. It goes to `%LOCALAPPDATA%\Programs\Unix` and adds a Start menu shortcut, a desktop shortcut and an entry in Settings > Apps. Installing a newer version over an old one keeps that person's settings and chats.
- The uninstaller asks whether to keep the person's chats and settings.
- Each person needs Claude Code installed and signed in with their own Claude account. The installer tells them if it is missing.
- The engine reads `brand.json` next to it, and the chat, the agent's own name and the window title then use the app's name. Without that file (your own `foxa` app) everything stays "Unix".
- The installer is not code-signed, so Windows SmartScreen shows "Windows protected your PC": More info > Run anyway. A code-signing certificate removes this.
- Licences: Blender is GPL-3.0-or-later, and so is this add-on. `ABOUT.txt` in the installed folder says where Blender's source is.

## Requirements

- Blender 4.2 or newer (developed on 4.5 LTS).
- The Claude CLI, signed in: `npm i -g @anthropic-ai/claude-code`, then `claude auth login` once.
- Optional: the Codex CLI, signed in with `codex login`. It adds AI image and texture generation. (The model list offers Claude Sonnet and Opus, every version from 4.6 to 5.5; the Codex agent loop is still in the code but is not listed.) No API key is used.

## Install as an add-on instead

```powershell
.\scripts\install.ps1            # links the add-on into Blender 4.5 and enables it
.\scripts\install.ps1 -Uninstall
```

The installer makes a directory link, so edits in this folder are live after restarting Blender. Or zip the `unix` folder and use *Preferences > Add-ons > Install from Disk*.

## Use

1. In the 3D viewport press `N` and open the **Unix** tab.
2. Type a request and press **Send**. `Ctrl+Shift+Space` opens a quick prompt from anywhere in the viewport.
3. Watch the viewport. A purple frame shows while Unix is working, and edits in the viewport are locked (navigation still works). **Stop** ends the run; **Continue** resumes it.
4. `Ctrl+Z` once undoes everything from that turn. The arrow beside any of your messages restores the scene to before that message (it opens a restored copy; your file on disk is not touched).

The Unix island floats in the viewport: mode tabs, the conversation, a text box you type into, and Attach / Auto / model / Send.

Panel header buttons: new chat, chat history, this file's rules, data folders, the Unix theme, show or hide the viewport card. The `+` beside the message box attaches reference images or 3D files.

Every message automatically tells Unix what you have selected and what you changed by hand since its last turn.

## What it can do

| Tool | Purpose |
|---|---|
| `scene_info`, `object_info` | Read the scene and one object in detail |
| `run_python` | Run a script in Blender, with a safety guard, a deadline and a report of what changed |
| `look`, `look_uv`, `view_image` | Render quick views (or your live viewport), the UV layout, or an image file, and see them. Bones are drawn solid inside a see-through mesh; `focus_bones` gives a cutaway close-up of small parts |
| `render_image` | Final still through the scene camera |
| `check_mesh`, `check_uv`, `check_materials`, `check_scene` | Measured quality checks. `check_scene` also counts what was built by kind (a house made of walls, roof, door and chimney is one house), and finds surfaces that flicker because two lie in the same plane, an object hidden inside another, different things standing in each other, and things that touch nothing that stands on the ground. Parts of one thing pushed into each other are normal and never reported. After a turn that changed the scene Unix runs this itself and goes back once for anything new |
| `look_map` | A labelled top-down map: every asset numbered, with an arrow for the way it faces |
| `set_material` | Correct Principled PBR material in one call, with a receipt of what drives the surface |
| `place_object` | Size in meters, seat on the ground or another object, keep clear of neighbours |
| `light_rig` | Three-point, product, outdoor sun or overcast lighting sized to the subject |
| `asset_search`, `asset_hdri`, `asset_material`, `asset_model` | Free CC0 HDRIs, scanned materials and models from Poly Haven |
| `generate_image`, `generate_texture` | AI concept images, front/side/back reference sheets and tiling PBR materials (OpenAI through your Codex login) |
| `reference_search`, `trace_reference`, `profile_solid`, `lathe_solid`, `project_reference`, `look_around` | Look up freely licensed photos of a real thing (Wikimedia Commons, no key) and study them before modelling; read the true outline, openings included, out of a square-on photo and build it as a solid at real size; make every part from an outline with a truly rounded cross-section (big parts follow the real silhouette, and each carries the photo surface where it sits); make round parts with their bore; lay the photo over the finished form as its surface; and see all six sides in one picture. When you ask for something realistic, Unix is also sent back after its first version to compare it with the photos and improve it (preference: Review realistic work against real photos) |
| `match_reference` | Image to 3D by modelling: your reference and its render side by side, corrected over a few rounds |
| `find_limbs`, `rig_chain` | Measure any creature's limbs (arms, legs, tails, fingers, toes, wing spars) and build bone chains on the measured centre lines, or through points moved to the middle of the mesh |
| `rig_bind`, `check_rig` | Bind a mesh to an armature without it moving or tearing (rotated parents, meshes split into touching pieces, loose nails), and measure the rig: bones outside their part, seams that open, weights that bleed |
| `motion_search`, `retarget_motion` | Real recorded human motion on any rig: search the CMU motion capture library (about 2,600 recordings of real people, free for any use, no key) and put a clip on the character whatever its bone names, size and rest pose. Planted feet stay planted, the real shoe soles stay on the floor, a cycle is found and closed for game loops (`loop`, `in_place`), `mirror` gives the other side, `blend_from`/`blend_to` join starts and stops to the idle and the cycle. Also takes an imported BVH or FBX armature as the source |
| `work_in_parallel` | Working as a team: the agent splits a job into two to four parts (a building, its site, its light) and each is built at the same time by an agent of its own with the same tools, then the lead joins them up and checks the whole. Faster, and each part gets the attention detail needs. The island shows a chip per helper with what it is doing and a ✕ to stop it |
| `look_motion`, `check_motion` | See and measure an animation: a strip of frames with one scale and the floor drawn (the camera travels with a moving body), and a report of feet in or above the floor, sliding, limbs through each other, IK out of reach, pops, loop seams, a rigid torso and actions that would be lost on save. A turn that made character animation without watching it is sent back once to watch and fix it |
| `vfx_create`, `vfx_flipbook` | Animated effects from presets, and sprite sheets of them for game engines. Fire and explosion are real gas simulations by default (baked in about a minute); `quality: 'fast'` gives an instant stylised stand-in |
| `vfx_column` | A column of fire, smoke, dust, steam or energy shaped by numbers (radius profile, swirl, twist, sway, things carried round): fire whirls, dust devils, plumes, jet flames, beams. A volume shader and geometry nodes with no bake, so it is built at once |
| `fuse_meshes` | Merge a blockout of separate parts into one continuous, watertight body for characters and creatures |
| `send_to_engine` (Roblox Studio) | A live link: a plugin installed into Roblox Studio builds what you send inside the open place, in front of the Studio camera. Boxes, cylinders, balls and wedge-shaped prisms (ramps, gable roofs) become native Parts and every other shape a MeshPart. A surface arrives whole: its colour, normal, roughness and metal pictures (up to 1024 px), what its alpha cuts out and the light it gives off become a SurfaceAppearance, and a look made by shader nodes is baked into such pictures on the way. An effect made in Unix that plays on the spot (a fire, a column, a plume) arrives as itself: its frames are filmed and Roblox plays them as a looping flipbook; bursts, weather and orbs are rebuilt from Roblox's own particle emitters. Small things travel together, a heavy mesh is cut into pieces of 10,000 triangles instead of being simplified, a shape used many times is sent once, and motion from Blender's timeline comes along. A rigged character arrives rigged: its meshes skinned to the armature's bones, and every action of the rig as an animation that loops while you edit and in Play (the model's `Animation` attribute picks one by name), with no animation asset to upload. With several Studio windows open Unix asks which place. Roblox keeps a mesh, a surface's pictures and an effect's pictures only as assets on your Roblox account: without that upload a mesh and its pictures look right while you edit and become a checkered box in Play, and an effect is only a stock-particle stand-in, so the first time a send needs it Unix asks (Upload always / this time / preview only; also in the add-on preferences) |
| `roblox_build`, `roblox_look`, `roblox_query`, `roblox_remove` | Work inside the open Roblox Studio place, for what belongs in Roblox itself: Luau scripts, UI, controls, and things you ask to have built from Roblox's own parts and emitters (animated in Edit mode and in Play). A thing you ask to be made and sent is made in Blender and sent, never built here instead: the first native build of a message is held back once with that rule. Also: take a screenshot of Studio to check the result, read the place, remove earlier attempts |
| `engine_targets`, `send_to_engine` | Find Unity, Unreal, Godot and Roblox projects and send assets straight into them |
| `import_file`, `export_file` | Bring files in; export and verify the result |
| `save_snapshot` | Safety copy of the .blend |
| `skill_list`, `skill_load` | Expert playbooks with tested code, loaded per stage of work |
| `api_lookup` | The real API of the running Blender version |
| `update_plan`, `ask_user` | A visible plan and a question when it matters |

## Folder map

```
unix/        the add-on (see docs/ARCHITECTURE.md)
  bridge/           model <-> Blender transport and main-thread dispatch
  agent/            Claude CLI runner
  tools/            what the model can do in Blender
  skills/           playbooks by domain (format: docs/SKILL_SPEC.md)
  prompts/          system prompt
  ui/               chat panel, operators, preferences, viewport halo
  core.py           the engine
app/                the built standalone app (generated; about 900 MB)
app_template/       what turns the engine into Unix: splash menu, modes, top bar, launcher source
branding/           logo, icon and splash (generated by scripts/make_branding.py)
docs/               architecture and the skill specification
scripts/            build_app.py, make_branding.py, app_setup.py, install.ps1, run_tests.ps1
tests/              bridge tests (no Blender), tool tests (headless Blender), one real end-to-end turn
```

## Tests

```powershell
.\scripts\run_tests.ps1          # bridge, tools, engine and skills; no model calls, no internet
.\scripts\run_tests.ps1 -Agent   # also one real turn (uses your Claude usage)
```

## Safety model

- The model can act only through the tools above. The CLI's own file and shell tools are disabled.
- Scripts are checked before they run: file deletion, subprocess, network and file-open/save operators are refused unless you enable *Unrestricted scripts* in the add-on preferences. This is a guard against mistakes, not a sandbox.
- The bridge listens on 127.0.0.1 with a random port and a per-session token.
- The only internet access is the asset tools, which talk to polyhaven.com hosts only, verify each file and can be switched off in the preferences.

## Limits

See *Known limits* in `docs/ARCHITECTURE.md`.
