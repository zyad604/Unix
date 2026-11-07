---
name: task-planning
domain: workflow
when: A request needs three or more steps, or it is unclear what to build first or whether to ask the user.
---

# Task Planning
A short visible plan whose steps each end in something checkable, and at most one question to the user.

## Do
- Call `scene_info` first. Editing the user's object is a different plan from a new build.
- Plan when the work has 3 or more steps. One or two steps: just do them.
- Write 3 to 7 steps. Each step is an outcome with a number, not an activity: "Block out desk 1.4 x 0.7 x 0.75 m", not "Start modelling".
- Order: blockout, refine form, UVs, materials, lights and camera, checks. The last step is always verification.
- Send the whole list on every `update_plan` call. Exactly one step is `in_progress`. Mark it `done` only after its `look` or check passed.
- Call `ask_user` only when a wrong guess costs more than about 5 tool calls: unknown subject, style, real-world size, or a destructive choice on the user's objects. One question, 2 to 4 options, your default first.
- Everything else: decide, and state the assumption in the final reply ("assumed 0.75 m desk height").
- Load skills with `skill_load` as you reach each step, not all at the start.
- When a check fails or the user changes the brief, rewrite the remaining steps instead of pushing on.

## Code
```text
scene_info()
ask_user(question="Which desk style?", options=["Modern, flat panels", "Classic, turned legs"])  # only if needed
update_plan(steps=[
  {"title": "Block out desk 1.4 x 0.7 x 0.75 m", "status": "in_progress"},
  {"title": "Bevels, leg joints, drawer front", "status": "pending"},
  {"title": "Oak and steel materials", "status": "pending"},
  {"title": "Verify: check_mesh, check_scene, look from 2 views", "status": "pending"}])
skill_load(names=["scripting-patterns"])
run_python(...) then look(...)                     # step 1
update_plan(steps=[...])                           # step 1 "done", step 2 "in_progress", rest unchanged
```

## Avoid
- One plan step per tool call: 15 steps the user cannot follow. Group by outcome.
- Asking what `scene_info` already answers, or asking a second question: the user waits each time.
- Marking all steps done in one call at the end: the panel shows no progress, then jumps.

Check: the last `update_plan` shows every step `done`, and each step was marked only after its `look` or check tool passed.
