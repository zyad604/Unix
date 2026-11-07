---
name: safe-changes
domain: workflow
when: Before deleting, applying, joining, decimating or bulk-editing existing objects, or when a script was refused by the safety guard.
---

# Safe Changes
Destructive work that the user can always get back from.

## Do
- One turn is one undo step: a single Ctrl+Z reverts everything you changed for that message. Say so after a large change.
- The add-on saves a copy of the .blend before the first change of each turn (if snapshots are enabled; the newest 20 are kept). It does not cover later steps of the same turn.
- Call `save_snapshot(label=...)` before a destructive step mid-turn: applying modifiers, joining, deleting, remeshing or decimating user objects, or a batch edit over about 20 objects. Give the returned path in your reply.
- Prefer reversible forms: live modifiers, hiding (`obj.hide_set(True)`, `obj.hide_render = True`) instead of deleting, and a copy in a hidden `_Backup` collection as below.
- Do not delete or rename user-made objects, and do not change render settings, the active camera or the frame range, unless asked.
- The script guard refuses imports of `subprocess`, `socket`, `ctypes`, `shutil`, `urllib`, `http`, `requests`; `os.remove`, `os.rename`, `os.system`; `eval`, `exec`, `compile`, `__import__`; `bpy.ops.wm.open_mainfile`, `save_mainfile`, `save_as_mainfile`, `revert_mainfile`, `read_homefile`, `read_factory_settings`, `quit_blender`, `save_userpref`, `url_open`; and add-on install, remove or disable.
- Use the tools instead: `import_file`, `export_file`, `render_image`, `save_snapshot`.

## Code
```python
src = D.objects["Chair_Seat"]                       # about to be changed destructively
bak_col = D.collections.get("_Backup") or D.collections.new("_Backup")
if not C.scene.user_of_id(bak_col):
    C.scene.collection.children.link(bak_col)
bak_col.hide_viewport = bak_col.hide_render = True
bak = D.objects.get(src.name + "_bak")
if bak is None:                                     # a re-run keeps the first backup
    bak = src.copy()                                # copies transforms, modifiers, material slots
    bak.data = src.data.copy()                      # own mesh, or the backup changes with the source
    bak.name = src.name + "_bak"
    bak_col.objects.link(bak)
result = {"backup": bak.name, "verts": len(bak.data.vertices), "visible": bak.visible_get()}
```

## Avoid
- `bpy.ops.ed.undo()` or `undo_push` in a script: it breaks the turn's single undo step and invalidates every object reference.
- Rewording a refused script to get past the guard: it is refused again. Switch to the tool that does the job.
- Applying modifiers "to clean up": the user loses the editable stack. Apply only on an export or print copy.

Check: `save_snapshot` returned a path before the destructive `run_python`, and `scene_info(collection="_Backup")` lists the `_bak` copy.
