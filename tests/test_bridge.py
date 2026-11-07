# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Bridge tests that need no Blender: MCP transport, auth, dispatcher, registry, guard.
Run: python tests/test_bridge.py"""
import importlib.util, json, os, sys, threading, time, urllib.error, urllib.request

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unix")

def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec); sys.modules[name] = mod; spec.loader.exec_module(mod); return mod

mcp = load("mcp_server", "bridge/mcp_server.py")
main_thread = load("main_thread", "bridge/main_thread.py")
registry_mod = load("registry", "tools/registry.py")
guard = load("guard", "tools/guard.py")
fails = []

def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {detail}"))
    if not cond: fails.append(label)

# registry
reg = registry_mod.Registry()
@reg.tool("add", "Add two numbers.", {"a": {"type": "integer"}, "b": {"type": "integer"}}, ["a", "b"], main_thread=False)
def add(a, b): return {"sum": a + b}
@reg.tool("boom", "Always fails.", main_thread=False)
def boom(): raise RuntimeError("kaput")
res = reg.call("add", {"a": 2, "b": 3}); check("registry call", json.loads(res["content"][0]["text"]) == {"sum": 5} and not res["isError"])
check("registry missing arg", reg.call("add", {"a": 1})["isError"])
check("registry wrong type", "must be integer" in reg.call("add", {"a": "x", "b": 1})["content"][0]["text"])
check("registry unknown tool", reg.call("nope", {})["isError"])
res = reg.call("boom", {}); check("registry error is a result", res["isError"] and "kaput" in res["content"][0]["text"])

# transport + auth
srv = mcp.McpServer(reg.descriptors, reg.call); srv.start()
def rpc(payload, token=None):
    req = urllib.request.Request(srv.url, json.dumps(payload).encode(), {"Content-Type": "application/json",
                                 "Authorization": f"Bearer {token or srv.token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read(); return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None
status, body = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
check("mcp initialize", status == 200 and body["result"]["serverInfo"]["name"] == "china-unix", body)
check("mcp notification -> 202", rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})[0] == 202)
status, body = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}); check("mcp tools/list", [t["name"] for t in body["result"]["tools"]] == ["add", "boom"], body)
status, body = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "add", "arguments": {"a": 4, "b": 5}}})
check("mcp tools/call", json.loads(body["result"]["content"][0]["text"])["sum"] == 9, body)
check("mcp unknown method", rpc({"jsonrpc": "2.0", "id": 4, "method": "nope"})[1]["error"]["code"] == -32601)
check("mcp bad token -> 401", rpc({"jsonrpc": "2.0", "id": 5, "method": "ping"}, token="wrong")[0] == 401)
srv.stop(); check("mcp stops", not srv.running)

# dispatcher
disp = main_thread.MainThreadDispatcher(); out = {}
t = threading.Thread(target=lambda: out.setdefault("v", disp.submit(lambda: threading.get_ident(), 5))); t.start()
deadline = time.time() + 3
while t.is_alive() and time.time() < deadline: disp.pump(); time.sleep(0.005)
check("dispatcher runs job on main thread", out.get("v") == threading.get_ident(), out)
err = {}
def worker():
    try: disp.submit(lambda: 1 / 0, 5)
    except ZeroDivisionError: err["ok"] = True
t = threading.Thread(target=worker); t.start()
while t.is_alive(): disp.pump(); time.sleep(0.005)
check("dispatcher re-raises on the worker", err.get("ok"))
timed = {}
def slow():
    try: disp.submit(lambda: 1, 0.2)
    except main_thread.DispatchTimeout: timed["ok"] = True
t = threading.Thread(target=slow); t.start(); t.join(); disp.pump()
check("dispatcher timeout when main thread is busy", timed.get("ok"))

# guard
code, problem = guard.prepare("for m in obj.modifiers:\n    obj.modifiers.remove(m)"); check("guard compiles + rewrites", code is not None and problem is None)
check("guard blocks subprocess", guard.prepare("import subprocess")[1] is not None)
check("guard blocks os.remove", "os.remove" in (guard.prepare("import os\nos.remove('a')")[1] or ""))
check("guard blocks eval", guard.prepare("eval('1')")[1] is not None)
check("guard blocks file open op", guard.prepare("bpy.ops.wm.open_mainfile(filepath='x')")[1] is not None)
check("guard unrestricted allows", guard.prepare("import subprocess", unrestricted=True)[1] is None)
check("guard syntax error message", "SyntaxError" in (guard.prepare("def f(:")[1] or ""))
ns = {"items": type("O", (), {"mods": [1, 2, 3]})()}; code, _ = guard.prepare("out = []\nfor m in items.mods:\n    out.append(m)")
exec(code, ns); check("rewrite keeps behaviour", ns["out"] == [1, 2, 3])

# clipboard pictures (the conversion only: the test never writes to the clipboard)
import struct, tempfile
load("window_shot", "bridge/window_shot.py")
clipboard = load("clipboard", "bridge/clipboard.py")
header = struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 24, 0, 16, 0, 0, 0, 0)
# 24-bit rows are stored bottom-up, blue first, each padded to four bytes: bottom row blue, green; top row red, white
rows = bytes([255, 0, 0, 0, 255, 0, 0, 0]) + bytes([0, 0, 255, 255, 255, 255, 0, 0])
picture = clipboard.dib_to_rgb(header + rows)
check("a copied 24-bit picture is read top-down as RGB", picture == (2, 2, bytes([255, 0, 0, 255, 255, 255, 0, 0, 255, 0, 255, 0])), picture)
header = struct.pack("<IiiHHIIiiII", 40, 1, -2, 1, 32, 0, 8, 0, 0, 0, 0)        # a negative height: rows already top-down
picture = clipboard.dib_to_rgb(header + bytes([10, 20, 30, 0, 40, 50, 60, 0]))
check("a 32-bit top-down picture too, whatever its alpha says", picture == (1, 2, bytes([30, 20, 10, 60, 50, 40])), picture)
check("a kind of bitmap it cannot read is refused, not guessed", clipboard.dib_to_rgb(struct.pack("<IiiHHIIiiII", 40, 2, 2, 1, 8, 0, 8, 0, 0, 0, 0) + bytes(8)) is None)
big = struct.pack("<IiiHHIIiiII", 40, 5000, 10, 1, 24, 0, 0, 0, 0, 0, 0) + bytes(5000 * 3 * 10)
picture = clipboard.dib_to_rgb(big)
check("a huge picture is thinned to a sensible size", picture is not None and picture[0] <= clipboard.MAX_SIDE and len(picture[2]) == picture[0] * picture[1] * 3,
      picture and picture[:2])
out = os.path.join(tempfile.mkdtemp(prefix="unix_clip_"), "p.png")
clipboard.save_png(out, 2, 2, bytes([255, 0, 0, 255, 255, 255, 0, 0, 255, 0, 255, 0]))
check("and written as a PNG", open(out, "rb").read(8) == b"\x89PNG\r\n\x1a\n")
found = clipboard.grab(tempfile.mkdtemp(prefix="unix_clip_"))
check("reading the real clipboard never fails, whatever is on it", isinstance(found, list) and all(os.path.isfile(p) for p in found), found)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
