# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for the VFX tools.
Run: blender -b --factory-startup --python tests/test_vfx.py"""
import glob, json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import numpy as np
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_vfx_")  # keep test data out of the user's config
from unix import tools
tools.load_all()
from unix.tools import registry, interact, vfx  # noqa: E402
interact.interactive["enabled"] = False
fails = []

EFFECTS = vfx.EFFECTS


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"], sum(1 for c in res["content"] if c["type"] == "image")


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:500]}"))
    if not cond:
        fails.append(label)


def newest_look():
    files = sorted(glob.glob(os.path.join(_util.data_dir("looks"), "look_*.jpg")), key=os.path.getmtime)
    if not files:
        return 0.0
    img = bpy.data.images.load(files[-1])
    try:
        px = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        return float(px.reshape(-1, 4)[:, :3].var())
    finally:
        bpy.data.images.remove(img)


# -- clean start -------------------------------------------------------------
for o in list(bpy.data.objects):
    bpy.data.objects.remove(o)

# -- create every effect -----------------------------------------------------
ranges = {}
for eff in EFFECTS:
    err, text, _ = call("vfx_create", effect=eff, size_m=1.0, start_frame=1, quality="fast")
    d = json.loads(text) if not err else {}
    root = bpy.data.objects.get(d.get("root", "")) if not err else None
    ok = (not err and root is not None and len(root.children) > 0
          and "unix_start" in root and "unix_end" in root and "unix_loop" in root
          and d["frame_range"][0] == 1)
    check(f"create {eff}", ok, text)
    if root is not None:
        ranges[eff] = (int(root["unix_start"]), int(root["unix_end"]), bool(root["unix_loop"]))

check("Unix_VFX collection exists", "Unix_VFX" in bpy.data.collections)

# -- re-create same name does not duplicate ----------------------------------
obj_before = len(bpy.data.objects)
mat_before = len(bpy.data.materials)
mesh_before = len(bpy.data.meshes)
err, text, _ = call("vfx_create", effect="fire", size_m=1.0, start_frame=1, quality="fast")
check("re-create fire does not duplicate objects", not err and len(bpy.data.objects) == obj_before, text)
check("re-create fire does not duplicate materials", len(bpy.data.materials) == mat_before,
      f"{mat_before} -> {len(bpy.data.materials)}")
check("re-create fire does not duplicate meshes", len(bpy.data.meshes) == mesh_before,
      f"{mesh_before} -> {len(bpy.data.meshes)}")

# -- step through each effect's range without errors --------------------------
step_ok = True
for eff, (s, e, _loop) in ranges.items():
    try:
        for f in range(s, e + 1, max(1, (e - s) // 12)):
            bpy.context.scene.frame_set(f)
            bpy.context.view_layer.update()
    except Exception as exc:  # noqa: BLE001
        step_ok = False
        check(f"step through {eff}", False, exc)
check("step through every effect range", step_ok)

# -- rendered look for fire, explosion, magic_swirl --------------------------
for eff in ("fire", "explosion", "magic_swirl"):
    s, e, _loop = ranges[eff]
    bpy.context.scene.frame_set((s + e) // 2 if eff != "explosion" else s + 3)
    err, text, imgs = call("look", targets=["VFX_" + eff], shading="rendered", views=["persp"], size=256)
    variance = newest_look() if not err else 0.0
    check(f"look rendered {eff} returns a non-uniform image", not err and imgs == 1 and variance > 1e-5,
          f"err={err} imgs={imgs} var={variance}")

# -- flipbook on two effects -------------------------------------------------
for eff, cols_expect in (("fire", None), ("magic_swirl", None)):
    err, text, imgs = call("vfx_flipbook", root="VFX_" + eff, frames=4, cell_px=64, view="front")
    d = json.loads(text) if not err else {}
    ok = not err and imgs == 1 and os.path.isfile(d.get("file", ""))
    sheet_ok = False
    alpha_ok = False
    if ok:
        sheet = bpy.data.images.load(d["file"])
        try:
            w, h = sheet.size
            sheet_ok = (w == d["columns"] * 64 and h == d["rows"] * 64)
            px = np.empty(w * h * 4, dtype=np.float32)
            sheet.pixels.foreach_get(px)
            a = px.reshape(-1, 4)[:, 3]
            alpha_ok = bool((a > 0.0).any() and (a == 0.0).any())
        finally:
            bpy.data.images.remove(sheet)
    check(f"flipbook {eff} sheet is columns*64 x rows*64", ok and sheet_ok, text)
    check(f"flipbook {eff} has opaque and transparent pixels", alpha_ok, text)

# -- restoration: settings, frame, camera, hide_render -----------------------
scene = bpy.context.scene
# add a decoy object that must stay renderable after an isolated flipbook
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(8, 8, 0))
decoy = bpy.context.active_object
decoy.name = "Decoy"
decoy.hide_render = False
scene.render.engine = 'CYCLES'
scene.frame_set(7)
cam_data = bpy.data.cameras.new("UserCam")
user_cam = bpy.data.objects.new("UserCam", cam_data)
scene.collection.objects.link(user_cam)
scene.camera = user_cam
film_before = scene.render.film_transparent
res_before = (scene.render.resolution_x, scene.render.resolution_y)
fmt_before = scene.render.image_settings.file_format
err, text, _ = call("vfx_flipbook", root="VFX_fire", frames=4, cell_px=64, view="front")
check("flipbook restores engine", scene.render.engine == 'CYCLES', scene.render.engine)
check("flipbook restores frame", scene.frame_current == 7, scene.frame_current)
check("flipbook restores camera", scene.camera == user_cam, scene.camera)
check("flipbook restores film_transparent", scene.render.film_transparent == film_before)
check("flipbook restores resolution", (scene.render.resolution_x, scene.render.resolution_y) == res_before)
check("flipbook restores image format", scene.render.image_settings.file_format == fmt_before)
check("flipbook restores decoy hide_render", decoy.hide_render is False)
check("flipbook removed its temp camera", "_vfx_flip" not in bpy.data.objects
      and "_vfx_flip" not in bpy.data.cameras)

# -- ToolError cases ---------------------------------------------------------
err, text, _ = call("vfx_create", effect="plasma")
check("vfx_create unknown effect", err and ("one of" in text or "Unknown effect" in text), text)
err, text, _ = call("vfx_create", effect="fire", name="bad/name")
check("vfx_create bad name", err and "name" in text, text)
err, text, _ = call("vfx_create", effect="fire", size_m=5000)
check("vfx_create bad size", err and "size_m" in text, text)
err, text, _ = call("vfx_flipbook", root="VFX_nope", frames=4)
check("vfx_flipbook unknown root", err and "VFX_nope" in text, text)
err, text, _ = call("vfx_flipbook", root="VFX_fire", frames=2)
check("vfx_flipbook frames too low", err and "frames" in text, text)
err, text, _ = call("vfx_flipbook", root="VFX_fire", frames=4, cell_px=16)
check("vfx_flipbook cell_px too low", err and "cell_px" in text, text)

# -- realistic (simulated) fire and explosion ----------------------------------
scene.render.engine = 'BLENDER_EEVEE_NEXT'
for eff in ("fire", "explosion"):
    err, text, _ = call("vfx_create", effect=eff, name="sim_" + eff, size_m=1.0, start_frame=1, detail=32,
                        duration_frames=12, location=[4, 0, 0])
    d = json.loads(text) if not err else {}
    domain = bpy.data.objects.get(f"VFX_sim_{eff}_domain")
    sim = d.get("simulation", {})
    warm = 24 if eff == "fire" else 0
    check(f"realistic {eff} is simulated and baked", not err and domain is not None
          and any(m.type == 'FLUID' for m in domain.modifiers) and sim.get("frames_baked") == 12 + warm
          and os.path.isdir(sim.get("cache", "")) and d["frame_range"] == [1 + warm, 12 + warm]
          and "realistic" in d["quality"], text)
    scene.frame_set(d["frame_range"][0] + 6 if not err else 1)
    err, text, imgs = call("look", targets=["VFX_sim_" + eff], shading="rendered", views=["front"], size=256)
    variance = newest_look() if not err else 0.0
    check(f"realistic {eff} renders visible flames", not err and imgs == 1 and variance > 1e-5, f"var={variance} {text[:200]}")
objects_before = len(bpy.data.objects)
err, text, _ = call("vfx_create", effect="fire", name="sim_fire", size_m=1.0, start_frame=1, detail=32, duration_frames=8,
                    location=[4, 0, 0])
check("re-creating a simulated fire replaces it", not err and len(bpy.data.objects) == objects_before, text)
err, text, _ = call("vfx_create", effect="fire", detail=999)
check("vfx_create bad detail", err and "detail" in text, text)
err, text, _ = call("vfx_create", effect="smoke", name="plain_smoke", start_frame=1)
check("effects without a simulation ignore quality", not err and "fast" in json.loads(text)["quality"], text)

# -- columns: shaped by numbers, no bake -------------------------------------------
from unix.tools import vfx_column  # noqa: E402
from unix.tools.registry import Pending, Registry  # noqa: E402

for look in vfx_column.LOOKS:
    err, text, _ = call("vfx_column", look=look, name="col_" + look, height_m=4.0, location=[12, 0, 0], start_frame=1,
                        swirl=0.5, twist=1.0, sway=0.2, hollow=0.3)
    d = json.loads(text) if not err else {}
    root = bpy.data.objects.get("VFX_col_" + look)
    box = bpy.data.objects.get(f"VFX_col_{look}_column")
    glows = vfx_column.LOOKS[look][0] is not None
    check(f"column {look} is built", not err and root is not None and box is not None and d["frame_range"] == [1, 120]
          and abs(box.dimensions.z - 4.0) < 1e-4 and root["unix_start"] == 1 and "unix_effect" in root
          and (bpy.data.objects.get(f"VFX_col_{look}_glow") is not None) == glows, text)
err, text, _ = call("vfx_column", look="fire", name="whirl", height_m=6.0, radius_m=1.5, start_frame=1, carried=120,
                    profile=[[0, 0.3], [0.5, 0.5], [1, 1]], swirl=0.6, twist=1.5, hollow=0.4, sway=0.3, location=[20, 0, 0])
check("column with a profile and carried things is built", not err, text)
carried = bpy.data.objects.get("VFX_whirl_carried")


def carried_places(frame):
    scene.frame_set(frame)
    evaluated = carried.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return np.array([tuple(inst.matrix_world.translation) for inst in bpy.context.evaluated_depsgraph_get().object_instances
                     if inst.is_instance and inst.parent == evaluated])


first, later = carried_places(10), carried_places(22)
check("carried things: one for every point", len(first) == 120 and len(later) == 120, f"{len(first)} {len(later)}")
if len(first) == 120 and len(later) == 120:
    local = later - np.array([20.0, 0.0, 0.0])
    check("carried things stay inside the column's height and reach", local[:, 2].min() >= -1e-4 and local[:, 2].max() <= 6.0 + 1e-4
          and np.hypot(local[:, 0], local[:, 1]).max() <= 1.5 * 1.35 + 1e-4, f"{local.min(axis=0)} {local.max(axis=0)}")
    check("carried things move with the frame", float(np.abs(later - first).max()) > 0.2, float(np.abs(later - first).max()))
objects_before, materials_before = len(bpy.data.objects), len(bpy.data.materials)
err, text, _ = call("vfx_column", look="fire", name="whirl", height_m=6.0, radius_m=1.5, start_frame=1, carried=120,
                    location=[20, 0, 0])
check("re-creating a column replaces it", not err and len(bpy.data.objects) == objects_before
      and len(bpy.data.materials) == materials_before, f"{objects_before}->{len(bpy.data.objects)} {materials_before}->{len(bpy.data.materials)}")
scene.frame_set(40)
err, text, imgs = call("look", targets=["VFX_whirl"], shading="rendered", views=["front"], size=256)
variance = newest_look() if not err else 0.0
check("column renders visible fire", not err and imgs == 1 and variance > 1e-4, f"var={variance} {text[:200]}")
err, text, _ = call("vfx_column", look="fire", name="tipped", height_m=2.0, rotation_deg=[0, 90, 0], start_frame=1)
check("column can be tipped over", not err and abs(bpy.data.objects["VFX_tipped"].rotation_euler.y - 1.5708) < 1e-3, text)
for label, bad in (("look", dict(look="lava")), ("profile", dict(look="fire", profile=[[0, 0.5]])),
                   ("profile", dict(look="fire", profile=[[0, 0.5], [1, 2.0]])), ("hollow", dict(look="fire", hollow=3)),
                   ("carried", dict(look="fire", carried=10 ** 6)), ("height_m", dict(look="fire", height_m=0))):
    err, text, _ = call("vfx_column", **bad)
    check(f"vfx_column refuses a bad {label}", err and label in text, text)
check("a refused column leaves nothing behind", "VFX_fire_column" not in bpy.data.objects)

# -- sparks, drops and flakes do not grow into boulders with the effect --------------
for eff, part, limit in (("sparks", "spark_src", 0.4), ("snow", "flake_src", 0.2), ("rain", "drop_src", 0.6)):
    err, text, _ = call("vfx_create", effect=eff, name="big_" + eff, size_m=20.0, start_frame=1)
    grain = bpy.data.objects.get(f"VFX_big_{eff}_{part}")
    check(f"a 20 m {eff} is not made of huge pieces", not err and grain is not None and max(grain.dimensions) < limit,
          grain and tuple(grain.dimensions))

# -- a long job whose turn has ended stops, and cleans up ------------------------------
fresh = Registry()
seen = {"polls": 0, "cancelled": False}


def _poll():
    seen["polls"] += 1
    if seen["polls"] == 2:
        fresh.abandon()                   # the agent's turn ends while the job is still running
    return None


fresh.tool("long_job", "A job that never finishes.")(
    lambda: Pending(_poll, every=0.01, cancel=lambda: seen.__setitem__("cancelled", True)))
res = fresh.call("long_job", {})
check("an abandoned long job stops and is cleaned up", res["isError"] and seen["cancelled"] and seen["polls"] == 2
      and "stopped" in res["content"][0]["text"], f"{seen} {res['content'][0]['text']}")

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
