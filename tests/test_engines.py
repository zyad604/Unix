# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for the game-engine bridge, using stand-in project folders in a temp dir.
Run: blender -b --factory-startup --python tests/test_engines.py"""
import json, os, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix import tools
from unix.tools import _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")  # keep test data out of the user's config
tools.load_all()
from unix.tools import engines  # noqa: E402
from unix.bridge import drop_tile  # noqa: E402
from unix.bridge import unreal_link  # noqa: E402
unreal_link.GROUP = ("239.0.0.1", 6798)    # not Unreal's 6766: an editor the user has open never hears these tests
fails = []


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"]


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:500]}"))
    if not cond:
        fails.append(label)


# -- what is really on this machine (informational) --------------------------------
real = engines.find_targets(refresh=True)
print("DETECTED", [(t["engine"], t["name"], "running" if t["running"] else "known") for t in real])
check("detection returns a Roblox target at least", any(t["engine"] == "roblox" for t in real), real)

# -- stand-in projects ---------------------------------------------------------------
tmp = tempfile.mkdtemp()
unity = os.path.join(tmp, "MyUnityGame"); os.makedirs(os.path.join(unity, "Assets"))
unreal = os.path.join(tmp, "MyUnrealGame"); os.makedirs(os.path.join(unreal, "Content"))
open(os.path.join(unreal, "MyUnrealGame.uproject"), "w").write("{}")
godot = os.path.join(tmp, "my-godot-game"); os.makedirs(godot)
open(os.path.join(godot, "project.godot"), "w").write("config_version=5\n")
engines._running_editors = lambda: [("Unity.exe", f'"C:\\Unity\\Unity.exe" -projectpath "{unity}" -useHub'),
                                    ("UnrealEditor.exe", f'"C:\\UE\\UnrealEditor.exe" "{os.path.join(unreal, "MyUnrealGame.uproject")}"'),
                                    ("RobloxStudioBeta.exe", '"C:\\Roblox\\RobloxStudioBeta.exe"')]
engines._recent_projects = lambda: [("godot", godot.replace("\\", "/")), ("unity", os.path.join(tmp, "missing"))]
engines._saved = lambda: {}
targets = engines.find_targets(refresh=True)
summary = {(t["engine"], t["name"], t["running"]) for t in targets}
check("finds running Unity and Unreal projects from command lines",
      ("unity", "MyUnityGame", True) in summary and ("unreal", "MyUnrealGame", True) in summary, summary)
check("finds the recent Godot project and Roblox Studio", ("godot", "my-godot-game", False) in summary
      and ("roblox", "open place", True) in summary, summary)
check("skips projects that no longer exist", not any(t["name"] == "missing" for t in targets))
err, text = call("engine_targets")
check("engine_targets tool", not err and len(json.loads(text)["targets"]) == 4, text)

# -- an asset: a root empty with two mesh children, placed away from the origin ------
root = bpy.data.objects.new("Chest Root", None)
bpy.context.scene.collection.objects.link(root)
root.location = (4.0, 3.0, 0.0)
for name, z in (("Chest_Body", 0.25), ("Chest_Lid", 0.6)):
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(4.0, 3.0, z))
    part = bpy.context.active_object
    part.name = name
    part.parent = root
    part.matrix_parent_inverse = root.matrix_world.inverted()
bpy.context.view_layer.update()
cube = bpy.data.objects["Cube"]
for o in bpy.context.view_layer.objects:
    o.select_set(False)
cube.select_set(True)
bpy.context.view_layer.objects.active = cube
before = {o.name: tuple(round(v, 5) for v in o.matrix_world.translation) for o in bpy.data.objects}

err, text = call("send_to_engine", engine="unity", objects=["Chest Root"])
d = json.loads(text) if not err else {}
dest = os.path.join(unity, "Assets", "Unix", "Chest_Root.fbx")
check("unity: FBX lands in Assets/Unix under the asset's name", not err and os.path.isfile(dest)
      and d["in_engine"] == "Assets/Unix/Chest_Root.fbx" and d["editor_running"] and not d["updated_existing"], text)
check("unity preset", not err and d["preset"]["apply_scale_options"] == "FBX_SCALE_UNITS" and d["preset"]["bake_space_transform"], text)
after = {o.name: tuple(round(v, 5) for v in o.matrix_world.translation) for o in bpy.data.objects}
check("scene placement is restored after a pivot-at-origin export", before == after, (before, after))
check("selection and active object are restored", cube.select_get() and bpy.context.view_layer.objects.active == cube
      and not bpy.data.objects["Chest_Body"].select_get())
err, text = call("send_to_engine", engine="unity", objects=["Chest Root"])
check("sending again updates the same file in place", not err and json.loads(text)["updated_existing"]
      and len(os.listdir(os.path.join(unity, "Assets", "Unix"))) == 1, text)

err, text = call("send_to_engine", engine="unreal", objects=["Chest Root"], name="Treasure Chest")
d = json.loads(text) if not err else {}
check("unreal: FBX in Content/Unix with a safe name", not err
      and os.path.isfile(os.path.join(unreal, "Content", "Unix", "Treasure_Chest.fbx"))
      and d["in_engine"] == "/Game/Unix/Treasure_Chest" and d["preset"]["apply_scale_options"] == "FBX_SCALE_NONE", text)

err, text = call("send_to_engine", engine="godot", objects=["Chest Root"])
d = json.loads(text) if not err else {}
check("godot: GLB in res://unix that really holds two meshes", not err
      and os.path.isfile(os.path.join(godot, "unix", "Chest_Root.glb")) and d["file_contains"]["meshes"] == 2
      and not d["editor_running"], text)

err, text = call("send_to_engine", engine="roblox", objects=["Chest Root"])
d = json.loads(text) if not err else {}
check("roblox: FBX in the exports folder, tile status reported", not err and d["file"].endswith(os.path.join("roblox", "Chest_Root.fbx"))
      and os.path.isfile(d["file"]) and "drag_tile" in d and "Import 3D" in d["next"], text)

err, text = call("send_to_engine", engine="godot")
check("no objects named: sends the selection", not err and json.loads(text)["objects"] == ["Cube"], text)
cube.select_set(False)
err, text = call("send_to_engine", engine="godot")
check("nothing selected is a clear error", err and "Select the objects" in text, text)
err, text = call("send_to_engine", engine="godot", objects=["Camera"])
check("no mesh is a clear error", err and "no mesh" in text, text)
err, text = call("send_to_engine", engine="unity", objects=["Cube"], project=tmp)
check("a folder that is not a project is refused", err and "not a Unity project" in text, text)
engines._running_editors = lambda: []
engines._recent_projects = lambda: []
engines.find_targets(refresh=True)
err, text = call("send_to_engine", engine="unreal", objects=["Cube"])
check("no project found tells the user what to do", err and "Open the project in Unreal" in text, text)
err, text = call("send_to_engine", engine="unreal", objects=["Cube"], project=os.path.join(unreal, "MyUnrealGame.uproject"))
check("an explicit .uproject path works", not err and os.path.isfile(os.path.join(unreal, "Content", "Unix", "Cube.fbx")), text)

# heavy mesh warning for Roblox
bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=96)
bpy.context.active_object.name = "Dense_Sphere"
err, text = call("send_to_engine", engine="roblox", objects=["Dense_Sphere"])
check("roblox triangle limit warning", not err and any("triangles" in w for w in json.loads(text).get("warnings", [])), text)

# -- Roblox live link, with stand-ins for the Studio plugin ---------------------------------
import math, threading, time, urllib.request
from unix.bridge import roblox_link
link = roblox_link.link
err, text = call("send_to_engine", engine="roblox", objects=["Dense_Sphere"])
check("without a linked Studio the send falls back to a file and says why",
      not err and json.loads(text)["file"].endswith(".fbx") and json.loads(text).get("live_link"), text[:300])
check("the link server starts on a loopback port", link.start(ports=(0,)) and link.port > 0, link.port)   # private port
check("a port one Unix owns is never shared with a second one", roblox_link.RobloxLink().start(ports=(link.port,)) is False)


class FakeStudio(threading.Thread):
    """Does what the plugin does on the wire: ask for work, report what it 'built'."""

    def __init__(self, place):
        super().__init__(daemon=True)
        self.place, self.session, self.jobs, self.alive = place, place + "-session", [], True

    def post(self, path, body):
        request = urllib.request.Request(f"http://127.0.0.1:{link.port}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Unix-Plugin": "test"})
        return json.loads(urllib.request.urlopen(request, timeout=30).read())

    def run(self):
        while self.alive:
            job = self.post("/unix/poll", {"session": self.session, "place": self.place, "placeId": 1, "gameId": 1,
                                            "version": "test"}).get("job")
            if job and job.get("kind") in ("tree", "query", "remove"):
                self.jobs.append(job)
                report = {"tree": {"created": 3, "notes": ["Orb.Shap: Shap is not a valid member of Part"],
                                   "model": "Workspace." + str(job.get("asset")), "origin": [0, 0, 0], "replaced": False},
                          "query": {"place": self.place, "tree": {"name": "Workspace", "class": "Workspace", "childCount": 2}},
                          "remove": {"removed": job.get("path"), "place": self.place}}[job["kind"]]
                ok = job.get("path") != "Workspace.Missing"
                self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": ok, "report": report,
                                            "message": "" if ok else "nothing at Workspace.Missing"})
            elif job and job.get("kind") == "picture":
                self.jobs.append(job)
                self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": True,
                                            "report": {"picture": job["key"], "rows": job["rows"], "notes": []}})
            elif job:
                self.jobs.append(job)
                meshes = job.get("meshes") or []
                keep = bool(job.get("upload"))        # with the upload agreed, every mesh ends up on an asset
                self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": True, "report": {
                    "parts": len(job.get("parts") or []), "meshes": len(meshes),
                    "effects": len(job.get("effects") or []), "notes": [], "model": "Workspace." + job["asset"],
                    "uploaded": sum(1 for m in meshes if "v" in m) if keep else 0, "reused": 0,
                    "kept": len(meshes) if keep else 0, "preview": 0 if keep else len(meshes)}})


def wait_for(count):
    deadline = time.time() + 5
    while len(link.sessions()) != count and time.time() < deadline:
        time.sleep(0.05)
    return len(link.sessions()) == count


try:
    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{link.port}/unix/poll", data=b"{}"), timeout=5)
    refused = False
except Exception:
    refused = True
check("the link refuses callers that are not the plugin", refused)

studio = FakeStudio("My Obby")
studio.start()
check("a Studio window links by itself and shows up as a target", wait_for(1)
      and [(t["name"], t.get("linked")) for t in engines.find_targets(refresh=True) if t["engine"] == "roblox"] == [("My Obby", True)],
      engines.find_targets(refresh=True))

S = 1 / 0.28
bpy.ops.mesh.primitive_cube_add(size=1.0, location=(10, 20, 3))
building = bpy.context.active_object
building.name = "Building"
building.scale = (2, 4, 6)
building.rotation_euler = (0, 0, math.radians(90))
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(12, 20, 0.5))
ball = bpy.context.active_object
ball.name = "Ball"
bpy.ops.mesh.primitive_monkey_add(size=1.0, location=(14, 20, 0.5))
statue = bpy.context.active_object
statue.name = "Statue"
red = bpy.data.materials.new("Red")
red.use_nodes = True
red.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (1.0, 0.0, 0.0, 1.0)
red.node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value = 1.0
statue.data.materials.append(red)
err, text = call("vfx_create", effect="fire", name="torch", quality="fast", location=[11, 20, 0], size_m=0.5, start_frame=1)
check("a fire to send exists", not err, text)
err, text = call("vfx_create", effect="magic_swirl", name="orb", location=[30, 30, 0], size_m=2.0, start_frame=1,
                 color=[0.2, 0.9, 1.0])
bpy.context.view_layer.update()


def parts_of(jobs):
    return [x for job in jobs for x in (job.get("parts") or [])]


def meshes_of(jobs):
    return [x for job in jobs for x in (job.get("meshes") or [])]


def near(a, b, tolerance=0.01):
    return all(abs(x - y) < tolerance for x, y in zip(a, b)) and len(a) == len(b)


def part_corners(part):
    """World corners of a Part or WedgePart as Roblox builds it from size and CFrame."""
    c = part["cframe"]
    sx, sy, sz = (v / 2 for v in part["size"])
    local = [(x, y, z) for x in (-sx, sx) for y, z in ((-sy, -sz), (-sy, sz), (sy, sz))]
    if part.get("shape") != "Wedge":
        local += [(x, sy, -sz) for x in (-sx, sx)]
    return [tuple(c[i] + c[3 + 3 * i] * x + c[4 + 3 * i] * y + c[5 + 3 * i] * z for i in range(3)) for x, y, z in local]


def roblox_points(obj):
    """An object's vertices where Roblox would have them when it is sent with at_origin=False."""
    return [(w.x * S, w.z * S, -w.y * S) for w in (obj.matrix_world @ v.co for v in obj.data.vertices)]


