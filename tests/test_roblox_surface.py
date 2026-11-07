# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for what a Blender surface becomes on its way to Roblox Studio: which pictures
travel, what is in them, and what the jobs say about them. A thread stands in for the plugin.
Run: blender -b --factory-startup --python tests/test_roblox_surface.py"""
import base64, json, math, os, sys, tempfile, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import numpy as np
from unix import tools
from unix.tools import _util

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")  # keep test data out of the user's config
tools.load_all()
from unix.tools import interact, roblox, roblox_surface  # noqa: E402
from unix.bridge import roblox_link  # noqa: E402
fails = []
link = roblox_link.link


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


class FakeStudio(threading.Thread):
    """Does what the plugin does on the wire: ask for work, put pictures together from their bands,
    and report what it 'built'. With the upload agreed every new picture becomes an asset."""

    def __init__(self, place, version):
        super().__init__(daemon=True)
        self.place, self.session, self.version, self.jobs, self.alive = place, place + "-session", version, [], True
        self.bands, self.assets, self.shapes = {}, set(), set()

    def post(self, path, body):
        request = urllib.request.Request(f"http://127.0.0.1:{link.port}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Unix-Plugin": self.version})
        return json.loads(urllib.request.urlopen(request, timeout=30).read())

    def run(self):
        while self.alive:
            job = self.post("/unix/poll", {"session": self.session, "place": self.place, "placeId": 1, "gameId": 1,
                                            "version": self.version}).get("job")
            if not job:
                continue
            if job.get("kind") == "known":      # what the place holds as assets, as the plugin's uploadedBefore finds it
                self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": True,
                                            "report": {"meshes": sorted(self.shapes), "pictures": sorted(self.assets), "notes": []}})
                continue
            self.jobs.append(job)
            if job.get("kind") == "picture":
                if job["y"] == 0:
                    self.bands[job["key"]] = []
                self.bands[job["key"]].append(job)
                report = {"picture": job["key"], "rows": job["rows"], "notes": []}
            else:
                meshes = job.get("meshes") or []
                keep = bool(job.get("upload"))
                named = {key for mesh in meshes for key in ((mesh.get("surface") or {}).get("maps") or {}).values()}
                fresh = named - self.assets if keep else set()
                self.assets |= fresh
                if keep:
                    self.shapes |= {m["key"] for m in meshes if m.get("key")}
                report = {"parts": len(job.get("parts") or []), "meshes": len(meshes), "effects": 0, "notes": [],
                          "model": "Workspace." + job["asset"], "uploaded": sum(1 for m in meshes if "v" in m) if keep else 0,
                          "reused": 0, "kept": len(meshes) if keep else 0, "preview": 0 if keep else len(meshes),
                          "pictures_uploaded": len(fresh)}
            self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": True, "report": report})

    def picture(self, key):
        """A picture as the plugin would hold it: (rows, columns, channels) bytes from the bands that arrived."""
        bands = sorted(self.bands[key], key=lambda band: band["y"])
        data = b"".join(base64.b64decode(band["data"]) for band in bands)
        return np.frombuffer(data, dtype=np.uint8).reshape(bands[0]["h"], bands[0]["w"], bands[0]["c"])


def drive(sender, answer=None):
    """Run a Sender the way the registry does. Returns (report, the question it asked, if any)."""
    asked, deadline = None, time.time() + 120
    while time.time() < deadline:
        report = sender.step()
        if report is not None:
            return report, asked
        question = interact.pending_question()
        if question is not None and asked is None:
            asked = question["text"]
            sender.posted_when_asked = sender.posted
            interact.answer_question(answer)
        time.sleep(0.005)
    return {}, asked


check("the link server starts on a private port", link.start(ports=(0,)) and link.port > 0, link.port)
studio = FakeStudio("Look Place", roblox_link.plugin_version())
studio.start()
deadline = time.time() + 5
while not link.sessions() and time.time() < deadline:
    time.sleep(0.05)
session = link.sessions()[0]
check("the stand-in plugin is one that builds surfaces", roblox_surface.plugin_shows_surfaces(session)
      and int(roblox_link.plugin_version()) >= roblox_surface.SURFACE_PLUGIN, session)


def send(names, asset="Look", keep="preview", answer=None, to=None, at=None):
    """Send objects by name. Returns (report, the jobs of this send, the question asked)."""
    target = to or studio
    before = len(target.jobs)
    sender = roblox.Sender([bpy.data.objects[n] for n in names], asset, at or session, False, keep)
    if answer is not None:
        sender.can_ask = True
    report, asked = drive(sender, answer)
    posted_first[0] = getattr(sender, "posted_when_asked", None)
    return report, target.jobs[before:], asked


posted_first = [None]       # jobs the last send had posted when it asked its question


def meshes_of(jobs):
    return {x["name"]: x for job in jobs for x in (job.get("meshes") or [])}


def parts_of(jobs):
    return {x["name"]: x for job in jobs for x in (job.get("parts") or [])}


def pictures_in(jobs):
    """Keys of the pictures these jobs carried, in the order their first band left."""
    return [job["key"] for job in jobs if job.get("kind") == "picture" and job["y"] == 0]


# -- pictures made here: nothing is downloaded -------------------------------------------------
def image(name, pixels, colour=True, as_float=False):
    """A Blender image from a top-down (rows, columns, 4) array of 0..1 values, stored as given."""
    rows, columns = pixels.shape[:2]
    made = bpy.data.images.new(name, columns, rows, alpha=True, float_buffer=as_float)
    if not colour:
        made.colorspace_settings.name = 'Non-Color'
    made.pixels.foreach_set(np.ascontiguousarray(pixels[::-1], dtype=np.float32).ravel())
    return made


def filled(rows, columns, rgba):
    return np.tile(np.array(rgba, dtype=np.float32), (rows, columns, 1))


def material(name):
    made = bpy.data.materials.new(name)
    made.use_nodes = True
    return made, made.node_tree.nodes, made.node_tree.links, made.node_tree.nodes["Principled BSDF"]


def picture_node(nodes, picture):
    node = nodes.new("ShaderNodeTexImage")
    node.image = picture
    return node


spot = [0]


def shape(name, made, kind="torus"):
    """A mesh that is no native Roblox shape (a torus), a ball, or a few faces without UVs."""
    spot[0] += 3
    if kind == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=0.5, minor_radius=0.2, major_segments=12, minor_segments=6, location=(spot[0], 0, 1))
    elif kind == "ball":
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(spot[0], 0, 1))
    else:
        mesh = bpy.data.meshes.new(name)
        mesh.from_pydata([(0, 0, 0.7), (0, 0, -0.7), (0.5, 0, 0), (0, 0.5, 0), (-0.5, 0, 0), (0, -0.5, 0)], [],
                         [(0, 2, 3), (0, 3, 4), (0, 4, 5), (0, 5, 2), (1, 3, 2), (1, 4, 3), (1, 5, 4), (1, 2, 5)])
        mesh.update()
        bpy.context.scene.collection.objects.link(bpy.data.objects.new(name, mesh))
        bpy.data.objects[name].location = (spot[0], 0, 1)
        bpy.context.view_layer.objects.active = bpy.data.objects[name]
    obj = bpy.context.view_layer.objects.active
    obj.name = name
    obj.data.materials.append(made)
    bpy.context.view_layer.update()
    return obj


# -- 1. a material made of pictures: colour, normal, roughness, metal ----------------------------
colours = np.zeros((16, 16, 4), dtype=np.float32)
colours[..., 3] = 1.0
colours[:8, :, 0], colours[8:, :, 2] = 200 / 255, 100 / 255          # top half red, bottom half blue
rough = filled(16, 16, (0.25, 0.25, 0.25, 1.0))
rough[:, 8:, :3] = 0.75
metal = filled(16, 16, (1.0, 1.0, 1.0, 1.0))
metal[4:, :, :3] = 0.0
tilted = filled(16, 16, (0.8, 0.5, 0.8, 1.0))                         # a normal leaning to +X: (0.6, 0, 0.6) before it is made unit length
pbr, nodes, links, shader = material("Pictured")
links.new(picture_node(nodes, image("T_Colour", colours)).outputs["Color"], shader.inputs["Base Color"])
links.new(picture_node(nodes, image("T_Rough", rough, colour=False)).outputs["Color"], shader.inputs["Roughness"])
links.new(picture_node(nodes, image("T_Metal", metal, colour=False)).outputs["Color"], shader.inputs["Metallic"])
bump = nodes.new("ShaderNodeNormalMap")
links.new(picture_node(nodes, image("T_Normal", tilted, colour=False)).outputs["Color"], bump.inputs["Color"])
links.new(bump.outputs["Normal"], shader.inputs["Normal"])
shape("Pictured_Ball", pbr, "ball")
report, jobs, _asked = send(["Pictured_Ball"])
piece = meshes_of(jobs).get("Pictured_Ball", {})
surface = piece.get("surface") or {}
maps = surface.get("maps") or {}
check("a ball with pictures goes as a mesh: a Part cannot carry them", piece and not parts_of(jobs), list(parts_of(jobs)))
check("colour, normal, roughness and metal pictures each become a map of the surface", sorted(maps) == ["color", "metalness", "normal", "roughness"]
      and "texture" not in piece, surface)
order = [("picture", job["key"]) if job.get("kind") == "picture" else ("mesh", None) for job in jobs]
check("the pictures leave before the mesh that uses them, each once", order[-1] == ("mesh", None) and len(order) == 5
      and sorted(key for _kind, key in order[:4]) == sorted(maps.values()), order)
kinds = {role: studio.picture(key).shape for role, key in maps.items()}
check("colour and normal travel as RGBA, roughness and metal as one grey byte a pixel, in the picture's own size",
      kinds == {"color": (16, 16, 4), "normal": (16, 16, 4), "roughness": (16, 16), "metalness": (16, 16)}
      or kinds == {"color": (16, 16, 4), "normal": (16, 16, 4), "roughness": (16, 16, 1), "metalness": (16, 16, 1)}, kinds)
sent = studio.picture(maps["color"])
check("an 8-bit sRGB picture arrives as it was painted, top row first", sent[0, 0].tolist() == [200, 0, 0, 255] and sent[15, 0].tolist() == [0, 0, 100, 255],
      (sent[0, 0].tolist(), sent[15, 0].tolist()))
sent = studio.picture(maps["roughness"]).reshape(16, 16)
check("the roughness picture carries the values the shader reads", int(sent[0, 0]) == 64 and int(sent[0, 15]) == 191, (sent[0, 0], sent[0, 15]))
sent = studio.picture(maps["metalness"]).reshape(16, 16)
check("the metal picture carries metal where it is", int(sent[0, 0]) == 255 and int(sent[15, 0]) == 0, (sent[0, 0], sent[15, 0]))
sent = studio.picture(maps["normal"])
check("a normal map arrives as it is (OpenGL, green up), made unit length", abs(int(sent[0, 0, 0]) - 218) <= 1 and abs(int(sent[0, 0, 1]) - 128) <= 1
      and abs(int(sent[0, 0, 2]) - 218) <= 1, sent[0, 0].tolist())
check("the report counts the pictures by what they are for", "4 pictures went along for the surfaces of 1 meshes" in report.get("textures", "")
      and "1 colour, 1 normal, 1 roughness, 1 metalness" in report["textures"], report.get("textures"))
check("without the upload the report says the pictures are only shown from memory", "shown from memory" in report.get("textures", "")
      and "4 pictures" in report.get("preview_only", ""), (report.get("textures"), report.get("preview_only")))
check("nothing private travels", not any(k.startswith("_") for item in list(meshes_of(jobs).values()) for k in list(item) + list(item.get("surface") or {})))

# -- 2. size: own size up to 1024 px, and big pictures in bands -----------------------------------
big, nodes, links, shader = material("BigPicture")
gradient = np.zeros((2048, 2048, 4), dtype=np.float32)
gradient[..., 0] = np.linspace(0, 1, 2048, dtype=np.float32)[None, :]
gradient[..., 3] = 1.0
links.new(picture_node(nodes, image("T_Big", gradient)).outputs["Color"], shader.inputs["Base Color"])
shape("Big_Torus", big)
odd, nodes, links, shader = material("OddPicture")
links.new(picture_node(nodes, image("T_Odd", filled(200, 300, (0.2, 0.4, 0.6, 1.0)) * np.linspace(0.5, 1, 300, dtype=np.float32)[None, :, None])).outputs["Color"],
          shader.inputs["Base Color"])
shape("Odd_Torus", odd)
report, jobs, _asked = send(["Big_Torus", "Odd_Torus"])
sent = meshes_of(jobs)
key = sent["Big_Torus"]["surface"]["maps"]["color"]
bands = [job for job in jobs if job.get("kind") == "picture" and job["key"] == key]
check("a 2048 px picture is shrunk to Roblox's 1024 px and travels in bands of about a megabyte", [b["y"] for b in bands] == [0, 256, 512, 768]
      and all(b["w"] == 1024 and b["h"] == 1024 and b["rows"] == 256 and len(b["data"]) < 1_500_000 for b in bands),
      [(b["y"], b["rows"], b["w"], len(b["data"])) for b in bands])
check("the bands put together are the picture", studio.picture(key).shape == (1024, 1024, 4)
      and abs(int(studio.picture(key)[500, 1023, 0]) - 255) <= 1 and int(studio.picture(key)[500, 0, 0]) <= 1, studio.picture(key)[500, ::341, 0])
check("a smaller picture travels in its own size, not blown up", studio.picture(sent["Odd_Torus"]["surface"]["maps"]["color"]).shape == (200, 300, 4))
check("a matt surface gets its roughness as a few pixels of one grey", sent["Odd_Torus"]["surface"]["maps"]["roughness"] == "flat:128"
      and studio.picture("flat:128").size == 16, sent["Odd_Torus"]["surface"]["maps"])

# -- 3. colour: a 32-bit linear picture is written out as sRGB ------------------------------------
linear, nodes, links, shader = material("LinearPicture")
half = filled(8, 8, (0.5, 0.2140, 0.0, 1.0))                           # linear light: sRGB 188, 128, 0
half[4:, :, :3] = 1.0
links.new(picture_node(nodes, image("T_Linear", half, as_float=True)).outputs["Color"], shader.inputs["Base Color"])
shape("Linear_Torus", linear)
report, jobs, _asked = send(["Linear_Torus"])
sent = studio.picture(meshes_of(jobs)["Linear_Torus"]["surface"]["maps"]["color"])
check("linear light is turned into sRGB once, not sent as if it were sRGB", abs(int(sent[0, 0, 0]) - 188) <= 1 and abs(int(sent[0, 0, 1]) - 128) <= 1,
      sent[0, 0].tolist())

# -- 4. one packed picture, a channel for each number (how glTF models arrive), through nodes ----
packed = filled(8, 8, (1.0, 0.25, 1.0, 1.0))
packed[:, 4:, 1], packed[4:, :, 2] = 0.9, 0.0
orm, nodes, links, shader = material("Packed")
split = nodes.new("ShaderNodeSeparateColor")
links.new(picture_node(nodes, image("T_Packed", packed, colour=False)).outputs["Color"], split.inputs["Color"])
links.new(split.outputs["Green"], shader.inputs["Roughness"])
links.new(split.outputs["Blue"], shader.inputs["Metallic"])
shader.inputs["Base Color"].default_value = (0.8, 0.1, 0.1, 1.0)
shape("Packed_Torus", orm)
gloss, nodes, links, shader = material("GlossInverted")
invert = nodes.new("ShaderNodeInvert")
links.new(picture_node(nodes, image("T_Gloss", filled(8, 8, (0.6, 0.6, 0.6, 1.0)), colour=False)).outputs["Color"], invert.inputs["Color"])
scale = nodes.new("ShaderNodeMath")
scale.operation = 'MULTIPLY'
scale.inputs[1].default_value = 0.5
links.new(invert.outputs["Color"], scale.inputs[0])
links.new(scale.outputs["Value"], shader.inputs["Roughness"])
shape("Gloss_Torus", gloss)
report, jobs, _asked = send(["Packed_Torus", "Gloss_Torus"])
sent = meshes_of(jobs)
maps = sent["Packed_Torus"]["surface"]["maps"]
r, m = studio.picture(maps["roughness"]).reshape(8, 8), studio.picture(maps["metalness"]).reshape(8, 8)
check("roughness is taken from the green channel and metal from the blue one of a packed picture", int(r[0, 0]) == 64 and abs(int(r[0, 7]) - 230) <= 1
      and (int(m[0, 0]), int(m[7, 0])) == (255, 0) and "color" not in maps, (r[0, 0], r[0, 7], m[0, 0], m[7, 0], maps))
check("a plain colour stays the part's colour next to its maps", sent["Packed_Torus"]["color"] == [roblox_surface.srgb(0.8), roblox_surface.srgb(0.1), roblox_surface.srgb(0.1)],
      sent["Packed_Torus"]["color"])
# (1 - 0.6) * 0.5 = 0.2 all over: one grey, so it travels as a few pixels
check("arithmetic between the picture and the socket is done to the pixels", sent["Gloss_Torus"]["surface"]["maps"] == {"roughness": "flat:51"},
      sent["Gloss_Torus"]["surface"]["maps"])
check("nothing was baked: pictures travelled as pictures", "baked" not in report, report.get("baked"))

# -- 5. the Normal Map node's strength --------------------------------------------------------------
soft, nodes, links, shader = material("SoftBumps")
bump = nodes.new("ShaderNodeNormalMap")
bump.inputs["Strength"].default_value = 0.5
links.new(picture_node(nodes, bpy.data.images["T_Normal"]).outputs["Color"], bump.inputs["Color"])
links.new(bump.outputs["Normal"], shader.inputs["Normal"])
shape("Soft_Torus", soft)
report, jobs, _asked = send(["Soft_Torus"])
sent = studio.picture(meshes_of(jobs)["Soft_Torus"]["surface"]["maps"]["normal"])
wanted = np.array([0.3, 0.0, 0.8]) / np.linalg.norm([0.3, 0.0, 0.8])     # half way from flat (0, 0, 1) to (0.6, 0, 0.6)
check("half strength leans the normals half as far", abs(int(sent[0, 0, 0]) - round((wanted[0] * 0.5 + 0.5) * 255)) <= 2
      and abs(int(sent[0, 0, 2]) - round((wanted[2] * 0.5 + 0.5) * 255)) <= 2, (sent[0, 0].tolist(), wanted))

# -- 6. plain numbers: Roblox's stock materials where they do, a few grey pixels where they do not ---
gold, nodes, links, shader = material("Gold")
shader.inputs["Base Color"].default_value = (1.0, 0.72, 0.25, 1.0)
shader.inputs["Metallic"].default_value, shader.inputs["Roughness"].default_value = 1.0, 0.18
shape("Gold_Torus", gold)
shape("Gold_Torus_2", gold)
shape("Gold_Ball", gold, "ball")
shape("Gold_Gem", gold, "no uvs")
matt, nodes, links, shader = material("Matt")
shader.inputs["Base Color"].default_value = (0.2, 0.6, 0.2, 1.0)
shader.inputs["Roughness"].default_value = 0.7
shape("Matt_Torus", matt)
report, jobs, _asked = send(["Gold_Torus", "Gold_Torus_2", "Gold_Ball", "Gold_Gem", "Matt_Torus"])
sent, native = meshes_of(jobs), parts_of(jobs)
check("polished metal on a mesh: its roughness and metal travel as two tiny pictures", sent["Gold_Torus"]["surface"]["maps"] == {"roughness": "flat:46", "metalness": "flat:255"}
      and sent["Gold_Torus"]["material"] == "Metal" and sent["Gold_Torus"]["color"] == [1.0, roblox_surface.srgb(0.72), roblox_surface.srgb(0.25)],
      sent["Gold_Torus"].get("surface"))
check("the same pictures are named again, not sent again", pictures_in(jobs) == ["flat:46", "flat:255"]
      and sent["Gold_Torus_2"]["surface"]["maps"] == sent["Gold_Torus"]["surface"]["maps"] and "first" not in sent["Gold_Torus_2"]["surface"]
      and sent["Gold_Torus"]["surface"].get("first") == 2, (pictures_in(jobs), sent["Gold_Torus_2"]["surface"]))
check("a ball stays a native Part with Roblox's own Metal: a Part has no SurfaceAppearance", native.get("Gold_Ball", {}).get("material") == "Metal"
      and "surface" not in native.get("Gold_Ball", {}), native.get("Gold_Ball"))
check("a mesh without UVs still gets the pictures that are one grey all over", "uv" not in sent["Gold_Gem"]
      and sent["Gold_Gem"]["surface"]["maps"] == {"roughness": "flat:46", "metalness": "flat:255"}, sent["Gold_Gem"].get("surface"))
check("a matt plain colour needs no pictures at all: Roblox's Plastic shows it", "surface" not in sent["Matt_Torus"] and sent["Matt_Torus"]["material"] == "Plastic",
      sent["Matt_Torus"].get("surface"))

# -- 7. see-through: cut out, soft, and all over ----------------------------------------------------
leafy = filled(32, 32, (0.1, 0.5, 0.1, 1.0))
leafy[:, :16, 3] = 0.0
leaf, nodes, links, shader = material("Leaf")
node = picture_node(nodes, image("T_Leaf", leafy))
links.new(node.outputs["Color"], shader.inputs["Base Color"])
links.new(node.outputs["Alpha"], shader.inputs["Alpha"])
shape("Leaf_Torus", leaf)
fading = filled(32, 32, (0.9, 0.5, 0.1, 1.0))
fading[..., 3] = np.linspace(0, 1, 32, dtype=np.float32)[None, :]
film, nodes, links, shader = material("Film")
node = picture_node(nodes, image("T_Film", fading))
links.new(node.outputs["Color"], shader.inputs["Base Color"])
links.new(node.outputs["Alpha"], shader.inputs["Alpha"])
shape("Film_Torus", film)
clear, nodes, links, shader = material("Clear")
shader.inputs["Alpha"].default_value = 0.4
shape("Clear_Torus", clear)
glass, nodes, links, shader = material("Glass")
shader.inputs["Transmission Weight"].default_value = 1.0
shader.inputs["Roughness"].default_value = 0.0
shape("Glass_Torus", glass)
report, jobs, _asked = send(["Leaf_Torus", "Film_Torus", "Clear_Torus", "Glass_Torus"])
sent = meshes_of(jobs)
cut = studio.picture(sent["Leaf_Torus"]["surface"]["maps"]["color"])
check("a picture's alpha travels in the colour map and cuts the surface out", sent["Leaf_Torus"]["surface"].get("alpha") == "Transparency"
      and cut[0, 0, 3] == 0 and cut[0, 31, 3] == 255 and sent["Leaf_Torus"]["transparency"] == 0.0, (sent["Leaf_Torus"]["surface"], cut[0, ::31, 3]))
check("soft alpha is blended instead of cut: the part gets the least transparency that makes Roblox blend",
      sent["Film_Torus"]["surface"].get("alpha") == "Transparency" and sent["Film_Torus"]["transparency"] == roblox_surface.SOFT_ALPHA, sent["Film_Torus"]["transparency"])
check("an alpha that is one number is the part's transparency", sent["Clear_Torus"]["transparency"] == 0.6 and "surface" not in sent["Clear_Torus"],
      (sent["Clear_Torus"]["transparency"], sent["Clear_Torus"].get("surface")))
check("clear glass stays Roblox's Glass, with no pictures in the way", sent["Glass_Torus"]["material"] == "Glass" and sent["Glass_Torus"]["transparency"] == 0.5
      and "surface" not in sent["Glass_Torus"], sent["Glass_Torus"].get("surface"))

# -- 8. light given off -----------------------------------------------------------------------------
lava, nodes, links, shader = material("Lava")
shader.inputs["Emission Color"].default_value = (1.0, 0.35, 0.05, 1.0)
shader.inputs["Emission Strength"].default_value = 4.0
shape("Lava_Torus", lava)
sign = filled(16, 16, (0.0, 0.0, 0.0, 1.0))
sign[:, :8, :3] = (0.0, 0.9, 1.0)                                       # the left half glows cyan
screen, nodes, links, shader = material("Screen")
shader.inputs["Base Color"].default_value = (0.03, 0.03, 0.04, 1.0)
links.new(picture_node(nodes, image("T_Sign", sign)).outputs["Color"], shader.inputs["Emission Color"])
shader.inputs["Emission Strength"].default_value = 3.0
shape("Screen_Torus", screen)
report, jobs, _asked = send(["Lava_Torus", "Screen_Torus"])
sent = meshes_of(jobs)
check("a strong plain glow is Roblox's Neon in the light's colour", sent["Lava_Torus"]["material"] == "Neon" and "surface" not in sent["Lava_Torus"]
      and sent["Lava_Torus"]["color"] == [1.0, roblox_surface.srgb(0.35), roblox_surface.srgb(0.05)], sent["Lava_Torus"])
surface = sent["Screen_Torus"]["surface"]
mask, albedo = studio.picture(surface["maps"]["emissive"]).reshape(16, 16), studio.picture(surface["maps"]["color"])
lit = roblox_surface._decode(np.array([0.0, 0.9, 1.0]))                 # the picture is sRGB: the shader sees this
check("a picture that glows becomes a mask, and the light's own colour goes into the colour map (Roblox multiplies the two)",
      int(mask[0, 0]) == 255 and int(mask[0, 15]) == 0 and albedo[0, 0, 0] == 0 and albedo[0, 0, 2] == 255
      and abs(int(albedo[0, 0, 1]) - 230) <= 1 and albedo[0, 15].tolist()[:3] == [round(roblox_surface.srgb(0.03) * 255), round(roblox_surface.srgb(0.03) * 255), round(roblox_surface.srgb(0.04) * 255)],
      (mask[0, ::15], albedo[0, 0].tolist(), albedo[0, 15].tolist()))
check("one unit of strength is one unit of light: mask x strength x colour is the light that was asked for",
      abs(surface["emissive_strength"] * 1.0 * 1.0 - 3.0 * float(lit.max())) < 0.02, surface["emissive_strength"])
check("the report says the glow was approximated, and how", any("multiplies" in note for note in report.get("approximated", [])), report.get("approximated"))

# -- 9. a tint Roblox can apply itself, a tint it cannot, and tiling --------------------------------
planks = image("T_Planks", filled(8, 8, (0.5, 0.5, 0.5, 1.0)))
for name, blend, constant in (("Tinted", 'MULTIPLY', (0.0, 0.5, 1.0, 1.0)), ("Washed", 'MIX', (1.0, 1.0, 1.0, 1.0))):
    tinted, nodes, links, shader = material(name)
    node = picture_node(nodes, planks)
    mapping, coords = nodes.new("ShaderNodeMapping"), nodes.new("ShaderNodeTexCoord")
    mapping.inputs["Scale"].default_value = (3.0, 3.0, 3.0)
    links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    mix = nodes.new("ShaderNodeMix")
    mix.data_type, mix.blend_type = 'RGBA', blend
    mix.inputs["Factor"].default_value = 1.0 if blend == 'MULTIPLY' else 0.5
    links.new(node.outputs["Color"], mix.inputs["A"])
    mix.inputs["B"].default_value = constant
    links.new(mix.outputs["Result"], shader.inputs["Base Color"])
    shape(name + "_Torus", tinted)
report, jobs, _asked = send(["Tinted_Torus", "Washed_Torus"])
sent = meshes_of(jobs)
surface = sent["Tinted_Torus"]["surface"]
check("a plain multiply tint travels as the surface's tint and the picture stays as it is", surface.get("tint") == [0.0, roblox_surface.srgb(0.5), 1.0]
      and abs(int(studio.picture(surface["maps"]["color"])[0, 0, 0]) - 128) <= 1, (surface.get("tint"), studio.picture(surface["maps"]["color"])[0, 0]))
washed = studio.picture(sent["Washed_Torus"]["surface"]["maps"]["color"])[0, 0]
# half the picture (linear 0.214) and half white: linear 0.607, which is sRGB 204
check("a tint Roblox has no setting for is put into the pixels, in linear light", "tint" not in sent["Washed_Torus"]["surface"] and abs(int(washed[0]) - 204) <= 2,
      (sent["Washed_Torus"]["surface"].get("tint"), washed))
us = sent["Tinted_Torus"]["uv"][0::2]
check("the Mapping node's tiling goes into the UVs of a surface too", 2.5 < max(us) - min(us) <= 3.01, (min(us), max(us)))

# -- 10. a look made by shader nodes is baked, every part of it that varies --------------------------
rock, nodes, links, shader = material("Rock")
noise = nodes.new("ShaderNodeTexNoise")
noise.inputs["Scale"].default_value = 4.0
ramp = nodes.new("ShaderNodeValToRGB")
ramp.color_ramp.elements[0].color, ramp.color_ramp.elements[1].color = (0.6, 0.1, 0.05, 1.0), (0.1, 0.3, 0.8, 1.0)
links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
links.new(ramp.outputs["Color"], shader.inputs["Base Color"])
links.new(noise.outputs["Fac"], shader.inputs["Roughness"])
cells = nodes.new("ShaderNodeTexVoronoi")
cells.inputs["Scale"].default_value = 9.0
bumps = nodes.new("ShaderNodeBump")
bumps.inputs["Distance"].default_value = 0.03
links.new(cells.outputs["Distance"], bumps.inputs["Height"])
links.new(bumps.outputs["Normal"], shader.inputs["Normal"])
bpy.ops.mesh.primitive_monkey_add(size=1.0, location=(0, 6, 1))
monkey = bpy.context.active_object
monkey.name = "Rock"
for polygon in monkey.data.polygons:
    polygon.use_smooth = True
monkey.data.materials.append(rock)
boxed, nodes, links, shader = material("Boxed")
node = picture_node(nodes, bpy.data.images["T_Colour"])
node.projection = 'BOX'
coords = nodes.new("ShaderNodeTexCoord")
links.new(coords.outputs["Object"], node.inputs["Vector"])
links.new(node.outputs["Color"], shader.inputs["Base Color"])
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(3, 6, 1))
crate = bpy.context.active_object
crate.name = "Crate"
crate.data.materials.append(boxed)
bpy.context.view_layer.update()
engine_before = bpy.context.scene.render.engine
check("noise in the colour, the roughness and the bumps is a look made by nodes; a box-projected picture is not",
      roblox_surface.made_by_nodes(monkey) and not roblox_surface.made_by_nodes(crate)
      and not roblox_surface.made_by_nodes(bpy.data.objects["Pictured_Ball"]))
report, jobs, _asked = send(["Rock", "Crate"])
sent = meshes_of(jobs)
maps = sent.get("Rock", {}).get("surface", {}).get("maps", {})
check("colour, roughness and bumps are each baked to a picture on fresh UVs", sorted(maps) == ["color", "normal", "roughness"]
      and all(studio.picture(key).shape[:2] == (roblox_surface.BAKE_PX, roblox_surface.BAKE_PX) for key in maps.values())
      and len(sent["Rock"].get("uv", [])) > 0, {role: studio.picture(key).shape for role, key in maps.items()})
bpy.ops.mesh.primitive_monkey_add(size=0.2, location=(6, 6, 1))
pebble = bpy.context.active_object
pebble.name = "Pebble"
pebble.data.materials.append(rock)
bpy.context.view_layer.update()
check("the baked pictures are sized by the object: a small prop gets small ones", roblox_surface.bake_px(monkey, monkey.data) == roblox_surface.BAKE_PX
      and roblox_surface.bake_px(pebble, pebble.data) == 256, (roblox_surface.bake_px(monkey, monkey.data), roblox_surface.bake_px(pebble, pebble.data)))
if len(maps) == 3:
    colour, gloss, normal = (studio.picture(maps[role]) for role in ("color", "roughness", "normal"))
    drawn = normal[..., 2] > 140                                         # texels a face covers: a normal map is never dark in blue there
    check("the baked colour and roughness vary the way the noise does", len(np.unique(colour[drawn][:, 0])) > 20 and len(np.unique(gloss.reshape(gloss.shape[:2])[drawn])) > 20,
          (len(np.unique(colour[drawn][:, 0])), len(np.unique(gloss))))
    check("the bumps arrive as a tangent-space normal map: blue on the whole, leaning where the bumps are",
          float(normal[drawn][:, 2].mean()) > 200 and float(normal[drawn][:, 0].std()) > 4.0 and abs(float(normal[drawn][:, 0].mean()) - 127.5) < 6.0,
          (normal[drawn][:, :3].mean(axis=0), normal[drawn][:, 0].std()))
crate_uv = np.array(sent.get("Crate", {}).get("uv", [0.0, 0.0])).reshape(-1, 2)
check("a picture laid on by box projection travels as itself, on UVs that project it the way Blender does",
      "color" in sent.get("Crate", {}).get("surface", {}).get("maps", {}) and studio.picture(sent["Crate"]["surface"]["maps"]["color"]).shape[:2]
      == tuple(bpy.data.images["T_Colour"].size[:])[::-1] and np.ptp(crate_uv, axis=0).min() > 0.9
      and not parts_of(jobs) and "plain_colours" not in report, (sent.get("Crate", {}).get("surface"), np.ptp(crate_uv, axis=0)))
check("the report says what was baked", "1 objects had a look made by shader nodes" in report.get("baked", "") and "bumps" in report["baked"], report.get("baked"))
check("the bake leaves the scene as it was", "_unix_bake" not in bpy.data.objects and not any(i.name.startswith("Baked_") for i in bpy.data.images)
      and [m.name for m in monkey.data.materials] == ["Rock"] and "UnixBake" not in monkey.data.uv_layers
      and bpy.context.scene.render.engine == engine_before and rock.node_tree.nodes["Principled BSDF"].inputs["Roughness"].is_linked,
      ([o.name for o in bpy.data.objects if o.name.startswith("_")], bpy.context.scene.render.engine))
roblox_surface.bake_cache.finish()                                       # the first send's bakes are kept on disk now
began = time.time()
report, jobs, _asked = send(["Rock"])
again = meshes_of(jobs).get("Rock", {})
check("the unchanged look sent again uses the bake kept from the send before: the very same pictures and UVs",
      "used its bake" in report.get("baked", "") and again.get("surface", {}).get("maps") == maps
      and again.get("uv") == sent["Rock"].get("uv"), (report.get("baked"), again.get("surface", {}).get("maps"), maps))
print(f"   (sent again in {time.time() - began:.1f} s)")
noise.inputs["Scale"].default_value = 7.0
report, jobs, _asked = send(["Rock"])
changed = meshes_of(jobs).get("Rock", {}).get("surface", {}).get("maps", {})
check("after the material changes it is baked afresh", "used its bake" not in report.get("baked", "")
      and changed.get("color") not in (None, maps.get("color")), (report.get("baked"), changed))
noise.inputs["Scale"].default_value = 4.0
split, nodes, links, shader = material("TwoLayouts")                      # the colour tiled twice, the roughness four times: one set of UVs cannot do both
for socket, picture, tiles in (("Base Color", bpy.data.images["T_Colour"], 2.0), ("Roughness", bpy.data.images["T_Rough"], 4.0)):
    node, mapping, coords = picture_node(nodes, picture), nodes.new("ShaderNodeMapping"), nodes.new("ShaderNodeTexCoord")
    mapping.inputs["Scale"].default_value = (tiles, tiles, tiles)
    links.new(coords.outputs["UV"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], node.inputs["Vector"])
    links.new(node.outputs["Color"], shader.inputs[socket])
check("pictures laid on in two different ways are baked into one layout", roblox_surface.made_by_nodes(shape("TwoLayouts_Torus", split)))

# -- 11. the upload: asked for with the pictures counted, and reported -----------------------------
expected = roblox_surface.expected_pictures([bpy.data.objects[n] for n in ("Pictured_Ball", "Gold_Torus", "Gold_Torus_2", "Matt_Torus")])
check("the pictures a send needs are counted from the materials before anything is read", expected == {"color": 1, "normal": 1, "roughness": 2, "metalness": 2, "emissive": 0},
      expected)
report, jobs, asked = send(["Pictured_Ball", "Gold_Torus", "Gold_Torus_2", "Matt_Torus"], asset="Asked", keep="ask", answer=roblox.KEEP_ONCE)
check("the question says how many pictures would become image assets, and what for", asked is not None and "up to 6 pictures" in asked
      and "1 colour, 1 normal, 2 roughness, 2 metalness" in asked and "image assets" in asked and "mesh assets" in asked, asked)
check("nothing leaves before the answer, not even a picture, and after a yes everything is uploaded", posted_first[0] == 0
      and all(job.get("upload") is True for job in jobs if job.get("kind") != "picture") and len(pictures_in(jobs)) == 6,
      (posted_first[0], [(job.get("kind"), job.get("upload")) for job in jobs]))
check("the report says how many pictures are image assets on the account now", "Image assets on the user's Roblox account hold 6 of them (6 uploaded now, 0 already there)"
      in report.get("textures", "") and "preview_only" not in report, report.get("textures"))
report, jobs, asked = send(["Pictured_Ball", "Gold_Torus"], asset="Asked", keep="ask", answer=roblox.KEEP_NO)
check("after a no nothing is uploaded and the report says the pictures are a preview too", not any(job.get("upload") for job in jobs)
      and "chose not to upload" in report.get("preview_only", "") and "6 pictures" in report["preview_only"], report.get("preview_only"))
weights = roblox.Sender([bpy.data.objects["Pictured_Ball"]], "W", session, False, "upload")
plain_weight = weights._weight("meshes", {"v": [0]}, 100)
check("a mesh that brings new pictures counts for more in an upload job: each picture is an upload of its own",
      weights._weight("meshes", {"v": [0], "surface": {"first": 4}}, 100) == plain_weight + 4 * (roblox.BATCH_TRIANGLES // roblox.UPLOADS_PER_JOB), plain_weight)

# -- 12. an older plugin gets what it understands ---------------------------------------------------
old = FakeStudio("Old Place", "12")
old.start()
deadline = time.time() + 5
while old.session not in link._sessions and time.time() < deadline:
    time.sleep(0.05)
old_session = {"session": old.session, "place": "Old Place", "version": "12", "testing": False}
report, jobs, _asked = send(["Pictured_Ball", "Gold_Torus"], asset="Old", to=old, at=old_session)
sent = meshes_of(jobs)
texture = sent.get("Pictured_Ball", {}).get("texture") or {}
check("a plugin before version 13 gets one colour picture inside the mesh's job and no surface", not pictures_in(jobs) and "surface" not in sent["Pictured_Ball"]
      and "surface" not in sent["Gold_Torus"] and texture.get("key", "").startswith("T_Colour:")
      and list(base64.b64decode(texture.get("data", ""))[:4]) == [200, 0, 0, 255] and sent["Gold_Torus"]["material"] == "Metal", (sorted(sent["Pictured_Ball"]), texture.get("key")))
check("and the report says only colour arrived, and why", "older Unix Bridge" in report.get("textures", ""), report.get("textures"))
old.alive = False

# -- 13. the plugin and the sender agree on names -----------------------------------------------------
with open(roblox_link._SOURCE, encoding="utf-8") as fh:
    plugin = fh.read()
check("the plugin knows every kind of picture the sender names", all(f"{role} = \"" in plugin for role in roblox_surface.ROLES)
      and "surfaces.apply" in plugin and "job.c ~= 1" in plugin, [role for role in roblox_surface.ROLES if f"{role} = \"" not in plugin])

# -- 14. a scene laid out in studs (units None) arrives the size it has here -------------------------------
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(40, 0, 1))
bpy.context.active_object.name = "StudCube"
bpy.context.scene.unit_settings.system = 'NONE'
check("units None: one Blender unit is one stud", roblox.studs_per_unit() == 1.0)
report, jobs, _asked = send(["StudCube"], asset="Studs")
stud_cube = parts_of(jobs).get("StudCube") or meshes_of(jobs).get("StudCube") or {}
check("so a 2-unit cube arrives 2 studs wide, not 7 (the sender took every scene for metres)",
      stud_cube and all(abs(v - 2.0) < 1e-3 for v in stud_cube.get("size", [0, 0, 0])), stud_cube)
bpy.context.scene.unit_settings.system = 'METRIC'
check("metric: one metre is 1/0.28 studs, as before", abs(roblox.studs_per_unit() - 1 / 0.28) < 1e-9)

# -- 15. sending again: what the place already holds does not travel again ---------------------------------
stripes = filled(16, 16, (0.1, 0.6, 0.2, 1.0))
stripes[::2, :, 0] = 0.9                                              # pixels no earlier send carried
again_mat, nodes, links, shader = material("AgainMat")
links.new(picture_node(nodes, image("T_Again", stripes)).outputs["Color"], shader.inputs["Base Color"])
bpy.ops.mesh.primitive_cone_add(vertices=7, location=(50, 0, 1))     # a shape no earlier send carried
again = bpy.context.active_object
again.name = "AgainBall"
again.data.materials.append(again_mat)
report, first_jobs, _ = send(["AgainBall"], asset="Again", keep="upload")
first_bands = len(pictures_in(first_jobs))
first_geometry = sum(1 for m in meshes_of(first_jobs).values() if "v" in m)
report, again_jobs, _ = send(["AgainBall"], asset="Again", keep="upload")
check("a second send of the same thing carries no geometry and no picture Studio already holds",
      first_geometry > 0 and not pictures_in(again_jobs) and not any("v" in m for m in meshes_of(again_jobs).values())
      and "not_sent_again" in report, (first_bands, first_geometry, len(again_jobs), report.get("not_sent_again")))
check("and the place still gets every mesh", meshes_of(again_jobs).keys() == meshes_of(first_jobs).keys(),
      (list(meshes_of(again_jobs)), list(meshes_of(first_jobs))))

# -- 16. a mesh wider than a Roblox part may be: cut into pieces that each fit ------------------------------
bpy.context.scene.unit_settings.system = 'NONE'                     # laid out in studs, as the Lasso Ranch world is
bpy.ops.mesh.primitive_grid_add(x_subdivisions=60, y_subdivisions=40, size=1, location=(0, 0, -5))
ground = bpy.context.active_object
ground.name = "WideGround"
ground.scale = (5000, 3000, 1)
ground.data.materials.append(again_mat)
bpy.context.view_layer.update()
report, wide_jobs, _ = send(["WideGround"], asset="Wide")
wide = [m for m in meshes_of(wide_jobs).values() if m["name"].startswith("WideGround")]
check("a mesh 5,000 studs wide arrives in pieces no wider than 2,048 studs",
      len(wide) > 1 and all(max(m["size"]) <= roblox.MAX_PART_STUDS for m in wide), [m["size"] for m in wide])
carried = {m["key"]: len(m["t"]) // 3 for m in wide if "t" in m}      # copies of one shape carry it once
landed = sum(carried.get(m["key"], 0) for m in wide)
check("and no triangle is lost on the way", landed == 2 * len(ground.data.polygons),
      (landed, 2 * len(ground.data.polygons), len(wide), len(carried)))

# -- 17. collection instances: the objects they show travel, each where its instance puts it -----------------
kit = bpy.data.collections.new("FarmKit")
bpy.context.scene.collection.children.link(kit)
for name, kind, at in (("KitBarn", "torus", (1, 0, 0.5)), ("KitCrate", "cube", (-1, 0, 0.5))):
    if kind == "torus":
        bpy.ops.mesh.primitive_torus_add(major_radius=1.0, minor_radius=0.3, location=at)
    else:
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=at)
    piece = bpy.context.active_object
    piece.name = name
    for owner in list(piece.users_collection):
        owner.objects.unlink(piece)
    kit.objects.link(piece)
bpy.context.view_layer.layer_collection.children["FarmKit"].exclude = True      # the kit itself is not in the scene
for name, at, turn in (("Farm_A", (100, 40, 0), 0.0), ("Farm_B", (-60, 10, 2), math.radians(90))):
    farm = bpy.data.objects.new(name, None)
    farm.instance_type, farm.instance_collection = 'COLLECTION', kit
    farm.location, farm.rotation_euler[2] = at, turn
    bpy.context.scene.collection.objects.link(farm)
bpy.context.view_layer.update()
report, farm_jobs, _ = send(["Farm_A", "Farm_B"], asset="Farms")
farm_meshes, farm_parts = meshes_of(farm_jobs), parts_of(farm_jobs)
arrived = set(farm_meshes) | set(farm_parts)
check("the objects of two collection instances arrive, each named after its instance",
      {"Farm_A_KitBarn", "Farm_B_KitBarn", "Farm_A_KitCrate", "Farm_B_KitCrate"} <= arrived, sorted(arrived))
real = bpy.data.objects["KitBarn"].copy()                                  # a real copy where Farm_B shows its barn
real.name = "RealBarn"
bpy.context.scene.collection.objects.link(real)
real.matrix_world = bpy.data.objects["Farm_B"].matrix_world @ bpy.data.objects["KitBarn"].matrix_world
bpy.context.view_layer.update()
report, real_jobs, _ = send(["RealBarn"], asset="RealBarn")
mine, theirs = farm_meshes.get("Farm_B_KitBarn", {}), meshes_of(real_jobs).get("RealBarn", {})
level = [1, 0, 0, 0, 1, 0, 0, 0, 1]
check("and stand where the instance puts them, turned with it, as a real copy there would",
      bool(mine) and bool(theirs) and np.allclose(mine["center"], theirs["center"], atol=0.01)
      and np.allclose(mine.get("rot", level), theirs.get("rot", level), atol=1e-4),
      (mine.get("center"), theirs.get("center"), mine.get("rot"), theirs.get("rot")))
check("both instances' barns are one shape", farm_meshes.get("Farm_A_KitBarn", {}).get("key") == mine.get("key") is not None,
      (farm_meshes.get("Farm_A_KitBarn", {}).get("key"), mine.get("key")))

studio.alive = False
link.stop()
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
