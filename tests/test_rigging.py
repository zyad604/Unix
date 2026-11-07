# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for rigging.

* A mesh under a rotated parent (the Sketchfab/FBX case) must stay where it is when bound,
  and a skeleton that misses its mesh must be reported.
* A mesh split into touching pieces must not tear at the seam when a bone moves.
* Fingers must be found by measuring the mesh, and their bones must land inside them.
Run: blender -b --factory-startup --python tests/test_rigging.py"""
import json, math, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bmesh, bpy
from mathutils import Matrix, Vector
from unix import tools
from unix.tools import _shape, _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
tools.load_all()
fails = []


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"], sum(1 for c in res["content"] if c["type"] == "image")


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


def world_verts(obj):
    return [tuple(round(c, 4) for c in obj.matrix_world @ v.co) for v in obj.data.vertices]


# -- an "imported" limb: a cylinder lying along local Z, stood up in the world by two parents ----
bpy.data.objects.remove(bpy.data.objects["Cube"])
bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.12, depth=1.2, location=(0, 0, 0))
limb = bpy.context.active_object
limb.name = "Limb"
bpy.ops.object.mode_set(mode='EDIT')
bpy.ops.mesh.subdivide(number_cuts=6)
bpy.ops.object.mode_set(mode='OBJECT')
outer = bpy.data.objects.new("Import_Root", None)
inner = bpy.data.objects.new("Import_Node", None)
for o in (outer, inner):
    bpy.context.scene.collection.objects.link(o)
inner.parent = outer
limb.parent = inner
limb.rotation_mode = 'QUATERNION'                      # glTF imports use quaternions
outer.rotation_euler = (math.radians(-90), 0, 0)       # the classic axis-conversion parent
outer.scale = (2.0, 2.0, 2.0)
inner.location = (0.5, 0.0, 0.3)
bpy.context.view_layer.update()

err, text, _ = call("object_info", name="Limb")
info = json.loads(text)
check("object_info warns about a rotated, scaled parent chain", not err and "transform_warning" in info
      and info["parent_chain"] == ["Import_Node", "Import_Root"] and abs(info["world_scale"][0] - 2.0) < 1e-3, text)

# skeleton placed in WORLD space along the limb
points = [limb.matrix_world @ v.co for v in limb.data.vertices]
lo = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
hi = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
axis = max(range(3), key=lambda i: hi[i] - lo[i])
centre = (lo + hi) / 2
start, mid, end = centre.copy(), centre.copy(), centre.copy()
start[axis], end[axis] = lo[axis], hi[axis]
arm = bpy.data.armatures.new("Rig")
rig = bpy.data.objects.new("Rig", arm)
bpy.context.scene.collection.objects.link(rig)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
lower = arm.edit_bones.new("Lower"); lower.head, lower.tail = start, mid
upper = arm.edit_bones.new("Upper"); upper.head, upper.tail = mid, end
upper.parent, upper.use_connect = lower, True
bpy.ops.object.mode_set(mode='OBJECT')

before = world_verts(limb)
err, text, _ = call("rig_bind", armature="Rig", meshes=["Limb"])
bound = json.loads(text)["bound"][0] if not err else {}
check("rig_bind succeeds and weights every vertex", not err and bound["unweighted_verts"] == 0 and bound["vertex_groups"] == 2, text)
check("the mesh does not move when bound", world_verts(limb) == before and bound["moved_m"] == 0.0, bound)
check("world rotation and scale are baked into the mesh", bound["frozen_transforms"]
      and limb.matrix_world.to_quaternion().angle < 1e-5 and abs(limb.matrix_world.to_scale()[0] - 1.0) < 1e-5)
check("bound to the rig with exactly one Armature modifier", limb.parent == rig
      and [m.object for m in limb.modifiers if m.type == 'ARMATURE'] == [rig])
err, text, _ = call("rig_bind", armature="Rig", meshes=["Limb"], method="nearest")
check("binding again is safe", not err and world_verts(limb) == before
      and len([m for m in limb.modifiers if m.type == 'ARMATURE']) == 1, text)

err, text, _ = call("check_rig", armature="Rig")
report = json.loads(text)
check("check_rig passes a correct rig", not err and report["issues"] == []
      and report["meshes"][0]["bones_in_mesh_bounds"] == "2/2" and report["meshes"][0]["pose_test"]["no_effect"] == [], text)
check("check_rig leaves the pose and rotation modes untouched",
      all(b.matrix_basis == Matrix.Identity(4) for b in rig.pose.bones) and rig.pose.bones[0].rotation_mode == 'QUATERNION')

objects_before, materials_before = len(bpy.data.objects), len(bpy.data.materials)
err, text, images = call("look", targets=["Rig"], views=["persp", "front"], size=256)
data = json.loads(text) if not err else {}
check("look draws the bones and brings the bound mesh along", not err and images == 2 and "bones" in data
      and data["framed"] == ["Limb"], text)
check("look leaves no bone stand-ins behind", len(bpy.data.objects) == objects_before
      and len(bpy.data.materials) == materials_before and not bpy.context.scene.display.shading.show_xray)
err, text, images = call("look", targets=["Limb"], shading="rendered", size=256)
check("rendered look has no bone overlay", not err and "bones" not in json.loads(text), text)

# the failure this was built for: the mesh ends up at a right angle to its skeleton
limb.matrix_world = Matrix.Rotation(math.radians(90), 4, 'X') @ limb.matrix_world
bpy.context.view_layer.update()
err, text, _ = call("check_rig", armature="Rig")
issues = json.loads(text)["issues"]
check("check_rig reports a skeleton that misses its mesh", not err and any("do not occupy the same space" in i
      or "outside the surface" in i for i in issues), issues)

loose = bpy.data.objects.new("Loose", bpy.data.armatures.new("Loose"))
bpy.context.scene.collection.objects.link(loose)
err, text, _ = call("check_rig", armature="Loose")
check("check_rig on an unbound armature says so", not err and "rig_bind" in json.loads(text)["issues"][0], text)
err, text, _ = call("rig_bind", armature="Limb", meshes=["Limb"])
check("rig_bind rejects a non-armature", err and "not an armature" in text, text)
err, text, _ = call("rig_bind", armature="Loose", meshes=["Limb"])
check("rig_bind rejects an armature with no deform bones", err and "no deform bones" in text, text)


# == seams: a tube cut into two pieces whose border vertices only touch, plus a loose stud ======
def tube(name, length=1.2, radius=0.12, rings=13, sides=16, split_ring=None, stud=False):
    bm = bmesh.new()
    loops = [[bm.verts.new((radius * math.cos(2 * math.pi * i / sides), radius * math.sin(2 * math.pi * i / sides),
                            -length / 2 + length * k / (rings - 1))) for i in range(sides)] for k in range(rings)]
    for k in range(rings - 1):
        for i in range(sides):
            bm.faces.new((loops[k][i], loops[k][(i + 1) % sides], loops[k + 1][(i + 1) % sides], loops[k + 1][i]))
    bm.faces.new(list(reversed(loops[0])))
    bm.faces.new(loops[-1])
    if split_ring is not None:
        ring = set(loops[split_ring])
        bmesh.ops.split_edges(bm, edges=[e for e in bm.edges if e.verts[0] in ring and e.verts[1] in ring])
    if stud:
        bmesh.ops.create_cube(bm, size=0.04, matrix=Matrix.Translation((radius + 0.012, 0.0, 0.3)))
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    mesh = bpy.data.meshes.new(name)
    bm.to_mesh(mesh)
    bm.free()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def make_rig(name, spec):
    data = bpy.data.armatures.new(name)
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, tail, parent in spec:
        bone = data.edit_bones.new(bone_name)
        bone.head, bone.tail = head, tail
        if parent:
            bone.parent = data.edit_bones[parent]
    bpy.ops.object.mode_set(mode='OBJECT')
    return obj


def bone_weights(obj, index):
    names = {g.index: g.name for g in obj.vertex_groups}
    return {names[g.group]: round(g.weight, 4) for g in obj.data.vertices[index].groups if g.weight > 1e-4}


seam = tube("Seam", split_ring=6, stud=True)
make_rig("Rig2", [("Lower", (0, 0, -0.6), (0, 0, 0), None), ("Upper", (0, 0, 0), (0, 0, 0.6), "Lower")])
err, text, _ = call("rig_bind", armature="Rig2", meshes=["Seam"])
bound = json.loads(text)["bound"][0] if not err else {}
twins = _shape.Welded(seam).twin_groups()
check("rig_bind finds the seam and attaches the loose stud", not err and bound.get("seam_verts") == 32 and len(twins) == 16
      and bound.get("loose_pieces_attached") == 1 and bound["unweighted_verts"] == 0, text)
check("touching vertices get identical weights", all(bone_weights(seam, g[0]) == bone_weights(seam, g[1]) for g in twins)
      and all(len(bone_weights(seam, g[0])) == 2 for g in twins), [bone_weights(seam, i) for i in twins[0]])
stud = list(range(len(seam.data.vertices) - 8, len(seam.data.vertices)))
check("a loose piece moves rigidly with the surface under it", len({json.dumps(bone_weights(seam, i), sort_keys=True) for i in stud}) == 1
      and bone_weights(seam, stud[0]), [bone_weights(seam, i) for i in stud])
check("the mesh itself is untouched (no vertices merged)", len(seam.data.vertices) == 13 * 16 + 16 + 8)
err, text, _ = call("check_rig", armature="Rig2")
report = json.loads(text)
check("check_rig: a sealed seam does not open", not err and report["meshes"][0]["pose_test"]["seams_open"] == []
      and not any("tears" in i for i in report["issues"]), report["issues"])

for a, b in twins:      # what weighting each piece on its own does: the two sides of the seam disagree
    for index, name in ((a, "Lower"), (b, "Upper")):
        for group in seam.vertex_groups:
            group.remove([index])
        seam.vertex_groups[name].add([index], 1.0, 'REPLACE')
err, text, _ = call("check_rig", armature="Rig2")
report = json.loads(text)
check("check_rig reports a seam that tears open", not err and any("tears open" in i for i in report["issues"])
      and report["meshes"][0]["pose_test"]["seams_open"], report["issues"])
err, text, _ = call("rig_bind", armature="Rig2", meshes=["Seam"], method="keep")
bound = json.loads(text)["bound"][0] if not err else {}
err2, text2, _ = call("check_rig", armature="Rig2")
check("rig_bind method 'keep' repairs the seam without recomputing weights", not err and bound["weights"] == "keep"
      and bound["seam_verts_repaired"] == 32 and not any("tears" in i for i in json.loads(text2)["issues"]), text + text2)
err, text, _ = call("rig_bind", armature="Rig2", meshes=["Seam"], method="nearest")
check("nearest-bone weights also seal the seam", not err
      and all(bone_weights(seam, g[0]) == bone_weights(seam, g[1]) for g in _shape.Welded(seam).twin_groups()), text)

# a small bone beside the surface: far inside the old tolerance (3% of the model), wrong for a finger
rig2 = bpy.data.objects["Rig2"]
bpy.context.view_layer.objects.active = rig2
bpy.ops.object.mode_set(mode='EDIT')
stray = rig2.data.edit_bones.new("Stray")
stray.head, stray.tail, stray.parent = (0.15, 0, 0.1), (0.15, 0, 0.16), rig2.data.edit_bones["Upper"]
bpy.ops.object.mode_set(mode='OBJECT')
err, text, _ = call("check_rig", armature="Rig2")
check("check_rig reports a bone no vertex follows", not err and any("Stray" in i and "no vertices" in i for i in json.loads(text)["issues"]), text)
near = [v.index for v in seam.data.vertices if v.co.x > 0.1 and 0.05 < v.co.z < 0.25]
(seam.vertex_groups.get("Stray") or seam.vertex_groups.new(name="Stray")).add(near, 0.6, 'REPLACE')
err, text, _ = call("check_rig", armature="Rig2")
report = json.loads(text)
check("check_rig judges each bone against its own length", not err
      and [o["bone"] for o in report["meshes"][0].get("outside", [])] == ["Stray"]
      and any("outside the surface" in i for i in report["issues"]), text)


# == limbs: a hand grown from a stick figure ====================================================
def skinned(name, points, edges, radii, levels=2):
    cage_mesh = bpy.data.meshes.new(name + "_cage")
    cage_mesh.from_pydata(points, edges, [])
    cage = bpy.data.objects.new(name + "_cage", cage_mesh)
    bpy.context.scene.collection.objects.link(cage)
    cage.modifiers.new("Skin", 'SKIN')
    for i, radius in enumerate(radii):
        cage_mesh.skin_vertices[0].data[i].radius = radius if isinstance(radius, tuple) else (radius, radius)
    cage_mesh.skin_vertices[0].data[0].use_root = True
    cage.modifiers.new("Subdivision", 'SUBSURF').levels = levels
    bpy.context.view_layer.update()
    mesh = bpy.data.meshes.new_from_object(cage.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    mesh.name = name
    bpy.data.objects.remove(cage, do_unlink=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj


def arm_with_hand(place=lambda p: p):
    """Stick figure of a forearm and hand at life size: points, edges, radii, and the five tips."""
    points = [(0, -0.28, 0), (0, -0.14, 0), (0, 0, 0), (0, 0.05, 0)]
    radii = [0.045, 0.038, (0.028, 0.02), (0.04, 0.015)]
    edges = [(0, 1), (1, 2), (2, 3)]
    tips = {}
    fingers = {"thumb": [(-0.045, 0.03, -0.005), (-0.07, 0.055, -0.01), (-0.09, 0.08, -0.012)],
               "index": [(-0.03, 0.1, 0), (-0.031, 0.14, -0.002), (-0.032, 0.172, -0.022)],
               "middle": [(-0.01, 0.1, 0), (-0.01, 0.145, -0.002), (-0.01, 0.182, -0.024)],
               "ring": [(0.01, 0.1, 0), (0.011, 0.14, -0.002), (0.012, 0.175, -0.022)],
               "pinky": [(0.03, 0.095, 0), (0.033, 0.13, -0.002), (0.036, 0.153, -0.018)]}
    for finger, joints in fingers.items():
        previous = 3
        for joint in joints:
            points.append(joint)
            radii.append(0.011 if finger == "thumb" else 0.0085)
            edges.append((previous, len(points) - 1))
            previous = len(points) - 1
        tips[finger] = Vector(place(joints[-1]))
    return [place(p) for p in points], edges, radii, tips


points, edges, radii, tips = arm_with_hand()
hand = skinned("Hand", points, edges, radii)
err, text, _ = call("find_limbs", mesh="Hand")
found = json.loads(text) if not err else {}
part = found["parts"][0] if not err else {}
fans = part.get("fans", [])
check("find_limbs finds one arm with five fingers", not err and len(found["parts"]) == 1 and len(part["limbs"]) == 5
      and [f["kind"] for f in fans] == ["hand"], text[:900])
by_guess = {l.get("guess"): l for l in part.get("limbs", [])}
check("find_limbs names the fingers and finds their tips", set(by_guess) == set(tips)
      and all((Vector(by_guess[name]["tip"]) - tips[name]).length < 0.02 for name in tips),
      {name: by_guess.get(name, {}).get("tip") for name in tips})
check("find_limbs knows which way the palm faces", fans and fans[0]["palm_side_known"]
      and fans[0]["palm_faces"][2] < -0.7, fans)
narrow = part["trunk"]["narrow_points"] if not err else []
wrist = narrow[-1]["t"] if narrow else 0.8
chains = [{"limb": "A.trunk", "at": [0.0, wrist, 1.0], "names": ["Forearm", "Palm"]}]
chains += [{"limb": f"A.{name}", "names": [f"{name}.{i}" for i in (1, 2, 3)], "parent": "Palm"} for name in tips]
err, text, _ = call("rig_chain", armature="HandRig", chains=chains)
built = json.loads(text) if not err else {}
check("rig_chain builds 17 bones along the measured centre lines", not err and built["bones_total"] == 17
      and all(int(c["inside_mesh"][:-1]) >= 85 for c in built["chains"]), [c["inside_mesh"] for c in built.get("chains", [])])
hand_rig = bpy.data.objects.get("HandRig")
check("finger bones end on the fingertips and start inside the palm", hand_rig is not None
      and all((hand_rig.data.bones[f"{name}.3"].tail_local - tips[name]).length < 0.02 for name in tips)
      and all(hand_rig.data.bones[f"{name}.1"].head_local.y < 0.1 for name in ("index", "middle", "ring", "pinky")),
      {name: tuple(hand_rig.data.bones[f"{name}.1"].head_local) for name in tips} if hand_rig else None)
err, text, _ = call("rig_bind", armature="HandRig", meshes=["Hand"])
err2, text2, _ = call("check_rig", armature="HandRig")
report = json.loads(text2) if not err2 else {}
check("the measured hand rig binds and passes check_rig", not err and not err2 and report["issues"] == []
      and report["meshes"][0]["bones_outside_surface"] == 0, text2)
rest_tip = hand.matrix_world @ hand.data.vertices[min(range(len(hand.data.vertices)),
                                                      key=lambda i: (hand.data.vertices[i].co - tips["index"]).length)].co
curl = hand_rig.pose.bones["index.1"]
curl.rotation_mode = 'XYZ'
curl.rotation_euler = (math.radians(50), 0, 0)
bpy.context.view_layer.update()
posed = hand.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices
moved = [(posed[i].co - hand.data.vertices[i].co) for i in range(len(posed))]
index_tip = max(range(len(moved)), key=lambda i: moved[i].length)
check("a positive X rotation curls a finger toward the palm", moved[index_tip].z < -0.02
      and (hand.data.vertices[index_tip].co - tips["index"]).length < 0.03, tuple(moved[index_tip]))
curl.rotation_euler = (0, 0, 0)
err, text, images = call("look", targets=["HandRig"], focus_bones=["index.1", "index.2", "index.3"], views=["top", "persp"], size=256)
check("look frames a close-up of named bones", not err and images == 2 and json.loads(text).get("close_up"), text)
err, text, _ = call("look", targets=["HandRig"], focus_bones=["nope"], size=256)
check("look names the bones when a focus bone is missing", err and "index.1" in text, text)
err, text, _ = call("rig_chain", armature="HandRig", chains=[{"limb": "A.9", "names": ["x"]}])
check("rig_chain rejects an unknown limb and lists the real ones", err and "A.trunk" in text, text)
hand_rig.location.x += 0.25      # the mesh is parented to the rig: it moves, so the measurement is stale
bpy.context.view_layer.update()
err, text, _ = call("rig_chain", armature="HandRig", chains=[{"limb": "A.index", "names": ["y1", "y2", "y3"]}])
check("rig_chain refuses a measurement of a mesh that has moved since", err and "find_limbs again" in text, text)
hand_rig.location.x -= 0.25
bpy.context.view_layer.update()
err, text, _ = call("rig_chain", armature="HandRig", chains=[{"points": [[0, 0, 0], [0, 0.1, 0]], "names": ["extra"], "parent": "Palm"}])
check("rig_chain accepts explicit points", not err and json.loads(text)["bones_total"] == 18, text)

# a whole body: two arms on a torso, measured from the pelvis
points, edges, radii = [(0, 0, -0.35), (0, 0, 0), (0, 0, 0.3)], [(0, 1), (1, 2)], [0.12, 0.13, 0.1]
all_tips = {}
for side, sign in (("L", 1.0), ("R", -1.0)):
    arm_points, arm_edges, arm_radii, arm_tips = arm_with_hand(lambda p, sign=sign: (sign * (p[1] + 0.45), p[0], p[2] + 0.3))
    offset = len(points)
    points += arm_points
    radii += arm_radii
    edges += [(2, offset)] + [(a + offset, b + offset) for a, b in arm_edges]
    all_tips[side] = arm_tips
body = skinned("Body", points, edges, radii)
err, text, _ = call("find_limbs", mesh="Body", root=[0, 0, -0.35], smallest=0.02)
found = json.loads(text) if not err else {}
part = found["parts"][0] if not err else {}
hands = [f for f in part.get("fans", []) if f["kind"] == "hand"]
check("find_limbs on a whole body finds both hands and their arms", not err and len(hands) == 2
      and all(h["stem"] for h in hands) and len(part.get("stems", [])) >= 2, text[:1500])
thumbs = [Vector(l["tip"]) for l in part.get("limbs", []) if l.get("guess") == "thumb"]
check("each hand gets its own thumb", len(thumbs) == 2
      and all(min((t - all_tips[side]["thumb"]).length for t in thumbs) < 0.02 for side in all_tips), thumbs)
err, text, _ = call("rig_chain", armature="BodyRig", chains=[{"limb": "A.index", "names": ["a", "b", "c"]}])
check("a finger name is refused when two hands share it", err and "hands" in text, text)
left = next((h for h in hands if h["origin"][0] > 0), None)
err, text, _ = call("rig_chain", armature="BodyRig", chains=[
    {"limb": left["stem"], "at": [0.0, 0.5, 1.0], "names": ["UpperArm.L", "Forearm.L"]},
    {"limb": left["fingers"]["index"], "names": ["Index.01.L", "Index.02.L", "Index.03.L"], "parent": "Forearm.L"}]) if left else (True, "", 0)
check("an arm stem and its finger build from a whole body", not err
      and all(int(c["inside_mesh"][:-1]) >= 85 for c in json.loads(text)["chains"]), text[:900])


# == not only hands: a tail, and a wing (a thin sheet with spars) ================================
tail = tube("Tail", length=2.0, radius=0.1, rings=41)
err, text, _ = call("find_limbs", mesh="Tail")
found = json.loads(text) if not err else {}
check("find_limbs on a plain tube reports one trunk and says so", not err and found["parts"][0]["limbs"] == []
      and "hint" in found and abs(found["parts"][0]["trunk"]["length"] - 2.0) < 0.25, text[:600])
err, text, _ = call("rig_chain", armature="TailRig", chains=[{"limb": "A.trunk", "names": [f"Tail.{i:02d}" for i in range(1, 7)]}])
built = json.loads(text) if not err else {}
lengths = built["chains"][0]["lengths"] if not err else [0, 1]
check("rig_chain spaces a tail's bones evenly along the centre line", not err and built["bones_total"] == 6
      and max(lengths) - min(lengths) < 0.05 and built["chains"][0]["inside_mesh"] == "100%"
      and all(abs(j[0]) < 0.02 and abs(j[1]) < 0.02 for j in built["chains"][0]["joints"][1:-1]), text[:700])
err, text, _ = call("rig_bind", armature="TailRig", meshes=["Tail"])
err2, text2, _ = call("check_rig", armature="TailRig")
check("the tail rig binds and passes check_rig", not err and not err2 and json.loads(text2)["issues"] == [], text2)


def wing(name, thickness=0.03):
    outline = [(0, 0.08), (1.0, 0.25), (2.0, 0.3), (1.55, -0.1), (1.9, -0.5), (1.3, -0.45), (1.4, -0.9), (0.7, -0.45), (0, -0.3)]
    bm = bmesh.new()
    bm.faces.new([bm.verts.new((x, y, 0.0)) for x, y in outline])
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    for _round in range(3):
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    flat = bpy.data.meshes.new(name + "_flat")
    bm.to_mesh(flat)
    bm.free()
    sheet = bpy.data.objects.new(name + "_flat", flat)
    bpy.context.scene.collection.objects.link(sheet)
    solid = sheet.modifiers.new("Solidify", 'SOLIDIFY')
    solid.thickness, solid.offset = thickness, 0.0
    bpy.context.view_layer.update()
    mesh = bpy.data.meshes.new_from_object(sheet.evaluated_get(bpy.context.evaluated_depsgraph_get()))
    mesh.name = name
    bpy.data.objects.remove(sheet, do_unlink=True)
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    bpy.context.view_layer.update()
    return obj


membrane = wing("Wing")
spar_tips = [Vector((2.0, 0.3, 0)), Vector((1.9, -0.5, 0)), Vector((1.4, -0.9, 0))]
err, text, _ = call("find_limbs", mesh="Wing", root=[0, -0.1, 0])
found = json.loads(text) if not err else {}
part = found["parts"][0] if not err else {}
fans = part.get("fans", [])
check("find_limbs finds a wing's three spar tips as a fan", not err and len(part["limbs"]) == 3 and len(fans) == 1
      and fans[0]["kind"] == "fan" and all(min((Vector(l["tip"]) - t).length for l in part["limbs"]) < 0.08 for t in spar_tips),
      text[:1400])
origin = Vector(fans[0]["origin"]) if fans else Vector((9, 9, 9))
check("the fan's origin is where the spars spread from (the wing's wrist)", (origin - Vector((1.0, 0.0, 0.0))).length < 0.5
      and abs(origin.z) < 0.01 and fans and abs(fans[0]["plane_normal"][2]) > 0.95, fans)
chains = [{"points": [[0.05, -0.1, 0.012], [0.5, 0.0, -0.01], list(origin)], "centre": True, "names": ["WingArm", "WingForearm"]}]
chains += [{"limb": l["id"], "start": "origin", "names": [f"Spar{n}.1", f"Spar{n}.2"], "parent": "WingForearm"}
           for n, l in enumerate(part.get("limbs", []), start=1)]
err, text, _ = call("rig_chain", armature="WingRig", chains=chains, mesh="Wing")
built = json.loads(text) if not err else {}
arm_joints = built["chains"][0]["joints"] if not err else []
check("explicit points are centred in a thin sheet without sliding along it", not err and built["bones_total"] == 8
      and all(abs(j[2]) < 0.004 for j in arm_joints)
      and abs(arm_joints[0][0] - 0.05) < 0.03 and abs(arm_joints[1][0] - 0.5) < 0.03 and abs(arm_joints[1][1]) < 0.03, text[:600])
wing_rig = bpy.data.objects.get("WingRig")
check("spar chains start at the fan's origin and end on the tips", wing_rig is not None
      and all((wing_rig.data.bones[f"Spar{n}.1"].head_local - origin).length < 1e-3 for n in (1, 2, 3))
      and all(min((wing_rig.data.bones[f"Spar{n}.2"].tail_local - t).length for n in (1, 2, 3)) < 0.08 for t in spar_tips))
err, text, _ = call("rig_bind", armature="WingRig", meshes=["Wing"])
err2, text2, _ = call("check_rig", armature="WingRig")
report = json.loads(text2) if not err2 else {}
check("the wing rig binds; shared membrane is not reported as bleed", not err and not err2
      and report["meshes"][0]["bones_outside_surface"] == 0 and report["meshes"][0]["unweighted_verts"] == 0
      and report["meshes"][0]["pose_test"]["bleed"] == [] and report["meshes"][0]["pose_test"]["no_effect"] == [], text2)


def swing(bone_name, axis):
    bone = wing_rig.pose.bones[bone_name]
    bone.rotation_mode = 'XYZ'
    bone.rotation_euler = tuple(math.radians(30) if i == axis else 0.0 for i in range(3))
    bpy.context.view_layer.update()
    posed = membrane.evaluated_get(bpy.context.evaluated_depsgraph_get()).data.vertices
    moves = [posed[i].co - membrane.data.vertices[i].co for i in range(len(posed))]
    bone.rotation_euler = (0, 0, 0)
    bpy.context.view_layer.update()
    return max(moves, key=lambda m: m.length)


flap, fold = swing("Spar1.1", 0), swing("Spar1.1", 2)
check("in a fan, local X bends out of the plane and local Z folds within it", abs(flap.z) > 0.8 * flap.length > 0.1
      and abs(fold.z) < 0.2 * fold.length and fold.length > 0.1, (tuple(flap), tuple(fold)))

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
