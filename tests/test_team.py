# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Working as a team, without a model: stand-in helpers that call tools through the real server.

* Each helper has a token of its own; its calls are counted under its name, and the tools that
  belong to the lead are refused to it.
* work_in_parallel starts every part, waits for all of them, and reports what each said.
* Stopping one helper, or the turn, ends the wait.
Run: blender -b --factory-startup --python tests/test_team.py"""
import json, os, queue, sys, tempfile, threading, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.core import engine
from unix.tools import _util, registry, team

_util._base_dir = tempfile.mkdtemp(prefix="unix_test_")
engine.start()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:700]}"))
    if not cond:
        fails.append(label)


def rpc(config, name, arguments):
    server = config["mcpServers"]["unix"]
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).encode()
    request = urllib.request.Request(server["url"], data=body, headers={**server["headers"], "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())["result"]


class Helper:
    """What the engine's launcher returns: something with an events queue and stop(), doing work through the server."""

    def __init__(self, name, prompt, seconds=0.0):
        self.events, self.name, self.prompt, self.stopped = queue.Queue(), name, prompt, False
        self.config = engine.server.guest_config(name)
        threading.Thread(target=self._work, args=(seconds,), daemon=True).start()

    def _work(self, seconds):
        made = rpc(self.config, "run_python", {"code": f"o = D.objects.new({self.name!r}, None); C.scene.collection.objects.link(o); result = o.name"})
        refused = rpc(self.config, "ask_user", {"question": "which colour?"})
        again = rpc(self.config, "work_in_parallel", {"parts": [{"name": "a", "brief": "x"}, {"name": "b", "brief": "y"}]})
        waited = 0.0
        while waited < seconds and not self.stopped:
            time.sleep(0.05)
            waited += 0.05
        self.events.put({"kind": "text", "text": "working"})
        self.events.put({"kind": "done", "is_error": False, "cost_usd": 0.25,
                         "text": f"made {self.name}; ask refused={refused['isError']}; split refused={again['isError']}; ok={not made['isError']}"})

    def stop(self):
        self.stopped = True


started = []


def launcher(name, prompt, seconds=0.0):
    helper = Helper(name, prompt, seconds)
    started.append(helper)
    return helper


def pump_while(thread):
    while thread.is_alive():
        engine.dispatcher.pump()
        time.sleep(0.01)


def run_team(**arguments):
    box = {}
    thread = threading.Thread(target=lambda: box.update(out=registry.call("work_in_parallel", arguments)))
    thread.start()
    pump_while(thread)
    out = box["out"]
    return out["isError"], out["content"][0]["text"]


team.launch = None
err, text = run_team(design="d", parts=[{"name": "A", "brief": "x"}, {"name": "B", "brief": "y"}])
check("without an engine to start helpers, the lead is told to build it itself", err and "yourself" in text, text)
team.launch, team.dismiss = launcher, engine.server.dismiss
err, text = run_team(design="d", parts=[{"name": "Only one", "brief": "x"}])
check("one part is not a team", err and "two to" in text, text)
err, text = run_team(design="d", parts=[{"name": "Same", "brief": "x"}, {"name": "same", "brief": "y"}])
check("parts need different names", err, text)

team.state["job"] = "make a house with a garden"
err, text = run_team(parts=[{"name": "House walls", "brief": "The walls, 10 x 7 m."}, {"name": "Garden", "brief": "Lawn and a path."},
                            {"name": "Sun sky", "brief": "Daylight."}], shared="Front faces -Y. Ground at z = 0.", design="A fisherman's cottage with a blue door.")
data = json.loads(text) if not err else {}
check("every part is started and reported", not err and [p["part"] for p in data["parts"]] == ["House walls", "Garden", "Sun sky"]
      and all(p["status"] == "done" for p in data["parts"]) and "not_finished" not in data, text)
check("each helper worked in the scene through the tools", all(n in bpy.data.objects for n in ("House walls", "Garden", "Sun sky")),
      [o.name for o in bpy.data.objects])
check("helpers cannot ask the user or split the work again", all("ask refused=True; split refused=True; ok=True" in p["report"] for p in data["parts"]), text)
check("their tool calls are counted under their names", all(p["tool_calls"] == 1 for p in data["parts"]), text)
brief = started[0].prompt
check("a helper is told the job, its part, what is shared and who else is working", "make a house with a garden" in brief
      and "The walls, 10 x 7 m." in brief and "Front faces -Y" in brief and "blue door" in brief and '"Garden"' in brief and '"Sun sky"' in brief
      and '"House walls", one of 3' in brief, brief[:600])
check("the helpers' cost is handed to the turn once", abs(team.take_cost() - 0.75) < 1e-9 and team.take_cost() == 0.0)
check("their access ends with the job", engine.server.guests == {}, engine.server.guests)
check("the lead's own calls are not counted as a helper's", not registry.call("scene_info", {})["isError"]
      and all(w["steps"] == 1 for w in team.state["workers"]))

team.launch = lambda name, prompt: launcher(name, prompt, seconds=30.0)
box = {}
thread = threading.Thread(target=lambda: box.update(out=registry.call("work_in_parallel", {"design": "d", "parts": [
    {"name": "Slow one", "brief": "x"}, {"name": "Slow two", "brief": "y"}]})))
thread.start()
began = time.time()
while time.time() - began < 3.0 and len([w for w in team.state["workers"] if w["steps"]]) < 2:
    engine.dispatcher.pump()
    time.sleep(0.01)
check("while they work, each helper shows what it is doing", [w["status"] for w in team.state["workers"]] == ["working", "working"]
      and all(w["doing"] == "Ran a script" for w in team.state["workers"]), team.state["workers"])
team.stop("Slow one")
engine.stop_turn()
pump_while(thread)
data = json.loads(box["out"]["content"][0]["text"])
check("stopping ends the wait and says who did not finish", time.time() - began < 15.0 and data.get("not_finished") == ["Slow one", "Slow two"],
      box["out"]["content"][0]["text"])
print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
engine.shutdown()