def covers(points, corners, tolerance=0.02):
    return all(min(math.dist(p, c) for c in corners) < tolerance for p in points)


err, text = call("send_to_engine", engine="roblox", objects=["Building", "Ball", "Statue", "VFX_torch"], name="Town")
d = json.loads(text) if not err else {}
check("roblox link: parts, meshes and effects are built in the open place", not err and d.get("place") == "My Obby"
      and d["built"] == {"parts": 2, "meshes": 1, "effects": 1} and "parts_note" in d, text)
check("a fire that was not uploaded is reported as a stand-in, not as the fire made here", "VFX_torch" in d.get("effects_not_as_made", "")
      and "NOT the effect made here" in d["effects_not_as_made"] and "effects_as_made" not in d, d.get("effects_not_as_made"))
check("a mesh that was not uploaded is reported as a preview that Play will not keep", "preview_only" in d
      and "checkered" in d["preview_only"] and "meshes_kept" not in d, d.get("preview_only"))
first = studio.jobs[0] if studio.jobs else {}
part = next((x for x in parts_of(studio.jobs) if x["name"] == "Building"), {})
check("small things travel together: one job, opened and closed", len(studio.jobs) == 1 and first.get("first") is True
      and first.get("last") is True and d.get("jobs") == 1,
      [(sorted(k for k in j if k in ("first", "last", "parts", "meshes", "effects"))) for j in studio.jobs])
