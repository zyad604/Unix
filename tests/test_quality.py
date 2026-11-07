# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for the scene audit: flickering (coplanar) surfaces, assets inside each
other, floating assets, the inventory, the labelled map, and the automatic check after a turn.
Run: blender -b --factory-startup --python tests/test_quality.py"""
import json, math, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix import tools
from unix.tools import _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
tools.load_all()
from unix.tools import quality  # noqa: E402
fails = []


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"], sum(1 for c in res["content"] if c["type"] == "image")


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:700]}"))
    if not cond:
        fails.append(label)


def box(name, size, location, rotation_z=0.0, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location, rotation=(0, 0, rotation_z))
    obj = bpy.context.active_object
    obj.name, obj.scale = name, size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    if parent is not None:
        obj.parent = parent
        obj.matrix_parent_inverse = parent.matrix_world.inverted()
    return obj


def plane(name, size, z):
    bpy.ops.mesh.primitive_plane_add(size=size, location=(0, 0, z))
    obj = bpy.context.active_object
    obj.name = name
    return obj


def pairs(found, key_a="a", key_b="b"):
    return {tuple(sorted((item[key_a], item[key_b]))) for item in found}


bpy.data.objects.remove(bpy.data.objects["Cube"])

# -- a clean little scene: nothing to report ------------------------------------------------
ground = plane("Ground", 40, 0.0)
house = box("House", (4, 4, 3), (0, 0, 1.5))            # stands on the ground: faces meet, they do not share a side
shed = box("Shed", (2, 2, 2), (6, 0, 1.0))
bpy.context.view_layer.update()
clean = quality.audit()
check("a tidy scene has no problems", clean["problems"] == [], clean)
check("an object standing on the ground is not a flicker", clean["flickering_surfaces"] == [], clean["flickering_surfaces"])

# -- surfaces in the same plane ----------------------------------------------------------------
patio = plane("Patio", 6, 0.0)                           # lies exactly in the ground's plane
patio.location = (10, 10, 0.0)
bpy.context.view_layer.update()
found = quality.audit()
check("a surface lying in another's plane is reported", ("Ground", "Patio") in pairs(found["flickering_surfaces"])
      and any("flicker" in line for line in found["problems"]), found["flickering_surfaces"])
area = next((i["area_m2"] for i in found["flickering_surfaces"] if {i["a"], i["b"]} == {"Ground", "Patio"}), 0)
check("the shared area is measured", abs(area - 36.0) < 0.5, area)
patio.location.z = 0.02
bpy.context.view_layer.update()
check("lifting it two centimetres clears it", quality.audit()["flickering_surfaces"] == [], quality.audit()["flickering_surfaces"])
wall_a = box("Wall_A", (4, 0.2, 3), (20, 0, 1.5))
wall_b = box("Wall_B", (4, 0.2, 3), (22, 0, 1.5))        # same wall line, overlapping by two metres
bpy.context.view_layer.update()
found = quality.audit()
check("two walls overlapping in one plane are reported", ("Wall_A", "Wall_B") in pairs(found["flickering_surfaces"]),
      found["flickering_surfaces"])
wall_b.location.x = 24.0                                  # end to end: they touch, nothing is shared
bpy.context.view_layer.update()
check("walls placed end to end are fine", ("Wall_A", "Wall_B") not in pairs(quality.audit()["flickering_surfaces"]))
bpy.data.objects.remove(wall_a)
bpy.data.objects.remove(wall_b)

# -- assets inside each other -----------------------------------------------------------------
second = box("House.001", (4, 4, 3), (1.5, 0.3, 1.5))     # pushed more than half into the first house
bpy.context.view_layer.update()
found = quality.audit()
check("two assets standing inside each other are reported", ("House", "House.001") in pairs(found["overlapping_assets"])
      and any("stands inside" in line and "different things" in line for line in found["problems"]), found)
second.location.x = -8.0
bpy.context.view_layer.update()
check("moved apart, they are fine", quality.audit()["overlapping_assets"] == [], quality.audit()["overlapping_assets"])
roof = box("House_Roof", (4.4, 4.4, 0.6), (0, 0, 2.9), parent=house)     # sunk into its own house: one asset
bpy.context.view_layer.update()
check("parts of one asset may overlap each other", quality.audit()["overlapping_assets"] == [], quality.audit()["overlapping_assets"])


# -- things put together the way builders do it: nothing here is a problem -------------------------
def solid(name, verts, faces):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def gable(name, x, y, z, half_x, half_y, height):
    verts = [(x - half_x, y - half_y, z), (x + half_x, y - half_y, z), (x + half_x, y + half_y, z), (x - half_x, y + half_y, z),
             (x - half_x, y, z + height), (x + half_x, y, z + height)]
    return solid(name, verts, [(0, 3, 2, 1), (0, 1, 5, 4), (2, 3, 4, 5), (0, 4, 3), (1, 2, 5)])


def cone(name, x, y, z, radius, height, sides=8):
    verts = [(x + radius * math.cos(2 * math.pi * i / sides), y + radius * math.sin(2 * math.pi * i / sides), z)
             for i in range(sides)] + [(x, y, z + height)]
    return solid(name, verts, [(i, (i + 1) % sides, sides) for i in range(sides)] + [tuple(range(sides))[::-1]])


made = [box("Hut_0_Walls", (4, 4, 2.8), (40, 0, 1.4)), gable("Hut_0_Roof", 40, 0, 2.8, 2.4, 2.4, 1.6),
        box("Hut_0_Door", (1, 0.1, 1.9), (40, -2.0, 0.95)),                     # set into the wall
        box("Hut_0_Chimney", (0.6, 0.6, 1.5), (41.2, 0, 3.8)),                  # stands in the roof slope, top well clear
        box("Pine_3_Trunk", (0.4, 0.4, 1.8), (50, 0, 0.9)), cone("Pine_3_Top", 50, 0, 1.4, 1.5, 3.5),
        cone("Pine_3_Top2", 50, 0, 2.8, 1.1, 2.8),                              # tops pushed over the trunk and each other
        box("Basin_Base", (2, 2, 0.9), (60, 0, 0.45)), box("Basin_Water", (1.6, 1.6, 0.8), (60, 0, 0.6)),   # water shows at the top
        box("Basin_Post-1", (0.15, 0.15, 2.2), (59.1, 0, 1.9)), box("Basin_Post1", (0.15, 0.15, 2.2), (60.9, 0, 1.9)),
        gable("Basin_Roof", 60, 0, 2.9, 1.3, 0.5, 0.8),                         # rests on two thin posts
        box("Lane_Main", (3, 30, 0.03), (70, 0, 0.015)), box("Lane_Cross", (20, 2.5, 0.03), (70, 4, 0.035)),
        box("Rail_Bar", (9, 0.08, 0.1), (80, 0, 0.75))] + [box(f"Rail_{i}", (0.12, 0.12, 1.0), (76 + 2 * i, 0, 0.5)) for i in range(5)]
# names that say nothing about what belongs together: size and softness have to
made += [box("Frame", (1.2, 0.3, 2.1), (40, 2.0, 1.05)),                         # a door frame set into the hut's back wall
         box("OakTrunk", (0.5, 0.5, 3.0), (90, 0, 1.5)), cone("OakCrown", 90, 0, 2.0, 2.5, 4.0),
         cone("Shrub_1", 90, 6, 0, 1.0, 1.2), cone("Shrub_2", 90.9, 6, 0, 1.0, 1.2), cone("Shrub_3", 91.8, 6, 0, 1.0, 1.2)]
knob = box("Knob", (0.06, 0.06, 0.06), (40, -2.08, 1.0))                          # a door knob flush against its door
made.append(knob)
# one mesh made of two boxes that cross in the same plane, in one material: a window frame's bars
frame = solid("Bars", [(0, 0, 0), (2, 0, 0), (2, 0, 0.2), (0, 0, 0.2), (0.9, 0, -1), (1.1, 0, -1), (1.1, 0, 1), (0.9, 0, 1)],
              [(0, 1, 2, 3), (4, 5, 6, 7)])
frame.location = (80, 6, 1.0)
made.append(frame)
ground.scale = (5, 5, 1)
bpy.context.view_layer.update()
found = quality.audit()
check("a house, a tree, a well, crossing paths and a fence built from parts raise nothing", found["problems"] == [], found["problems"])
counted = quality.inventory()
check("parts are counted as the things they make up", counted["by_kind"].get("Hut") == 1 and counted["by_kind"].get("Pine") == 1
      and counted["by_kind"].get("Basin") == 1 and counted["by_kind"].get("Rail") == 5 and counted["by_kind"].get("Lane") == 1,
      counted["by_kind"])
chimney = bpy.data.objects["Hut_0_Chimney"]
chimney.location = (40.0, 0.0, 3.55)                                             # sunk into the ridge: two slivers show
bpy.context.view_layer.update()
found = quality.audit()
check("a chimney sunk into its own roof is reported as hidden", [(i["object"], i["inside"]) for i in found["buried"]] == [("Hut_0_Chimney", "Hut_0_Roof")]
      and any("hidden inside" in line for line in found["problems"]), found)
chimney.location = (41.2, 0.0, 3.8)
pine_top = bpy.data.objects["Pine_3_Top"]
pine_top.location = (-10.5, 0, 0)                                                # the tree top now pokes through the hut's wall
bpy.data.objects["Pine_3_Top2"].location = (-10.5, 0, 0)
bpy.data.objects["Pine_3_Trunk"].location = (39.5, 0, 0.9)
bpy.context.view_layer.update()
found = quality.audit()
check("a tree standing in a house is reported: they are different things", any({i["a"].split("_")[0], i["b"].split("_")[0]} == {"Pine", "Hut"}
                                                                                  for i in found["overlapping_assets"]), found)
check("a hedge, a frame in a wall and a trunk in its crown are still not", not any({i["a"], i["b"]} & {"Frame", "OakTrunk", "OakCrown", "Shrub_1", "Shrub_2", "Shrub_3"}
                                                                                    for i in found["overlapping_assets"]), found["overlapping_assets"])
bpy.data.objects["Basin_Roof"].location.z = 0.6                                  # lifted off its posts
bpy.context.view_layer.update()
floating = {item["asset"]: item for item in quality.audit()["floating"]}
check("a roof lifted off its posts is in the air, measured to the post tops", abs(floating.get("Basin_Roof", {}).get("gap_m", 0) - 0.5) < 0.05,
      floating)
inside_out = bpy.data.objects["Hut_0_Roof"]
inside_out.data.flip_normals()
chimney.location = (40.0, 0.0, 3.55)
bpy.context.view_layer.update()
found = quality.audit()
check("a solid built inside out is still a solid", ("Hut_0_Chimney", "Hut_0_Roof") in [(i["object"], i["inside"]) for i in found["buried"]],
      found["buried"])
check("a trunk standing wholly inside a house is hidden too", ("Pine_3_Trunk", "Hut_0_Walls") in [(i["object"], i["inside"]) for i in found["buried"]],
      found["buried"])
for obj in made:
    bpy.data.objects.remove(obj)
ground.scale = (1, 1, 1)
bpy.context.view_layer.update()

# -- assets in the air ------------------------------------------------------------------------
crate = box("Crate", (1, 1, 1), (12, -6, 1.5))            # one metre above the ground
lamp = box("Lamp", (0.3, 0.3, 0.3), (6, 0, 2.15))         # sits on the shed's roof
bpy.context.view_layer.update()
found = quality.audit()
floating = {item["asset"]: item["gap_m"] for item in found["floating"]}
check("an asset hanging in the air is reported with its gap", abs(floating.get("Crate", 0) - 1.0) < 0.02, found["floating"])
check("an asset resting on another is not", "Lamp" not in floating and "Shed" not in floating, found["floating"])
crate.location.z = 0.5
bpy.context.view_layer.update()
check("seated, nothing floats", quality.audit()["floating"] == [], quality.audit()["floating"])

# -- inventory, check_scene, the map ------------------------------------------------------------
shed.rotation_euler.z = math.radians(90)                  # its front (-Y) now faces +X
bpy.context.view_layer.update()
counted = quality.inventory()
check("the inventory counts assets by kind", counted["by_kind"].get("House") == 2 and counted["assets"] == 7, counted["by_kind"])
patio.location.z = 0.0
bpy.context.view_layer.update()
err, text, _ = call("check_scene")
report = json.loads(text) if not err else {}
check("check_scene reports the inventory, and takes a shared plane apart itself", not err
      and report["inventory"]["by_kind"]["House"] == 2 and report.get("separated", [{}])[0].get("moved") == "Patio"
      and "flickering_surfaces" not in report["geometry"] and abs(patio.matrix_world.translation.z - 0.012) < 1e-4, text[:600])
patio.location.z = 0.02

# -- things that are not problems, and one that is -------------------------------------------------
housing = box("Lantern_Body", (0.2, 0.2, 0.3), (80, 0, 0.15))
bulb = box("Lantern_Glow", (0.08, 0.08, 0.1), (80, 0, 0.15))
glow = bpy.data.materials.new("Glow")
glow.use_nodes = True
glow.node_tree.nodes["Principled BSDF"].inputs["Emission Strength"].default_value = 5.0
glow.node_tree.nodes["Principled BSDF"].inputs["Emission Color"].default_value = (1.0, 0.8, 0.5, 1.0)
bulb.data.materials.append(glow)
line = box("Squeezed", (1.2, 1.0, 1.0), (82, 0, 1.0))
line.scale = (1.0, 0.0, 0.0)
bpy.context.view_layer.update()
found = quality.audit()
check("a light inside its housing is not reported as hidden", not any(i["object"] == "Lantern_Glow" for i in found["buried"]), found["buried"])
check("an object squeezed to a line is reported as that, not as floating", found["collapsed"] == ["Squeezed"]
      and not any(i["asset"] == "Squeezed" for i in found["floating"]) and any("squeezed to a line" in p for p in found["problems"]), found)
for extra in (housing, bulb, line):
    bpy.data.objects.remove(extra)

# -- taking shared planes apart ------------------------------------------------------------------
wall = box("Facade", (8, 0.3, 3), (60, 0, 1.5))
glass = box("Pane", (1.2, 0.02, 1.2), (60, -0.14, 1.6))      # a pane whose face lies in the wall's face
pillar = box("Pillar", (0.5, 0.25, 0.5), (62, -0.025, 1.0))  # a block set into the wall, flush with its face
near = box("Trim", (2.0, 0.02, 0.2), (58, -0.143, 2.6))      # 3 mm proud of an 8 m wall: flickers from a distance
flush = box("Post", (0.4, 0.3, 1.0), (57, 0, 0.5))           # as thick as the wall: flush on both of its faces
bpy.context.view_layer.update()
found = quality.audit()
check("faces a few millimetres apart on a big thing count as sharing a plane", ("Facade", "Trim") in pairs(found["flickering_surfaces"])
      and ("Facade", "Pane") in pairs(found["flickering_surfaces"]), found["flickering_surfaces"])
moved = {item["moved"]: item for item in quality.separate()}
bpy.context.view_layer.update()
check("a sheet on a surface comes out to sit on it; a block goes in", glass.matrix_world.translation.y < -0.14 - 0.009
      and pillar.matrix_world.translation.y > -0.025 + 0.009 and set(moved) >= {"Pane", "Pillar", "Trim"}
      and wall.matrix_world.translation.y == 0.0, {k: tuple(bpy.data.objects[k].matrix_world.translation) for k in moved})
check("what is flush on both sides ends up proud on one and sunk on the other", abs(abs(flush.matrix_world.translation.y) - 0.0096) < 1e-4,
      tuple(flush.matrix_world.translation))
bpy.data.objects.remove(flush)
check("afterwards nothing flickers", quality.audit()["flickering_surfaces"] == [], quality.audit()["flickering_surfaces"])
check("and a second pass moves nothing more", quality.separate() == [])
glass.location.y = -0.14
bpy.context.view_layer.update()
left = quality.separate(movable={"Facade"})
check("when only the bigger thing may be moved, it moves the other way", {i["moved"] for i in left} == {"Facade"}
      and wall.matrix_world.translation.y > 0.005 and abs(glass.matrix_world.translation.y + 0.14) < 1e-6, left)
check("and nothing moves when neither is ours", quality.separate(movable={"Shed"}) == [])
small_a = box("Plate_A", (0.10, 0.02, 0.05), (70, 0, 1.0))
small_b = box("Plate_B", (0.04, 0.02, 0.03), (70, -0.0005, 1.0))   # half a millimetre proud on a 10 cm part: fine detail
bpy.context.view_layer.update()
check("fine detail on a small thing is left alone", ("Plate_A", "Plate_B") not in pairs(quality.audit()["flickering_surfaces"]))
for extra in (wall, glass, pillar, near, small_a, small_b):   # (the post is already gone)
    bpy.data.objects.remove(extra)
objects_before, materials_before = len(bpy.data.objects), len(bpy.data.materials)
curves_before, meshes_before = len(bpy.data.curves), len(bpy.data.meshes)
err, text, images = call("look_map", size=512)
data = json.loads(text) if not err else {}
legend = {row["name"]: row for row in data.get("legend", [])}
check("look_map returns one picture and a numbered legend", not err and images == 1 and len(legend) == 7
      and sorted(row["n"] for row in data["legend"]) == list(range(1, 8)) and data["by_kind"]["House"] == 2, text[:400])
check("the legend says which way each asset faces", legend.get("Shed", {}).get("faces") == "east (+X)"
      and legend.get("House", {}).get("faces") == "south (-Y)" and "faces" not in legend.get("Ground", {}), legend)
check("look_map leaves nothing behind", (len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.curves), len(bpy.data.meshes))
      == (objects_before, materials_before, curves_before, meshes_before))

# -- the automatic check after a turn --------------------------------------------------------------
from unix.core import engine  # noqa: E402
sent = []
engine.send = lambda text, **kwargs: sent.append((text, kwargs))          # no model is started in this test
engine._last_send = {"model": "sonnet", "claude_bin": "", "rules": "", "mode": "agent", "instruction": "", "effort": "medium"}
engine._check_rounds = 0
engine.settings["auto_check"] = True
engine._check_after_turn()
check("a clean scene sends nobody back", sent == [], sent)
patio.location.z = 0.0
crate.location.z = 1.5
bpy.context.view_layer.update()
engine._check_after_turn()
check("problems send the agent back with the list", len(sent) == 1 and "[Automatic scene check]" in sent[0][0]
      and "Patio" in sent[0][0] and "Crate" in sent[0][0] and "2 problems" in sent[0][1]["shown"]
      and sent[0][1]["model"] == "sonnet", sent[:1])
engine._check_after_turn()
check("a problem the agent was shown and left is not brought up again", len(sent) == 1, len(sent))
engine._check_rounds = 0                                   # the user's next message (say, 'send it to Roblox')
engine._check_after_turn()
check("and a later turn that changes nothing does not start the fixing again", len(sent) == 1, len(sent))
lamp.location.z = 4.0                                      # a fix that broke something else
bpy.context.view_layer.update()
engine._check_after_turn()
check("only what is new goes back", len(sent) == 2 and "Lamp" in sent[1][0] and "Crate" not in sent[1][0]
      and "1 problem " in sent[1][1]["shown"], sent[1:])
engine._check_rounds = 2
shed.location.z = 5.0
bpy.context.view_layer.update()
engine._check_after_turn()
check("it gives up after two rounds for one message", len(sent) == 2, len(sent))
engine.settings["auto_check"] = False
engine._check_rounds = 0
engine._problems_shown = set()
engine._check_after_turn()
check("switched off, it does nothing", len(sent) == 2)

# -- the review pass when the user asked for realism ------------------------------------------------
from unix import core  # noqa: E402
check('asking for realism is recognised, in the words people use', all(core._REALISM.search(t) for t in (
    'make the most realistic glock', 'make it look real life', 'photoreal chair please', 'make same as this house', 'an accurate model of a violin'))
      and not any(core._REALISM.search(t) for t in ('make a village', 'low poly tree', 'get it to roblox', 'a really big cube')))
sent.clear()
engine.settings['auto_review'] = True
engine._wants_realism, engine._review_rounds = True, 0
first = engine._review_after_turn()
check('a first version of realistic work is sent back to be compared with the real thing', first is True and len(sent) == 1
      and '[Automatic review]' in sent[0][0] and 'match_reference' in sent[0][0] and 'wrong side' in sent[0][0]
      and 'Automatic review' in sent[0][1]['shown'], sent[:1])
engine._review_after_turn()
check('at most twice per message', engine._review_after_turn() is False and len(sent) == 2, len(sent))
engine._wants_realism, engine._review_rounds = False, 0
check('other requests are not reviewed', engine._review_after_turn() is False and len(sent) == 2)
engine.settings['auto_review'] = False
engine._wants_realism = True
check('and it can be switched off', engine._review_after_turn() is False and len(sent) == 2)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
