# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for placement, lighting, vision extras, material receipts and the asset
tools. The asset tools run against a local fixture server, so no internet is used.
Run: blender -b --factory-startup --python tests/test_tools_extended.py"""
import hashlib, http.server, json, math, os, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import tempfile as _tempfile
from unix.tools import _util as _base
_base._base_dir = _tempfile.mkdtemp(prefix="unix_test_")   # snapshots, looks and assets stay out of the app's own data
from unix import tools
from unix.tools import registry, interact

tools.load_all()
from unix.tools import assets  # noqa: E402
interact.interactive["enabled"] = False
fails = []


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"], sum(1 for c in res["content"] if c["type"] == "image")


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:500]}"))
    if not cond:
        fails.append(label)


def box(name, size, location):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.active_object
    obj.name, obj.scale = name, size
    bpy.ops.object.transform_apply(scale=True)
    return obj


# -- place_object ------------------------------------------------------------
bpy.data.objects.remove(bpy.data.objects["Cube"])
table = box("Table", (1.6, 0.9, 0.75), (0, 0, 0.375))
vase = box("Vase", (0.5, 0.5, 2.0), (5, 5, 3))
err, text, _ = call("place_object", object="Vase", xy=[0.2, 0.1], on="Table", size_m=0.3, yaw_deg=30)
d = json.loads(text) if not err else {}
check("place on table: seated, sized", not err and abs(d["base_z"] - 0.75) < 1e-3 and abs(max(d["dimensions"]) - 0.3) < 0.02
      and d["seated_on"] == "Table", text)
chair = box("Chair", (0.5, 0.5, 0.9), (3, 3, 2))
err, text, _ = call("place_object", object="Chair", xy=[0.0, 0.0])
d = json.loads(text) if not err else {}
check("place on ground: nudged clear of table", not err and d["base_z"] == 0 and d.get("nudged_m", 0) > 0.3, text)
err, text, _ = call("place_object", object="Chair", xy=[0.0, 0.0], avoid_overlap=False)
d = json.loads(text) if not err else {}
check("place exact when overlap allowed", not err and "nudged_m" not in d and abs(d["footprint_centre"][0]) < 1e-3, text)
err, text, _ = call("place_object", object="Vase", xy=[9, 9], on="Table")
check("place off the support is an error", err and "no surface" in text, text)

# -- light_rig ---------------------------------------------------------------
for style, expect in (("three_point", 3), ("product", 4), ("outdoor_sun", 1), ("overcast", 1)):
    err, text, _ = call("light_rig", style=style, targets=["Table"])
    d = json.loads(text) if not err else {}
    rig = [o.name for o in bpy.data.collections["Unix_Lighting"].objects] if "Unix_Lighting" in bpy.data.collections else []
    check(f"light_rig {style}", not err and len(d["lights"]) == expect and len(rig) == expect, text)
check("light_rig replaces previous rig", len(bpy.data.collections["Unix_Lighting"].objects) == 1)
err, text, imgs = call("look", targets=["Table", "Vase"], shading="rendered", views=["persp"])
check("look rendered with rig", not err and imgs == 1, text)

# -- set_material receipt ----------------------------------------------------
err, text, _ = call("set_material", name="Wood", objects=["Table"], base_color=[0.4, 0.25, 0.1], roughness=0.5)
d = json.loads(text) if not err else {}
check("set_material receipt", not err and d["bsdf_reaches_output"] and d["base_color_from"] == [0.4, 0.25, 0.1], text)
err, text, _ = call("set_material", name="Wood", objects=["Chair"], roughness=0.9)
d = json.loads(text) if not err else {}
check("set_material reports sharing", not err and d.get("also_used_by") == ["Table"], text)

# -- look_uv / view_image ----------------------------------------------------
err, text, imgs = call("look_uv", object="Table")
check("look_uv", not err and imgs == 1, text)
tmp = tempfile.mkdtemp()
img = bpy.data.images.new("ref", 1600, 900)
img.generated_color = (0.2, 0.6, 0.9, 1.0)
ref_path = os.path.join(tmp, "ref.png")
img.filepath_raw, img.file_format = ref_path, 'PNG'
img.save()
err, text, imgs = call("view_image", path=ref_path)
check("view_image", not err and imgs == 1 and json.loads(text)["original_size"] == [1600, 900], text)
err, text, _ = call("view_image", path=os.path.join(tmp, "missing.png"))
check("view_image missing file", err and "not found" in text, text)
err, text, _ = call("look", views=["viewport"])
check("viewport view refused headless", err and "background" in text, text)

# -- match_reference ---------------------------------------------------------
err, text, imgs = call("match_reference", image=ref_path, targets=["Table"], view="front")
check("match_reference returns one side-by-side sheet", not err and imgs == 1 and "LEFT = reference" in text, text)
import glob as _glob
from unix.tools._util import data_dir as _dd
sheet = bpy.data.images.load(sorted(_glob.glob(os.path.join(_dd("looks"), "match_*.jpg")))[-1])
check("match sheet is wider than tall (two panels)", sheet.size[0] > sheet.size[1] * 1.5 and sheet.size[1] == 640, list(sheet.size))
err, text, _ = call("match_reference", image=os.path.join(tmp, "nope.png"))
check("match_reference missing image", err and "not found" in text, text)

# -- generate_image / generate_texture (image model call replaced by a local file) --
from unix.tools import generate
import numpy as _np
noise = bpy.data.images.new("fx_gen", 256, 256)
_px = _np.random.default_rng(1).random((256, 256, 4), dtype=_np.float32)
_px[..., 3] = 1.0
noise.pixels.foreach_set(_px.ravel())
gen_src = os.path.join(tmp, "gen_src.png")
noise.filepath_raw, noise.file_format = gen_src, 'PNG'
noise.save()
prompts = []
import time as _time
generate._signed_out["until"] = _time.monotonic() + 60.0       # the Codex sign-in was refused a moment ago
began = _time.monotonic()
err, text, _ = call("generate_image", prompt="a brass desk lamp", purpose="concept")
check("a refused sign-in fails at once and tells the agent to keep building", err and _time.monotonic() - began < 2.0
      and "codex login" in text and "keep building" in text and "Do not stop" in text, text)
generate._signed_out["until"] = 0.0
from unix.ui import island as _island
image_mode = _island.MODE_PROMPTS["image"]
check("the Image tab does not trap a request to build something", "build it as in 3D mode" in image_mode
      and "whether or not image generation is available" in image_mode, image_mode)
generate.run_codex_image = lambda prompt, reference="", timeout=300.0: (prompts.append((prompt, reference)), gen_src)[1]
err, text, imgs = call("generate_image", prompt="a brass desk lamp", purpose="reference_sheet", reference=ref_path)
d = json.loads(text) if not err else {}
check("generate_image returns the file and a preview", not err and imgs == 1 and os.path.isfile(d["file"])
      and "front view" in prompts[-1][0] and prompts[-1][1] == ref_path, text)
err, text, _ = call("generate_image", prompt="x", purpose="concept", reference=os.path.join(tmp, "none.png"))
check("generate_image rejects a missing reference", err and "not found" in text, text)
err, text, imgs = call("generate_texture", prompt="mossy cobblestone", objects=["Table"], scale=3.0, bump=0.8)
d = json.loads(text) if not err else {}
gmat = bpy.data.materials.get(d.get("material", ""))
tex_nodes = [n for n in gmat.node_tree.nodes if n.type == 'TEX_IMAGE'] if gmat else []
check("generate_texture builds a 3-map mirrored material", not err and imgs == 1 and len(tex_nodes) == 3
      and all(n.extension == 'MIRROR' for n in tex_nodes) and d["bsdf_reaches_output"]
      and gmat.node_tree.nodes["unix_mapping"].inputs["Scale"].default_value[0] == 3.0
      and abs(gmat.node_tree.nodes["unix_normal_map"].inputs["Strength"].default_value - 0.8) < 1e-6, text)
nrm = bpy.data.images.load(d["base_color_file"].replace(".png", "_normal.png")) if not err else None
check("derived normal map is on disk and the right size", nrm is not None and list(nrm.size) == [256, 256])
err, text, _ = call("check_materials", objects=["Table"])
check("generated material passes check_materials", not err and json.loads(text)["issues"] == [], text)
generate.settings["enabled"] = False
check("generation can be switched off", generate.settings["enabled"] is False)
generate.settings["enabled"] = True

# -- run_python extras -------------------------------------------------------
err, text, _ = call("run_python", code="result = 1")
check("run_python reports triangles", not err and json.loads(text)["scene_triangles"] > 0, text)

# -- assets against a local fixture server -----------------------------------
fixtures = os.path.join(tmp, "srv")
os.makedirs(os.path.join(fixtures, "textures"))
hdr = bpy.data.images.new("fx_hdr", 64, 32, float_buffer=True)
hdr.filepath_raw, hdr.file_format = os.path.join(fixtures, "sky_1k.hdr"), 'HDR'
hdr.save()
for name, color in (("diff", (0.5, 0.3, 0.1, 1)), ("nor", (0.5, 0.5, 1, 1)), ("rough", (0.6, 0.6, 0.6, 1))):
    im = bpy.data.images.new(f"fx_{name}", 32, 32)
    im.generated_color = color
    im.filepath_raw, im.file_format = os.path.join(fixtures, f"planks_{name}_1k.jpg"), 'JPEG'
    im.save()
bpy.ops.object.select_all(action='DESELECT')
bpy.data.objects["Vase"].select_set(True)
bpy.ops.export_scene.gltf(filepath=os.path.join(fixtures, "prop_1k.gltf"), use_selection=True, export_format='GLTF_SEPARATE')

handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=fixtures, **k)  # noqa: E731
server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"
assets.ALLOWED_HOSTS.add("127.0.0.1")


def entry(rel):
    path = os.path.join(fixtures, rel)
    return {"url": f"{base}/{rel}", "size": os.path.getsize(path), "md5": hashlib.md5(open(path, "rb").read()).hexdigest()}


catalog = {
    "hdri": {"fx_sky": {"name": "Fixture Sky", "categories": ["outdoor", "overcast"], "tags": ["field", "cloudy"], "downloads": 5}},
    "texture": {"fx_planks": {"name": "Fixture Oak Planks", "categories": ["wood"], "tags": ["oak", "floor"], "downloads": 9}},
    "model": {"fx_prop": {"name": "Fixture Prop", "categories": ["props"], "tags": ["vase"], "downloads": 2}},
}
bin_name = next(f for f in os.listdir(fixtures) if f.endswith(".bin"))
files_api = {
    "fx_sky": {"hdri": {"1k": {"hdr": entry("sky_1k.hdr")}}},
    "fx_planks": {"Diffuse": {"1k": {"jpg": entry("planks_diff_1k.jpg")}}, "nor_gl": {"1k": {"jpg": entry("planks_nor_1k.jpg")}},
                  "Rough": {"1k": {"jpg": entry("planks_rough_1k.jpg")}}},
    "fx_prop": {"gltf": {"1k": {"gltf": dict(entry("prop_1k.gltf"), include={bin_name: entry(bin_name)})}}},
}
assets._catalog = lambda kind: catalog[kind]
assets._get_json = lambda url: files_api[url.rsplit("/", 1)[1]]

err, text, _ = call("asset_search", kind="texture", query="oak wood")
check("asset_search", not err and json.loads(text)["results"][0]["id"] == "fx_planks", text)
err, text, _ = call("asset_search", kind="hdri", query="zzzz")
check("asset_search no match", not err and json.loads(text)["results"] == [], text)
err, text, _ = call("asset_hdri", id="fx_sky", resolution="2k", strength=1.5, rotation_deg=90)
d = json.loads(text) if not err else {}
world = bpy.context.scene.world
check("asset_hdri builds world (falls back to 1k)", not err and d["resolution"] == "1k"
      and any(n.type == 'TEX_ENVIRONMENT' and n.image for n in world.node_tree.nodes), text)
err, text, _ = call("asset_material", id="fx_planks", objects=["Table"], resolution="1k", scale=2.0)
d = json.loads(text) if not err else {}
mat = bpy.data.materials.get("PH_fx_planks")
check("asset_material wires maps + mapping", not err and d["maps"] == ["base_color", "normal", "roughness"]
      and mat is not None and mat.node_tree.nodes["unix_mapping"].inputs["Scale"].default_value[0] == 2.0
      and d["bsdf_reaches_output"] and "image" in str(d["base_color_from"]), text)
before = len(bpy.data.objects)
err, text, _ = call("asset_model", id="fx_prop")
check("asset_model imports", not err and json.loads(text)["imported"] and len(bpy.data.objects) > before, text)
err, text, _ = call("asset_hdri", id="nope")
check("asset unknown id", err and "asset_search" in text, text)
bad = dict(files_api["fx_sky"]["hdri"]["1k"]["hdr"], md5="0" * 32)
files_api["fx_bad"] = {"hdri": {"1k": {"hdr": dict(bad, url=bad["url"] + "?x=1")}}}
catalog["hdri"]["fx_bad"] = catalog["hdri"]["fx_sky"]
err, text, _ = call("asset_hdri", id="fx_bad", resolution="1k")
check("asset integrity check rejects bad md5", err and "integrity" in text, text)
assets.ALLOWED_HOSTS.discard("127.0.0.1")
files_api["fx_evil"] = {"hdri": {"1k": {"hdr": {"url": "http://example.com/x.hdr", "size": 10, "md5": ""}}}}
catalog["hdri"]["fx_evil"] = catalog["hdri"]["fx_sky"]
err, text, _ = call("asset_hdri", id="fx_evil", resolution="1k")
check("asset host allowlist", err and "not allowed" in text, text)
assets.settings["enabled"] = False
err, text, _ = call("asset_search", kind="hdri", query="sky")
check("assets can be switched off", err and "turned off" in text, text)
server.shutdown()

# -- fuse_meshes: a blockout of separate parts becomes one continuous body -------------------
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=(20, 0, 1.0))
torso = bpy.context.active_object
torso.name = "Blob_Torso"
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.18, location=(20, 0, 1.38))
bpy.context.active_object.name = "Blob_Head"
bpy.ops.mesh.primitive_cylinder_add(radius=0.07, depth=0.7, location=(20.32, 0, 1.0), rotation=(0, 1.2, 0))
bpy.context.active_object.name = "Blob_Arm"
skin = bpy.data.materials.new("Blob_Skin")
torso.data.materials.append(skin)
parts = ["Blob_Torso", "Blob_Head", "Blob_Arm"]
err, text, _ = call("fuse_meshes", objects=parts, name="Blob")
d = json.loads(text) if not err else {}
blob = bpy.data.objects.get("Blob")
check("fuse_meshes makes one object and keeps the parts hidden", not err and blob is not None and d["parts_hidden_in"] == "Blob_parts"
      and all(bpy.data.objects[n].hide_render and bpy.data.objects[n].users_collection[0].name == "Blob_parts" for n in parts), text)
err2, text2, _ = call("check_mesh", objects=["Blob"])
report = json.loads(text2) if not err2 else {}
entry = (report.get("objects") or [report])[0]
check("the fused body is one watertight piece", not err2 and '"watertight":true' in text2.replace(" ", ""), text2[:500])
check("the fused body spans all the parts and takes their material", blob is not None and blob.dimensions.x > 0.8
      and blob.dimensions.z > 0.8 and blob.data.materials[0] == skin, tuple(blob.dimensions) if blob else None)
err, text, _ = call("fuse_meshes", objects=["Blob"], name="Table")
check("fuse_meshes will not take the name of another object", err and "already exists" in text, text)
err, text, _ = call("fuse_meshes", objects=["Blob"], name="Blob2", voxel_m=5.0)
check("fuse_meshes rejects a voxel size coarser than the model", err and "too coarse" in text, text)

print("TOOLS", len(registry.names()), registry.names())
# -- the outline of a real thing, read from a photo (no internet: the photo is drawn here) ---------
import numpy as _np2
from unix.tools import references
W, H = 400, 240
shot = _np2.ones((H, W, 4), dtype=_np2.float32)                     # a white studio background
shot[60:120, 60:340, :3] = 0.08                                     # a long body ...
shot[120:210, 250:320, :3] = 0.10                                   # ... with a handle hanging from it
shot[120:180, 150:250, :3] = 0.09                                   # ... and a loop in front of the handle
shot[140:170, 165:225, :3] = 1.0                                    # the loop's opening
shot[100:104, 100:103, :3] = 1.0                                    # a speck of glare: not an opening
photo = bpy.data.images.new("fx_photo", W, H)
photo.pixels.foreach_set(_np2.ascontiguousarray(shot[::-1]).ravel())
photo_path = os.path.join(tmp, "fx_photo.png")
photo.filepath_raw, photo.file_format = photo_path, 'PNG'
photo.save()
err, text, imgs = call('trace_reference', image=photo_path, name='Traced_Tool', length_m=0.28, thickness_m=0.03)
d = json.loads(text) if not err else {}
traced = bpy.data.objects.get('Traced_Tool')
check('trace_reference builds the outline at real size, standing on the ground', not err and imgs == 1 and traced is not None
      and abs(d['size_m'][0] - 0.28) < 0.004 and abs(d['size_m'][2] - 0.15) < 0.004 and abs(d['size_m'][1] - 0.03) < 1e-6
      and abs(min((traced.matrix_world @ v.co).z for v in traced.data.vertices)) < 1e-6, text[:400])
check('the opening is kept and the speck of glare is not', d.get('openings') == 1, d.get('openings'))
err2, text2, _ = call('check_mesh', objects=['Traced_Tool'])
m = json.loads(text2)['objects'][0] if not err2 else {}
solid = (280 * 60 + 70 * 90 + 100 * 60 - 60 * 30) / (280.0 ** 2) * 0.28 ** 2 * 0.03
check('it is a closed solid with the volume of the drawn shape', m.get('watertight') and m.get('zero_area_faces') == 0
      and abs(m.get('volume_m3', 0) - solid) < 0.04 * solid, (m, solid))
check('the outline is corners, not pixel steps', 8 <= d.get('outline_points', 0) <= 40, d.get('outline_points'))
err, text, _ = call('trace_reference', image=photo_path, name='Traced_Mirror', length_m=0.28, mirror=True)
flipped = bpy.data.objects.get('Traced_Mirror')
handle_side = lambda ob: sum((ob.matrix_world @ v.co).x for v in ob.data.vertices if (ob.matrix_world @ v.co).z < 0.02)   # noqa: E731
check('mirror flips left and right', not err and handle_side(traced) > 0 > handle_side(flipped), (handle_side(traced), handle_side(flipped)))
bpy.data.objects.remove(flipped)

# -- the model measured against the photo: outline and openings --------------------------------
err, text, imgs = call("match_reference", image=photo_path, targets=["Traced_Tool"], view="front")
measured = json.loads(text).get("outline_measured", {}) if not err else {}
check("match_reference measures the outline against the photo", not err and imgs == 1 and measured.get("outline_overlap_percent", 0) >= 96
      and measured.get("real_outline_not_covered_percent", 9) < 2 and "match the photo" in measured.get("verdict", ""), measured)
opening_photo = measured.get("openings_percent_of_area", {}).get("photo", 0)
check("and its openings", abs(opening_photo - 100 * 1800 / 27300) < 1.0
      and abs(measured["openings_percent_of_area"]["model"] - opening_photo) < 1.0, measured.get("openings_percent_of_area"))
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
plug = bpy.context.active_object
plug.name = "Plug"
frame = traced["unix_photo_frame"]
plug.scale = (0.05, 0.02, 0.025)                                    # a part pushed into the loop's opening
plug.location = (frame[0] + (195 / W) * frame[2], 0.0, frame[1] - (155 / H) * frame[3])
bpy.context.view_layer.update()
err, text, _ = call("match_reference", image=photo_path, targets=["Traced_Tool", "Plug"], view="front")
measured = json.loads(text).get("outline_measured", {}) if not err else {}
check("an opening that something fills is reported, with what to do", "openings are" in measured.get("verdict", "")
      and "Open them up" in measured["verdict"] and measured["openings_percent_of_area"]["model"] < 0.5 * opening_photo, measured)
bpy.data.objects.remove(plug)
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0.0, 0.0, 0.0))
slab = bpy.context.active_object
slab.name = "Slab"
slab.scale = (0.28, 0.03, 0.06)
slab.location = (0.0, 0.0, 0.12)                                    # only the long body: the handle and loop are missing
bpy.context.view_layer.update()
err, text, _ = call("match_reference", image=photo_path, targets=["Slab"], view="front")
measured = json.loads(text).get("outline_measured", {}) if not err else {}
check("a model that lacks half the thing is measured as such", measured.get("outline_overlap_percent", 100) < 75
      and ("missing" in measured.get("verdict", "") or "outside" in measured.get("verdict", "")), measured)
bpy.data.objects.remove(slab)

# -- a part from its outline, placed by photo fractions ---------------------------------------------
err, text, _ = call("profile_solid", name="Lever", points=[[0.30, 0.30], [0.45, 0.30], [0.45, 0.45], [0.30, 0.45]],
                    thickness_m=0.01, y_m=-0.02, round_m=0.002, smooth=2)
d = json.loads(text) if not err else {}
lever = bpy.data.objects.get("Lever")
expect_x, expect_z = frame[0] + 0.375 * frame[2], frame[1] - 0.375 * frame[3]
check("profile_solid makes a part where the photo fractions say", not err and lever is not None
      and abs(d["centre_m"][0] - expect_x) < 0.002 and abs(d["centre_m"][2] - expect_z) < 0.002 and d["outline_points"] == 16
      and len(lever.modifiers) == 0 and abs(d["centre_m"][1] + 0.02) < 1e-6 and "followed_real_outline" not in d, text)
depths = sorted({round(abs(v.co.y + 0.02), 5) for v in lever.data.vertices})
check("its faces curve down to the rim: a quarter circle of the rounding it was given", abs(depths[0] - 0.003) < 1e-5
      and abs(depths[-1] - 0.005) < 1e-5 and len(depths) > 4, depths[:8])
err2, text2, _ = call("check_mesh", objects=["Lever"])
check("it is a closed solid, not a box", not err2 and json.loads(text2)["objects"][0]["watertight"]
      and len(lever.data.vertices) > 60, text2[:300])
# a big part: a loose region around the handle, running outside the silhouette on two sides
err, text, _ = call("profile_solid", name="Handle", points=[[0.60, 0.55], [0.90, 0.55], [0.90, 0.95], [0.60, 0.95]],
                    thickness_m=0.03, round_m=0.015)
d = json.loads(text) if not err else {}
handle = bpy.data.objects.get("Handle")
right_edge = max((v.co.x for v in handle.data.vertices), default=0) if handle else 0
check("a region drawn loosely around a part follows the real outline", not err and "followed_real_outline" in d
      and abs(d["size_m"][0] - 0.080) < 0.004 and abs(d["size_m"][2] - 0.078) < 0.004
      and abs(right_edge - (frame[0] + (320 / W) * frame[2])) < 0.002, text[:400])
across = [abs(v.co.y) for v in handle.data.vertices] if handle else [1]
err2, text2, _ = call("check_mesh", objects=["Handle"])
check("rounding of half the thickness gives an oval section with no flat rim", not err2 and json.loads(text2)["objects"][0]["watertight"]
      and max(across) <= 0.0151 and min(across) < 1e-6 and 0.0149 < max(across), (min(across), max(across), text2[:200]))
err, text, _ = call("profile_solid", name="Nowhere", points=[[0.02, 0.02], [0.08, 0.02], [0.08, 0.08]], thickness_m=0.01)
check("a part drawn where the photo has only background is refused with the reason", err and "outside the subject" in text, text)
err, text, _ = call("profile_solid", name="Scope", points=[[0.30, 0.10], [0.60, 0.10], [0.60, 0.22], [0.30, 0.22]], thickness_m=0.02, free=True)
check("an added accessory may sit outside the outline when asked", not err and "followed_real_outline" not in json.loads(text), text[:300])
check("a part placed by the photo carries the photo's surface by itself", handle.data.uv_layers.active is not None
      and handle.data.uv_layers.active.name == "UnixPhoto" and len(handle.data.materials) == 1
      and any(n.type == 'TEX_IMAGE' for n in handle.data.materials[0].node_tree.nodes)
      and max(handle.data.materials[0].diffuse_color[:3]) < 0.05 and "surface" in d, [m.name for m in handle.data.materials])
check("an accessory drawn where the photo shows nothing stays plain", len(bpy.data.objects["Scope"].data.materials) == 0)
err, text, _ = call("profile_solid", name="Wedge", points=[[0.20, 0.28], [0.80, 0.28], [0.80, 0.46], [0.20, 0.46]], thickness_m=0.02,
                    round_m=0, smooth=0, surface="plain", width_profile=[[0, 1.0], [1, 0.4]])
wedge = bpy.data.objects.get("Wedge")
left_end = max(abs(v.co.y) for v in wedge.data.vertices if v.co.x < min(u.co.x for u in wedge.data.vertices) + 1e-4)
right_end = max(abs(v.co.y) for v in wedge.data.vertices if v.co.x > max(u.co.x for u in wedge.data.vertices) - 1e-4)
check("width_profile makes a part thinner toward one end", not err and abs(left_end - 0.01) < 1e-4 and abs(right_end - 0.004) < 1e-4
      and len(wedge.data.materials) == 0, (left_end, right_end))
err, text, _ = call("lathe_solid", name="Barrel", start=[0.15, 0.37], end=[0.85, 0.37], radius_m=0.008, bore_m=0.0045,
                    radii=[[0, 0.008], [0.9, 0.008], [0.9, 0.011], [1, 0.011]])
d = json.loads(text) if not err else {}
barrel = bpy.data.objects.get("Barrel")
err2, text2, _ = call("check_mesh", objects=["Barrel"])
m = json.loads(text2)["objects"][0] if not err2 else {}
expect_len = 0.70 * frame[2]
solid_volume = math.pi * (0.008 ** 2 * 0.9 + 0.011 ** 2 * 0.1) * expect_len
check("lathe_solid makes a round part between two points on the photo", not err and abs(d["length_m"] - expect_len) < 0.001
      and d["hollow"] is True and abs(max(v.co.y for v in barrel.data.vertices) - 0.011) < 1e-5 and m.get("watertight"), text)
check("with a real bore through it", abs(m.get("volume_m3", 0) - (solid_volume - math.pi * 0.0045 ** 2 * expect_len)) < 0.03 * solid_volume
      and "surface" in d, (m.get("volume_m3"), solid_volume))
err, text, _ = call("lathe_solid", name="Rod", start=[0.0, 0.0], end=[0.0, 0.1], radius_m=0.01, space="scene")
check("a solid rod in meters too", not err and json.loads(text)["hollow"] is False and abs(json.loads(text)["length_m"] - 0.1) < 1e-6, text)
err, text, _ = call("lathe_solid", name="Bad", start=[0.0, 0.0], end=[0.0, 0.1], radius_m=0.01, bore_m=0.02, space="scene")
check("a bore wider than the part is refused", err and "smaller than" in text, text)
for leftover in ("Wedge", "Barrel", "Rod"):
    bpy.data.objects.remove(bpy.data.objects[leftover])
err, text, imgs = call("look_around", targets=["Handle", "Lever", "Traced_Tool"])
d = json.loads(text) if not err else {}
check("look_around shows all six sides in one picture", not err and imgs == 1 and len(d["tiles"]["top_row"]) == 3
      and len(d["tiles"]["bottom_row"]) == 3 and "stacked" in d["judge"], text[:300])
for leftover in ("Handle", "Scope"):
    bpy.data.objects.remove(bpy.data.objects[leftover])
err, text, _ = call("profile_solid", name="Plate", points=[[0.0, 0.0], [0.1, 0.0], [0.1, 0.05], [0.0, 0.05]], thickness_m=0.004,
                    space="scene", smooth=0, round_m=0, holes=[[[0.03, 0.02], [0.07, 0.02], [0.07, 0.03], [0.03, 0.03]]])
d = json.loads(text) if not err else {}
check("in meters too, with an opening and sharp corners when asked", not err and d["size_m"] == [0.1, 0.004, 0.05] and d["openings"] == 1
      and len(bpy.data.objects["Plate"].modifiers) == 0, text)
err, text, _ = call("profile_solid", name="Bad", points=[[2.0, 3.0], [4.0, 3.0], [4.0, 5.0]], thickness_m=0.01)
check("photo fractions outside the photo are refused with the reason", err and "0 to 1" in text, text)

# -- the photo on the model ------------------------------------------------------------------------
tool_mat = bpy.data.materials.new("Tool_Steel")
tool_mat.use_nodes = True
tool_mat.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.3
traced.data.materials.append(tool_mat)
err, text, _ = call("project_reference", image=photo_path, objects=["Traced_Tool"])
d = json.loads(text) if not err else {}
skin = traced.data.materials[0]
shader = skin.node_tree.nodes["Principled BSDF"]
base_link = shader.inputs["Base Color"].links[0].from_node if shader.inputs["Base Color"].is_linked else None
check("project_reference lays the photo over a copy of the material and keeps its settings", not err and skin.name == "Tool_Steel_Photo"
      and tool_mat.users == 0 or tool_mat.name in bpy.data.materials and base_link is not None and base_link.type == 'MIX'
      and abs(shader.inputs["Roughness"].default_value - 0.3) < 1e-6 and shader.inputs["Normal"].is_linked and "warning" not in d, text)
layer = traced.data.uv_layers.get("UnixPhoto_front")
us = [layer.data[loop.index].uv[0] for loop in traced.data.loops] if layer else [0]
vs = [1 - layer.data[loop.index].uv[1] for loop in traced.data.loops] if layer else [0]
# the drawn subject spans pixels 60..340 across and 60..210 down, in a 400 x 240 photo
check("the photo is lined up with the model by the subject's outline", layer is not None and traced.data.uv_layers.active == layer
      and abs(min(us) - 60 / W) < 0.01 and abs(max(us) - 340 / W) < 0.01 and abs(min(vs) - 60 / H) < 0.015
      and abs(max(vs) - 210 / H) < 0.015, (min(us), max(us), min(vs), max(vs)))
mix_node = next(n for n in skin.node_tree.nodes if n.type == 'MIX')
check("faces turned away from the photo take the photo's own tone, not the old colour", max(mix_node.inputs["A"].default_value[:3]) < 0.05,
      tuple(mix_node.inputs["A"].default_value))
from unix.tools import roblox as _roblox
check("and the photo travels to a game engine as the texture", "texture" in _roblox._surface(skin, {}), sorted(_roblox._surface(skin, {})))
err, text, _ = call("project_reference", image=photo_path, objects=["Traced_Tool"])
check("projecting again replaces the layer instead of stacking it", not err
      and sum(1 for n in traced.data.materials[0].node_tree.nodes if n.type == 'TEX_IMAGE') == 1
      and traced.data.materials[0].name == "Tool_Steel_Photo", [n.name for n in traced.data.materials[0].node_tree.nodes])
import shutil as _shutil
other_photo = os.path.join(tmp, "fx_other_photo.png")              # a photo nothing was traced from: lined up by the model's bounds
_shutil.copyfile(photo_path, other_photo)
err, text, _ = call("project_reference", image=other_photo, objects=["Plate"])
check("a model with other proportions than the photo gets a warning", not err and "stretched" in json.loads(text).get("warning", ""), text[:300])
err, text, _ = call("project_reference", image=photo_path, objects=["Plate"])
check("the photo a model was traced from is laid exactly where it was read", not err and "warning" not in json.loads(text), text[:300])

# -- a triangle budget ---------------------------------------------------------------------------------
bpy.ops.mesh.primitive_uv_sphere_add(segments=400, ring_count=200, radius=0.1, location=(3, 3, 0.1))
bpy.context.active_object.name = "HeavyBall"
err, text, _ = call("check_scene")
check("check_scene names an object far over the triangle budget", not err and any("HeavyBall" in i and "Decimate" in i for i in json.loads(text)["issues"]),
      json.loads(text)["issues"][:4] if not err else text)
bpy.data.objects.remove(bpy.data.objects["HeavyBall"])
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(6, 6, 0.5))
bpy.context.active_object.name = "Blob_A"
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.4, location=(6.5, 6, 0.6))
bpy.context.active_object.name = "Blob_B"
err, text, _ = call("fuse_meshes", objects=["Blob_A", "Blob_B"], name="Blob_Budget", voxel_m=0.004, max_triangles=20000)
d = json.loads(text) if not err else {}
check("fuse_meshes keeps the result inside its triangle budget", not err and d["triangles"] <= 21000 and d.get("reduced_from_triangles", 0) > 60000, text[:300])

busy = _np2.random.default_rng(3).random((H, W, 4), dtype=_np2.float32)
busy[..., 3] = 1.0
noisy = bpy.data.images.new('fx_busy', W, H)
noisy.pixels.foreach_set(busy.ravel())
busy_path = os.path.join(tmp, 'fx_busy.png')
noisy.filepath_raw, noisy.file_format = busy_path, 'PNG'
noisy.save()
err, text, _ = call('trace_reference', image=busy_path, name='Traced_Busy', length_m=1.0)
check('a photo with a busy background is refused with what to do', err and 'plain' in text and 'Traced_Busy' not in bpy.data.objects, text)
assets.settings['enabled'] = False
err, text, _ = call('reference_search', query='office chair')
check('reference_search respects the online switch', err and 'turned off' in text, text)
assets.settings['enabled'] = True
references._fetch = lambda url, timeout=30.0: (_ for _ in ()).throw(OSError('no network in tests'))
err, text, _ = call('reference_search', query='office chair')
check('with no network it says so and tells the agent to carry on', err and 'Could not reach' in text and 'Work from what you know' in text, text)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
