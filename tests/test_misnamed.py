# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""A tool called by its bare name ("run_python" instead of "mcp__unix__run_python") is not taken for a missing tool
(5 Nov: the agent said "the tool I use to delete objects isn't available" and stopped). No model calls.
Run: blender -b --factory-startup --python tests/test_misnamed.py"""
import json, os, queue, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy  # noqa: F401
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_misnamed_")
from unix import core
from unix.core import engine
from unix.agent import runner

engine.start()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


# -- the stream parser marks a bare name ------------------------------------------------------------
run = runner.AgentRun.__new__(runner.AgentRun)
run.events = queue.Queue()
for name in ("run_python", "mcp__unix__run_python"):
    run._handle({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": name, "name": name, "input": {}}]}})
seen = [run.events.get_nowait() for _ in range(2)]
check("a bare name is marked misnamed", seen[0]["misnamed"] and seen[0]["name"] == "run_python", seen[0])
check("a full name is not", not seen[1]["misnamed"] and seen[1]["name"] == "run_python", seen[1])

# -- the turn is sent back once, with the full name ----------------------------------------------------
runs, prompts = [], []
plays = []      # per run: a list of tool names it calls (bare or full)


class FakeRun:
    def __init__(self, **kwargs):
        self.kwargs, self.events = kwargs, queue.Queue()
        runs.append(self)
        prompts.append(kwargs["prompt"])

    def start(self):
        names = plays.pop(0) if plays else []
        self.events.put({"kind": "session", "session_id": "s", "bridge": "connected", "tools": 50})
        for i, name in enumerate(names):
            bare = name[len(runner.TOOL_PREFIX):] if name.startswith(runner.TOOL_PREFIX) else name
            self.events.put({"kind": "tool_use", "id": f"t{len(runs)}{i}", "name": bare, "input": {},
                             "misnamed": not name.startswith(runner.TOOL_PREFIX)})
            self.events.put({"kind": "tool_result", "id": f"t{len(runs)}{i}", "is_error": True,
                             "summary": "No such tool available", "text": "No such tool available"})
        self.events.put({"kind": "text", "text": "I can't, the tool isn't available."})
        self.events.put({"kind": "done", "text": "", "is_error": False, "stopped": False, "turns": 2,
                         "cost_usd": 0.1, "session_id": "s", "seconds": 1.0})

    def stop(self):
        pass


core.AgentRun = FakeRun


def settle(wanted, limit=4.0):
    end = time.time() + limit
    while time.time() < end:
        engine.tick()
        if len(runs) >= wanted and not engine.busy:
            time.sleep(0.05)
            engine.tick()
            if len(runs) >= wanted and not engine.busy:
                return
        time.sleep(0.01)


plays[:] = [["run_python"], ["run_python"], ["run_python"]]
engine.send("delete the houses", claude_bin="fake")
settle(3)
check("a misnamed call sends the turn back once, not more", len(runs) == 2, len(runs))
check("...telling it the full name", len(runs) == 2 and "mcp__unix__run_python" in prompts[1], prompts[-1][:300])
check("...and the chat says why", any(m.get("auto") and "mistyped" in m.get("text", "") for m in engine.messages if m["role"] == "user"))

runs.clear(); prompts.clear()
plays[:] = [["mcp__unix__run_python"]]
engine.send("list the folder", claude_bin="fake")
settle(1)
check("a real tool error is not sent back", len(runs) == 1, len(runs))

runs.clear(); prompts.clear()
plays[:] = [["run_python"], []]
engine.send("again", claude_bin="fake")
settle(2)
check("a new message may be sent back again", len(runs) == 2, len(runs))

with open(os.path.join(os.path.dirname(core.__file__), "prompts", "system.md"), encoding="utf-8") as fh:
    check("the system prompt names the prefix", "mcp__unix__run_python" in fh.read())
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
