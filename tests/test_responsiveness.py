# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Responsiveness: long main-thread work gives the main thread back in slices, so Blender's window keeps
redrawing and answering (the island, Stop, orbiting the view) while the agent works on a big scene.
Run: blender -b --factory-startup --python tests/test_responsiveness.py"""
import json, os, sys, tempfile, threading, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bmesh
import bpy
from unix import tools
from unix.bridge.main_thread import WAIT, MainThreadDispatcher, drain
from unix.tools import _util, registry

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
tools.load_all()
from unix.tools import quality  # noqa: E402
fails = []
SLICE_MS = 250          # the longest the main thread may be held by one slice here (the budget is 50 ms plus one step)


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:700]}"))
    if not cond:
        fails.append(label)


def drive(disp, work, limit=120.0):
    """Run ``work`` on a worker thread while this (main) thread pumps ``disp``, as Blender's timer does.
    Returns (what work returned, the longest single pump in ms, how many pumps ran something)."""
    box = {}
    thread = threading.Thread(target=lambda: box.update(out=work()))
    thread.start()
    longest, busy, end = 0.0, 0, time.time() + limit
    while thread.is_alive() and time.time() < end:
        began = time.perf_counter()
        if disp.pump():
            busy += 1
        longest = max(longest, (time.perf_counter() - began) * 1000)
        time.sleep(0.002)
    thread.join(1.0)
    return box.get("out"), longest, busy


# -- the dispatcher runs a generator in slices ----------------------------------------------
disp = MainThreadDispatcher()
disp.bind_main_thread()
order = []


def slow_steps():
    for k in range(200):            # one second of work in 5 ms steps
        time.sleep(0.005)
        order.append("slow")
        yield
    return "finished"


out, longest, busy = drive(disp, lambda: disp.submit(slow_steps, 30))
check("a sliced job returns its result", out == "finished", out)
check("a sliced job gives the main thread back many times", busy >= 10, busy)
check(f"no slice holds the main thread longer than {SLICE_MS} ms", longest < SLICE_MS, longest)

order.clear()
box = {}
worker = threading.Thread(target=lambda: box.update(a=disp.submit(slow_steps, 30)))
worker.start()
while not disp._active:
    disp.pump()
    time.sleep(0.001)
other = threading.Thread(target=lambda: box.update(b=disp.submit(lambda: order.append("other") or "done", 30)))
other.start()
while worker.is_alive() or other.is_alive():
    disp.pump()
    time.sleep(0.002)
check("a job queued meanwhile waits until the sliced job is done (no interleaving)",
      box.get("b") == "done" and order[-1] == "other" and order.count("other") == 1, order[-3:])

closed = {}


def endless():
    try:
        while True:
            yield
    finally:
        closed["yes"] = True


job = disp.spawn(endless(), "endless")
for _ in range(5):
    disp.pump(0.005)
job.cancelled = True
disp.pump()
check("a spawned job stops when cancelled, and its finally runs", closed.get("yes") and job.done.is_set(), closed)

waits = {"n": 0}


def waiting():
    for _ in range(3):
        waits["n"] += 1
        yield WAIT
    return "ok"


job = disp.spawn(waiting(), "waiting")
disp.pump(10.0)
check("WAIT ends a slice at once, however much budget is left", waits["n"] == 1, waits)
check("a job that only waits does not ask the timer to hurry", disp.pending == 0, disp.pending)
while not job.done.is_set():
    disp.pump()
check("drain runs a sliced job to the end inline", drain(slow_steps()) == "finished")

out, _longest, _busy = drive(disp, lambda: disp.submit(lambda: time.sleep(0.3) or 1, 30))
check("a long job that does not give the thread back is logged as a stall",
      any(seconds >= 0.25 for _label, seconds, _when in disp.stalls), list(disp.stalls)[-3:])

# -- a sliced tool through the registry: result, Stop, undo --------------------------------------
registry.run_on_main = disp.submit
finally_ran = {"n": 0}


@registry.tool("_test_sliced", "test only", {"steps": {"type": "integer"}}, timeout=60.0)
def _test_sliced(steps: int = 50):
    try:
        for _ in range(steps):
            time.sleep(0.004)
            yield
        return {"done": steps}
    finally:
        finally_ran["n"] += 1


res, longest, _busy = drive(disp, lambda: registry.call("_test_sliced", {"steps": 60}))
check("a sliced tool answers through the registry", not res["isError"] and json.loads(res["content"][0]["text"]) == {"done": 60}, res)
check("the sliced tool's slices are short", longest < SLICE_MS, longest)

stopper = threading.Timer(0.15, registry.abandon)
stopper.start()
res, _longest, _busy = drive(disp, lambda: registry.call("_test_sliced", {"steps": 2000}))
check("Stop ends a sliced tool between two slices", res["isError"] and "stopped" in res["content"][0]["text"], res)
check("its finally runs when it is stopped", finally_ran["n"] == 2, finally_ran)


def undo_soon():
    time.sleep(0.15)
    _util._data_replaced()          # what Blender's undo handler does, between two slices


threading.Thread(target=undo_soon).start()
res, _longest, _busy = drive(disp, lambda: registry.call("_test_sliced", {"steps": 2000}))
check("an undo meanwhile stops a sliced tool (it must not use what it held)", res["isError"] and "undone" in res["content"][0]["text"], res)

# -- a big scene: the audit and check_scene run in short slices -------------------------------
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj)
cube = bpy.data.meshes.new("_unit_cube")
bm = bmesh.new()
bmesh.ops.create_cube(bm, size=1.0)
bm.to_mesh(cube)
bm.free()
ground = bpy.data.objects.new("Ground", bpy.data.meshes.new("Ground"))
ground.data.from_pydata([(-200, -200, 0), (200, -200, 0), (200, 200, 0), (-200, 200, 0)], [], [(0, 1, 2, 3)])
bpy.context.scene.collection.objects.link(ground)
for i in range(240):
    x, y = (i % 16) * 9.0 - 70, (i // 16) * 9.0 - 70
    for part, size, z in (("Walls", (4, 4, 3), 1.5), ("Roof", (4.4, 4.4, 0.6), 3.3)):
        obj = bpy.data.objects.new(f"House_{i}_{part}", cube.copy())
        obj.data.transform(__import__("mathutils").Matrix.Diagonal((*size, 1.0)))
        obj.location = (x, y, z)
        mod = obj.modifiers.new("Bevel", 'BEVEL')
        mod.width = 0.05
        bpy.context.scene.collection.objects.link(obj)
bpy.context.view_layer.update()
print("SCENE", len(bpy.data.objects), "objects")

quality._bodies.clear()
quality._measured.clear()
began = time.perf_counter()
whole = quality.audit()
audit_s = time.perf_counter() - began

quality._bodies.clear()
quality._measured.clear()
steps = quality.audit_steps()
longest, count = 0.0, 0
while True:
    began = time.perf_counter()
    try:
        next(steps)
    except StopIteration as done:
        sliced = done.value
        break
    longest = max(longest, (time.perf_counter() - began) * 1000)
    count += 1
check("the audit in steps finds what the audit finds", sliced == whole, (sliced["problems"][:3], whole["problems"][:3]))
check(f"no step of the audit holds the main thread longer than {SLICE_MS} ms (audit {audit_s:.1f}s, {count} steps)",
      longest < SLICE_MS, longest)

began = time.perf_counter()
again = quality.audit()
warm_s = time.perf_counter() - began
check(f"an unchanged scene is measured again from what was kept ({warm_s:.2f}s, first {audit_s:.2f}s)",
      again == whole and warm_s < max(0.6 * audit_s, 0.3), (warm_s, audit_s))
bpy.data.objects["House_3_Roof"].location.z += 0.5
bpy.context.view_layer.update()
moved = quality.audit()
check("a moved object is measured again: it now floats", any("House_3_Roof" in line for line in moved["problems"]), moved["problems"])
bpy.data.objects["House_3_Roof"].location.z -= 0.5
bpy.context.view_layer.update()

res, longest, busy = drive(disp, lambda: registry.call("check_scene", {}))
check("check_scene answers when run in slices", not res["isError"] and "inventory" in json.loads(res["content"][0]["text"]), res)
check(f"check_scene gives the main thread back (longest slice {longest:.0f} ms, {busy} slices)", longest < SLICE_MS and busy > 3,
      (longest, busy))

# -- the look renders one view per slice --------------------------------------------------------
engine_before = bpy.context.scene.render.engine
res, longest, busy = drive(disp, lambda: registry.call("look", {"views": ["persp", "top", "front"], "size": 256}))
check("look answers when run in slices", not res["isError"] and sum(1 for c in res["content"] if c["type"] == "image") == 3,
      res["content"][0]["text"][:300])
check("look gives the main thread back between its views", busy >= 3, busy)
check("look leaves no temporary camera behind", not [o.name for o in bpy.data.objects if o.name.startswith("_unix_look")])
check("look puts the render settings back", bpy.context.scene.render.engine == engine_before, bpy.context.scene.render.engine)

# -- run_python says how long it held Blender ---------------------------------------------------------
res, _longest, _busy = drive(disp, lambda: registry.call("run_python", {"code": "result = len(D.objects)"}))
reply = json.loads(res["content"][0]["text"])
check("run_python reports its seconds", "seconds" in reply and reply["seconds"] >= 0, reply)
expected = sum(sum(len(p.vertices) - 2 for p in o.data.polygons) for o in bpy.context.scene.objects if o.type == 'MESH')
check("run_python counts triangles without visiting each face, and gets the same number", reply.get("scene_triangles") == expected,
      (reply.get("scene_triangles"), expected))

# -- the island draws a long chat quickly ------------------------------------------------------------
from unix import ui  # noqa: E402
from unix.core import engine  # noqa: E402
from unix.ui import island  # noqa: E402
import blf  # noqa: E402
ui.register()


class _NoGpu:
    """Background mode has no GPU drawing: these calls do nothing, everything else (layout, wrapping,
    measuring text) runs for real."""
    def __getattr__(self, name):
        return self

    def __call__(self, *args, **kwargs):
        return self


island.gpu, island.batch_for_shader = _NoGpu(), _NoGpu()
island.blf = types.SimpleNamespace(size=blf.size, dimensions=blf.dimensions, color=lambda *a: None,
                                   position=lambda *a: None, draw=lambda *a: None)
region = types.SimpleNamespace(width=1600, height=1000, type='WINDOW', as_pointer=lambda: 1)
area = types.SimpleNamespace(regions=[], type='VIEW_3D')
messages = [{"role": "user", "text": "/goal " + "Build a harbour town with a market and a lighthouse. " * 3800,
             "attachments": [], "mode": "agent"}]
for i in range(250):
    messages.append({"role": "user", "text": f"Message {i}: make the roofs redder and move the well north by two metres.",
                     "attachments": [], "mode": "agent"})
    steps = [{"name": "run_python", "label": "Ran a script", "detail": f"step {k}", "acted": True, "status": "ok",
              "summary": "", "result": "3 modified", "images": [], "input": {}} for k in range(12)]
    messages.append({"role": "assistant", "parts": [{"type": "steps", "steps": steps, "open": False},
                                                    {"type": "text", "text": "Done. " + "I changed the roofs and moved the well. " * 8}],
                     "meta": "12s · $0.10"})
engine.messages = messages
times = []
for k in range(60):
    island.composer["text"] = "typing a long message " * 4 + "x" * k        # a new draft every frame, as when typing
    began = time.perf_counter()
    island._draw_island(region, area)
    times.append((time.perf_counter() - began) * 1000)
times = sorted(times[1:])
check(f"the island draws a 500-message chat quickly while typing (median {times[len(times) // 2]:.1f} ms, worst {times[-1]:.1f} ms)",
      times[len(times) // 2] < 15 and times[-1] < 40, times[-5:])
engine.messages = messages[:2] + messages[-6:]       # the 200,000-character goal in view
island._draw_island(region, area)
times = []
for k in range(400):
    island.composer["text"] = "typing " * 10 + "y" * k
    began = time.perf_counter()
    island._draw_island(region, area)
    times.append((time.perf_counter() - began) * 1000)
check(f"typing never re-wraps a long goal in view (worst draw {max(times):.1f} ms over 400 keys)", max(times) < 40, max(times))
engine.messages = []

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
