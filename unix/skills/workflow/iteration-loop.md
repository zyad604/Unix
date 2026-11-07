---
name: iteration-loop
domain: workflow
when: Building or editing anything in more than one script, or a result looks wrong and needs correcting.
---

# Iteration Loop
Each build step is run, seen, measured and corrected before the next one starts.

## Do
- One logical step per `run_python` call, under about 80 lines: blockout first, then one refinement at a time.
- Return the facts you will judge by in `result`: names, dimensions in meters, vertex counts, lowest Z.
- Read `changes` in the reply. Unexpected created, modified or deleted names: stop and find out why.
- `look` after every step that changes shape, placement, materials or lights. `shading="solid"` with views `["persp", "front"]` for form, `shading="rendered"` for materials and lighting, `use_scene_camera=true` for a shot. Pass `targets`; add `isolate=true` when something blocks the view.
- Judge the image as a critic: proportions, floating or intersecting parts, dark or inside-out faces, flat lighting. Name the defect before fixing it.
- Measure what an image cannot show. `object_info`: dimensions within 2 % of the target, base at Z = 0 within 1 mm. `check_mesh` after modelling.
- Fix the smallest cause with an edit script, not a rebuild. Then `look` again from the same view to compare.
- Three failed fixes for the same defect: stop. Change the approach, use `api_lookup` if the API was the problem, or tell the user what is still wrong.

## Code
```text
run_python(code=<blockout>, purpose="Block out chair")          # result: dims, min_z
look(targets=["Chair_Seat", "Chair_Back"], views=["persp", "front"])
object_info(name="Chair_Seat")                                  # seat top at 0.45 m?
run_python(code=<move back rest down 0.02 m>, purpose="Close gap under back rest")
look(targets=["Chair_Seat", "Chair_Back"], views=["front"])     # same view: gap closed?
check_mesh(objects=["Chair_Seat", "Chair_Back"])
```

## Avoid
- Building everything in one long script: one error leaves a half-built scene and no image to judge.
- Skipping `look` because the numbers are right: floating parts and flipped normals pass every number.
- Re-running a failing script with small guesses: fourth attempt, same error.

Check: the final `look` from two views shows no defect you can name, and `object_info` dimensions are within 2 % of the stated target.