check("a box goes as a native Part, in studs, Y up", first.get("asset") == "Town" and first.get("place") == "camera"
      and part.get("shape") == "Block" and near(part["size"], (4 * S, 6 * S, 2 * S)) and abs(part["cframe"][1] - 3 * S) < 0.01, part)
check("the Part stands upright on world axes whichever way the box was turned", near(part["cframe"][3:], (1, 0, 0, 0, 1, 0, 0, 0, 1)),
      part.get("cframe"))
round_part = next((x for x in parts_of(studio.jobs) if x["name"] == "Ball"), {})
check("a sphere goes as a native Ball", round_part.get("shape") == "Ball" and near(round_part["size"], (S, S, S), 0.02)
      and abs(round_part["cframe"][1] - 0.5 * S) < 0.01, round_part)
effect = (first.get("effects") or [{}])[0]
check("an effect goes as its preset, not as geometry", effect.get("effect") == "fire" and abs(effect["size"] - 0.5 * S) < 0.01
      and abs(effect["position"][1]) < 0.01, effect)
pieces = meshes_of(studio.jobs)
piece = pieces[0] if pieces else {}
names = [m["name"] for m in pieces]
check("only real meshes are sent as meshes (no effect internals)", names == ["Statue"], names)
vertices = len(piece.get("v", [])) // 3
check("the mesh arrives as indexed triangles with normals and UVs", vertices > 100 and len(piece["n"]) == vertices * 3
      and len(piece["uv"]) == vertices * 2 and len(piece["t"]) % 3 == 0 and max(piece["t"]) == vertices - 1
      and min(piece["t"]) == 0 and abs(max(piece["v"]) - max(piece["size"]) / 2) < 0.01 and len(piece.get("key", "")) == 16,
      (vertices, len(piece.get("t", [])), piece.get("size")))
check("material becomes Roblox colour and material", piece.get("material") == "Metal" and piece.get("color") == [1.0, 0.0, 0.0]
      and abs(piece["center"][1] - 0.5 * S) < 0.2, {k: piece.get(k) for k in ("material", "color", "center")})
check("nothing private or unserialisable travels", not any(k.startswith("_") for item in pieces + parts_of(studio.jobs) for k in item))

# -- shapes Roblox has natively, and the ones it does not ------------------------------------
shapes = bpy.data.collections.new("Shapes")
bpy.context.scene.collection.children.link(shapes)


def make(name, verts, faces, location=(0, 0, 0)):
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    obj.location = location
    shapes.objects.link(obj)
    return obj


def cone(name, x, y, z, r, h, n=8):
    verts = [(x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n), z) for i in range(n)] + [(x, y, z + h)]
    return make(name, verts, [(i, (i + 1) % n, n) for i in range(n)] + [tuple(range(n))[::-1]])


# a gable roof with its place written into the vertices and turned 30 degrees, as scripts build them
turn = math.radians(30)
gable = [(-2.3, -1.8, 0), (2.3, -1.8, 0), (2.3, 1.8, 0), (-2.3, 1.8, 0), (-2.3, 0, 1.6), (2.3, 0, 1.6)]
gable = [(50 + a * math.cos(turn) - b * math.sin(turn), 60 + a * math.sin(turn) + b * math.cos(turn), 2.5 + c) for a, b, c in gable]
roof = make("Roof", gable, [(0, 1, 2, 3), (0, 4, 5, 1), (2, 5, 4, 3), (0, 3, 4), (1, 5, 2)])
ramp = make("Ramp", [(0, 0, 0), (3, 0, 0), (3, 2, 0), (0, 2, 0), (0, 0, 1), (0, 2, 1)],
            [(0, 3, 2, 1), (0, 1, 4), (3, 5, 2), (1, 2, 5, 4), (0, 4, 5, 3)], location=(70, 0, 0))
