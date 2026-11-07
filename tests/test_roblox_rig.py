# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for rigs on their way to Roblox: skinned meshes and their animation.

* A mesh that an armature deforms must travel with its bones (in the armature's hierarchy), at most
  four weights per vertex that add up to 1, and every piece of a mesh cut for Roblox's triangle
  limit must hang on the same skeleton.
* Skinning the sent vertices by hand, the way Roblox does, must give the mesh Blender shows: in
  the rest pose, and at the sampled keys of every action.
* An object parented to a bone must ride that bone; a rig with no action must arrive in its pose.
* The report must say what arrived and what Roblox's bones cannot do, and a Studio that still runs
  an older plugin must get the old, unskinned send with a plain note.
No Studio is needed: a thread stands in for the plugin, as in test_engines.py.
Run: blender -b --factory-startup --python tests/test_roblox_rig.py"""
import base64, json, math, os, sys, tempfile, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
import numpy as np
from mathutils import kdtree
from unix import tools
from unix.tools import _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")  # keep test data out of the user's config
tools.load_all()
from unix.tools import engines, interact, motion, roblox  # noqa: E402
from unix.bridge import roblox_link  # noqa: E402
link = roblox_link.link
fails = []
scene = bpy.context.scene
S = roblox.STUDS_PER_METER


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


class FakeStudio(threading.Thread):
    """Does what the plugin does on the wire: ask for work, report what it 'built'."""

    def __init__(self, place, version="test"):
        super().__init__(daemon=True)
        self.place, self.session, self.version, self.jobs, self.alive = place, place + "-session", version, [], True

    def post(self, path, body):
        request = urllib.request.Request(f"http://127.0.0.1:{link.port}{path}", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", "X-Unix-Plugin": "test"})
        return json.loads(urllib.request.urlopen(request, timeout=30).read())

    def run(self):
        while self.alive:
            job = self.post("/unix/poll", {"session": self.session, "place": self.place, "placeId": 1, "gameId": 1,
                                            "version": self.version}).get("job")
            if not job:
                continue
            self.jobs.append(job)
            if job.get("kind") == "motion":
                report = {"notes": [], "model": "Workspace." + job["asset"]}
                if job.get("values"):
                    report["motion"] = job["name"]
                if job.get("done"):
                    report.update(rig_ready=job["rig"], keyframes=2, actions=2)
            else:
                meshes = job.get("meshes") or []
                report = {"parts": len(job.get("parts") or []), "meshes": len(meshes), "effects": 0, "notes": [],
                          "model": "Workspace." + job["asset"], "uploaded": 0, "reused": 0, "kept": 0, "preview": len(meshes),
                          "skinned": sum(1 for m in meshes if "skin" in m)}
            self.post("/unix/result", {"session": self.session, "job": job["id"], "ok": True, "report": report, "message": ""})


def drive(sender):
    """Run a Sender the way the registry does, until it hands back its report."""
    deadline = time.time() + 120
    while time.time() < deadline:
        report = sender.step()
        if report is not None:
            return report
        time.sleep(0.005)
    return {}


# -- a small rig: a tall tube on a chain of five bones, heavy enough to be cut in two ----------------
bpy.data.objects.remove(bpy.data.objects["Cube"])
HEIGHT, RINGS, SIDES = 2.0, 90, 64
verts = [(0.25 * math.cos(2 * math.pi * s / SIDES), 0.25 * math.sin(2 * math.pi * s / SIDES), HEIGHT * r / RINGS)
         for r in range(RINGS + 1) for s in range(SIDES)]
faces = [(r * SIDES + s, r * SIDES + (s + 1) % SIDES, (r + 1) * SIDES + (s + 1) % SIDES, (r + 1) * SIDES + s)
         for r in range(RINGS) for s in range(SIDES)]
mesh = bpy.data.meshes.new("Tube")
mesh.from_pydata(verts, [], faces)
mesh.update()
tube = bpy.data.objects.new("Tube", mesh)
scene.collection.objects.link(tube)

armature = bpy.data.armatures.new("Spine")
rig = bpy.data.objects.new("Spine", armature)
scene.collection.objects.link(rig)
rig.location = (3.0, -2.0, 0.5)             # the rig stands away from the origin, turned
rig.rotation_euler = (0.0, 0.0, math.radians(30))
tube.parent = rig
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode='EDIT')
NAMES = ["Base", "Low", "Mid", "High", "Top"]
above = None
for at, name in enumerate(NAMES):
    bone = armature.edit_bones.new(name)
    bone.head, bone.tail = (0, 0, HEIGHT * at / 5), (0, 0, HEIGHT * (at + 1) / 5)
    if above is not None:
        bone.parent, bone.use_connect = above, True
    above = bone
side = armature.edit_bones.new("Arm")       # a branch, so the hierarchy is not just a chain
side.head, side.tail = (0, 0, HEIGHT * 0.5), (0.6, 0, HEIGHT * 0.5)
side.parent = armature.edit_bones["Mid"]
handle = armature.edit_bones.new("Handle")  # a control bone no vertex follows: it must not travel
handle.head, handle.tail = (1, 1, 0), (1, 1, 0.3)
bpy.ops.object.mode_set(mode='OBJECT')
handle_bone = armature.bones["Handle"]
handle_bone.use_deform = False
for name in NAMES + ["Arm"]:
    tube.vertex_groups.new(name=name)
for vertex in mesh.vertices:
    height = vertex.co.z / HEIGHT * 5        # 0..5 along the chain
    for at, name in enumerate(NAMES):
        weight = max(0.0, 1.0 - abs(height - (at + 0.5)) / 1.2)      # each bone reaches into its neighbours
        if weight > 0:
            tube.vertex_groups[name].add([vertex.index], weight, 'REPLACE')
    # a far, faint fifth and sixth influence on the middle rings: more than Roblox's four
    if 2.2 < height < 2.8:
        tube.vertex_groups["Base"].add([vertex.index], 0.004, 'REPLACE')
        tube.vertex_groups["Top"].add([vertex.index], 0.004, 'REPLACE')
    if vertex.co.x > 0.2 and 2.0 < height < 3.0:
        tube.vertex_groups["Arm"].add([vertex.index], 0.5, 'REPLACE')
deform = tube.modifiers.new("Armature", 'ARMATURE')
deform.object = rig

bpy.ops.mesh.primitive_cube_add(size=0.3, location=(0, 0, 0))
lamp = bpy.context.active_object            # a rigid thing carried by a bone
lamp.name = "Lamp"
bpy.ops.object.mode_set(mode='EDIT')        # bevelled, so it is no native box
bpy.ops.mesh.bevel(offset=0.05, segments=2)
bpy.ops.object.mode_set(mode='OBJECT')
lamp.parent, lamp.parent_type, lamp.parent_bone = rig, 'BONE', "Arm"
lamp.location = (0.0, 0.2, 0.0)
bpy.context.view_layer.update()


def key(action_name, poses, loop):
    """An action from {frame: {bone: (euler xyz degrees, optional offset)}}."""
    rig.animation_data_create()
    action = bpy.data.actions.new(action_name)
    action.use_fake_user = True
    motion.assign(rig, action)
    for frame, bones in poses.items():
        for name, (turn, *shift) in bones.items():
            bone = rig.pose.bones[name]
            bone.rotation_mode = 'XYZ'
            bone.rotation_euler = [math.radians(v) for v in turn]
            bone.keyframe_insert("rotation_euler", frame=frame)
            if shift:
                bone.location = shift[0]
                bone.keyframe_insert("location", frame=frame)
    if loop:
        action.use_cyclic = True
    for bone in rig.pose.bones:
        bone.rotation_euler, bone.location = (0, 0, 0), (0, 0, 0)
    return action


bend = key("Bend", {1: {n: ((0, 0, 0),) for n in NAMES + ["Arm"]},
                    11: {"Low": ((35, 0, 0),), "Mid": ((30, 10, 0),), "High": ((25, 0, 20),), "Top": ((20, 0, 0),), "Arm": ((0, 40, 0),),
                         "Base": ((0, 0, 0),)},
                    21: {n: ((0, 0, 0),) for n in NAMES + ["Arm"]}}, loop=True)
hop = key("Hop", {1: {"Base": ((0, 0, 0), (0, 0, 0)), "Mid": ((0, 0, 0),)},
                  9: {"Base": ((0, 0, 45), (0.2, 0.5, 0.1)), "Mid": ((-40, 0, 0),)},
                  17: {"Base": ((0, 0, 90), (0, 0, 0)), "Mid": ((0, 0, 0),)}}, loop=False)
motion.assign(rig, bend)
scene.frame_set(6)
bpy.context.view_layer.update()

check("the link server starts on a private port", link.start(ports=(0,)) and link.port > 0, link.port)
studio = FakeStudio("Rig Place")
studio.start()
deadline = time.time() + 5
while not link.sessions() and time.time() < deadline:
    time.sleep(0.05)
engines._mesh_policy = lambda: "preview"
interact.interactive["enabled"] = False

sender = engines.send_roblox(["Spine"], "Creature", "Rig Place", at_origin=False)
report = drive(sender)
jobs = studio.jobs[:]
builds = [j for j in jobs if j.get("kind") is None]
moves = [j for j in jobs if j.get("kind") == "motion"]
pieces = [m for j in builds for m in (j.get("meshes") or [])]
skeleton = (builds[0].get("rigs") or {}).get("Spine", {}) if builds else {}
bones = skeleton.get("bones", [])
names = [b["name"] for b in bones]

# -- the skeleton ---------------------------------------------------------------------------------
check("the job that opens the model carries the skeleton", builds and builds[0].get("first") is True and bool(bones), sorted(builds[0]) if builds else jobs)
wanted_parents = {"Base": None, "Low": "Base", "Mid": "Low", "High": "Mid", "Top": "High", "Arm": "Mid"}
got_parents = {b["name"]: (names[b["parent"] - 1] if b["parent"] else None) for b in bones}
check("the bones are the armature's deforming bones, in its hierarchy, parents first", got_parents == wanted_parents
      and all(b["parent"] - 1 < at for at, b in enumerate(bones)), got_parents)


def matrix(numbers):
    """A CFrame's twelve numbers as a 4 x 4 matrix."""
    out = np.eye(4)
    out[:3, 3], out[:3, :3] = numbers[:3], np.array(numbers[3:]).reshape(3, 3)
    return out


def to_roblox(points):
    """Blender world points as Roblox points (this send keeps scene placement: no offset)."""
    points = np.asarray(points, dtype=np.float64)
    return np.stack([points[:, 0], points[:, 2], -points[:, 1]], axis=1) * S


rest = np.array([matrix(b["cframe"]) for b in bones]) if bones else np.zeros((0, 4, 4))
heads = to_roblox([rig.matrix_world @ armature.bones[n].head_local for n in names]) if bones else np.zeros((0, 3))
check("each bone rests where the armature has it, in studs, Y up", bones and np.abs(rest[:, :3, 3] - heads).max() < 2e-3,
      np.abs(rest[:, :3, 3] - heads).max() if bones else None)
check("the rest places are rigid frames (a CFrame has no scale)", bones and np.abs(rest[:, :3, :3] @ rest[:, :3, :3].transpose(0, 2, 1)
                                                                               - np.eye(3)).max() < 1e-4)

# -- the skin ---------------------------------------------------------------------------------------
body = [p for p in pieces if p["name"].startswith("Tube")]
carried = [p for p in pieces if p["name"].startswith("Lamp")]
check("a mesh over the triangle limit is cut, and every piece is skinned to the same skeleton", len(body) == 2
      and all(p.get("skin", {}).get("rig") == "Spine" for p in body) and all(len(p["t"]) // 3 <= 10000 for p in body)
      and sum(len(p["t"]) // 3 for p in body) == 2 * RINGS * SIDES, [(p["name"], len(p.get("t", [])) // 3) for p in pieces])
check("skinned pieces stay put: bones move them, so they carry no track of their own", all("track" not in p for p in pieces))
check("nothing private or unserialisable travels", not any(k.startswith("_") for p in pieces for k in p))


def skin_of(piece):
    """(vertices in the part, bone index per influence into the skeleton, weights 0..1, each bone's rest place in the part)."""
    skin = piece["skin"]
    count = len(piece["v"]) // 3
    held = np.frombuffer(base64.b64decode(skin["vb"]), dtype=np.uint8).reshape(count, 4)
    shares = np.frombuffer(base64.b64decode(skin["vw"]), dtype=np.uint8).reshape(count, 4)
    own = np.array([names.index(n) for n in skin["names"]])
    return np.array(piece["v"]).reshape(count, 3), own[held], shares / 255.0, np.array([matrix(b) for b in skin["bind"]]), held


def part_of(piece):
    out = np.eye(4)
    out[:3, 3] = piece["center"]
    if "rot" in piece:
        out[:3, :3] = np.array(piece["rot"]).reshape(3, 3)
    return out


sums, most, parent_ok, bind_ok = [], 0, True, True
for piece in pieces:
    points, held, shares, bind, raw = skin_of(piece)
    raw_shares = np.frombuffer(base64.b64decode(piece["skin"]["vw"]), dtype=np.uint8).reshape(-1, 4)
    sums.append((int(raw_shares.sum(axis=1).min()), int(raw_shares.sum(axis=1).max())))
    most = max(most, int((raw_shares > 0).sum(axis=1).max()))
    local_names = piece["skin"]["names"]
    for at, above in enumerate(piece["skin"]["parents"]):
        wanted = wanted_parents[local_names[at]]
        parent_ok = parent_ok and (local_names[above - 1] if above else None) == wanted and above - 1 < at
    if piece in body:       # the bone's rest place seen from the part, put back into the world, is the skeleton's rest place
        back = part_of(piece) @ bind
        bind_ok = bind_ok and np.abs(back - rest[[names.index(n) for n in local_names]]).max() < 2e-3
check("every vertex follows at most four bones, with weights that add up to exactly 1", pieces and most <= 4
      and all(low == 255 and high == 255 for low, high in sums), (most, sums))
check("each piece names its bones with their parents, and where each rests in the piece's own space", parent_ok and bind_ok)
check("the lamp parented to a bone is skinned to that one bone", len(carried) == 1
      and carried[0]["skin"]["names"] == ["Base", "Low", "Mid", "Arm"]          # the chain above it comes along
      and set(skin_of(carried[0])[1][skin_of(carried[0])[2] > 0].tolist()) == {names.index("Arm")}
      and float(skin_of(carried[0])[2].max(axis=1).min()) == 1.0, carried[0]["skin"]["names"] if carried else pieces)


def skinned(piece, world):
    """The piece's vertices in the world, placed the way Roblox places a skinned vertex: the sum over its
    bones of weight x (the bone's place now) x (the inverse of the bone's rest place in the mesh)."""
    points, held, shares, bind, raw = skin_of(piece)
    own = [names.index(n) for n in piece["skin"]["names"]]
    carry = world[own] @ np.linalg.inv(bind)                    # per bone of the piece: mesh space -> world, now
    homogeneous = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    out = np.zeros((len(points), 3))
    for slot in range(4):
        moved = np.einsum("nij,nj->ni", carry[raw[:, slot]], homogeneous)[:, :3]
        out += moved * shares[:, slot][:, None]
    return out


def blender_points(obj):
    """Where Blender has the object's vertices now, modifiers and all, as Roblox points."""
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    data = evaluated.to_mesh()
    points = [evaluated.matrix_world @ v.co for v in data.vertices]
    evaluated.to_mesh_clear()
    return to_roblox(points)


def gap(sent, truth):
    """The farthest any sent vertex is from the nearest true one."""
    tree = kdtree.KDTree(len(truth))
    for index, point in enumerate(truth):
        tree.insert(point, index)
    tree.balance()
    return max(tree.find(point)[2] for point in sent)


# the rest pose: with every bone at rest, the sent mesh is the undeformed mesh
armature.pose_position = 'REST'
bpy.context.view_layer.update()
truth = blender_points(tube)
rest_gap = max(gap(skinned(p, rest), truth) for p in body) if body else 9
armature.pose_position = 'POSE'
bpy.context.view_layer.update()
check("skinned by hand with every bone at rest, the pieces are the mesh in its rest shape", rest_gap < 5e-3, rest_gap)


# -- the actions --------------------------------------------------------------------------------------
def track_of(name):
    first = next((j for j in moves if j.get("name") == name and j.get("at") == 0), None)
    if first is None:
        return None
    text = "".join(value for j in moves if j.get("name") == name for value in j["values"])
    data = base64.b64decode(text)
    count, shifting = len(names), first["moving"]
    stride = count * 8 + len(shifting) * 12
    rows = np.frombuffer(data, dtype=np.uint8).reshape(first["frames"], stride)
    turns = rows[:, :count * 8].copy().view("<i2").reshape(first["frames"], count, 4) / 32767.0
    shifts = np.zeros((first["frames"], count, 3))
    if shifting:
        shifts[:, shifting] = rows[:, count * 8:].copy().view("<f4").reshape(first["frames"], len(shifting), 3)
    return {"fps": first["fps"], "frames": first["frames"], "moving": shifting, "loop": first["loop"], "turns": turns, "shifts": shifts}


def posed(track, at):
    """Every bone's place in the world at one key, built the way Roblox builds it: the parent's place now,
    then the bone's rest offset from its parent, then its Transform."""
    world = np.zeros((len(names), 4, 4))
    for index, bone in enumerate(bones):
        x, y, z, w = track["turns"][at, index]
        pose = np.eye(4)
        pose[:3, :3] = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
        pose[:3, 3] = track["shifts"][at, index]
        above = bone["parent"] - 1
        if above < 0:
            world[index] = rest[index] @ pose
        else:
            world[index] = world[above] @ np.linalg.inv(rest[above]) @ rest[index] @ pose
    return world


tracks = {name: track_of(name) for name in ("Bend", "Hop")}
check("both actions of the rig travel, each with its keys, speed and loop mark", all(tracks.values())
      and tracks["Bend"]["frames"] == 21 and tracks["Hop"]["frames"] == 17 and abs(tracks["Bend"]["fps"] - 24) < 1e-3
      and tracks["Bend"]["loop"] is True and tracks["Hop"]["loop"] is False,
      {n: (t and (t["frames"], t["fps"], t["loop"])) for n, t in tracks.items()})
check("only a bone that really shifts carries an offset; the others travel as rotations", all(tracks.values())
      and tracks["Bend"]["moving"] == [] and tracks["Hop"]["moving"] == [names.index("Base")], {n: t and t["moving"] for n, t in tracks.items()})
closing = [j for j in moves if j.get("done")]
check("the last job of the rig says which action plays first: the one the armature has", len(closing) == 1 and moves[-1] is closing[0]
      and closing[0]["default"] == "Bend" and closing[0]["rig"] == "Spine" and closing[0]["thin"] == 1, closing)
check("the poses come after the model is built, a job per action", jobs.index(moves[0]) > jobs.index(builds[-1])
      and [j.get("name") for j in moves[:-1]] == ["Bend", "Hop"], [(j.get("kind"), j.get("name")) for j in jobs])

worst = {}
for name, action in (("Bend", bend), ("Hop", hop)):
    track = tracks[name]
    if not track:
        continue
    with motion.Showing(rig, action):
        for at in (0, 5, 8, 10, track["frames"] - 1):
            scene.frame_set(1 + at)
            world = posed(track, at)
            truth = blender_points(tube)
            worst[(name, at, "tube")] = max(gap(skinned(p, world), truth) for p in body)
            worst[(name, at, "lamp")] = gap(skinned(carried[0], world), blender_points(lamp))
tube_worst = max(v for k, v in worst.items() if k[2] == "tube")
lamp_worst = max(v for k, v in worst.items() if k[2] == "lamp")
print("GAPS rest", round(rest_gap, 5), "tube", round(tube_worst, 5), "lamp", round(lamp_worst, 5))
# the tube is seven studs tall and bends through several; a weight is one of 255 steps, and four bones are kept
check("skinned by hand at the sampled keys, the pieces are the mesh Blender shows in that frame", tube_worst < 0.02,
      {k: round(v, 4) for k, v in worst.items() if k[2] == "tube"})
check("the lamp rides its bone through both actions", lamp_worst < 5e-3, {k: round(v, 4) for k, v in worst.items() if k[2] == "lamp"})
moved = float(np.abs(posed(tracks["Bend"], 10)[:, :3, 3] - rest[:, :3, 3]).max()) if tracks["Bend"] else 0
check("the poses are real motion: at the middle of the bend the top is studs away from rest", moved > 1.0, moved)

# -- what the scene is left like ----------------------------------------------------------------------
check("the scene is left as it was: frame, action, pose and modifiers", scene.frame_current == 6 and rig.animation_data.action == bend
      and deform.show_viewport and armature.pose_position == 'POSE' and len(bpy.data.actions) == 2,
      (scene.frame_current, rig.animation_data.action, deform.show_viewport))

# -- the report ---------------------------------------------------------------------------------------
check("the report says what arrived skinned and to how many bones", "2 meshes arrived skinned" in report.get("rig", "")
      and "6 bones of Spine" in report["rig"] and "not_transferred" not in report, report.get("rig"))
check("the report names the actions and how to steer them", all(word in report.get("animations", "") for word in
                                                                ("Bend (21 keys)", "Hop (17 keys)", "Animation", "Enabled", "now 'Bend'",
                                                                 "KeyframeSequence")), report.get("animations"))
trimmed = sum(1 for v in mesh.vertices if sum(1 for g in v.groups if g.weight > 0) > 4)
check("vertices held by more than four bones are counted, not passed over in silence", trimmed > 0
      and f"{trimmed} vertices were held by more than 4 bones" in report.get("rig_not_carried", ""), (trimmed, report.get("rig_not_carried")))
check("a preview is still called a preview: bones do not make a mesh part of the game", "preview_only" in report and "rig_in_play" in report)
check("the motion jobs raise no false problems", "problems" not in report and "rig_problem" not in report, report.get("problems"))

# -- what Roblox's bones cannot do is said ----------------------------------------------------------
with motion.Showing(rig, hop):
    rig.pose.bones["High"].scale = (1.0, 1.6, 1.0)
    rig.pose.bones["High"].keyframe_insert("scale", frame=9)
    rig.pose.bones["High"].scale = (1.0, 1.0, 1.0)
    rig.pose.bones["High"].keyframe_insert("scale", frame=1)
tube.shape_key_add(name="Basis")
tube.shape_key_add(name="Bulge")
deform.use_deform_preserve_volume = True
before = len(studio.jobs)
report = drive(engines.send_roblox(["Spine"], "Creature", "Rig Place", at_origin=False))
lost = report.get("rig_not_carried", "")
check("bone scaling, shape keys and 'Preserve Volume' are reported as not carried", "bone scaling (stretch) on High" in lost
      and "shape keys" in lost and "Preserve Volume" in lost and "Tube" in lost, lost)
deform.use_deform_preserve_volume = False
tube.shape_key_clear()

# -- a rig with no action stands in its pose -----------------------------------------------------------
for action in list(bpy.data.actions):
    bpy.data.actions.remove(action)
rig.pose.bones["Mid"].rotation_euler = (math.radians(50), 0, 0)
rig.pose.bones["High"].scale = (1.0, 1.0, 1.0)
scene.frame_set(3)
bpy.context.view_layer.update()
before = len(studio.jobs)
report = drive(engines.send_roblox(["Spine"], "Creature", "Rig Place", at_origin=False))
jobs = studio.jobs[before:]
moves = [j for j in jobs if j.get("kind") == "motion"]
pieces = [m for j in jobs if j.get("kind") is None for m in (j.get("meshes") or [])]
body = [p for p in pieces if p["name"].startswith("Tube")]
still = track_of("Pose")
pose_gap = max(gap(skinned(p, posed(still, 0)), blender_points(tube)) for p in body) if still and body else 9
check("a rig with no action arrives standing in the pose it has here", still is not None and still["frames"] == 1 and pose_gap < 0.03
      and "pose" in report and "animations" not in report, (still and still["frames"], pose_gap, sorted(report)))

# -- a Studio that still runs the older plugin ---------------------------------------------------------
old = FakeStudio("Old Place", version="12")
old.start()
deadline = time.time() + 5
while len(link.sessions()) < 2 and time.time() < deadline:
    time.sleep(0.05)
report = drive(engines.send_roblox(["Spine"], "Creature", "Old Place", at_origin=False))
jobs = old.jobs[:]
check("an older plugin gets the unskinned send it understands, and the user is told to restart Studio",
      not any(j.get("kind") == "motion" for j in jobs) and not any("rigs" in j for j in jobs)
      and not any("skin" in m for j in jobs for m in (j.get("meshes") or []))
      and "older Unix Bridge" in report.get("not_transferred", "") and "rig" not in report, report.get("not_transferred"))

studio.alive = old.alive = False
link.stop()
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
