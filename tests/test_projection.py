# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Pictures projected from an object's own coordinates (Texture Coordinate Object or Generated, maybe
through a Mapping node) travel as pictures: the sent mesh gets UVs that repeat them, instead of one
bake stretched over the whole surface. No engine needed.
Run: blender -b --factory-startup --python tests/test_projection.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import numpy as np
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_projection_")  # keep test data out of the user's config
from unix.tools import roblox_surface as rs

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


bpy.ops.wm.read_factory_settings(use_empty=True)
picture = bpy.data.images.new("Tiles", 8, 8)


def material(name, output, scale=None, second=None):
    """Principled with a picture on Base Color (and on Roughness when ``second``), laid on by
    Texture Coordinate ``output``, through a Mapping node of ``scale`` when given."""
    made = bpy.data.materials.new(name)
    made.use_nodes = True
    nodes, links = made.node_tree.nodes, made.node_tree.links
    shader = nodes["Principled BSDF"]
    coords = nodes.new("ShaderNodeTexCoord")
    image = nodes.new("ShaderNodeTexImage"); image.image = picture
    vector = coords.outputs[output]
    if scale is not None:
        mapping = nodes.new("ShaderNodeMapping")
        mapping.inputs["Scale"].default_value = scale
        links.new(vector, mapping.inputs["Vector"])
        vector = mapping.outputs["Vector"]
    links.new(vector, image.inputs["Vector"])
    links.new(image.outputs["Color"], shader.inputs["Base Color"])
    if second is not None:
        other = nodes.new("ShaderNodeTexImage"); other.image = picture
        links.new(coords.outputs[second], other.inputs["Vector"])
        links.new(other.outputs["Color"], shader.inputs["Roughness"])
    return made


bpy.ops.mesh.primitive_plane_add(size=10.0)
floor = bpy.context.active_object
while floor.data.uv_layers:                      # a big surface made without UVs, as generated scenes often are
    floor.data.uv_layers.remove(floor.data.uv_layers[0])
tiled = material("Tiled", "Object", scale=(0.5, 0.5, 0.5))
floor.data.materials.append(tiled)

plan = rs._plan(rs._shader(tiled))
check("a picture projected from Object coordinates through a Mapping node travels as a picture",
      plan["color"][0] == "image" and not plan["bake"] and plan["projection"] is not None
      and plan["projection"][0] == "object" and plan["uv"] is None, {k: plan[k] for k in ("color", "bake", "projection", "uv")})
check("so the object is not baked", not rs.made_by_nodes(floor))

mesh = floor.data
uv = rs.projected_uvs(mesh, [tiled], rs.texspace(floor, mesh))
co = np.array([mesh.vertices[loop.vertex_index].co[:] for loop in mesh.loops])
check("its UVs are the projected coordinates: object x, y times the Mapping's scale (repeating past 0..1)",
      uv is not None and np.allclose(uv, co[:, :2] * 0.5, atol=1e-5) and np.abs(uv).max() > 1.0, (uv, co))

generated = material("Generated", "Generated")
mesh.materials[0] = generated
uv = rs.projected_uvs(mesh, [generated], rs.texspace(floor, mesh))
check("Generated coordinates run 0..1 over the object's texture space",
      uv is not None and np.allclose(uv, (co[:, :2] + 5.0) / 10.0, atol=1e-4), uv)

mixed = material("Mixed", "Object", second="UV")
check("pictures laid on two different ways in one material still bake (a mesh has one set of UVs)",
      rs._plan(rs._shader(mixed))["bake"])

laid_by_uv = material("ByUV", "UV", scale=(2.0, 2.0, 1.0))
plan = rs._plan(rs._shader(laid_by_uv))
check("a picture on the UVs keeps its Mapping as a UV tiling, with no projection",
      not plan["bake"] and plan["projection"] is None and plan["uv"] == {"scale": [2.0, 2.0], "shift": [0.0, 0.0], "turn": 0.0}, plan["uv"])
mesh.materials[0] = laid_by_uv
check("and its mesh's UVs are left alone", rs.projected_uvs(mesh, [laid_by_uv], rs.texspace(floor, mesh)) is None)