slanted = [(a * math.cos(turn) - b * math.sin(turn), a * math.sin(turn) + b * math.cos(turn), c)
           for a in (-1, 1) for b in (-2, 2) for c in (0, 3)]
crate = make("Crate", slanted, [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)], location=(80, 0, 0))
bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=1.0, depth=3.0, location=(90, 0, 1), rotation=(math.radians(90), 0, 0))
log = bpy.context.active_object
log.name = "Log"
bpy.ops.mesh.primitive_cylinder_add(vertices=6, radius=0.3, depth=2.0, location=(95, 0, 1))
trunk = bpy.context.active_object
trunk.name = "Trunk"
bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=1.0, depth=1.0, end_fill_type='NOTHING', location=(100, 0, 1))
pipe = bpy.context.active_object
pipe.name = "Pipe"
bpy.ops.mesh.primitive_uv_sphere_add(radius=1.0, location=(105, 0, 1))
egg = bpy.context.active_object
egg.name = "Egg"
egg.scale = (1, 1, 1.6)
bpy.context.view_layer.update()
before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["Roof", "Ramp", "Crate", "Log", "Trunk", "Pipe", "Egg"], name="Shapes",
                 project="My Obby", at_origin=False)
d = json.loads(text) if not err else {}
sent = studio.jobs[before:]
by_name = {x["name"]: x for x in parts_of(sent)}
halves = [by_name.get("Roof"), by_name.get("Roof_2")]
check("a gable roof becomes two wedges that are exactly the roof", all(halves) and all(h["shape"] == "Wedge" for h in halves)
      and covers(roblox_points(roof), [c for h in halves for c in part_corners(h)])
      and all(near(sorted(h["size"]), sorted((4.6 * S, 1.6 * S, 1.8 * S)), 0.02) for h in halves), halves)
check("a ramp becomes one wedge", by_name.get("Ramp", {}).get("shape") == "Wedge" and "Ramp_2" not in by_name
      and covers(roblox_points(ramp), part_corners(by_name["Ramp"])) and covers(part_corners(by_name["Ramp"]), roblox_points(ramp)),
      by_name.get("Ramp"))
check("a box whose turn is written into its vertices is still a Part, turned", by_name.get("Crate", {}).get("shape") == "Block"
      and near(sorted(by_name["Crate"]["size"]), sorted((2 * S, 4 * S, 3 * S)), 0.02)
      and covers(roblox_points(crate), part_corners(by_name["Crate"])), by_name.get("Crate"))
check("a round cylinder becomes a Cylinder part along its own axis", by_name.get("Log", {}).get("shape") == "Cylinder"
      and near(by_name["Log"]["size"], (3 * S, 2 * S, 2 * S), 0.02) and abs(abs(by_name["Log"]["cframe"][9]) - 1) < 0.01,
      by_name.get("Log"))
kept_as_mesh = sorted(m["name"] for m in meshes_of(sent))
check("a six-sided trunk, an open pipe and a stretched sphere stay meshes", kept_as_mesh == ["Egg", "Pipe", "Trunk"]
      and not {"Trunk", "Pipe", "Egg"} & set(by_name), (kept_as_mesh, sorted(by_name)))
check("the send is not called finished when meshes are only a preview", d.get("built", {}).get("parts") == 5
      and "do not call the send finished" in d.get("preview_only", ""), d)

# a shape used many times travels once
trees = [cone(f"Leaves{i}", 120 + 4 * i, 0, 1, 1.2 * size, 3.0 * size) for i, size in enumerate((0.8, 1.0, 1.37, 0.93, 1.21))]
bpy.context.view_layer.update()
before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=[t.name for t in trees], name="Trees", project="My Obby", at_origin=False)
d = json.loads(text) if not err else {}
copies = meshes_of(studio.jobs[before:])
check("forty trees are one shape: sent once, then named", not err and len(copies) == 5 and "v" in copies[0]
      and all("v" not in c and c["key"] == copies[0]["key"] for c in copies[1:]) and "copies" in d, [sorted(c) for c in copies])
check("each copy carries its own size and place", all(abs(c["size"][1] - 3.0 * size * S) < 0.01 and abs(c["center"][0] - (120 + 4 * i) * S) < 0.01
                                                       for i, (c, size) in enumerate(zip(copies, (0.8, 1.0, 1.37, 0.93, 1.21)))),
      [(c["size"], c["center"]) for c in copies])

# faces are made right on the way: nothing has to be checked or repaired before sending
def volume_of(piece):
    v, t = piece["v"], piece["t"]
    point = lambda i: (v[3 * i], v[3 * i + 1], v[3 * i + 2])      # noqa: E731
    total = 0.0
    for k in range(0, len(t), 3):
        a, b, c = point(t[k]), point(t[k + 1]), point(t[k + 2])
        total += a[0] * (b[1] * c[2] - b[2] * c[1]) + a[1] * (b[2] * c[0] - b[0] * c[2]) + a[2] * (b[0] * c[1] - b[1] * c[0])
    return total / 6


