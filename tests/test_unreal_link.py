# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for the Unreal link: the remote-execution client against a stand-in editor (local
UDP and TCP on a private port, so a real editor never joins), the conversion into Unreal's space,
the export manifest of a real scene, and the not-linked fallback that switches the link on in a
stand-in project's files.
Run: blender -b --factory-startup --python tests/test_unreal_link.py"""
import ast, json, math, os, re, socket, sys, tempfile, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import numpy as np
from mathutils import Euler, Matrix, Vector
from unix import tools
from unix.tools import _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
tools.load_all()
from unix.bridge import unreal_link  # noqa: E402
from unix.tools import engines, unreal  # noqa: E402

GROUP = ("239.0.0.1", 6797)          # not Unreal's 6766: a real editor never hears these tests
unreal_link.GROUP = GROUP
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"]


class FakeEditor:
    """Answers pings like an Unreal editor with remote execution on, connects back on open_connection,
    and answers each command (in two pieces, as TCP may deliver it). A command that runs the builder
    gets a report written next to its manifest, as the real builder does."""

    def __init__(self, project_root, name="Game"):
        self.node = "fake-" + name
        self.project_root, self.name = project_root, name
        self.commands, self.alive = [], True
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self.udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.udp.bind(("0.0.0.0", GROUP[1]))
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP[0]) + socket.inet_aton("0.0.0.0"))
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        self.udp.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        self.udp.settimeout(0.1)
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while self.alive:
            try:
                raw = self.udp.recv(65536)
            except (socket.timeout, OSError):
                continue
            msg = unreal_link.parse(raw)
            if not msg or msg["source"] == self.node or (msg.get("dest") and msg["dest"] != self.node):
                continue
            if msg["type"] == "ping":
                data = {"user": "me", "machine": "here", "engine_version": "5.1.1", "engine_root": "C:/UE/",
                        "project_root": self.project_root.replace("\\", "/") + "/", "project_name": self.name}
                self.udp.sendto(unreal_link.message("pong", self.node, msg["source"], data), GROUP)
            elif msg["type"] == "open_connection":
                conn = socket.create_connection((msg["data"]["command_ip"], msg["data"]["command_port"]))
                threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            while self.alive:
                raw = conn.recv(1 << 20)
                if not raw:
                    return
                msg = unreal_link.parse(raw)        # the real editor needs each command in one piece
                if msg is None or msg["type"] != "command":
                    self.commands.append(("unreadable", raw[:200]))
                    continue
                command = msg["data"]["command"]
                self.commands.append(("command", command, msg["data"]["exec_mode"]))
                found = re.search(r"'MANIFEST': ('(?:[^'\\]|\\.)*')", command)
                if found:
                    self._build(ast.literal_eval(found.group(1)))
                answer = unreal_link.message("command_result", self.node, msg["source"],
                                             {"success": True, "command": command, "result": "None",
                                              "output": [{"type": "Info", "output": "x" * 5000}]})
                conn.sendall(answer[:50])
                time.sleep(0.05)
                conn.sendall(answer[50:])

    def _build(self, manifest_path):
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        self.manifest = manifest
        kinds = [a["kind"] for a in manifest["actors"]]
        report = {"ok": True, "level": "/UGC999/Maps/Foo", "content": f"/UGC999/Unix/{manifest['asset']}",
                  "masters": ["opaque"], "replaced": 0, "notes": [],
                  "imported": {"meshes": len(manifest["meshes"]), "textures": len(manifest["textures"]),
                               "materials": len(manifest["materials"])},
                  "spawned": {"meshes": kinds.count("mesh"), "lights": kinds.count("light"), "cameras": kinds.count("camera")}}
        with open(os.path.join(os.path.dirname(manifest_path), "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh)

    def stop(self):
        self.alive = False
        self.udp.close()


# -- the protocol ---------------------------------------------------------------------
tmp = tempfile.mkdtemp()
game = os.path.join(tmp, "MyGame")
os.makedirs(game)
open(os.path.join(game, "MyGame.uproject"), "w").write('{\n\t"FileVersion": 3\n}\n')
check("no editor answers: an empty list", unreal_link.find_editors(0.6) == [])
fake = FakeEditor(game, "MyGame")
editors = unreal_link.find_editors(0.8)
check("discovery finds the stand-in editor with its project", len(editors) == 1 and editors[0]["node"] == fake.node
      and unreal_link.same_folder(editors[0]["project_root"], game), editors)
check("pick_editor matches the project folder however it is written",
      unreal.pick_editor(editors, game + os.sep) is editors[0] and unreal.pick_editor(editors, tmp) is None)
answer = unreal_link.run_once(fake.node, "print(1)", unreal_link.EXEC_STATEMENT, seconds=10)
check("a command round trip, its result arriving in pieces", answer["success"] and fake.commands[-1][1:] == ("print(1)", "ExecuteStatement")
      and len(answer["output"][0]["output"]) == 5000, (answer, fake.commands))
bad = unreal_link.parse(b'{"version": 1, "magic": "not_ue", "type": "pong", "source": "x"}')
check("messages without the ue_py magic are ignored", bad is None)
try:
    unreal_link.Channel("nobody-home").open(seconds=1.5)
    check("an editor that never connects back is a clear error", False)
except unreal_link.LinkError as exc:
    check("an editor that never connects back is a clear error", "command channel" in str(exc), exc)

# -- Unreal's space, numerically ---------------------------------------------------------
rng = np.random.default_rng(7)
worst = 0.0
for _ in range(40):
    angles = rng.uniform(-math.pi, math.pi, 3)
    scale = rng.uniform(0.2, 3.0, 3) * rng.choice([1.0, -1.0], 3)
    matrix = Matrix.LocRotScale(Vector(rng.uniform(-50, 50, 3)), Euler(angles).to_quaternion(), Vector(scale))
    unit = float(rng.choice([1.0, 0.01]))
    t = unreal.to_unreal(matrix, unit)
    for point in rng.uniform(-2, 2, (5, 3)):
        # Unreal: the imported mesh point, scaled, turned, moved -- where Blender has the point in the world
        placed = unreal.rotate(t["quat"], np.array(t["scale"]) * unreal.point_to_unreal(point, unit)) + np.array(t["location"])
        wanted = unreal.point_to_unreal(np.array(matrix @ Vector(point)), unit)
        worst = max(worst, float(np.abs(placed - wanted).max()))
check("every point of a moved, turned, scaled (and mirrored) object lands where Blender has it", worst < 1e-3, worst)
check("units: 1 m in Blender is 100 cm, Blender +Y is Unreal -Y",
      np.allclose(unreal.to_unreal(Matrix.Translation((1, 2, 3)))["location"], [100, -200, 300]))
camera_matrix = Matrix.LocRotScale(Vector((0, -10, 2)), Euler((math.radians(80), 0, math.radians(15))).to_quaternion(), None)
aimed = unreal.to_unreal(camera_matrix, aim=True)
forward = unreal.rotate(aimed["quat"], [1, 0, 0])
up = unreal.rotate(aimed["quat"], [0, 0, 1])
blender_forward = np.array(camera_matrix.to_3x3() @ Vector((0, 0, -1)))
blender_up = np.array(camera_matrix.to_3x3() @ Vector((0, 1, 0)))
check("a camera or light looks the same way: Unreal +X along Blender -Z, +Z along Blender +Y",
      np.allclose(forward, unreal.point_to_unreal(blender_forward) / 100) and np.allclose(up, unreal.point_to_unreal(blender_up) / 100),
      (forward, up))

# -- a real scene: the export and its manifest -------------------------------------------
bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080
grid = bpy.data.images.new("Grid", 64, 32)
grid.generated_type = 'UV_GRID'
bumps = bpy.data.images.new("Bumps", 16, 16)
bumps.generated_color = (0.5, 0.5, 1.0, 1.0)
bumps.colorspace_settings.name = 'Non-Color'


def material(name, image=None, normal=None, metal=0.0, rough=0.5, noise=False, emit=0.0):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    shader = nodes["Principled BSDF"]
    shader.inputs["Metallic"].default_value, shader.inputs["Roughness"].default_value = metal, rough
    if image is not None:
        tex = nodes.new("ShaderNodeTexImage"); tex.image = image
        links.new(tex.outputs["Color"], shader.inputs["Base Color"])
    if normal is not None:
        tex = nodes.new("ShaderNodeTexImage"); tex.image = normal
        nm = nodes.new("ShaderNodeNormalMap")
        links.new(tex.outputs["Color"], nm.inputs["Color"]); links.new(nm.outputs["Normal"], shader.inputs["Normal"])
    if noise:
        tex = nodes.new("ShaderNodeTexNoise")
        links.new(tex.outputs["Color"], shader.inputs["Base Color"])
    if emit:
        shader.inputs["Emission Color"].default_value = (1.0, 0.4, 0.1, 1.0)
        shader.inputs["Emission Strength"].default_value = emit
    return mat


wood, gold, cloud = material("Wood", grid, bumps), material("Gold", metal=1.0, rough=0.2), material("Cloud", noise=True)
lamp = material("Lamp", emit=5.0)
bpy.ops.mesh.primitive_cube_add(size=2, location=(0, 0, 1))
crate = bpy.context.active_object; crate.name = "Crate"; crate.data.materials.append(wood); crate.data.materials.append(gold)
crate.data.polygons[0].material_index = 1
copy = crate.copy(); copy.name = "Crate_Copy"; copy.location = (5, 2, 1); copy.rotation_euler = (0, 0, 0.7)
scene.collection.objects.link(copy)                    # a linked duplicate: same mesh data
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(-3, 0, 0.5))
ball = bpy.context.active_object; ball.name = "Ball"; ball.data.materials.append(cloud)
ball.modifiers.new("Smooth", 'SUBSURF')
bpy.ops.mesh.primitive_cylinder_add(radius=0.2, depth=1, location=(2, -3, 0.5))
post = bpy.context.active_object; post.name = "Post"; post.data.materials.append(lamp)
post.visible_shadow = False             # a lamp globe: casts no shadow in Blender, so none in Unreal
props = bpy.data.collections.new("Props")
bpy.ops.mesh.primitive_cone_add(location=(0, 0, 0.5))
cone = bpy.context.active_object; cone.name = "Cone"
for c in list(cone.users_collection):
    c.objects.unlink(cone)
props.objects.link(cone)
bpy.context.scene.collection.children.link(props)
bpy.context.view_layer.layer_collection.children["Props"].exclude = True
# pictures saved as files, nothing done to their pixels but a tint: the files themselves travel
saved = {}
for name, colour, space in (("Oak.png", (0.6, 0.3, 0.1, 1.0), 'sRGB'), ("Oak_Normal.png", (0.5, 0.5, 1.0, 1.0), 'Non-Color')):
    made = bpy.data.images.new(name, 8, 8)
    made.generated_color = colour
    made.filepath_raw = os.path.join(tempfile.mkdtemp(prefix="unix_oak_"), name)
    made.file_format = 'PNG'
    made.save()
    made.colorspace_settings.name = space
    saved[name] = made
oak = material("Oak", normal=saved["Oak_Normal.png"])
tex = oak.node_tree.nodes.new("ShaderNodeTexImage"); tex.image = saved["Oak.png"]
tint = oak.node_tree.nodes.new("ShaderNodeMix"); tint.data_type = 'RGBA'; tint.blend_type = 'MULTIPLY'
tint.inputs["Factor"].default_value = 1.0; tint.inputs[7].default_value = (0.5, 0.8, 1.0, 1.0)
oak.node_tree.links.new(tex.outputs["Color"], tint.inputs[6])
oak.node_tree.links.new(tint.outputs[2], oak.node_tree.nodes["Principled BSDF"].inputs["Base Color"])
bpy.ops.mesh.primitive_cube_add(size=1, location=(-4, 4, 0.5))
plank = bpy.context.active_object; plank.name = "Plank"; plank.data.materials.append(oak)
scene.world = bpy.data.worlds.new("Dim")          # a dim grey world: the light in a scene's shade
scene.world.use_nodes = True
scene.world.node_tree.nodes["Background"].inputs["Color"].default_value = (0.05, 0.05, 0.05, 1.0)
holder = bpy.data.objects.new("Props_Here", None)
holder.instance_type, holder.instance_collection = 'COLLECTION', props
holder.location = (-4, 4, 0)
scene.collection.objects.link(holder)
for kind, place in (('POINT', (2, 2, 3)), ('SPOT', (-2, 2, 4)), ('SUN', (0, 0, 10)), ('AREA', (0, -3, 3))):
    data = bpy.data.lights.new(kind.title(), kind)
    data.energy = 1000.0 if kind != 'SUN' else 3.0
    light = bpy.data.objects.new(kind.title(), data)
    light.location = place
    scene.collection.objects.link(light)
cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
cam.location, cam.rotation_euler = (8, -8, 5), (math.radians(65), 0, math.radians(45))
scene.collection.objects.link(cam)
scene.camera = cam
for o in bpy.context.view_layer.objects:
    o.select_set(False)
bpy.context.view_layer.update()
before = (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.images))
selected_before = [o.name for o in bpy.context.view_layer.objects if o.select_get()]

fake.stop()
fake = FakeEditor(game, "MyGame")
engines._running_editors = lambda: []
engines._recent_projects = lambda: []
engines._saved = lambda: {}
engines.find_targets(refresh=True)
err, text = call("send_to_engine", engine="unreal", project=game, name="Village")
d = json.loads(text) if not err else {}
manifest = getattr(fake, "manifest", {})
check("with nothing selected the whole scene goes and the editor's report comes back", not err and d.get("level") == "/UGC999/Maps/Foo"
      and d["built"] == {"meshes": 6, "lights": 4, "cameras": 1}, text)
meshes = {m["name"]: m for m in manifest.get("meshes", [])}
actors = {a["name"]: a for a in manifest.get("actors", [])}
check("each distinct mesh is exported once: the linked duplicate shares the crate's",
      len(meshes) == 5 and actors["Crate"]["mesh"] == actors["Crate_Copy"]["mesh"] and "copies" in d, (list(meshes), list(actors)))
check("a collection instance arrives with the instancer's placement", "Props_Here_Cone" in actors
      and np.allclose(actors["Props_Here_Cone"]["location"], [-400, -400, 50], atol=1e-3), actors.get("Props_Here_Cone"))
staging = os.path.join(_util.data_dir("exports", "unreal"), "Village")
fbx = open(os.path.join(staging, manifest["meshes"][0]["file"]), "rb").read() if manifest.get("meshes") else b""
check("the FBX names its slots UnixSlot0..n for Unreal to find", b"UnixSlot0" in fbx and b"UnixSlot1" in fbx)
check("the crate's slots carry its two materials", len(actors["Crate"]["materials"]) == 2, actors["Crate"])
mats = {m["key"]: m for m in manifest.get("materials", [])}
crate_mats = [mats[k] for k in actors["Crate"]["materials"]]
check("the textured material carries its colour and (DirectX) normal maps", set(crate_mats[0]["textures"]) == {"BaseColorMap", "NormalMap"}
      and crate_mats[0]["vectors"]["BaseColor"] == [1.0, 1.0, 1.0, 1.0], crate_mats[0])
check("the metal material is numbers only", crate_mats[1]["scalars"] == {"Roughness": 0.2, "Metallic": 1.0} and not crate_mats[1]["textures"],
      crate_mats[1])
sky = manifest.get("sky") or {}
hdr = bpy.data.images.load(os.path.join(staging, sky["file"])) if sky.get("file") else None
sky_pixels = np.array(hdr.pixels[:]).reshape(hdr.size[1], hdr.size[0], 4) if hdr is not None else np.zeros((2, 2, 4))
check("a whole scene takes its world along: a long-lat HDR sky picture on the lights' scale",
      hdr is not None and hdr.file_format == 'HDR' and np.allclose(sky_pixels[..., :3], 0.05 * unreal.LIGHT_SCALE, rtol=0.03), (sky, sky_pixels[0, 0]))
oak_mat = mats[actors["Plank"]["materials"][0]]
by_key = {t["key"]: t for t in manifest.get("textures", [])}
oak_colour, oak_normal = by_key.get(oak_mat["textures"].get("BaseColorMap")), by_key.get(oak_mat["textures"].get("NormalMap"))
check("an untouched picture file travels as itself, byte for byte, its tint becoming the material's colour",
      oak_colour is not None and open(os.path.join(staging, oak_colour["file"]), "rb").read() == open(saved["Oak.png"].filepath_raw, "rb").read()
      and np.allclose(oak_mat["vectors"]["BaseColor"][:3], [0.5, 0.8, 1.0]), (oak_colour, oak_mat["vectors"]))
check("an untouched normal map file travels as itself, marked to have its green turned over in Unreal",
      oak_normal is not None and oak_normal.get("flip_green") is True and oak_normal["file"].endswith(".png")
      and open(os.path.join(staging, oak_normal["file"]), "rb").read() == open(saved["Oak_Normal.png"].filepath_raw, "rb").read(), oak_normal)
ball_mat = mats[actors["Ball"]["materials"][0]]
check("a look made by shader nodes was baked into a picture", "BaseColorMap" in ball_mat["textures"], ball_mat)
check("light given off travels, on the lights' scale", abs(mats[actors["Post"]["materials"][0]]["vectors"].get("EmissiveColor", [0])[0]
                                                         - 5.0 * unreal.LIGHT_SCALE) < 1e-3)
check("an object that casts no shadow in Blender casts none in Unreal", actors["Post"]["shadows"] is False
      and actors["Crate"]["shadows"] is True, (actors["Post"].get("shadows"), actors["Crate"].get("shadows")))
textures = manifest.get("textures", [])
check("pictures are written as PNG files", textures and all(open(os.path.join(staging, t["file"]), "rb").read(8) == b"\x89PNG\r\n\x1a\n"
                                                            for t in textures), textures)
point, sun, spot = actors["Point"], actors["Sun"], actors["Spot"]
check("watts become candelas and lux, scaled for Unreal's fixed exposure", abs(point["intensity"] - 1000 / (4 * math.pi) * 1.2 / 0.18) < 0.01
      and abs(sun["intensity"] - 3 * 1.2 / 0.18) < 0.01
      and spot["outer"] == 22.5, (point, sun, spot))
check("the active camera keeps its horizontal field of view", actors["Camera"]["active"]
      and abs(actors["Camera"]["fov"] - math.degrees(2 * math.atan(18 / 50))) < 1e-3, actors["Camera"])
daylight = bpy.data.worlds.new("Daylight")
daylight.use_nodes = True
sun_sky = daylight.node_tree.nodes.new("ShaderNodeTexSky")
daylight.node_tree.links.new(sun_sky.outputs["Color"], daylight.node_tree.nodes["Background"].inputs["Color"])
scene.world = daylight
made = os.path.join(tempfile.mkdtemp(prefix="unix_sky_"), "sky.hdr")
digest = unreal.write_sky(scene, made, 1.0)
lit = bpy.data.images.load(made) if digest else None
lit_pixels = np.array(lit.pixels[:]).reshape(lit.size[1], lit.size[0], 4)[..., :3] if lit is not None else np.zeros((2, 2, 3))
check("a world made by nodes is rendered once and sent as its light from above and from below",
      digest is not None and lit_pixels[-1].mean() > 2.0 * lit_pixels[0].mean() > 0.0, (lit_pixels[-1, 0], lit_pixels[0, 0]))
check("the render leaves nothing behind", "_unix_sky" not in bpy.data.scenes and "_unix_sky" not in bpy.data.objects
      and "_unix_sky" not in bpy.data.cameras and "Render Result" not in bpy.data.images)
for loaded in (hdr, lit):
    if loaded is not None:
        bpy.data.images.remove(loaded)
command = next((c for c in fake.commands if c[0] == "command" and "MANIFEST" in c[1]), ("", ""))[1]
check("the command stays short: big things travel as files", 0 < len(command) < 1000 and "unreal_build.py" in command, command)
after = (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.images))
check("the Blender scene is as it was (no export objects, slot materials or bakes left)", before == after
      and not any(m.name.startswith("UnixSlot") for m in bpy.data.materials)
      and [s.name for s in bpy.data.scenes] == [bpy.context.scene.name]
      and not any(c.name.startswith("_unix") for c in bpy.data.collections), (before, after, list(bpy.data.scenes)))
check("the selection is as it was", [o.name for o in bpy.context.view_layer.objects if o.select_get()] == selected_before)

# a send stopped half way keeps what it wrote: the next one writes only the rest (31 Oct: a city's send was stopped
# after 25 minutes and the next one began from nothing)
for name in os.listdir(os.path.join(staging, "meshes")):
    os.remove(os.path.join(staging, "meshes", name))
os.remove(os.path.join(staging, "manifest.json"))
unreal.BATCH_SECONDS, batch_seconds = 0.0, unreal.BATCH_SECONDS        # one mesh a batch: the stop comes half way
sender = unreal.Sender(None, "Village", fake_editor := {"node": "nobody", "project_name": "MyGame"}, staging)
while not sender.exported:
    sender.step()
sender.close()
unreal.BATCH_SECONDS = batch_seconds
kept = {m["file"] for m in sender.exported}
written = []
unreal.Sender._write_fbx, plain_write = (lambda self, mesh, path: written.append(path) or plain_write(self, mesh, path)), unreal.Sender._write_fbx
sender = unreal.Sender(None, "Village", fake_editor, staging)
while sender.thread is None:
    sender.step()
sender.close()
unreal.Sender._write_fbx = plain_write
check("a stopped send keeps the meshes it wrote: the next writes only the others",
      kept and len(written) == len(sender.exported) - len(kept)
      and not any(os.path.relpath(p, staging).replace(os.sep, "/") in kept for p in written), (kept, written))
check("...and a stop leaves nothing behind in the scene either",
      (len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials)) == after[:3]
      and [s.name for s in bpy.data.scenes] == [bpy.context.scene.name])
crate.select_set(True)
err, text = call("send_to_engine", engine="unreal", project=game)
check("a selection sends just that, named after it", not err and json.loads(text)["asset"] == "Crate"
      and len(fake.manifest["actors"]) == 1, text)
crate.select_set(False)
fake.stop()

# -- not linked: the link is switched on in the project's files ------------------------------
ini_text = "[/Script/EngineSettings.GameMapsSettings]\r\nGameDefaultMap=/Game/Maps/Start\r\n"
os.makedirs(os.path.join(game, "Config"))
with open(os.path.join(game, "Config", "DefaultEngine.ini"), "wb") as fh:
    fh.write(ini_text.encode())
engines._running_editors = lambda: [("UnrealEditor.exe", f'"C:\\UE\\UnrealEditor.exe" "{os.path.join(game, "MyGame.uproject")}"')]
engines.find_targets(refresh=True)
err, text = call("send_to_engine", engine="unreal", objects=["Crate"])
d = json.loads(text) if not err else {}
check("no editor answers: the FBX goes into Content/Unix as before", not err
      and os.path.isfile(os.path.join(game, "Content", "Unix", "Crate.fbx")), text)
check("... and the link is switched on, in one plain sentence",
      d.get("live_link") == "Turned on the Unreal link in MyGame. Restart the Unreal editor once, then send again.", d.get("live_link"))
project_text = open(os.path.join(game, "MyGame.uproject"), encoding="utf-8").read()
state = unreal_link.link_state(game)
check("the .uproject now enables both plugins, in its own tab indentation", not state["plugins_missing"]
      and '\t"Plugins": [\n\t\t{\n\t\t\t"Name": "PythonScriptPlugin"' in project_text, project_text)
ini_now = open(os.path.join(game, "Config", "DefaultEngine.ini"), "rb").read().decode()
check("DefaultEngine.ini keeps every byte and gains the one key", ini_now.startswith(ini_text) and state["remote_on"]
      and ini_now.endswith("[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\r\nbRemoteExecution=True\r\n"), repr(ini_now))
check("both files were backed up first", open(os.path.join(game, "MyGame.uproject.bak")).read() == '{\n\t"FileVersion": 3\n}\n'
      and open(os.path.join(game, "Config", "DefaultEngine.ini.bak"), "rb").read() == ini_text.encode())
err, text = call("send_to_engine", engine="unreal", objects=["Crate"])
d = json.loads(text) if not err else {}
check("a project that already has the link is left alone, and the reply says what is left to do",
      not err and "is on in MyGame" in d.get("live_link", "") and not os.path.exists(os.path.join(game, "MyGame.uproject.bak1")), text)

# -- editing the files: only what has to change changes ------------------------------------
spaced = '{\r\n  "FileVersion": 3,\r\n  "Plugins": [\r\n    {\r\n      "Name": "PythonScriptPlugin",\r\n      "Enabled": false\r\n    }\r\n  ]\r\n}\r\n'
fixed = unreal_link.with_plugins(spaced)
check("a plugin listed but off is switched on in place; a missing one is added in the file's own style",
      fixed.startswith('{\r\n  "FileVersion": 3,\r\n  "Plugins": [\r\n    {\r\n      "Name": "PythonScriptPlugin",\r\n      "Enabled": true\r\n    },'
                       '\r\n    {\r\n      "Name": "EditorScriptingUtilities",\r\n      "Enabled": true\r\n    }\r\n  ]\r\n}')
      and json.loads(fixed), repr(fixed))
check("a file that already has both plugins is not touched", unreal_link.with_plugins(fixed) == fixed)
section = "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nbDeveloperMode=True\nbRemoteExecution=False\n[Other]\nA=1\n"
check("an existing key is changed where it stands", unreal_link.with_remote(section) == section.replace("bRemoteExecution=False", "bRemoteExecution=True"))
check("a section without the key gets it under its header",
      unreal_link.with_remote("[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nX=1\n") ==
      "[/Script/PythonScriptPlugin.PythonScriptPluginSettings]\nbRemoteExecution=True\nX=1\n")

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
