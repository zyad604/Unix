# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Goals ("/goal ..."): rounds carry on until a fresh reviewer agrees the goal is met. No model calls:
a stand-in for the CLI plays each round and files the round's report.
Run: blender -b --factory-startup --python tests/test_goal.py"""
import os, queue, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_goal_")  # keep test data out of the user's config
from unix import core
from unix.core import engine
from unix.tools import goal, registry

engine.start()
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


runs = []
script = []          # what each next round does: a report (dict), "error", or None (no report)


class FakeRun:
    """Plays one round: files the next scripted report the way the goal_report tool would, then finishes."""

    def __init__(self, **kwargs):
        self.kwargs, self.events = kwargs, queue.Queue()
        runs.append(self)

    def start(self):
        what = script.pop(0) if script else None
        session = self.kwargs["resume"] or f"session-{len(runs)}"
        self.events.put({"kind": "session", "session_id": session, "bridge": "connected", "tools": 50})
        if isinstance(what, dict):
            what = dict(what)
            if what.pop("touch", False):     # the round changes the scene (re-imports something, say)
                registry.call("run_python", {"code": "import bpy\nbpy.data.objects.new('Touched', None)\nresult = 1",
                                             "purpose": "touch the scene"})
            result = registry.call("goal_report", what)
            self.events.put({"kind": "text", "text": "reported" if not result["isError"] else result["content"][0]["text"]})
        error = what == "error"
        self.events.put({"kind": "done", "text": "usage limit reached" if error else "ok", "is_error": error, "stopped": False,
                         "turns": 3, "cost_usd": 0.5, "session_id": session, "seconds": 1.0})

    def stop(self):
        pass


core.AgentRun = FakeRun


def settle(rounds_wanted, limit=5.0):
    """Tick until that many rounds have run and the engine is idle (or the time is up)."""
    end = time.time() + limit
    while time.time() < end:
        engine.tick()
        if len(runs) >= rounds_wanted and not engine.busy:
            return True
        time.sleep(0.01)
    return False


def report(done, remaining=(), met=(), next_=""):
    return {"done": done, "remaining": list(remaining), "met": list(met), "evidence": "looked from four sides", "next": next_}


# -- outside a goal the report is refused ----------------------------------------------------------
res = registry.call("goal_report", report(True))
check("goal_report is refused when no goal is set", res["isError"] and "No goal" in res["content"][0]["text"])

# -- a goal: rounds until a fresh check agrees ---------------------------------------------------------
script[:] = [report(False, ["blue sphere"], ["red cube"], "add the sphere"),       # round 1
             report(True, [], ["red cube", "blue sphere"]),                          # round 2 says done
             report(False, ["the sphere is green, not blue"]),                       # the check disagrees
             report(True, [], ["red cube", "blue sphere"]),                          # round 3 says done
             report(True, [])]                                                       # the check agrees
error = engine.send("/goal build a red cube and a blue sphere", claude_bin="fake")
check("/goal starts a round", error is None and len(runs) == 1, error)
import json as _json
with open(engine._chat_path(engine.chat_id), encoding="utf-8") as _fh:
    _stored = _json.load(_fh)
check("the goal is on disk the moment it starts, not only when its first round ends (a crash in a long round kept nothing)",
      (_stored.get("goal") or {}).get("state") == "working" and _stored["messages"][-2]["text"].startswith("/goal"), _stored.get("goal"))
from unix import recovery as _recovery
_offer = _recovery.find_offer(ignore_seen=True)
check("so Unix dying mid-round is found on the next start, with the goal to carry on",
      _offer is not None and _offer["chat_id"] == engine.chat_id and "red cube" in _offer["goal"], _offer)
check("the chat shows what the user typed; the agent gets the goal brief",
      engine.messages[-2]["text"] == "/goal build a red cube and a blue sphere"
      and runs[0].kwargs["prompt"].count("[Goal]") == 1 and "a red cube and a blue sphere" in runs[0].kwargs["prompt"],
      runs[0].kwargs["prompt"][:300])
check("ask_user is refused while a goal runs", engine._call_tool("ask_user", {"question": "which colour?"})["isError"])
settle(5)
prompts = [r.kwargs["prompt"] for r in runs]
check("five rounds ran: work, work, check, work, check", len(runs) == 5, [p[:40] for p in prompts])
if len(runs) == 5:
    check("round 2 carries on from the report, in the same conversation",
          prompts[1].startswith("[Goal, round 2") and "blue sphere" in prompts[1] and runs[1].kwargs["resume"] == "session-1",
          (prompts[1][:400], runs[1].kwargs["resume"]))
    check("a 'done' is checked by a fresh agent, not taken on its word",
          prompts[2].startswith("[Goal check]") and runs[2].kwargs["resume"] is None, (prompts[2][:200], runs[2].kwargs["resume"]))
    check("the check's findings go back to the conversation that did the work",
          prompts[3].startswith("[Goal, round 3") and "the sphere is green" in prompts[3] and "strict check" in prompts[3]
          and runs[3].kwargs["resume"] == "session-1", (prompts[3][:500], runs[3].kwargs["resume"]))
check("only the fresh check's 'done' ends the goal", engine.goal["state"] == "done" and not goal.state["active"]
      and "Goal reached" in engine.messages[-1]["parts"][0]["text"], engine.goal)
check("the goal adds up its rounds' cost", abs(engine.goal["cost"] - 2.5) < 1e-6, engine.goal.get("cost"))
check("the island has a line for it", engine.goal_line().startswith("Goal · reached"), engine.goal_line())

# -- a report that says done while listing missing things is refused -------------------------------------
goal.state["active"] = True
res = registry.call("goal_report", report(True, ["the roof"]))
check("done with things still missing is refused", res["isError"] and "not done" in res["content"][0]["text"])
goal.state["active"] = False

# -- a failed round (a usage limit) waits and tries again, from a fresh conversation ------------------------
engine.new_chat()
runs.clear()
script[:] = ["error", report(False, ["everything"])]
engine.send("/goal a house", claude_bin="fake")
settle(1)
check("a failed round waits instead of giving up", engine.goal["state"] == "waiting" and engine.goal["retry_at"] > time.time()
      and "Trying again" in engine.messages[-1]["parts"][0]["text"], engine.goal)
engine.goal["retry_at"] = time.time() - 1
script.append(None)          # the round after that files no report at all; the goal then goes on round after round
settle(4, limit=1.0)
check("when the wait is over the next round starts afresh", len(runs) >= 2 and runs[1].kwargs["resume"] is None
      and runs[1].kwargs["prompt"].startswith("[Goal, round 1"), [r.kwargs["resume"] for r in runs])
check("a round without a report is told to end with one", len(runs) >= 4 and "without goal_report" in runs[3].kwargs["prompt"],
      runs[3].kwargs["prompt"][:400] if len(runs) >= 4 else runs)


def drain():
    end = time.time() + 3
    while engine.busy and time.time() < end:
        engine.tick()
        time.sleep(0.01)


check("rounds that change nothing pause the goal by themselves", engine.goal["state"] == "paused", engine.goal.get("state"))

# -- Stop ends it; a reopened chat can carry on with /goal continue -----------------------------------------
engine.new_chat()
runs.clear()
script[:] = [report(False, ["the roof"])] * 3
engine.send("/goal a house with a roof", claude_bin="fake")
engine.stop_turn()                           # pressed while the first round runs
drain()
check("Stop ends the goal, and no round follows", engine.goal["state"] == "stopped" and not engine.busy and len(runs) == 1
      and any("Goal stopped" in p.get("text", "") for m in engine.messages[-3:] for p in m.get("parts", [])), len(runs))
engine.goal["state"] = "working"            # as if Blender had closed in the middle of a round
engine.save_chat()
chat = engine.chat_id
engine.goal, engine.messages = None, []      # Blender starts again
engine.new_chat()
check("a new chat has no goal", engine.goal is None)
engine.open_chat(chat)
check("a goal cut off by closing Blender comes back paused", engine.goal and engine.goal["state"] == "paused", engine.goal)
runs.clear()
script[:] = [report(False, ["the roof"])]
error = engine.send("/goal continue", claude_bin="fake")
check("/goal continue picks it up", error is None and runs and runs[0].kwargs["prompt"].startswith("[Goal, round")
      and engine.messages[-2]["text"] == "/goal continue", error)
engine.stop_turn()
drain()
check("/goal status says where it stands", engine.send("/goal status", claude_bin="fake").startswith("Goal · stopped"))
check("/goal stop with nothing running says so", engine.send("/goal stop", claude_bin="fake") == "No goal is running.")

# -- rounds that change nothing pause the goal instead of spending on ------------------------------------------
engine.new_chat()
runs.clear()
script[:] = [report(False, ["sections 4 and 5 of the spec never arrived"])] * 6
engine.send("/goal a map from the spec", claude_bin="fake")
settle(3, limit=2.0)
check("three rounds that change nothing pause the goal and say why", engine.goal["state"] == "paused" and len(runs) == 3
      and "/goal continue" in engine.messages[-1]["parts"][0]["text"], (engine.goal.get("state"), len(runs)))

# -- rounds that touch the scene but leave the same things to do pause too (5 Nov: nine rounds re-imported broken files)
engine.new_chat()
runs.clear()
script[:] = [dict(report(False, [f"12 source files are still empty on disk; re-tested {names} this round, each imports 0 "
                                 "objects: Characters/Male/Torsos/{ExquisiteTunic,FineDress,WarmCoat}/S_Item_*_Drop.obj"]),
                  touch=True) for names in ("ExquisiteTunic", "WarmCoat and IronArmour", "FineDress", "RichCoat", "LightDress")]
engine.send("/goal every model in the folder", claude_bin="fake")
settle(3, limit=3.0)
check("three rounds with the same things left to do pause the goal, however worded and whatever they touched",
      engine.goal["state"] == "paused" and len(runs) == 3 and "same things to do" in engine.messages[-1]["parts"][0]["text"],
      (engine.goal.get("state"), len(runs), engine.goal.get("stalled"), engine.goal.get("idle")))
check("a to-do list that moved is not the same", not core._same_todo(["rig the fox", "rig the badger"], ["import the trailers"]))
check("a reworded one is", core._same_todo(["12 files still empty; re-tested A: Torsos/{A,B}/S_Item_*_Drop.obj"],
                                           ["12 files are still empty; re-tested B this round: Torsos/{A,B}/S_Item_*_Drop.obj"]))

# -- a pasted document arrives whole ------------------------------------------------------------------------
from unix.ui import island
check("the text box keeps a long pasted document whole", island.COMPOSER_MAX >= 100_000)

# -- a computer that dies mid-save: the file is zeros, the last good copy is kept beside it --------------
engine.save_chat()
engine.save_chat()
damaged = engine._chat_path(engine.chat_id)
size = os.path.getsize(damaged)
with open(damaged, "wb") as fh:
    fh.write(bytes(size))              # zeros, as the crash left it
kept_id = engine.chat_id
engine.new_chat()
check("a chat file a PC crash zeroed still shows in History, from its last good copy",
      any(row["id"] == kept_id for row in engine.list_chats()), engine.list_chats()[:3])
check("and opens", engine.open_chat(kept_id) is None and engine.messages, engine.chat_id)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