good = cone("Spire", 200, 0, 0, 1.0, 3.0)
bad = cone("Spire_Inside_Out", 210, 0, 0, 1.0, 3.0)
bad.data.flip_normals()
bad.data.update()
bpy.ops.mesh.primitive_grid_add(x_subdivisions=4, y_subdivisions=4, size=2.0, location=(220, 0, 1))
leaf = bpy.context.active_object
leaf.name = "Leaf"
leaf.rotation_euler = (0.4, 0.2, 0)
bpy.context.view_layer.update()
before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["Spire", "Spire_Inside_Out", "Leaf"], name="Faces", project="My Obby", at_origin=False)
d = json.loads(text) if not err else {}
faces = {m["name"]: m for m in meshes_of(studio.jobs[before:])}
twin = faces["Spire_Inside_Out"]
outward = sum(twin["n"][3 * i + k] * twin["v"][3 * i + k] for i in range(len(twin["v"]) // 3) for k in (0, 2))   # side normals lean away from the axis
check("a mesh built inside out is turned on the way: faces and normals point out", not err and volume_of(faces["Spire"]) > 0
      and abs(volume_of(twin) - volume_of(faces["Spire"])) < 0.01 * volume_of(faces["Spire"]) and outward > 0
      and "1 meshes built inside out" in d.get("faces", ""), (d.get("faces"), volume_of(twin), outward))
check("an open mesh is drawn from both sides", faces["Leaf"].get("double_sided") is True and "double_sided" not in faces["Spire"]
      and "1 open meshes" in d.get("faces", ""), sorted(faces["Leaf"]))
check("the Blender scene is left as it was", bad.data.polygons[0].normal.dot(bad.data.polygons[0].center - bad.matrix_world.translation
                                                                                 - __import__("mathutils").Vector((210, 0, 1))) < 0)

# the upload is the user's decision: asked once, when a send needs it
from unix.tools import interact, roblox


def drive(sender, answer=None):
    """Run a Sender the way the registry does. Returns (report, jobs already sent when the question came up)."""
    asked_after, deadline = None, time.time() + 20
    while time.time() < deadline:
        report = sender.step()
        if report is not None:
            return report, asked_after
        if interact.pending_question() is not None and asked_after is None:
            asked_after = sender.posted
            interact.answer_question(answer)
        time.sleep(0.01)
    return {}, asked_after


engines._mesh_policy = lambda: "ask"
remembered = []
before = len(studio.jobs)
sender = engines.send_roblox(["Building", "Statue"], "Asked", "My Obby")
sender.can_ask, sender.remember = True, lambda: remembered.append(True)
report, asked_after = drive(sender, roblox.KEEP_ONCE)
check("a send with a real mesh asks before anything is uploaded", asked_after == 0 and all(j.get("upload") is True for j in studio.jobs[before:])
      and "meshes_kept" in report and "preview_only" not in report and not remembered, (asked_after, report))
sender = engines.send_roblox(["Building", "Statue"], "Asked", "My Obby")
sender.can_ask, sender.remember = True, lambda: remembered.append(True)
report, asked_after = drive(sender, roblox.KEEP_ALWAYS)
check("'always' is remembered", remembered == [True] and "meshes_kept" in report, (remembered, report))
before = len(studio.jobs)
sender = engines.send_roblox(["Building", "Statue"], "Asked", "My Obby")
sender.can_ask = True
report, asked_after = drive(sender, roblox.KEEP_NO)
check("'no' sends a preview and says what that means", asked_after == 0 and not any(j.get("upload") for j in studio.jobs[before:])
      and "chose not to upload" in report.get("preview_only", ""), report)
sender = engines.send_roblox(["Building", "Ball"], "Asked", "My Obby")
sender.can_ask = True
report, asked_after = drive(sender, roblox.KEEP_NO)
check("a send made only of native parts never asks", asked_after is None and report["built"] == {"parts": 2, "meshes": 0, "effects": 0}
      and "preview_only" not in report, (asked_after, report))
sender = engines.send_roblox(["Statue"], "Asked", "My Obby")
report, asked_after = drive(sender)
check("with nobody to ask, nothing is uploaded and the report says so", asked_after is None
      and "could not be asked" in report.get("preview_only", ""), report)
err, text = call("send_to_engine", engine="roblox", objects=["Statue"], name="Asked", project="My Obby", upload=True)
check("upload=true (the user said so in the chat) uploads without asking", not err and "meshes_kept" in json.loads(text)
      and studio.jobs[-1].get("upload") is True, text)
engines._mesh_policy = lambda: "preview"

before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["VFX_orb"], name="Portal")
d = json.loads(text) if not err else {}
orb_job = studio.jobs[-1] if len(studio.jobs) > before else {}
flat = json.dumps(orb_job)
check("an orb-like effect goes as a layered, animated native build", not err and d["built"]["effects"] == 1
      and orb_job.get("kind") == "tree" and orb_job["asset"] == "Portal" and '"Flicker"' in flat and '"Spin"' in flat
      and flat.count('"Beam"') >= 10 and flat.count('"ParticleEmitter"') == 5 and "ForceField" in flat, text[:400])

# -- an effect made here arrives as itself: filmed, sent as pictures, played as a flipbook ---------
import base64
import numpy as np
err, text = call("vfx_column", look="fire", name="whirl", height_m=4.0, radius_m=1.0, swirl=0.6, twist=1.5, carried=60,
                 location=[40, 40, 0], start_frame=1)
check("a column to send exists", not err, text)
scene = bpy.context.scene
scene.frame_set(9)
state = (scene.camera, scene.render.engine, scene.frame_current, scene.render.resolution_x, scene.render.film_transparent,
         scene.render.filepath)
before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["VFX_whirl"], name="Whirl", project="My Obby", upload=True)
d = json.loads(text) if not err else {}
jobs = studio.jobs[before:]
bands = [j for j in jobs if j.get("kind") == "picture"]
build = next((j for j in jobs if j.get("kind") != "picture"), {})
item = (build.get("effects") or [{}])[0]
film = item.get("film") or {}
check("the effect's frames travel as one picture, in bands, before the model is built",
      not err and len(bands) == 4 and jobs[:4] == bands and [b["y"] for b in bands] == [0, 256, 512, 768]
      and all(b["w"] == 1024 and b["h"] == 1024 and b["rows"] == 256 and b["key"] == bands[0]["key"] for b in bands), text[:400])
pixels = np.frombuffer(b"".join(base64.b64decode(b["data"]) for b in bands), dtype=np.uint8) if len(bands) == 4 else np.zeros(4)
check("the picture is a full sheet with the effect in it and nothing round it",
      pixels.size == 1024 * 1024 * 4 and int(pixels.reshape(-1, 4)[:, 3].max()) > 100 and int(pixels.reshape(-1, 4)[:, 3].min()) == 0,
      pixels.size)
