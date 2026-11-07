# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Headless tests for animation: seeing it, measuring it, and putting recorded motion on a rig.

* A recording in the CMU library's format must be read into the right skeleton and poses.
* Bones must be told apart by what they are, whatever the naming style.
* Recorded motion must land on a rig with other names, other proportions and another rest pose:
  the same pose, planted feet where the recording planted them, a loop that closes, a mirror
  that swaps sides, and control bones driven when the rig has IK.
* check_motion must report feet under the floor, limbs through each other, pops and a loop that
  does not close, and must pass a clean move.
No network: the recording is written by the test itself.
Run: blender -b --factory-startup --python tests/test_motion.py"""
import json, math, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from mathutils import Matrix, Quaternion, Vector
from unix import tools
from unix.tools import _mocap, _util, assets, registry, retarget
from unix.tools import motion as motion_tools

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
tools.load_all()
fails = []
scene = bpy.context.scene


def call(tool_name, **args):
    res = registry.call(tool_name, args)
    return res["isError"], res["content"][0]["text"], sum(1 for c in res["content"] if c["type"] == "image")


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:700]}"))
    if not cond:
        fails.append(label)


def near(a, b, tol):
    return (Vector(a) - Vector(b)).length <= tol


# -- a recording, written the way the CMU library writes them -----------------------------------
def unit(x, y, z):
    v = Vector((x, y, z)).normalized()
    return f"{v.x:.6f} {v.y:.6f} {v.z:.6f}"


def bone(index, name, direction, length, dof="", axis="0 0 0"):
    return (f"  begin\n     id {index}\n     name {name}\n     direction {direction}\n     length {length}\n"
            f"     axis {axis}  XYZ\n" + (f"    dof {dof}\n    limits (-180.0 180.0)\n" if dof else "") + "  end\n")


BONES = [("lhipjoint", unit(0.6, -0.7, 0.3), 2.5, ""), ("lfemur", unit(0, -1, 0), 7.5, "rx ry rz"), ("ltibia", unit(0, -1, 0), 7.5, "rx"),
         ("lfoot", unit(0, -0.4, 0.9), 2.2, "rx rz"), ("ltoes", unit(0, 0, 1), 1.0, "rx"),
         ("rhipjoint", unit(-0.6, -0.7, 0.3), 2.5, ""), ("rfemur", unit(0, -1, 0), 7.5, "rx ry rz"), ("rtibia", unit(0, -1, 0), 7.5, "rx"),
         ("rfoot", unit(0, -0.4, 0.9), 2.2, "rx rz"), ("rtoes", unit(0, 0, 1), 1.0, "rx"),
         ("lowerback", unit(0, 1, 0), 2.0, "rx ry rz"), ("upperback", unit(0, 1, 0), 2.0, "rx ry rz"), ("thorax", unit(0, 1, 0), 2.0, "rx ry rz"),
         ("lowerneck", unit(0, 1, 0), 1.5, "rx ry rz"), ("upperneck", unit(0, 1, 0), 1.5, "rx ry rz"), ("head", unit(0, 1, 0), 1.6, "rx ry rz"),
         ("lclavicle", unit(1, 0.1, 0), 3.2, "ry rz"), ("lhumerus", unit(1, 0, 0), 5.0, "rx ry rz"), ("lradius", unit(1, 0, 0), 4.0, "rx"),
         ("lwrist", unit(1, 0, 0), 1.0, "ry"), ("lhand", unit(1, 0, 0), 1.0, "rx rz"),
         ("rclavicle", unit(-1, 0.1, 0), 3.2, "ry rz"), ("rhumerus", unit(-1, 0, 0), 5.0, "rx ry rz"), ("rradius", unit(-1, 0, 0), 4.0, "rx"),
         ("rwrist", unit(-1, 0, 0), 1.0, "ry"), ("rhand", unit(-1, 0, 0), 1.0, "rx rz")]
AXIS = {"lhumerus": "0 0 90"}
ASF = (":version 1.10\n:name VICON\n:units\n  mass 1.0\n  length 0.45\n  angle deg\n:documentation\n   a test skeleton\n"
       ":root\n   order TX TY TZ RX RY RZ\n   axis XYZ\n   position 0 0 0\n   orientation 0 0 0\n:bonedata\n"
       + "".join(bone(i + 1, n, d, l, dof, AXIS.get(n, "0 0 0")) for i, (n, d, l, dof) in enumerate(BONES))
       + ":hierarchy\n  begin\n    root lhipjoint rhipjoint lowerback\n    lhipjoint lfemur\n    lfemur ltibia\n    ltibia lfoot\n"
         "    lfoot ltoes\n    rhipjoint rfemur\n    rfemur rtibia\n    rtibia rfoot\n    rfoot rtoes\n    lowerback upperback\n"
         "    upperback thorax\n    thorax lowerneck lclavicle rclavicle\n    lowerneck upperneck\n    upperneck head\n"
         "    lclavicle lhumerus\n    lhumerus lradius\n    lradius lwrist\n    lwrist lhand\n    rclavicle rhumerus\n"
         "    rhumerus rradius\n    rradius rwrist\n    rwrist rhand\n  end\n")
DOF = {name: len(dof.split()) for name, _d, _l, dof in BONES}
ROOT_UP = 17.698                                         # toe joints on the ground


def frame_lines(number, values):
    out = [str(number), "root " + " ".join(f"{v:.5f}" for v in values.get("root", (0, ROOT_UP, 0, 0, 0, 0)))]
    for name, _d, _l, dof in BONES:
        if dof:
            given = list(values.get(name, ())) + [0.0] * DOF[name]
            out.append(name + " " + " ".join(f"{v:.5f}" for v in given[:DOF[name]]))
    return out


def amc(frames):
    lines = ["#!OML:ASF test", ":FULLY-SPECIFIED", ":DEGREES"]
    for number, values in enumerate(frames, start=1):
        lines += frame_lines(number, values)
    return "\n".join(lines) + "\n"


SPEED = 16.0                                             # file units a second (0.9 m/s)


def walk(seconds=3.0, turned=0.0):
    """A stride with real contact: each foot stays put on the ground for half of every second while the
    body passes over it at one speed, then swings forward. Legs are solved so the ankles are where that puts them."""
    frames = []
    reach = SPEED / 4.0                                  # how far ahead of the hip a foot lands
    for index in range(int(seconds * 120) + 1):
        t = index / 120.0
        values = {"root": (SPEED * t * math.sin(math.radians(turned)), ROOT_UP - 0.8, SPEED * t * math.cos(math.radians(turned)),
                           0.0, turned, 0.0)}
        for side, shift in (("l", 0.0), ("r", 0.5)):
            p = (t + shift) % 1.0
            if p < 0.5:
                ahead, lift = reach - 2 * reach * (p / 0.5), 0.0
            else:
                u = (p - 0.5) / 0.5
                ahead, lift = -reach + 2 * reach * (1 - math.cos(math.pi * u)) / 2, 1.2 * math.sin(math.pi * u)
            down = 14.2 - lift                            # the hip joint is this far above the ankle
            far = math.hypot(down, ahead)
            aim, fold = math.atan2(-ahead, down), math.acos(far / 15.0)
            thigh, knee = math.degrees(aim - fold), math.degrees(2 * fold)
            values[side + "femur"] = (thigh, 0, 0)
            values[side + "tibia"] = (knee,)
            values[side + "foot"] = (-(thigh + knee), 0)
        frames.append(values)
    return amc(frames)


S = 0.0254 / 0.45
clip = _mocap.read_acclaim(ASF, amc([{}, {"lfemur": (-90, 0, 0)}, {"lhumerus": (90, 0, 0)}, {"root": (0, ROOT_UP, 0, 0, 90, 0)}]))
rest = clip.rest()
check("the recording is read: bones, frames, frame rate", len(clip.names) == 27 and len(clip.frames) == 4 and clip.fps == 120.0
      and clip.parent["lfemur"] == "lhipjoint" and clip.parent["lhumerus"] == "lclavicle", (clip.names, len(clip.frames)))
hip = Vector((0.6, -0.7, 0.3)).normalized() * 2.5 * S
check("rest pose: left hip on +X, ankle straight below it, in metres with Z up",
      near(rest["lfemur"][1], (hip.x, -hip.z, hip.y), 1e-4) and near(rest["lfoot"][1] - rest["lfemur"][1], (0, 0, -15.0 * S), 1e-4),
      (tuple(rest["lfemur"][1]), tuple(rest["lfoot"][1])))
forward = clip.pose(1)
check("a thigh turned 90 degrees forward points along -Y (the body faces -Y)",
      near((forward["lfemur"][2] - forward["lfemur"][1]).normalized(), (0, -1, 0), 1e-4), tuple(forward["lfemur"][2] - forward["lfemur"][1]))
check("a bone's own axes are used: the upper arm turns about its tilted X axis",
      near((clip.pose(2)["lhumerus"][2] - clip.pose(2)["lhumerus"][1]).normalized(), (0, 1, 0), 1e-4),
      tuple(clip.pose(2)["lhumerus"][2] - clip.pose(2)["lhumerus"][1]))
turned = clip.pose(3)
check("turning the root a quarter turn turns the whole body with it", near(turned["lfemur"][1], (hip.z, hip.x, ROOT_UP * S + hip.y), 1e-4)
      and abs(turned["head"][0].angle - math.pi / 2) < 1e-4, tuple(turned["lfemur"][1]))
half = clip.sample(0.5 / 120.0)
check("between two frames the pose is blended", abs(half["lfemur"][0].angle - math.pi / 4) < 1e-3, half["lfemur"][0].angle)
try:
    _mocap.read_acclaim("hello", "world")
    check("a file that is not a recording is refused", False)
except ValueError:
    check("a file that is not a recording is refused", True)

# -- which bone is what, in any naming style ------------------------------------------------------
found = _mocap.roles(clip.names, clip.parent)
check("the recording's bones are named by what they are", found.get("hips") == "root" and found.get("thigh.L") == "lfemur"
      and found.get("shin.R") == "rtibia" and found.get("foot.L") == "lfoot" and found.get("toe.R") == "rtoes"
      and found.get("upper_arm.L") == "lhumerus" and found.get("forearm.R") == "rradius" and found.get("hand.L") == "lwrist"
      and found.get("spine") == ["lowerback", "upperback", "thorax"] and found.get("neck") == ["lowerneck", "upperneck"]
      and found.get("head") == "head" and found.get("clavicle.R") == "rclavicle", found)
STYLES = {
    "mixamo": ["mixamorig:Hips", "mixamorig:Spine", "mixamorig:Spine1", "mixamorig:Spine2", "mixamorig:Neck", "mixamorig:Head",
               "mixamorig:LeftShoulder", "mixamorig:LeftArm", "mixamorig:LeftForeArm", "mixamorig:LeftHand", "mixamorig:LeftHandIndex1",
               "mixamorig:LeftUpLeg", "mixamorig:LeftLeg", "mixamorig:LeftFoot", "mixamorig:LeftToeBase"],
    "blender": ["Root", "pelvis", "spine.001", "chest", "neck", "head", "shoulder.L", "upper_arm.L", "forearm.L", "hand.L",
                "f_index.01.L", "thigh.L", "shin.L", "foot.L", "toe.L"],
    "biped": ["Bip01", "Bip01 Pelvis", "Bip01 Spine", "Bip01 Spine1", "Bip01 Neck", "Bip01 Head", "Bip01 L Clavicle",
              "Bip01 L UpperArm", "Bip01 L Forearm", "Bip01 L Hand", "Bip01 L Finger0", "Bip01 L Thigh", "Bip01 L Calf",
              "Bip01 L Foot", "Bip01 L Toe0"],
}
CHAINS = {"mixamo": [-1, 0, 1, 2, 3, 4, 3, 6, 7, 8, 9, 0, 11, 12, 13], "blender": [-1, 0, 1, 2, 3, 4, 3, 6, 7, 8, 9, 1, 11, 12, 13],
          "biped": [-1, 0, 1, 2, 3, 4, 3, 6, 7, 8, 9, 1, 11, 12, 13]}
for style, names in STYLES.items():
    both = names + [n.replace("Left", "Right").replace(".L", ".R").replace(" L ", " R ") for n in names if n != names[0]
                    and ("Left" in n or ".L" in n or " L " in n)]
    parent = {}
    for index, name in enumerate(names):
        parent[name] = names[CHAINS[style][index]] if CHAINS[style][index] >= 0 else None
        twin = name.replace("Left", "Right").replace(".L", ".R").replace(" L ", " R ")
        if twin != name:
            up = parent[name]
            parent[twin] = up.replace("Left", "Right").replace(".L", ".R").replace(" L ", " R ") if up else None
    got = _mocap.roles(both, parent)
    sided = all(f"{role}.{side}" in got for role in ("clavicle", "upper_arm", "forearm", "hand", "thigh", "shin", "foot", "toe") for side in "LR")
    check(f"{style} names are read: hips, spine, head and every limb bone on both sides",
          sided and "hips" in got and "head" in got and got.get("spine") and "Finger" not in str(got) and "index" not in str(got).lower(), got)

# -- a rig with its own names, proportions and rest pose (arms down, bent knees) --------------------
bpy.data.objects.remove(bpy.data.objects["Cube"])
SPEC = [("Root", (0, 0, 0), (0, 0.2, 0), None, False), ("Pelvis", (0, 0, 1.0), (0, 0, 1.15), "Root", True),
        ("Spine", (0, 0, 1.15), (0, 0, 1.35), "Pelvis", True), ("Chest", (0, 0, 1.35), (0, 0, 1.55), "Spine", True),
        ("Neck", (0, 0, 1.55), (0, 0, 1.65), "Chest", True), ("Head", (0, 0, 1.65), (0, 0, 1.85), "Neck", True)]
for side, x in (("L", 1.0), ("R", -1.0)):
    SPEC += [(f"Clavicle.{side}", (0.03 * x, 0, 1.5), (0.18 * x, 0, 1.5), "Chest", True),
             (f"UpperArm.{side}", (0.18 * x, 0, 1.5), (0.38 * x, 0, 1.28), f"Clavicle.{side}", True),
             (f"Forearm.{side}", (0.38 * x, 0, 1.28), (0.55 * x, -0.01, 1.08), f"UpperArm.{side}", True),
             (f"Hand.{side}", (0.55 * x, -0.01, 1.08), (0.6 * x, -0.01, 1.0), f"Forearm.{side}", True),
             (f"Thigh.{side}", (0.1 * x, 0, 0.95), (0.1 * x, -0.02, 0.5), "Pelvis", True),
             (f"Shin.{side}", (0.1 * x, -0.02, 0.5), (0.1 * x, 0, 0.08), f"Thigh.{side}", True),
             (f"Foot.{side}", (0.1 * x, 0, 0.08), (0.1 * x, -0.14, 0.02), f"Shin.{side}", True),
             (f"Toe.{side}", (0.1 * x, -0.14, 0.02), (0.1 * x, -0.2, 0.02), f"Foot.{side}", True)]


def build_rig(name, spec, with_mesh=True):
    arm = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, arm)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode='EDIT')
    for bone_name, head, tail, parent, deform in spec:
        eb = arm.edit_bones.new(bone_name)
        eb.head, eb.tail, eb.use_deform = head, tail, deform
        eb.parent = arm.edit_bones[parent] if parent else None
    bpy.ops.object.mode_set(mode='OBJECT')
    if not with_mesh:
        return rig, None
    verts, faces, owners = [], [], []
    for bone_name, head, tail, _parent, deform in spec:      # a box around every bone, following it rigidly
        if not deform:
            continue
        head, tail = Vector(head), Vector(tail)
        along = (tail - head).normalized()
        side = along.orthogonal().normalized() * 0.045
        other = along.cross(side).normalized() * 0.045
        base = len(verts)
        for end in (head, tail):
            verts += [end + side + other, end - side + other, end - side - other, end + side - other]
        faces += [(base, base + 1, base + 2, base + 3), (base + 4, base + 7, base + 6, base + 5)]
        faces += [(base + i, base + 4 + i, base + 4 + (i + 1) % 4, base + (i + 1) % 4) for i in range(4)]
        owners += [bone_name] * 8
    mesh = bpy.data.meshes.new(name + "_Body")
    mesh.from_pydata([tuple(v) for v in verts], [], faces)
    body = bpy.data.objects.new(name + "_Body", mesh)
    scene.collection.objects.link(body)
    for index, owner in enumerate(owners):
        (body.vertex_groups.get(owner) or body.vertex_groups.new(name=owner)).add([index], 1.0, 'REPLACE')
    body.modifiers.new("Armature", 'ARMATURE').object = rig
    return rig, body


rig, body = build_rig("Rig", SPEC)
my_leg = (Vector((0.1, -0.02, 0.5)) - Vector((0.1, 0, 0.95))).length + (Vector((0.1, 0, 0.08)) - Vector((0.1, -0.02, 0.5))).length
ratio = my_leg / (15.0 * S)


def posed(obj, frame):
    scene.frame_set(frame)
    ev = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    return {b.name: (ev.matrix_world @ b.head, ev.matrix_world @ b.tail) for b in ev.pose.bones}


def transfer(recording, **args):
    """What retarget_motion does once the recording is in hand (the tool itself fetches it by id)."""
    options = dict(name="", start_s=None, end_s=None, loop=False, in_place=False, mirror=False, bone_map=None,
                   blend_from="", blend_to="", arms_out_deg=0.0, min_cycle_s=None, turn_deg=0.0)
    options.update(args)
    target = options.pop("target", "Rig")
    return retarget._transfer(target, recording, "test clip", options["name"], options["start_s"], options["end_s"],
                              options["loop"], options["in_place"], options["mirror"], options["bone_map"],
                              options["blend_from"], options["blend_to"], options["arms_out_deg"], options["min_cycle_s"],
                              options["turn_deg"])


stride = _mocap.read_acclaim(ASF, walk())
out = transfer(stride, name="Walk")
check("the move is written as one action on the rig, kept on file", out["action"] == "Walk" and rig.animation_data.action.name == "Walk"
      and bpy.data.actions["Walk"].use_fake_user and out["frames"] == [1, 73] and out["bones_driven"] >= 20, out)
check("the reply says how the recording moves and how big the rig is beside it", "forward" in out["recorded_move"]
      and abs(out["size_ratio"] - ratio) < 0.01 and out["feet_off_target_cm"] < 0.5, out)
first, last = posed(rig, 1), posed(rig, 73)
went = (last["Pelvis"][0] - first["Pelvis"][0])
check("the rig covers the recording's distance, scaled to its legs, the way it faces",
      abs(went.y + ratio * 3 * SPEED * S) < 0.01 and abs(went.x) < 0.01, tuple(went))
arm = (first["UpperArm.L"][1] - first["UpperArm.L"][0]).normalized()
check("a rig resting arms-down takes the recording's pose, not its own rest pose plus a turn", near(arm, (1, 0, 0), 0.02)
      and near((first["UpperArm.R"][1] - first["UpperArm.R"][0]).normalized(), (-1, 0, 0), 0.02), tuple(arm))
check("the left foot leads when the recording's does", first["Foot.L"][0].y < first["Foot.R"][0].y - 0.3
      and first["Shin.L"][0].y < first["Thigh.L"][0].y - 0.05, (tuple(first["Foot.L"][0]), tuple(first["Foot.R"][0])))
planted = [posed(rig, f)["Foot.L"][0] for f in range(1, 12)]   # the first half second: the left foot is down
check("a foot that is planted in the recording does not move on the rig",
      max((p - planted[0]).length for p in planted) < 0.004, max((p - planted[0]).length for p in planted))
worst = 0.0
for frame in (1, 7, 13, 19, 31, 50, 73):                   # ankles: where the recording's are, scaled about the floor
    there = stride.sample((frame - 1) / 24.0)
    mid = (there["lfemur"][1] + there["rfemur"][1]) / 2
    pose = posed(rig, frame)
    mine = (pose["Thigh.L"][0] + pose["Thigh.R"][0]) / 2
    for theirs, own in (("lfoot", "Foot.L"), ("rfoot", "Foot.R")):
        want = (there[theirs][1] - mid) * ratio
        got = pose[own][0] - mine
        worst = max(worst, (Vector((want.x, want.y)) - Vector((got.x, got.y))).length, want.z - got.z)
check("each ankle sits where the recording's ankle is, relative to the hips", worst < 0.012, worst)
low = min(min(posed(rig, f)[b][1].z for b in ("Toe.L", "Toe.R", "Foot.L", "Foot.R")) for f in range(1, 74, 3))
check("heels and toes stay on or above the floor", low > 0.02 - 2e-3, low)

out = transfer(stride, name="Walk_Loop", loop=True, in_place=True)
check("a loop is cut at one full stride", out["loops"] and out["cycle"]["seconds"] == 1.0 and out["cycle"]["match"] == "good"
      and bpy.data.actions["Walk_Loop"].use_cyclic, out)
a, b = posed(rig, 1), posed(rig, out["frames"][1])
check("the loop closes: the last frame is the first again", max((a[n][0] - b[n][0]).length for n in a) < 2e-3,
      max((a[n][0] - b[n][0]).length for n in a))
drift = max((posed(rig, f)["Pelvis"][0] - a["Pelvis"][0]).length for f in range(1, out["frames"][1] + 1, 4))
check("on the spot: the hips stay where they are", drift < 0.06, drift)
track = [posed(rig, f)["Foot.L"][0] for f in range(1, out["frames"][1] + 1)]
floor_z = min(p.z for p in track)
steps = [b.y - a.y for a, b in zip(track, track[1:]) if a.z < floor_z + 0.002 and b.z < floor_z + 0.002]
check("on the spot, a planted foot is carried back at one speed", len(steps) >= 8 and min(steps) > 0.02
      and max(steps) - min(steps) < 0.004 and abs(sum(steps) / len(steps) - ratio * SPEED * S / 24) < 0.003, steps)

out = transfer(stride, name="Walk_Mirror", mirror=True)
first = posed(rig, 1)
check("mirror swaps the sides", first["Foot.R"][0].y < first["Foot.L"][0].y - 0.3, (tuple(first["Foot.L"][0]), tuple(first["Foot.R"][0])))

sideways = _mocap.read_acclaim(ASF, walk(turned=90.0))      # the same stride, recorded facing another way
out = transfer(sideways, name="Walk_Turned")
went = posed(rig, 73)["Pelvis"][0] - posed(rig, 1)["Pelvis"][0]
check("the recording's first frame is turned to face the way the rig faces", abs(went.y + ratio * 3 * SPEED * S) < 0.02
      and abs(went.x) < 0.02, tuple(went))

out = transfer(stride, name="Walk_To_Left", turn_deg=90.0)
went = posed(rig, 73)["Pelvis"][0] - posed(rig, 1)["Pelvis"][0]
face = posed(rig, 37)
facing = (face["Thigh.L"][0] - face["Thigh.R"][0]).cross(Vector((0, 0, 1)))
check("turn_deg turns the whole move: the body faces and travels to its left", went.x > ratio * 3 * SPEED * S - 0.03
      and abs(went.y) < 0.03 and facing.normalized().x > 0.9 and "to its left" in out["recorded_move"]
      and "name_does_not_match" not in out, (tuple(went), tuple(facing), out))
rig.pose.bones["Root"].rotation_mode = 'QUATERNION'
rig.pose.bones["Root"].rotation_quaternion = Quaternion((0, 0, 1), 1.0)   # a turn some other action left on a bone
scene.frame_set(5)
check("every bone is keyed, so nothing is inherited from a pose another action left",
      rig.pose.bones["Root"].rotation_quaternion.angle < 1e-4 and rig.pose.bones["Spine"].scale == Vector((1, 1, 1)),
      tuple(rig.pose.bones["Root"].rotation_quaternion))

out = transfer(stride, name="Walk_Back")
check("a name that says the wrong direction is pointed out", "name_does_not_match" in out and "forward" in out["name_does_not_match"], out)
out = transfer(stride, name="Walk_Forward")
check("a name that matches the direction is not", "name_does_not_match" not in out, out)


def drifting(seconds=3.0):
    """The same stride on a capture floor that rises 4 cm from one end of the take to the other."""
    text = walk(seconds)
    out, frame = [], 0
    for line in text.splitlines():
        if line.isdigit():
            frame = int(line)
        elif line.startswith("root "):
            values = [float(v) for v in line.split()[1:]]
            values[1] += (4.0 / 100.0 / S) * (frame - 1) / (seconds * 120)
            line = "root " + " ".join(f"{v:.5f}" for v in values)
        out.append(line)
    return "\n".join(out) + "\n"


out = transfer(_mocap.read_acclaim(ASF, drifting()), name="Walk_Drift")
lows = [min(posed(rig, f)[b][0].z for b in ("Foot.L", "Foot.R")) for f in (1, 2, 3, 70, 71, 72, 73)]
check("a capture floor that is not level is followed: the feet land on the rig's floor at both ends of the take",
      max(lows) - min(lows) < 0.012, lows)

stand = amc([{"root": (0.0, ROOT_UP, 0.0, 0.0, 0.0, 0.0), "lowerback": (2.0 * math.sin(math.tau * i / 360.0), 0, 0),
              "lhumerus": (0, 0, 3.0 * math.sin(math.tau * i / 240.0))} for i in range(1200)])
out = transfer(_mocap.read_acclaim(ASF, stand), name="Idle_Slow", loop=True, in_place=True)
check("a slow move gets a long loop, not a twitch", out["cycle"]["seconds"] >= 2.0, out.get("cycle"))
out = transfer(_mocap.read_acclaim(ASF, stand), name="Idle_Short", loop=True, in_place=True, min_cycle_s=0.5)
check("min_cycle_s sets the shortest loop", 0.5 <= out["cycle"]["seconds"] < 2.0, out.get("cycle"))

out = transfer(stride, name="Walk_Wide", arms_out_deg=30.0)
arm = (posed(rig, 1)["UpperArm.L"][1] - posed(rig, 1)["UpperArm.L"][0]).normalized()
check("arms_out_deg lifts the arms away from the body", arm.z > 0.4 and arm.x > 0.7, tuple(arm))

out = transfer(stride, name="Start", end_s=1.0, in_place=True, blend_to="Walk_Loop")
a, b = posed(rig, out["frames"][1]), None
bpy.data.objects["Rig"].animation_data.action = bpy.data.actions["Walk_Loop"]
b = posed(rig, 1)
check("blend_to ends the move on the first pose of another action", max((a[n][0] - b[n][0]).length for n in a) < 5e-3,
      max((a[n][0] - b[n][0]).length for n in a))

err, text, _ = call("retarget_motion", target="Rig")
check("retarget_motion needs a clip or a source", err and "clip" in text, text)
err, text, _ = call("retarget_motion", target="Rig", clip="walk")
check("a clip id that is not one is refused before anything is fetched", err and "motion_search" in text, text)
assets.settings["enabled"] = False
err, text, _ = call("motion_search", query="run")
check("with online lookups off, motion_search says so", err and "turned off" in text, text)
err, text, _ = call("retarget_motion", target="Rig", clip="999_99")
check("with online lookups off, nothing is fetched", err and "turned off" in text, text)
assets.settings["enabled"] = True
page = ('<tr bgcolor="#909090"><td colspan="3"><center>Subject #6 (dribble, shoot basketball) <a href="/search.php?subjectnumber=6">file '
        'index</a> </center></td><td><a href="/subjects/06/06.asf">asf</a></td></tr><tr><td>Image</td><td>Trial #</td></tr>'
        '<tr bgcolor="#FF0000"><td></td><td>6</td><td>basketball - backward dribble</td><td><a href="/subjects/06/06_06.tvd">tvd</a></td>'
        '<td><a href="/subjects/06/06_06.amc">amc</a></td><td><a href="/subjects/06/06_06.avi">Animated</a></td><td>120</td><td>'
        '<a href="badtrial.php?subjectnumber=6&amp;motion=6">Feedback</a></td></tr><tr bgcolor="#909090"><td colspan="3"><center>'
        'Subject #9 (run) <a href="x">file index</a></center></td></tr><tr bgcolor="#FF0000"><td></td><td>12</td><td>navigate - walk '
        'forward, backward, sideways</td><td><a href="/subjects/09/09_12.amc">amc</a></td><td>60</td><td>Feedback</td></tr>')
listed = retarget.parse_search(page)
check("a library search page is read into clips", listed == [
    {"clip": "06_06", "motion": "basketball - backward dribble", "recorded": "dribble, shoot basketball", "fps": 120},
    {"clip": "09_12", "motion": "navigate - walk forward, backward, sideways", "recorded": "run", "fps": 60}], listed)

# -- another armature as the source: other names, other size, facing the other way ------------------
MOCAP = [("Hips", (0, 0, 1.2), (0, 0, 1.4), None, True), ("Spine", (0, 0, 1.4), (0, 0, 1.8), "Hips", True),
         ("Head", (0, 0, 1.8), (0, 0, 2.1), "Spine", True)]
for word, x in (("Left", -1.0), ("Right", 1.0)):           # facing +Y: its left is on -X
    MOCAP += [(f"{word}UpLeg", (0.12 * x, 0, 1.15), (0.12 * x, 0, 0.62), "Hips", True),
              (f"{word}Leg", (0.12 * x, 0, 0.62), (0.12 * x, 0, 0.1), f"{word}UpLeg", True),
              (f"{word}Foot", (0.12 * x, 0, 0.1), (0.12 * x, 0.18, 0.02), f"{word}Leg", True),
              (f"{word}Arm", (0.2 * x, 0, 1.75), (0.5 * x, 0, 1.75), "Spine", True)]
source, _none = build_rig("Mocap", MOCAP, with_mesh=False)
swing = source.pose.bones["LeftUpLeg"]
swing.rotation_mode = 'QUATERNION'
for frame, degrees in ((1, 0.0), (13, 50.0), (25, 0.0)):   # its left thigh swings toward its own front (+Y)
    swing.rotation_quaternion = (source.data.bones["LeftUpLeg"].matrix_local.to_quaternion().inverted()
                                 @ Quaternion((1, 0, 0), math.radians(degrees)) @ source.data.bones["LeftUpLeg"].matrix_local.to_quaternion())
    swing.keyframe_insert("rotation_quaternion", frame=frame)
theirs = posed(source, 13)
check("(the source's left knee really is in front of it)", theirs["LeftLeg"][0].y > 0.3, tuple(theirs["LeftLeg"][0]))
err, text, _ = call("retarget_motion", target="Rig", source="Mocap", name="From_Rig")
data = json.loads(text) if not err else {}
mid = posed(rig, 13)
check("motion is taken from another armature in the scene, matched by what the bones are", not err and data["frames"] == [1, 25]
      and mid["Shin.L"][0].y < mid["Thigh.L"][0].y - 0.15 and abs(mid["Shin.R"][0].y - mid["Thigh.R"][0].y) < 0.08, text)
check("the source rig is left as it was", source.animation_data.action is not None and scene.frame_current == 13
      and len(bpy.data.objects) == 5, [o.name for o in bpy.data.objects])
err, text, _ = call("retarget_motion", target="Rig", source="Mocap", bone_map={"Thigh.L": "Nope"})
check("bone_map names are checked", err and "Nope" in text, text)

# -- a BVH file the user brings (Mixamo, Rokoko, any library) -----------------------------------------
BVH = """HIERARCHY
ROOT Hips
{
  OFFSET 0 0 0
  CHANNELS 6 Xposition Yposition Zposition Zrotation Xrotation Yrotation
  JOINT LeftUpLeg
  {
    OFFSET 10 -5 0
    CHANNELS 3 Zrotation Xrotation Yrotation
    JOINT LeftLeg
    {
      OFFSET 0 -45 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      JOINT LeftFoot
      {
        OFFSET 0 -45 0
        CHANNELS 3 Zrotation Xrotation Yrotation
        End Site
        {
          OFFSET 0 -5 15
        }
      }
    }
  }
  JOINT RightUpLeg
  {
    OFFSET -10 -5 0
    CHANNELS 3 Zrotation Xrotation Yrotation
    JOINT RightLeg
    {
      OFFSET 0 -45 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      JOINT RightFoot
      {
        OFFSET 0 -45 0
        CHANNELS 3 Zrotation Xrotation Yrotation
        End Site
        {
          OFFSET 0 -5 15
        }
      }
    }
  }
  JOINT Spine
  {
    OFFSET 0 15 0
    CHANNELS 3 Zrotation Xrotation Yrotation
    JOINT Head
    {
      OFFSET 0 45 0
      CHANNELS 3 Zrotation Xrotation Yrotation
      End Site
      {
        OFFSET 0 20 0
      }
    }
  }
}
MOTION
Frames: 25
Frame Time: 0.0416667
"""
for index in range(25):
    swing = -40.0 * math.sin(math.pi * index / 24)          # the left thigh swings forward and back
    BVH += " ".join(["0", "100", "0", "0", "0", "0", "0", f"{swing:.3f}", "0"] + ["0"] * 21) + "\n"
bvh_path = os.path.join(_util._base_dir, "swing.bvh")
with open(bvh_path, "w") as handle:
    handle.write(BVH)
before = set(bpy.data.objects.keys())
err, text, _ = call("import_file", path=bvh_path)
brought = [n for n in bpy.data.objects.keys() if n not in before]
check("a .bvh file comes in as an armature with its motion", not err and len(brought) == 1
      and bpy.data.objects[brought[0]].type == 'ARMATURE' and bpy.data.objects[brought[0]].animation_data.action is not None, text)
if brought:
    err, text, _ = call("retarget_motion", target="Rig", source=brought[0], name="From_Bvh")
    data = json.loads(text) if not err else {}
    peak = posed(rig, 13)
    check("and its motion goes onto the rig", not err and data.get("bones_driven", 0) >= 7
          and peak["Shin.L"][0].y < peak["Thigh.L"][0].y - 0.1, text)
    imported = bpy.data.objects[brought[0]].animation_data.action
    bpy.data.objects.remove(bpy.data.objects[brought[0]])
    check("an action made for another skeleton is not counted as the rig's", imported.name not in motion_tools.actions_for(rig))
    bpy.data.actions.remove(imported)

# -- a rig driven by IK controls ---------------------------------------------------------------
IK_SPEC = SPEC + [(f"{n}.{side}", h, t, "Root", False) for side, x in (("L", 1.0), ("R", -1.0)) for n, h, t in
                  (("Foot_IK", (0.1 * x, 0, 0.08), (0.1 * x, -0.14, 0.02)), ("Knee_Pole", (0.1 * x, -0.6, 0.5), (0.1 * x, -0.7, 0.5)))]
ik_rig, _ik_body = build_rig("IkRig", IK_SPEC)
for side in "LR":
    shin = ik_rig.pose.bones[f"Shin.{side}"]
    ik = shin.constraints.new('IK')
    ik.target, ik.subtarget, ik.chain_count = ik_rig, f"Foot_IK.{side}", 2
    ik.pole_target, ik.pole_subtarget = ik_rig, f"Knee_Pole.{side}"
    rest_thigh, errors = ik_rig.data.bones[f"Thigh.{side}"].matrix_local, {}
    for deg in (-90, 0, 90, 180):
        ik.pole_angle = math.radians(deg)
        got = ik_rig.evaluated_get(bpy.context.evaluated_depsgraph_get()).pose.bones[f"Thigh.{side}"].matrix
        errors[deg] = sum(row.length for row in (got - rest_thigh))
    ik.pole_angle = math.radians(min(errors, key=errors.get))
    copy = ik_rig.pose.bones[f"Foot.{side}"].constraints.new('COPY_ROTATION')
    copy.target, copy.subtarget, copy.target_space, copy.owner_space = ik_rig, f"Foot_IK.{side}", 'WORLD', 'WORLD'
out = transfer(stride, target="IkRig", name="Ik_Walk")
first = posed(ik_rig, 1)
check("a rig with IK legs is driven through its controls: the feet land where they should", out["feet_off_target_cm"] < 1.5
      and "warning" not in out and first["Foot.L"][0].y < first["Foot.R"][0].y - 0.3
      and first["Shin.L"][0].y < first["Thigh.L"][0].y - 0.05, out)
keyed = {fc.data_path for fc in bpy.data.actions["Ik_Walk"].fcurves}
check("the IK targets and poles are keyed", 'pose.bones["Foot_IK.L"].location' in keyed and 'pose.bones["Knee_Pole.R"].location' in keyed,
      sorted(keyed)[:12])
flips = []
for frame in range(1, 74):                                  # the knees bend forward on every frame, straight leg or not
    pose = posed(ik_rig, frame)
    for side in "LR":
        hip, knee, ankle = pose[f"Thigh.{side}"][0], pose[f"Shin.{side}"][0], pose[f"Foot.{side}"][0]
        line = (ankle - hip).normalized()
        out = (knee - hip) - line * (knee - hip).dot(line)
        if out.length > 0.01 and out.normalized().y > -0.3:
            flips.append((frame, side, tuple(round(v, 3) for v in out)))
check("IK knees never flip backward", not flips, flips[:6])

# -- measuring a move ---------------------------------------------------------------------------
bpy.data.objects["Rig"].animation_data.action = bpy.data.actions["Walk_Loop"]
err, text, _ = call("check_motion", target="Rig", action="Walk_Loop")
data = json.loads(text) if not err else {}
check("check_motion passes a clean loop and reads it right", not err and data["issues"] == [] and data["measured"]["loop"]
      and data["measured"]["plays"] == "on the spot" and abs(data["measured"]["ground_speed_m_s"] - ratio * SPEED * S) < 0.1
      and set(data["measured"]["contacts"]) == {"Foot.L", "Foot.R"} and "forward" in data["measured"].get("moves", ""), text)
check("check_motion leaves the rig's action and the frame alone", rig.animation_data.action.name == "Walk_Loop"
      and "Walk" in data["other_actions"], data.get("other_actions"))
err, text, _ = call("check_motion", target="Rig_Body", action="Walk")
data = json.loads(text) if not err else {}
check("a move across the floor with planted feet has no sliding", not err and data["measured"]["plays"] == "across the floor"
      and not any("slides" in i for i in data["issues"]), text)


def keyed_action(name, keys, cyclic=False):
    """keys: [(bone, frame, kind, value)] with kind 'loc' (metres, bone space) or 'rot' (world-axis turn in degrees)."""
    for pbone in rig.pose.bones:
        pbone.matrix_basis = Matrix.Identity(4)
        pbone.rotation_mode = 'QUATERNION'
    action = bpy.data.actions.new(name)
    action.use_fake_user = True
    rig.animation_data.action = action
    for bone_name, frame, kind, value in keys:
        pbone = rig.pose.bones[bone_name]
        base = rig.data.bones[bone_name].matrix_local.to_quaternion()
        if kind == "loc":
            pbone.location = base.inverted() @ Vector(value)
            pbone.keyframe_insert("location", frame=frame)
        else:
            axis, degrees = value
            pbone.rotation_quaternion = base.inverted() @ Quaternion(axis, math.radians(degrees)) @ base
            pbone.keyframe_insert("rotation_quaternion", frame=frame)
    action.use_frame_range, action.use_cyclic = True, cyclic
    action.frame_start, action.frame_end = 1, max(k[1] for k in keys)
    return action


keyed_action("Sink", [("Pelvis", 1, "loc", (0, 0, 0)), ("Pelvis", 13, "loc", (0, 0, -0.12)), ("Pelvis", 25, "loc", (0, 0, 0))])
err, text, _ = call("check_motion", target="Rig", action="Sink")
data = json.loads(text) if not err else {}
check("feet pushed under the floor are reported with how deep and when", not err and any("sinks 12" in i for i in data["issues"]), text)

keyed_action("Cross", [("Thigh.L", 1, "rot", ((0, 1, 0), 0)), ("Thigh.L", 13, "rot", ((0, 1, 0), 14)), ("Thigh.L", 25, "rot", ((0, 1, 0), 0))])
err, text, _ = call("check_motion", target="Rig", action="Cross")
data = json.loads(text) if not err else {}
through = [i for i in data.get("issues", []) if "passes through" in i]
check("one leg swung into the other is reported once, as one pair of limbs", not err and len(through) == 1 and ".L" in through[0]
      and ".R" in through[0], text)

keyed_action("Pop", [("Forearm.R", f, "rot", ((0, 1, 0), 70 if f == 9 else 0)) for f in range(1, 21)])
err, text, _ = call("check_motion", target="Rig", action="Pop")
data = json.loads(text) if not err else {}
check("a single-frame snap is reported as a pop", not err and any(i.startswith("Pop: Forearm.R") for i in data["issues"]), text)

keyed_action("Open", [("UpperArm.L", 1, "rot", ((0, 1, 0), 0)), ("UpperArm.L", 12, "rot", ((0, 1, 0), 30)), ("UpperArm.L", 24, "rot", ((0, 1, 0), 20))],
             cyclic=True)
err, text, _ = call("check_motion", target="Rig", action="Open")
data = json.loads(text) if not err else {}
check("a loop whose last frame is not its first is reported", not err and any("does not close" in i for i in data["issues"]), text)

loose = keyed_action("Loose", [("Head", 1, "rot", ((0, 0, 1), 0)), ("Head", 12, "rot", ((0, 0, 1), 20))])
loose.use_fake_user = False
rig.animation_data.action = bpy.data.actions["Walk_Loop"]
err, text, _ = call("check_motion", target="Rig")
data = json.loads(text) if not err else {}
check("an action that would be lost on save is reported", not err and any("Loose" in i and "thrown away" in i for i in data["issues"]), text)
loose.use_fake_user = True

cube = bpy.data.meshes.new("Ball")
cube.from_pydata([(x, y, z) for x in (-0.1, 0.1) for y in (-0.1, 0.1) for z in (0.0, 0.2)], [], [(0, 1, 3, 2), (4, 6, 7, 5)])
ball = bpy.data.objects.new("Ball", cube)
scene.collection.objects.link(ball)
ball.location = (3, 0, 0)
for frame, z in ((1, 0.0), (8, -0.3), (16, 0.0)):
    ball.location.z = z
    ball.keyframe_insert("location", index=2, frame=frame)
err, text, _ = call("check_motion", target="Ball", ground=0.0)
data = json.loads(text) if not err else {}
check("a plain animated object is measured too", not err and any("sinks 30" in i for i in data["issues"]), text)

# -- seeing a move ------------------------------------------------------------------------------
objects_before, materials_before, meshes_before = len(bpy.data.objects), len(bpy.data.materials), len(bpy.data.meshes)
scene.frame_set(5)
err, text, images = call("look_motion", targets=["Rig"], action="Walk", count=6, size=200)
data = json.loads(text) if not err else {}
check("look_motion returns one strip with the frames it shows", not err and images == 1 and data["action"] == "Walk"
      and data["frames"][0] == 1 and data["frames"][-1] == 73 and len(data["frames"]) == 6 and len(data["rows"]) == 2, text)
check("look_motion leaves nothing behind and puts the frame and the action back", len(bpy.data.objects) == objects_before
      and len(bpy.data.materials) == materials_before and len(bpy.data.meshes) == meshes_before and scene.frame_current == 5
      and rig.animation_data.action.name == "Walk_Loop")
err, text, images = call("look_motion", targets=["Mocap"], views=["right"], count=4, size=200)
check("a bare skeleton is shown by its bones", not err and images == 1, text)
err, text, images = call("look_motion", targets=["Ball"], count=4, size=200, views=["front"])
check("a plain animated object is shown too", not err and images == 1 and json.loads(text)["frames"] == [1, 6, 11, 16], text)
err, text, _ = call("look_motion", targets=["Rig"], action="Nope")
check("an unknown action lists the ones that exist", err and "Walk_Loop" in text, text)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
