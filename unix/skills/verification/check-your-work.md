---
name: check-your-work
domain: verification
when: Any build or edit is finished and you are about to tell the user it is done.
---

# Check your work
A finished task backed by evidence: measured, seen from two sides, fixed or flagged, and reported truthfully.

## Do
- Re-read the request and list what it asked for: objects, counts, sizes, materials, placement, shot, file.
- `scene_info`: every requested object exists under a meaningful name, counts match, dimensions are within 10 % of real-world size, nothing was built twice. `object_info` on edited parts: world bounds start at Z = 0 for things on the floor, scale (1, 1, 1), parent, modifiers.
- `look` from at least two different views (`persp` + `front`, or `persp` + `persp_back`), `solid` shading. Add one `rendered` view if materials or lights were in scope, and `use_scene_camera` if the task was a shot. Judge with `visual-critique`.
- Run the checks that match the work, on the objects you made: modelled: `check_mesh`; unwrapped: `check_uv`; shaded: `check_materials`; always: `check_scene`.
- Fix every issue you caused, then re-run that check and `look` again. Stop after 3 rounds and flag what remains.
- Leave issues in the user's own objects alone; mention them.
- Report in four parts: what changed, what was verified and by which tool, what was not verified (playback, the export opened elsewhere, a final render), what is still imperfect.

## Code
```text
scene_info()                                              names, counts, dimensions, Z against the request
object_info(name=<each edited part>)                      world bounds on the floor; scale 1,1,1; parent
look(targets=[...], views=["persp","front"])              form, proportion, contact
look(targets=[...], views=["persp"], shading="rendered")  only if materials or lights were in scope
look(use_scene_camera=true, shading="rendered")           only if the task was a shot
check_mesh(objects=[...])                                 0 non-manifold, 0 duplicate verts, 0 zero-area, no issues
check_uv(objects=[...], texture_size=2048)                0 % outside 0-1, 0 zero-area, density spread under 4
check_materials(objects=[...])                            issues empty
check_scene()                                             issues empty or explained
fix -> re-run the failed check -> look again              at most 3 rounds, then flag
```

## Avoid
- Reporting done because `run_python` returned `ok`: scripts succeed while building the wrong thing.
- One view only: floating and intersecting parts hide behind the front face.
- "Verified" for things no tool measured, such as animation timing or how an export opens elsewhere.
- Renaming or repairing the user's own objects to silence `check_scene`.

Check: every `issues` list from `check_mesh`, `check_uv`, `check_materials` and `check_scene` is empty or named in the reply with a reason, and two `look` views show none of the faults listed in `visual-critique`.