if pixels.size == 1024 * 1024 * 4:
    cells = pixels.reshape(4, 256, 4, 256, 4).transpose(0, 2, 1, 3, 4).reshape(16, 256, 256, 4).astype(np.float32)
    seen = [float(c[..., 3].mean()) for c in cells]
    moved = float(np.abs(cells[6] - cells[7]).mean())
    seam = float(np.abs(cells[15] - cells[0]).mean())
    filled = np.where(cells[8][..., 3] > 25)
    check("every cell holds a frame, the frames differ, and the last runs on into the first",
          min(seen) > 0.5 and moved > 0.05 and seam < moved * 3.0, (min(seen), moved, seam))
    check("the effect fills its cell from side to side and top to bottom (a 4 m effect: 16 sharp cells)",
          np.ptp(filled[0]) > 180 and np.ptp(filled[1]) > 150, (np.ptp(filled[0]), np.ptp(filled[1])))
tall, wide = film.get("size", 0) * (1 + film.get("squash", 0)), film.get("size", 0) / (1 + film.get("squash", 0))
check("the model job says how to play it: grid, speed, and the size and stretch that undo the squeeze, in studs",
      film.get("key") == (bands[0]["key"] if bands else None) and film.get("grid") == 4 and 5 < film.get("fps", 0) <= 30
      and 0.0 < film.get("squash", 0) <= 1.5 and 3.6 * S < tall < 5.2 * S and 0.8 * S < wide < 3.6 * S
      and abs(film["centre"][1] - 2.0 * S) < 0.6 * S and film.get("glows") is True
      and build.get("upload") is True and item.get("effect") == "fire", film)
check("the report says the effect in Roblox is the one made here", d.get("built", {}).get("effects") == 1
      and "VFX_whirl" in d.get("effects_as_made", "") and "effects_not_as_made" not in d, text[:500])
check("filming leaves the scene as it was", state == (scene.camera, scene.render.engine, scene.frame_current,
                                                       scene.render.resolution_x, scene.render.film_transparent,
                                                       scene.render.filepath)
      and "_vfx_flip" not in bpy.data.objects and not bpy.data.objects["Building"].hide_render)
before = len(studio.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["VFX_whirl"], name="Whirl", project="My Obby")
d = json.loads(text) if not err else {}
jobs = studio.jobs[before:]
check("without the upload nothing is filmed, and the report does not call the stand-in the effect",
      not err and not any(j.get("kind") == "picture" for j in jobs) and "film" not in jobs[0]["effects"][0]
      and "switched off" in d.get("effects_not_as_made", "") and "effects_as_made" not in d, text[:500])
sender = engines.send_roblox(["VFX_whirl"], "Whirl", "My Obby", upload=True)
sender.session = dict(sender.session, version="11")
report, _asked = drive(sender)
check("a Studio still running the old bridge is told apart, and the user is told to restart it",
      "older Unix Bridge" in report.get("effects_not_as_made", ""), report)
engines._mesh_policy = lambda: "ask"
before = len(studio.jobs)
sender = engines.send_roblox(["VFX_whirl"], "Whirl", "My Obby")
sender.can_ask = True
report, asked_after = drive(sender, roblox.KEEP_NO)
check("an effect that needs its pictures uploaded asks first, and 'no' is respected", asked_after == 0
      and not any(j.get("kind") == "picture" for j in studio.jobs[before:])
      and "chose not to upload" in report.get("effects_not_as_made", ""), (asked_after, report))
engines._mesh_policy = lambda: "preview"
frames = [np.full((2, 2, 4), index / 100.0, dtype=np.float32) for index in range(72)]
for frame in frames:
    frame[..., 3] = 1.0
sheet = roblox._film_sheet(frames, 8)
first_cell, ninth = sheet[0, 0, 0] / 255.0, sheet[2, 0, 0] / 255.0
check("the seam is cross-faded: the first cell is nearly the frame after the last, the ninth is its own",
      abs(first_cell - (0.64 * 8 / 9 + 0.0 / 9)) < 0.01 and abs(ninth - 0.08) < 0.01 and sheet.shape == (16, 16, 4),
      (first_cell, ninth, sheet.shape))

# -- a thing asked for is made here first: the first native build of a message is held back once ----
from unix.core import engine as unix_engine
unix_engine._made_here_first = True
held = unix_engine._call_tool("roblox_build", {"name": "Orb", "children": [{"class": "Part"}]})
again = unix_engine._call_tool("roblox_build", {"name": "Orb", "children": [{"class": "Part"}], "place": "My Obby"})
check("the first roblox_build of a message is held back with the rule, the second goes through",
      held["isError"] and "made in Unix" in held["content"][0]["text"] and "send_to_engine" in held["content"][0]["text"]
      and not again["isError"], (held["content"][0]["text"][:120], again["content"][0]["text"][:200]))

second = FakeStudio("Zombie City")
second.start()
check("a second Studio window links too", wait_for(2))
err, text = call("send_to_engine", engine="roblox", objects=["Ball"])
check("two open places: the tool says to ask the user which", err and "My Obby" in text and "Zombie City" in text and "ask_user" in text, text)
sent_before = len(second.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["Ball"], project="zombie city")
check("naming the place sends to that window only", not err and json.loads(text)["place"] == "Zombie City"
      and len(second.jobs) > sent_before and all(j.get("asset") != "Ball" for j in studio.jobs), text[:300])
err, text = call("send_to_engine", engine="roblox", objects=["Ball"], project="Nowhere")
check("an unknown place name is refused with the open places", err and "Zombie City" in text, text)
err, text = call("engine_targets")
check("engine_targets lists both places and says to ask", not err and "Ask the user which place" in json.loads(text).get("roblox", "")
      and sum(1 for t in json.loads(text)["targets"] if t.get("live_link")) == 2, text)
