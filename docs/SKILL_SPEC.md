# Skill specification

A skill is a short expert playbook that the agent loads (`skill_load`) just before a stage of work. It is read by a model that already knows Blender in general, so a skill earns its place only by supplying what the model would otherwise get wrong: decisions, numbers, order of operations, version-correct code and a pass condition.

## File

`unix/skills/<domain>/<name>.md`, one skill per file, kebab-case name equal to the file name.

```
---
name: <kebab-name>
domain: <folder name>
when: <one sentence: the situation that should trigger loading this skill>
---

# <Title>
<One line: the outcome this skill produces.>

## Do
- 5 to 10 bullets. Decisions and rules with real numbers, in the order work happens.

## Code
```python
# The core operation(s), minimal and version-correct. 30 lines at most in total.
```

## Avoid
- 2 to 4 failure modes that actually happen, each with its symptom.

Check: <one line naming the Unix tool(s) to run and the condition that counts as a pass>
```

## Rules

1. **Length.** 60 lines and about 350 words at most. If it needs more, it is two skills.
2. **Code is for `run_python`.** Available names: `bpy`, `bmesh`, `mathutils`, `math`, `Vector`, `Matrix`, `Euler`, `Quaternion`, `Color`, `C`, `D`. Prefer `bpy.data` / `bmesh` / direct property assignment over `bpy.ops`. When an operator is unavoidable, set the active object and selection explicitly, and say so. No file operators, no `os`/`subprocess`/network, no `bpy.ops.wm.*` file calls. Return facts in a `result` variable.
3. **Every code block is tested.** Run it in headless Blender 4.5 LTS (`blender -b --factory-startup --python <file>`) and keep it only if it runs without error and does what the skill says. Where the API differs in Blender 5.x and you are sure of the difference, add a one-line note; do not guess.
4. **Only real tools.** A skill may name these tools and no others: `scene_info`, `object_info`, `run_python`, `look`, `render_image`, `check_mesh`, `check_uv`, `check_materials`, `check_scene`, `set_material`, `import_file`, `export_file`, `save_snapshot`, `skill_list`, `skill_load`, `api_lookup`, `update_plan`, `ask_user`. If a skill only makes sense with a tool that does not exist (AI image/mesh/video generation, job queues, asset search), delete the skill.
5. **No menu paths as instructions.** "Ctrl+A > Scale" is useless to an agent; give the API call. A menu name may appear only to help explain something to the user.
6. **Real-world numbers.** Meters, degrees, pixels per meter, sample counts, watts. Give the typical value and the range.
7. **The Check line is measurable.** It names a tool and a condition (`check_mesh` reports 0 non-manifold edges and `watertight: true`), or a specific thing to confirm in a `look` image.
8. **No duplication.** One skill per topic. Merge overlapping skills and delete the leftovers. Verification steps belong in the `Check:` line of the skill they verify, not in a separate "x-verification" skill, unless the verification is a procedure of its own.
9. **Original work.** Write from knowledge of Blender and 3D practice. Do not read or copy from any other product's prompts, skills or tool definitions.
10. **Plain writing.** Short sentences, imperative mood, no filler, no marketing words.
