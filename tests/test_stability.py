# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""What keeps a long session alive: nothing touches bpy off the main thread, the Windows calls are
declared, a fatal error leaves a traceback of every thread, and after a crash the scene and the goal
come back. No model calls, no network.
Run: blender -b --factory-startup --python tests/test_stability.py

The crash itself (operators under temp_override while another thread runs Python) needs a window:
tests/stress_threads.py reproduces it and checks the window pump against it."""
import ast, glob, hashlib, json, os, queue, subprocess, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bpy
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_stability_")   # keep test data out of the user's config
from unix import recovery, threadcheck
from unix.core import engine
from unix.tools import guard, registry
from unix.tools.registry import ToolOutput
registry_module = sys.modules["unix.tools.registry"]

recovery.start()
engine.start()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:900]}"))
    if not cond:
        fails.append(label)


PKG = os.path.join(ROOT, "unix")

# -- 1. no bpy off the main thread: static ---------------------------------------------------------------
# Every tool that runs on the request thread (main_thread=False), and the other code that runs off the main
# thread, is followed call by call through the add-on's own functions. A function handed to run_on_main runs
# on the main thread and is not followed. data_dir touches bpy only on its first call, made at engine start.
ALLOWED = {("tools._util", "data_dir")}
BPY_NAMES = {"bpy", "bmesh"}


def _modname(path):
    rel = os.path.relpath(path, PKG)[:-3].replace(os.sep, ".")
    if rel == "__init__":
        return ""
    return rel[:-9] if rel.endswith(".__init__") else rel


def _is_module(dotted):
    base = os.path.join(PKG, *dotted.split(".")) if dotted else PKG
    return os.path.isfile(base + ".py") or os.path.isdir(base)


trees, functions, imports = {}, {}, {}
for path in glob.glob(os.path.join(PKG, "**", "*.py"), recursive=True):
    name = _modname(path)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    trees[name] = tree
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[(name, node.name)] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[(name, f"{node.name}.{item.name}")] = item
    package = name.split(".") if path.endswith("__init__.py") else name.split(".")[:-1]
    package = [p for p in package if p]
    table = {}
    for node in ast.walk(tree):         # imports anywhere: many are inside functions
        if isinstance(node, ast.ImportFrom) and node.level:
            base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
            mod = ".".join(base + ([node.module] if node.module else []))
            for alias in node.names:
                full = f"{mod}.{alias.name}" if mod else alias.name
                table[alias.asname or alias.name] = ("module", full) if _is_module(full) else ("name", mod, alias.name)
    imports[name] = table


def _resolve(module, cls, call):
    func = call.func
    if isinstance(func, ast.Name):
        if (module, func.id) in functions:
            return (module, func.id)
        entry = imports[module].get(func.id)
        if entry and entry[0] == "name":
            return (entry[1], entry[2])
    elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = func.value.id
        if owner == "self" and cls:
            return (module, f"{cls}.{func.attr}")
        entry = imports[module].get(owner)
        if entry and entry[0] == "module":
            return (entry[1], func.attr)
    return None


def _main_thread_parts(fn):
    """Nested functions and lambdas handed to run_on_main / submit: they run on the main thread."""
    skip = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Attribute, ast.Name)):
            called = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            if called in ("run_on_main", "submit", "spawn"):
                for arg in node.args[:1]:
                    if isinstance(arg, ast.Lambda):
                        skip.add(id(arg))
                    elif isinstance(arg, ast.Name):
                        skip.add(arg.id)
    return skip


def off_thread_bpy(entries):
    found, seen, todo = [], set(), list(entries)
    while todo:
        key = todo.pop()
        if key in seen or key in ALLOWED or key not in functions:
            continue
        seen.add(key)
        module, qual = key
        cls = qual.split(".")[0] if "." in qual else None
        fn = functions[key]
        skip = _main_thread_parts(fn)
        stack = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.Lambda) and id(node) in skip:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in skip:
                continue
            if isinstance(node, ast.Name) and node.id in BPY_NAMES and isinstance(node.ctx, ast.Load):
                found.append(f"{module}.{qual} line {node.lineno}: {node.id}")
            if isinstance(node, ast.Call):
                target = _resolve(module, cls, node)
                if target:
                    todo.append(target)
            stack.extend(ast.iter_child_nodes(node))
    return found, seen


entries = []
for name in registry.names():
    t = registry.get(name)
    if not t.main_thread:
        entries.append((t.fn.__module__.split(".", 1)[1], t.fn.__name__))
entries += [("core", "Engine._call_tool"), ("tools.registry", "Registry.call"), ("tools.engines", "find_targets"),
            ("tools.team", "note")]
found, followed = off_thread_bpy(entries)
check(f"no bpy reached from the {len(entries)} off-main-thread entry points ({len(followed)} functions followed)",
      not found, found)
check("the static check sees a real off-thread bpy use", off_thread_bpy([("tools._util", "get_object")])[0])

# -- 2. no bpy off the main thread: at run time -------------------------------------------------------------
from unix.tools import assets, generate
from unix.bridge import roblox_link
assets.settings["enabled"], generate.settings["enabled"] = False, False      # no network
covered = threadcheck.install()
main_jobs = queue.Queue()
registry.run_on_main = lambda fn, timeout=300.0: _on_main(fn)


def _on_main(fn):
    box = {}
    done = threading.Event()
    main_jobs.put((fn, box, done))
    done.wait(120)
    if "error" in box:
        raise box["error"]
    return box.get("value")


ARGS = {"asset_search": {"kind": "hdri", "query": "studio"}, "asset_hdri": {"id": "x"}, "asset_material": {"id": "x"},
        "asset_model": {"id": "x"}, "engine_targets": {}, "generate_image": {"prompt": "a cup", "purpose": "concept"},
        "generate_texture": {"prompt": "wood"}, "goal_report": {"done": False, "remaining": ["a"], "evidence": "b"},
        "update_plan": {"steps": [{"title": "a", "status": "done"}]}, "skill_list": {}, "skill_load": {"names": ["x"]},
        "reference_search": {"query": "chair"}, "motion_search": {"query": "run"}, "roblox_build": {"name": "A", "children": [{"class": "Part"}]},
        "roblox_look": {}, "roblox_query": {}, "roblox_remove": {"path": "Workspace.A"},
        "retarget_motion": {"target": "Cube", "source": "Cube"}, "work_in_parallel": {"parts": [], "design": ""},
        "ask_user": {"question": "Which colour?"}, "send_to_engine": {"engine": "godot", "objects": ["NoSuchObject"]}}
results = {}


def worker():
    for name in registry.names():
        t = registry.get(name)
        if not t.main_thread and name in ARGS:
            results[name] = registry.call(name, ARGS[name])
    main_jobs.put(None)


threading.Thread(target=worker, daemon=True).start()
deadline = time.time() + 240
while time.time() < deadline:
    try:
        job = main_jobs.get(timeout=0.5)
    except queue.Empty:
        continue
    if job is None:
        break
    fn, box, done = job
    try:
        box["value"] = fn()
    except BaseException as exc:
        box["error"] = exc
    done.set()
threadcheck.uninstall()
off_thread = sorted(registry.get(n).name for n in registry.names() if not registry.get(n).main_thread)
check(f"every off-main-thread tool ran on a worker thread ({len(results)} of {len(off_thread)}; bpy swapped in {covered} modules)",
      len(results) == len(off_thread), sorted(set(off_thread) - set(results)))
check("none of them used bpy off the main thread", not threadcheck.violations,
      [(m, a) for m, a, _ in threadcheck.violations])

# -- 3. results leave the main thread as plain data ---------------------------------------------------------
cube = bpy.data.objects.get("Cube")
plain = registry_module._detached({"location": cube.location, "matrix": cube.matrix_world, "object": cube, "n": 1})
check("a tool's Blender-backed values are turned into plain data on the main thread",
      plain["location"] == list(cube.location) and isinstance(plain["matrix"], list) and isinstance(plain["object"], str), plain)
out = registry_module._detached(ToolOutput({"dim": cube.dimensions}, ["a.png"]))
check("an image result keeps its pictures and loses its Blender values", out.images == ["a.png"] and out.data["dim"] == list(cube.dimensions))

# -- 4. model scripts' own temp_override goes through window_context ----------------------------------------
code, problem = guard.prepare("area = None\nwith bpy.context.temp_override(window=bpy.context.window):\n    result = 1\n")
check("the guard routes a script's temp_override through window_context",
      problem is None and "_unix_window_context" in code.co_names, (problem, code and code.co_names))
before = _util.overrides["count"]
with _util.window_context(window=bpy.context.window, screen=bpy.context.screen):
    pass
check("an override of what the context already holds is skipped", _util.overrides["count"] == before)
scene_override = _util.window_context(scene=bpy.context.scene)
check("an override of anything else is a real one", _util.overrides["count"] == before + 1 and hasattr(scene_override, "__enter__"))
namespace = {"bpy": bpy}
exec(code, namespace)
check("a rewritten script runs", namespace.get("result") == 1)

# -- 5. every Windows call is declared ----------------------------------------------------------------------
if sys.platform == "win32":
    for rel in ("bridge/window_shot.py", "bridge/clipboard.py", "recovery.py", "__init__.py"):
        with open(os.path.join(PKG, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        called, declared = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                    and node.func.value.id in ("user32", "kernel32", "gdi32", "shell32"):
                called.add(f"{node.func.value.id}.{node.func.attr}")
            if isinstance(node, ast.Attribute) and node.attr in ("argtypes", "restype") and isinstance(node.value, ast.Attribute) \
                    and isinstance(node.value.value, ast.Name):
                declared.add(f"{node.value.value.id}.{node.value.attr}")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "declare" and node.args \
                    and isinstance(node.args[0], ast.Attribute) and isinstance(node.args[0].value, ast.Name):
                declared.add(f"{node.args[0].value.id}.{node.args[0].attr}")
        check(f"{rel}: every Windows function called has its argument and result types declared ({len(called)})",
              called <= declared, sorted(called - declared))
    shared = []
    for path in glob.glob(os.path.join(PKG, "**", "*.py"), recursive=True):
        with open(path, encoding="utf-8") as fh:
            for node in ast.walk(ast.parse(fh.read())):
                if isinstance(node, ast.Attribute) and node.attr == "windll" and isinstance(node.value, ast.Name)                         and node.value.id == "ctypes":
                    shared.append(f"{os.path.relpath(path, PKG)}:{node.lineno}")
    check("no module declares types on the shared ctypes.windll functions", not shared, shared)
    from unix.bridge import window_shot
    shots = 0
    for hwnd, title in window_shot._windows("explorer.exe"):
        try:
            width, height, rgb = window_shot.capture(hwnd, 300)
            shots += len(rgb) == width * height * 3
        except window_shot.ShotError:
            pass
    check("a window is captured with the declared calls", shots >= 1, shots)

# -- 6. a fatal error leaves every thread's traceback --------------------------------------------------------
import faulthandler
logs_dir = _util.data_dir("crash_logs")
WINDOWS = sys.platform == "win32"
if WINDOWS:     # faulthandler would dump on every exception Blender and the driver handle themselves
    check("on Windows the Python crash log stays off", not faulthandler.is_enabled(), recovery._state)
else:
    check("the crash log is on, in the add-on's data folder", faulthandler.is_enabled()
          and recovery._state["log_path"].startswith(logs_dir) and os.path.isfile(recovery._state["log_path"]), recovery._state)
crash_dir = tempfile.mkdtemp(prefix="unix_crash_")
crasher = os.path.join(crash_dir, "crash.py")
with open(crasher, "w", encoding="utf-8") as fh:
    fh.write(f"""import sys, threading, time, ctypes