dense = bpy.data.objects.get("Dense_Sphere")
if dense is not None:
    before = len(second.jobs)
    err, text = call("send_to_engine", engine="roblox", objects=["Dense_Sphere"], project="Zombie City")
    check("a dense sphere is still just a Ball", not err and [x.get("shape") for x in parts_of(second.jobs[before:])] == ["Ball"]
          and not meshes_of(second.jobs[before:]), text[:300])
    dense.scale = (1.0, 1.0, 1.5)
    bpy.context.view_layer.update()
    before = len(second.jobs)
    err, text = call("send_to_engine", engine="roblox", objects=["Dense_Sphere"], project="Zombie City")
    cut = meshes_of(second.jobs[before:])
    heavy = [len([m for m in (job.get("meshes") or []) if len(m.get("t", [])) // 3 > 5000]) for job in second.jobs[before:]]
    check("a mesh over Roblox's limit is cut into pieces, never simplified", not err and len(cut) >= 2
          and all(len(m["t"]) // 3 <= 10000 for m in cut) and "detail" in json.loads(text)
          and sum(len(m["t"]) // 3 for m in cut) == json.loads(text)["triangles"], (err, [len(m["t"]) // 3 for m in cut]))
    check("a heavy piece travels alone", max(heavy) == 1 and len(second.jobs) - before >= 2, heavy)

# motion: what moves in Blender's timeline moves in Roblox
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 20
scene.frame_set(1)
building.rotation_euler = (0, 0, math.radians(90))
building.keyframe_insert("rotation_euler", frame=1)
building.rotation_euler = (0, 0, math.radians(180))
building.keyframe_insert("rotation_euler", frame=20)
statue.rotation_euler = (0, 0, math.radians(90))
statue.keyframe_insert("location", frame=1)
statue.location.z += 2.0
statue.keyframe_insert("location", frame=20)
scene.frame_set(1)
before = len(second.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["Building", "Statue"], name="Movers", project="Zombie City")
d = json.loads(text) if not err else {}
sent = second.jobs[before:]
moving_part = next(iter(parts_of(sent)), {})
moving_mesh = next(iter(meshes_of(sent)), {})
track, mesh_track = moving_part.get("track") or {}, moving_mesh.get("track") or {}
# the box turns a further 90 degrees about Blender Z, which is Roblox Y: its X axis ends up along Z
check("a keyframed box carries its motion as a track", not err and len(track.get("frames", [])) == 20 and abs(track["fps"] - 24) < 0.01
      and near(track["frames"][0], moving_part["cframe"]) and abs(track["frames"][-1][3]) < 0.02
      and abs(abs(track["frames"][-1][9]) - 1) < 0.02 and abs(track["frames"][-1][7] - 1) < 0.02,
      (track.get("fps"), track.get("frames", [[]])[-1]))
# the statue is turned 90 degrees: its vertices travel unturned and the part carries the turn
check("a turned mesh keeps its own frame: the part is rotated, not the vertices", near(moving_mesh.get("rot", []), (0, 0, 1, 0, 1, 0, -1, 0, 0), 0.001)
      or near(moving_mesh.get("rot", []), (0, 0, -1, 0, 1, 0, 1, 0, 0), 0.001), moving_mesh.get("rot"))
check("a keyframed mesh moves as a whole", len(mesh_track.get("frames", [])) == 20
      and abs((mesh_track["frames"][-1][1] - mesh_track["frames"][0][1]) - 2.0 * S) < 0.05
      and near(mesh_track["frames"][0][3:], moving_mesh["rot"], 0.001) and near(mesh_track["frames"][0][:3], moving_mesh["center"], 0.01),
      mesh_track.get("frames", [[]])[-1][:4])
check("the report says the motion went along, and the frame is left where it was", "animation" in d and scene.frame_current == 1, text[:300])

# a texture travels once, however many pieces share it
bpy.ops.mesh.primitive_uv_sphere_add(segments=128, ring_count=64, radius=1.0, location=(40, 40, 1))
globe = bpy.context.active_object
globe.name = "Globe"
image = bpy.data.images.new("GlobeMap", 8, 8)
skin = bpy.data.materials.new("GlobeSkin")
skin.use_nodes = True
node = skin.node_tree.nodes.new("ShaderNodeTexImage")
node.image = image
skin.node_tree.links.new(node.outputs["Color"], skin.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
globe.data.materials.append(skin)
before = len(second.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["Globe"], project="Zombie City")
textures = [m.get("texture") for job in second.jobs[before:] for m in (job.get("meshes") or [])]
check("a texture is sent once and then named", not err and len(textures) >= 2 and "data" in textures[0]
      and textures[0]["key"].startswith("GlobeMap:") and all(t == {"key": textures[0]["key"]} for t in textures[1:]),
      [sorted(t) if t else t for t in textures])
check("a textured sphere stays a mesh: a Part cannot carry its picture", not parts_of(second.jobs[before:]))

# a picture behind a tint, tiled by a Mapping node: how library materials are usually wired
import base64 as _b64
planks = bpy.data.images.new("Planks", 8, 8)
planks.pixels.foreach_set([0.5, 0.5, 0.5, 1.0] * 64)
tinted = bpy.data.materials.new("TintedPlanks")
tinted.use_nodes = True
tree_nodes, tree_links = tinted.node_tree.nodes, tinted.node_tree.links
picture = tree_nodes.new("ShaderNodeTexImage")
picture.image = planks
mapping = tree_nodes.new("ShaderNodeMapping")
mapping.inputs["Scale"].default_value = (3.0, 3.0, 3.0)
coords = tree_nodes.new("ShaderNodeTexCoord")
tree_links.new(coords.outputs["UV"], mapping.inputs["Vector"])
tree_links.new(mapping.outputs["Vector"], picture.inputs["Vector"])
mix = tree_nodes.new("ShaderNodeMix")
mix.data_type, mix.blend_type = 'RGBA', 'MULTIPLY'
mix.inputs["Factor"].default_value = 1.0
tree_links.new(picture.outputs["Color"], mix.inputs["A"])
mix.inputs["B"].default_value = (0.0, 0.0, 1.0, 1.0)              # a pure blue tint
tree_links.new(mix.outputs["Result"], tinted.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
bpy.ops.mesh.primitive_cube_add(size=2.0, location=(60, 60, 1))
wall = bpy.context.active_object
wall.name = "PlankWall"
wall.data.materials.append(tinted)
noisy = bpy.data.materials.new("NoiseOnly")
noisy.use_nodes = True
noise_node = noisy.node_tree.nodes.new("ShaderNodeTexNoise")
noisy.node_tree.links.new(noise_node.outputs["Color"], noisy.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
bpy.ops.mesh.primitive_monkey_add(size=1.0, location=(64, 60, 1))
rock = bpy.context.active_object
rock.name = "Rock"
rock.data.materials.append(noisy)
bpy.context.view_layer.update()
before = len(second.jobs)
err, text = call("send_to_engine", engine="roblox", objects=["PlankWall", "Rock"], name="Textured", project="Zombie City")
d = json.loads(text) if not err else {}
sent_wall = next((m for m in meshes_of(second.jobs[before:]) if m["name"] == "PlankWall"), {})
pixel = list(_b64.b64decode(sent_wall.get("texture", {}).get("data", ""))[:4])
check("a picture behind a tint is found, and the tint is put into its pixels", not err and len(pixel) == 4 and pixel[0] <= 1 and pixel[1] <= 1
      and 120 <= pixel[2] <= 135 and "textures" in d, (pixel, text[:300]))
us, vs = sent_wall.get("uv", [0, 0])[0::2], sent_wall.get("uv", [0, 0])[1::2]
check("the Mapping node's tiling goes into the UVs: three tiles where there was one", 1.9 < max(us) - min(us) <= 3.01 and 1.9 < max(vs) - min(vs) <= 3.01,
      (min(us), max(us), min(vs), max(vs)))
check("a textured box is sent as a mesh so the picture fits it", not any(x["name"] == "PlankWall" for x in parts_of(second.jobs[before:])))
sent_rock = next((m for m in meshes_of(second.jobs[before:]) if m["name"] == "Rock"), {})
baked_pixels = _b64.b64decode(sent_rock.get("texture", {}).get("data", ""))
check("a look made only by shader nodes is baked into a picture on the way", "1 objects had a look made by shader nodes" in d.get("baked", "")
      and "plain_colours" not in d and len(baked_pixels) == sent_rock.get("texture", {}).get("w", 0) ** 2 * 4
      and len(set(baked_pixels[0::4])) > 20 and len(sent_rock.get("uv", [])) > 0, (d.get("baked"), d.get("plain_colours"), len(baked_pixels)))
check("the bake leaves the scene as it was", "_unix_bake" not in bpy.data.objects and not any(i.name.startswith("Baked_") for i in bpy.data.images)
      and [m.name for m in rock.data.materials] == ["NoiseOnly"] and "UnixBake" not in rock.data.uv_layers
      and bpy.context.scene.render.engine != 'CYCLES', ([o.name for o in bpy.data.objects][:5], bpy.context.scene.render.engine))
check("nothing private travels with textures either", not any(k.startswith("_") for item in meshes_of(second.jobs[before:]) for k in item))
# -- building natively in Studio ------------------------------------------------------------
tree = [{"class": "Part", "name": "Orb", "id": "orb", "props": {"Shap": "Enum.PartType.Ball", "Size": {"v3": [4, 4, 4]}},
         "children": [{"class": "ParticleEmitter", "name": "Glow", "props": {"Rate": 20, "Size": {"nseq": [[0, 1], [1, 0]]}}}]},
        {"class": "Script", "name": "Controller", "props": {"Source": "print('hi')"}}]
err, text = call("roblox_build", name="Orb", children=tree)
check("roblox_build with two open places says to ask which", err and "ask_user" in text and "Zombie City" in text, text)
err, text = call("roblox_build", name="Orb", children=tree, place="My Obby")
d = json.loads(text) if not err else {}
job = studio.jobs[-1]
check("roblox_build sends the tree as data to the chosen place", not err and job.get("kind") == "tree" and job["asset"] == "Orb"
      and job["parent"] == "Workspace" and job["place"] == "camera" and job["root"]["class"] == "Model"
      and job["root"]["children"] == tree and d["classes"] == {"Part": 1, "ParticleEmitter": 1, "Script": 1}, text)
check("properties Studio refuses come back by name", "Shap is not a valid member" in " ".join(d.get("refused_by_studio", []))
      and "fix" in d and "scripts" in d, d)
err, text = call("roblox_build", name="Orb", children=[{"name": "NoClass"}], place="My Obby")
check("a node without a class is refused before anything is sent", err and "class" in text, text)
err, text = call("roblox_build", name="Orb", children=[{"class": "Script", "name": "Empty"}], place="My Obby")
check("a script without Source is refused", err and "Source" in text, text)
err, text = call("roblox_query", path="Workspace", depth=1, place="My Obby")
check("roblox_query returns Studio's tree", not err and json.loads(text)["tree"]["class"] == "Workspace"
      and studio.jobs[-1]["kind"] == "query", text)
err, text = call("roblox_remove", path="Workspace.OldOrb", place="My Obby")
check("roblox_remove removes one instance", not err and json.loads(text)["removed"] == "Workspace.OldOrb", text)
err, text = call("roblox_remove", path="Workspace.Missing", place="My Obby")
check("a failure inside Studio comes back as a clear error", err and "nothing at Workspace.Missing" in text, text)
err, text = call("roblox_remove", path="Workspace", place="My Obby")
check("roblox_remove needs a full path", err and "full path" in text, text)

studio.alive = second.alive = False
link.stop()
err, text = call("roblox_build", name="Orb", children=tree)
check("with no Studio linked the build explains what to do", err and "not linked" in text, text)
check("plugin install is off outside the running app", roblox_link.install_plugin() in ("off", "no roblox"))

# -- the drag tile helper compiles and runs --------------------------------------------
try:
    exe = drop_tile.build(_util.data_dir("bin"))
    out = subprocess.run([exe, "--selftest"], capture_output=True, timeout=30)
    check("drag tile builds and starts", os.path.isfile(exe) and out.returncode == 0, out)
except drop_tile.DropTileError as exc:
    check("drag tile builds and starts", False, exc)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