# The proof: Cycles renders a cube with Blender's own projection, then the same cube with plain UVs made
# by projected_uvs. Seen from two opposite corners, all six sides must match, for flat and Box.
def render(path_tag, eye):
    scene = bpy.context.scene
    scene.render.engine = 'CYCLES'
    scene.cycles.samples, scene.cycles.use_denoising = 1, False
    scene.cycles.device = 'CPU'
    scene.render.resolution_x = scene.render.resolution_y = 96
    scene.view_settings.view_transform = 'Standard'
    scene.render.image_settings.file_format = 'PNG'
    camera = bpy.data.objects.get("ProofCam")
    if camera is None:
        camera = bpy.data.objects.new("ProofCam", bpy.data.cameras.new("ProofCam"))
        scene.collection.objects.link(camera)
        scene.camera = camera
    from mathutils import Vector
    camera.location = Vector(eye)
    camera.rotation_euler = (-Vector(eye)).to_track_quat('-Z', 'Y').to_euler()
    scene.render.filepath = os.path.join(_util._base_dir, path_tag + ".png")
    bpy.ops.render.render(write_still=True)
    shot = bpy.data.images.load(scene.render.filepath)
    pixels = np.array(shot.pixels[:]).reshape(96, 96, 4)[..., :3]
    bpy.data.images.remove(shot)
    return pixels


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.context.scene.world = bpy.data.worlds.new("Proof")
bpy.context.scene.world.use_nodes = True
bpy.context.scene.world.node_tree.nodes["Background"].inputs["Strength"].default_value = 0.0
art = bpy.data.images.new("Art", 64, 64)
gradient = np.zeros((64, 64, 4), dtype=np.float32)
gradient[..., 0] = np.linspace(0, 1, 64)[None, :]          # red runs along u, green along v: any flip or swap shows
gradient[..., 1] = np.linspace(0, 1, 64)[:, None]
gradient[..., 2] = (np.add.outer(np.arange(64), np.arange(64)) % 16 < 8) * 0.6
gradient[..., 3] = 1.0
art.pixels[:] = gradient.ravel()
bpy.ops.mesh.primitive_cube_add(size=1.6, rotation=(0.0, 0.0, 0.3))
cube = bpy.context.active_object
for box in (False, True):
    made = bpy.data.materials.new("Proj")
    made.use_nodes = True
    nodes, links = made.node_tree.nodes, made.node_tree.links
    glow = nodes["Principled BSDF"]                          # black and glowing: no light needed, the picture shows as it is
    glow.inputs["Base Color"].default_value = (0.0, 0.0, 0.0, 1.0)
    glow.inputs["Emission Strength"].default_value = 1.0
    coords, mapping, image = nodes.new("ShaderNodeTexCoord"), nodes.new("ShaderNodeMapping"), nodes.new("ShaderNodeTexImage")
    image.image, image.interpolation = art, 'Closest'
    image.projection = 'BOX' if box else 'FLAT'
    mapping.inputs["Scale"].default_value = (0.7, 0.9, 0.8)
    mapping.inputs["Location"].default_value = (0.2, 0.1, 0.3)
    links.new(coords.outputs["Object"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], image.inputs["Vector"])
    links.new(image.outputs["Color"], glow.inputs["Emission Color"])
    cube.data.materials.clear()
    cube.data.materials.append(made)
    check(f"a {'Box' if box else 'flat'} projection reads as projected", rs.projection(made) is not None and rs.projection(made)[2] == box)
    shots = [render(f"proj_{box}_{i}", eye) for i, eye in enumerate(((3.5, -3.0, 2.8), (-3.2, 3.4, -2.9)))]
    uv = rs.projected_uvs(cube.data, [made], rs.texspace(cube, cube.data))
    while cube.data.uv_layers:
        cube.data.uv_layers.remove(cube.data.uv_layers[0])
    cube.data.uv_layers.new(name="UVMap").data.foreach_set("uv", uv.astype(np.float32).ravel())
    links.remove(image.inputs["Vector"].links[0])           # now laid on by the UVs alone
    image.projection = 'FLAT'
    again = [render(f"uv_{box}_{i}", eye) for i, eye in enumerate(((3.5, -3.0, 2.8), (-3.2, 3.4, -2.9)))]
    worst = [float(np.mean(np.abs(a - b) > 0.05)) for a, b in zip(shots, again)]
    check(f"Cycles shows the same {'Box' if box else 'flat'}-projected cube from the made UVs (all six sides)",
          max(worst) < 0.03, f"share of pixels that differ per view: {worst}")

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