sys.path.insert(0, {ROOT!r})
from unix.tools import _util
_util._base_dir = {crash_dir!r}
from unix import recovery
recovery.start()
def waiting_thread():
    time.sleep(60)
threading.Thread(target=waiting_thread, name="UnixTestWaiter", daemon=True).start()
time.sleep(0.2)
import faulthandler
faulthandler._sigsegv()  # a real fatal error (an access violation inside ctypes would be caught and raised as OSError)
""")
subprocess.run([bpy.app.binary_path, "-b", "--factory-startup", "--python", crasher], capture_output=True, timeout=120)
written = [p for p in glob.glob(os.path.join(crash_dir, "crash_logs", "*.log")) if os.path.getsize(p)]
text = open(written[0], encoding="utf-8", errors="replace").read() if written else ""
if not WINDOWS:
    check("a crashed Blender left its Python traceback", "Fatal Python error" in text and "crash.py" in text, text[:400])
    check("...with the other threads in it", "waiting_thread" in text, text[:800])
markers = os.listdir(os.path.join(crash_dir, "sessions"))
check("...and its session marker, which a clean exit removes", len(markers) == 1, markers)
for index in range(8):           # old logs of earlier crashes
    with open(os.path.join(logs_dir, f"python_2026010{index}_000000_1.log"), "w") as fh:
        fh.write("old")
    os.utime(os.path.join(logs_dir, f"python_2026010{index}_000000_1.log"), (time.time() - 1000 + index,) * 2)
recovery._enable_faulthandler()
check(f"only the newest {recovery.KEEP_LOGS} crash logs are kept", len(recovery.crash_logs()) == recovery.KEEP_LOGS, recovery.crash_logs())

# -- 7. after a crash: the scene and the goal come back ------------------------------------------------------
data = _util.data_dir()
snapshot_dir = _util.data_dir("snapshots")
user_file = os.path.join(tempfile.mkdtemp(prefix="unix_user_"), "harbour.blend")
bpy.ops.wm.save_as_mainfile(filepath=user_file)
user_hash = hashlib.md5(open(user_file, "rb").read()).hexdigest()
bpy.ops.mesh.primitive_monkey_add()
bpy.context.active_object.name = "BuiltBeforeTheCrash"
snapshot = os.path.join(snapshot_dir, "20261016_155742_before_turn.blend")
bpy.ops.wm.save_as_mainfile(filepath=snapshot, copy=True)
snapshot_hash = hashlib.md5(open(snapshot, "rb").read()).hexdigest()
bpy.ops.object.delete()                   # what is open now is not what was saved
chat = {"id": "crashedchat1", "title": "/goal # Map Design: Sundial Bay", "updated": time.time() - 30, "session_id": "s-1",
        "provider": "claude", "file": user_file,
        "messages": [{"role": "user", "text": "/goal # Map Design: Sundial Bay ...", "attachments": [], "snapshot": snapshot,
                      "mode": "agent"}, {"role": "assistant", "parts": [], "meta": ""}],
        "goal": {"text": "# Map Design: \"Sundial Bay\"\n\nBuild the map.", "state": "working", "round": 10, "started": time.time() - 600,
                 "resumed": time.time() - 600, "spent": 0.0, "cost": 1.0, "met": ["ground"], "remaining": ["harbour"], "next": "",
                 "errors": 0, "stalled": 0, "checks": 0}}
with open(os.path.join(_util.data_dir("chats"), "crashedchat1.json"), "w", encoding="utf-8") as fh:
    json.dump(chat, fh)
dead_marker = os.path.join(_util.data_dir("sessions"), "4000000.json")
with open(dead_marker, "w") as fh:
    json.dump({"pid": 4000000, "started": time.time() - 700, "log": ""}, fh)

offer = recovery.find_offer()
check("a session that died is found with its chat, goal and newest snapshot", offer and offer["crashed"] and offer["chat_id"] == "crashedchat1"
      and offer["snapshot"] == snapshot and offer["round"] == 10, offer)
words = recovery.offer_text(offer) if offer else ""
check("the offer says in plain words what happened and what reopening does",
      "closed unexpectedly" in words and "Sundial Bay" in words and "round 10" in words and "copy" in words
      and "none of your own files" in words, words)
check("it is one click: reopen and carry on, reopen only, or not now", recovery.offer_options(offer) ==
      [recovery.REOPEN_CONTINUE, recovery.REOPEN_ONLY, recovery.NOT_NOW] if offer else False)

sent = []
from unix.ui import operators
real_send = operators._send
operators._send = lambda context, text, mode="agent", auto=False: sent.append(text)
problem = recovery.act(offer, recovery.REOPEN_CONTINUE)
check("reopen and carry on works", problem is None, problem)
check("the scene as it was at the snapshot is open", "BuiltBeforeTheCrash" in bpy.data.objects, [o.name for o in bpy.data.objects])
check("...from a copy: the snapshot itself is not the open file", os.path.normcase(bpy.data.filepath) != os.path.normcase(snapshot)
      and hashlib.md5(open(snapshot, "rb").read()).hexdigest() == snapshot_hash, bpy.data.filepath)
check("...and the user's own file is unchanged", hashlib.md5(open(user_file, "rb").read()).hexdigest() == user_hash)
check("the crashed chat is back", engine.chat_id == "crashedchat1" and engine.goal and engine.goal["round"] == 10, engine.chat_id)
check("the goal is carried on with /goal continue, from a fresh conversation that is told to look first",
      sent == ["/goal continue"] and engine.session_id is None and "snapshot" in engine.goal["next"], (sent, engine.session_id))
check("the chat says the scene was reopened and the user's files are untouched",
      any("Reopened the scene" in p.get("text", "") for p in engine.messages[-1]["parts"]), engine.messages[-1])
check("the dead session is dealt with: not offered again", not os.path.exists(dead_marker) and recovery.find_offer() is None)
# A goal paused before the last request (a send to Roblox asked after it): carrying on is that request, not the goal
sent.clear()
engine.goal["state"] = "paused"
problem = recovery.act({"chat_id": engine.chat_id, "goal": "", "snapshot": "", "crashed": True}, recovery.CARRY_ON_HERE)
check("a paused goal stays paused: the interrupted request is carried on instead",
      problem is None and sent and not sent[0].startswith("/goal") and engine.goal["state"] == "paused", (problem, sent))

# A goal left running by a version without session markers (the 15:59 crash) is offered too, and /recover works.
chat["id"], chat["updated"] = "cutoffchat2", time.time()
with open(os.path.join(_util.data_dir("chats"), "cutoffchat2.json"), "w", encoding="utf-8") as fh:
    json.dump(chat, fh)
offer = recovery.find_offer()
check("a goal still marked running is offered without a marker", offer and not offer["crashed"] and offer["chat_id"] == "cutoffchat2"
      and "still working" in recovery.offer_text(offer), offer)
recovery._state.update(offer=None, action=None)
engine.goal, engine.run = None, None
check("/recover is one command", engine.send("/recover", claude_bin="fake") is None and recovery._state["action"] == recovery.REOPEN_CONTINUE)
recovery._state.update(offer=None, action=None)
for name in os.listdir(_util.data_dir("chats")):
    os.remove(os.path.join(_util.data_dir("chats"), name))
check("/recover with nothing to recover says so", (engine.send("/recover", claude_bin="fake") or "").startswith("Nothing to recover"))
operators._send = real_send

# -- 8. long sessions: the chat keeps the inputs of recent steps only -------------------------------------
engine.new_chat()
engine.messages = [{"role": "user", "text": f"m{i}", "attachments": [], "snapshot": ""} if i % 2 == 0 else
                   {"role": "assistant", "meta": "", "parts": [{"type": "steps", "steps": [
                       {"name": "run_python", "input": {"code": "x" * 5000}, "summary": "ok", "status": "ok"}]}]}
                   for i in range(400)]
engine.save_chat()
size = os.path.getsize(engine._chat_path(engine.chat_id))
kept = sum(1 for m in engine.messages for p in m.get("parts", []) for s in p.get("steps", []) if "input" in s)
check(f"a 400-message chat keeps step inputs only for its last messages ({kept} steps, {size // 1024} KB on disk)",
      kept == 30 and size < 400_000 and engine.messages[1]["parts"][0]["steps"][0]["summary"] == "ok", (kept, size))

# -- 9. the safety save comes back when the scene changed and no tool is half-way ---------------------------
class _Dispatcher:
    pending, waiting = 0, False


class _Engine:
    settings = {"snapshots": True}
    busy = False
    dispatcher = _Dispatcher()


paths = bpy.context.preferences.filepaths
paths.use_auto_save_temporary_files, paths.auto_save_time = True, 2
now = time.monotonic()
recovery._state.update(saved=now - 30, changed=True, busy=False)
check("no safety save sooner than Blender's auto-save interval", not recovery._autosave_due(_Engine, now))
recovery._state.update(saved=now - 130)
check("a safety save once the interval has passed and the scene changed", recovery._autosave_due(_Engine, now))
_Engine.dispatcher.pending = 1
check("never while a tool is half-way through", not recovery._autosave_due(_Engine, now))
_Engine.dispatcher.pending = 0
recovery._state.update(saved=now - 70, busy=True)
check("a minute after a turn that changed the scene ended", recovery._autosave_due(_Engine, now))
recovery._state.update(changed=False)
check("not when nothing changed", not recovery._autosave_due(_Engine, now))
recovery._state.update(changed=True)
gen = recovery._autosave_steps()
next(gen)
for _ in gen:
    pass
saves = [n for n in os.listdir(snapshot_dir) if "_autosave" in n]
gen = recovery._autosave_steps()
for _ in gen:
    pass
saves2 = [n for n in os.listdir(snapshot_dir) if "_autosave" in n]
check("a safety save is a snapshot, and a second one does not delete the first", len(saves) >= 1 and len(saves2) > len(saves)
      or len(saves2) >= 2, (saves, saves2))
for index in range(recovery.KEEP_AUTOSAVES + 3):       # many safety saves: only the newest few stay
    old_path = os.path.join(snapshot_dir, f"2026010{index % 10}_00000{index % 10}_{index}_autosave.blend")
    open(old_path, "wb").close()
    os.utime(old_path, (time.time() - 5000 + index,) * 2)
for _ in recovery._autosave_steps():
    pass
saves3 = [n for n in os.listdir(snapshot_dir) if "_autosave" in n]
check(f"at most {recovery.KEEP_AUTOSAVES} safety saves are kept", len(saves3) == recovery.KEEP_AUTOSAVES, saves3)
recovery._file_loaded()
recovery._scene_changed()
check("opening a file does not count as work to save", recovery._state["changed"] is False)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
