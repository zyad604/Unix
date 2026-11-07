# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Stress test for the crash in long sessions: many concurrent MCP tool calls of every kind against a
windowed Blender, from client threads in the same process (busy Python threads are what made Blender
crash, see ui/window_pump.py). Needs a window: not part of run_tests.ps1.

Run: blender --factory-startup [scene.blend] --python tests/stress_threads.py -- <minutes> [workers] [--no-pump] [--only=look]
--only=    calls one tool only (memory growth per tool: the STRESS lines carry the process memory).
--no-pump  turns the window pump off, so tool calls run from the bare timer under temp_override as they
           did before the fix. On the Sundial Bay scene that crashed Blender within a minute (125 calls);
           with the pump, 12 minutes and 1,571 calls without a crash.

Prints STRESS lines while it runs and STRESS RESULT at the end, and quits. If Blender dies instead, the
Python traceback of every thread is in <data folder>/crash_logs (the folder is printed at the start).
Nothing goes to the internet: online tools are switched off, and the Roblox link listens on a private port.
"""
import json, os, random, sys, tempfile, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_stress_")
import unix
from unix.bridge import roblox_link, window_shot
from unix.tools import assets, generate
from unix.tools.registry import tool

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
args = [a for a in argv if not a.startswith("--")]
MINUTES = float(args[0]) if args else 5.0
WORKERS = int(args[1]) if len(args) > 1 else 6
NO_PUMP = "--no-pump" in argv
assets.settings["enabled"] = generate.settings["enabled"] = False


def _private_link():
    roblox_link.settings["install"] = False         # never write into Roblox Studio's plugin folder
    roblox_link.link.start(ports=(0,))              # and a port no real Studio looks for


unix._start_roblox_link = _private_link


@tool("stress_window_shot", "test only", {}, main_thread=False, timeout=60.0)
def stress_window_shot():
    try:
        return {"title": window_shot.shoot("blender.exe", os.path.join(_util.data_dir("looks"), "shot.png"), max_side=600)}
    except window_shot.ShotError as exc:
        return {"error": str(exc)}


def _memory_mb():
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in (
            "peak_working", "working", "peak_paged", "paged", "peak_nonpaged", "nonpaged", "private", "peak_private")]
    psapi, kernel32 = ctypes.WinDLL("psapi"), ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = Counters(cb=ctypes.sizeof(Counters))
    psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb)
    return round(counters.working / 2 ** 20), round(counters.private / 2 ** 20)


@tool("stress_stats", "test only", {}, timeout=60.0)
def stress_stats():
    import gc
    from unix.ui import window_pump
    working, private = _memory_mb()
    data = bpy.data
    return {"objects": len(data.objects), "meshes": len(data.meshes), "images": len(data.images),
            "materials": len(data.materials), "overrides": _util.overrides["count"], "pump": window_pump.alive(),
            "threads": threading.active_count(), "working_mb": working, "private_mb": private, "py_objects": len(gc.get_objects())}


SCRIPTS = [
    # what the crash log shows the agent doing: apply scale object by object, deselect in between
    "import random\nmeshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']\nrandom.shuffle(meshes)\n"
    "for o in meshes[:25]:\n    bpy.ops.object.select_all(action='DESELECT')\n    o.select_set(True)\n"
    "    bpy.context.view_layer.objects.active = o\n"
    "    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)\n"
    "bpy.ops.object.select_all(action='DESELECT')\nresult = len(meshes)",
    # operators that add and delete
    "for i in range(10):\n    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(i, 50, 0))\n"
    "    bpy.context.active_object.name = f'stress_cube_{i}'\n"
    "for o in [o for o in bpy.data.objects if o.name.startswith('stress_cube_')]:\n"
    "    mesh = o.data\n    bpy.data.objects.remove(o, do_unlink=True)\n    bpy.data.meshes.remove(mesh)\nresult = 'ok'",
    # viewport shading toggles, through the context the script is given
    "for t in ('MATERIAL', 'SOLID', 'MATERIAL', 'SOLID'):\n    bpy.context.space_data.shading.type = t\nresult = 'ok'",
    # the model's own override (the guard routes it through window_context)
    "area = next(a for a in bpy.context.screen.areas if a.type == 'VIEW_3D')\n"
    "region = next(r for r in area.regions if r.type == 'WINDOW')\n"
    "with bpy.context.temp_override(area=area, region=region):\n"
    "    for i in range(5):\n        bpy.ops.mesh.primitive_cube_add(size=0.2, location=(i, 60, 0))\n"
    "for o in [o for o in bpy.data.objects if o.name.startswith('Cube')]:\n"
    "    mesh = o.data\n    bpy.data.objects.remove(o, do_unlink=True)\n    bpy.data.meshes.remove(mesh)\nresult = 'ok'",
    # Blender-backed values in the result
    "o = next(o for o in bpy.context.scene.objects if o.type == 'MESH')\nresult = {'loc': o.location, 'dim': o.dimensions}",
]
NAMES = []
TOOLS = [("scene_info", lambda: {"limit": 40}, 6), ("object_info", lambda: {"name": random.choice(NAMES)}, 4),
         ("run_python", lambda: {"code": random.choice(SCRIPTS), "purpose": "stress"}, 8),
         ("look", lambda: {"shading": random.choice(["solid", "rendered"]), "size": 384}, 3),
         ("look_around", lambda: {"size": 200}, 1), ("look_map", lambda: {"size": 512}, 1),
         ("check_mesh", lambda: {"objects": random.sample(NAMES, min(5, len(NAMES)))}, 2), ("check_materials", lambda: {}, 1),
         ("check_scene", lambda: {}, 1), ("api_lookup", lambda: {"query": "Object.location"}, 2), ("skill_list", lambda: {}, 2),
         ("update_plan", lambda: {"steps": [{"title": "stress", "status": "in_progress"}]}, 2),
         ("goal_report", lambda: {"done": False, "remaining": ["x"], "evidence": "stress"}, 2),
         ("roblox_look", lambda: {}, 1), ("roblox_query", lambda: {}, 1), ("engine_targets", lambda: {}, 1),
         ("save_snapshot", lambda: {"label": "stress"}, 1), ("stress_window_shot", lambda: {}, 3)]
ONLY = next((a.split("=", 1)[1] for a in argv if a.startswith("--only=")), "")     # one tool, to see what grows
WEIGHTED = [t for t in TOOLS for _ in range(t[2]) if not ONLY or t[0] == ONLY]
counts = {"ok": 0, "tool_error": 0, "transport_error": 0}
lock, stop, server = threading.Lock(), threading.Event(), {}


def call(name, arguments, timeout=900):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    request = urllib.request.Request(server["url"], data=body.encode(), method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {server['token']}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())["result"]


def worker(index):
    rng = random.Random(index)
    while not stop.is_set():
        name, make, _weight = rng.choice(WEIGHTED)
        try:
            kind = "tool_error" if call(name, make()).get("isError") else "ok"
        except Exception:
            kind = "transport_error"
        with lock:
            counts[kind] += 1


def client():
    started = time.time()
    try:
        NAMES[:] = json.loads(call("run_python", {"code": "result = [o.name for o in bpy.context.scene.objects if o.type == 'MESH']"})
                              ["content"][0]["text"])["result"] or ["Cube"]
        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(WORKERS)]
        for thread in threads:
            thread.start()
        while time.time() - started < MINUTES * 60:
            time.sleep(20)
            stats = json.loads(call("stress_stats", {}, timeout=300)["content"][0]["text"])
            print("STRESS", round(time.time() - started), "s", json.dumps({**counts, **stats}), flush=True)
    finally:
        stop.set()
        print("STRESS RESULT", json.dumps({**counts, "minutes": round((time.time() - started) / 60, 1), "no_pump": NO_PUMP}), flush=True)


def quit_when_done():
    """Main thread (a timer): bpy is not called from the client threads."""
    if not stop.is_set():
        return 1.0
    bpy.ops.wm.quit_blender()
    return None


def boot():
    unix.register()
    if NO_PUMP:
        from unix.ui import window_pump
        window_pump.ensure = lambda: None
    from unix.core import engine
    server.update(url=engine.server.url, token=engine.server.token)
    print("STRESS data folder:", _util.data_dir(), "| pump:", not NO_PUMP, flush=True)
    threading.Thread(target=client, daemon=True, name="StressClient").start()
    bpy.app.timers.register(quit_when_done, first_interval=5.0)


bpy.app.timers.register(boot, first_interval=1.0)
