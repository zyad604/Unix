# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""End-to-end: real Claude drives real Blender through the bridge.
Run: blender -b --factory-startup --python tests/test_agent_e2e.py -- "prompt" [model]"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.core import engine

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
online = "--online" in argv
argv = [a for a in argv if a not in ("--online", "--roblox")]
from unix.tools import assets, interact
assets.settings["enabled"] = online  # no downloads unless asked for
interact.interactive["enabled"] = False  # nobody is here to answer ask_user
if "--roblox" in sys.argv:               # talk to the Roblox Studio that is really open on this machine
    from unix.bridge import roblox_link
    roblox_link.link.start()
    for _wait in range(60):
        if roblox_link.link.sessions():
            break
        time.sleep(0.25)
    print("ROBLOX", [(s["place"], s["version"]) for s in roblox_link.link.sessions()])
prompt = argv[0] if argv else "Build a simple wooden dining table with four legs, real-world size. Then verify it."
model = argv[1] if len(argv) > 1 else "sonnet"
engine.settings["snapshots"] = False
engine.start()
err = engine.send(prompt, model=model)
print("SEND", err)
t0 = time.time(); seen = 0
while engine.busy and time.time() - t0 < 2400:
    engine.tick()
    for part in engine.messages[-1]["parts"]:
        pass
    flat = []
    for part in engine.messages[-1]["parts"]:
        if part["type"] == "steps":
            flat += [("STEP", f"{'ACT' if s['acted'] else 'OBS'} {s['label']} | {s['detail']} | {s['status']}") for s in part["steps"]]
        else:
            flat.append((part["type"].upper(), part["text"][:600]))
    time.sleep(0.02)
print("=== TRANSCRIPT")
for part in engine.messages[-1]["parts"]:
    if part["type"] == "steps":
        for s in part["steps"]:
            print(f"  {'ACT' if s['acted'] else 'OBS'} {s['label']} | {s['detail']} | {s['status']} | {s['result']}")
    else:
        print(part["type"].upper() + ":", part["text"][:1500])
print("META", engine.messages[-1]["meta"], "| session", engine.session_id, "| secs", round(time.time() - t0, 1))
print("OBJECTS", [(o.name, o.type, [round(d, 2) for d in o.dimensions]) for o in bpy.data.objects])
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_e2e_result.blend")
bpy.ops.wm.save_as_mainfile(filepath=out, copy=True)
engine.shutdown()

