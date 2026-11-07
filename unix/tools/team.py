# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Working as a team: several agents building parts of one job at the same time.

One agent building a house, its garden and its lighting does them one after the other and gives
each a share of its attention. Most of a turn is the model thinking, not Blender working, so
three agents with one part each finish in about the time of the slowest, and each spends all of
its effort on its own part: that is where the small real things (a door handle, a house number,
a porch lamp) come from.

``work_in_parallel`` is called by the lead agent. Each part gets an agent of its own, started by
the engine (``launch``) with the same model, tools and skills as the lead. Tool calls from all of
them arrive at the same server and are run one at a time on Blender's main thread, so the scene
is never changed by two of them at once; what they share is only the order of things. A helper
cannot split work again, ask the user or rewrite the plan (``DENIED``).

No ``bpy`` here: the helpers are processes, and everything they do to the scene goes through
the same tools as the lead.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import _util
from .registry import ToolError, tool

MAX_PARTS = 4
TIME_LIMIT_S = 30 * 60
DENIED = {"work_in_parallel": "You are one of the helpers: build your own part yourself.",
          "ask_user": "Helpers cannot ask the user. Decide yourself and say what you assumed in your last lines.",
          "update_plan": "The plan belongs to the lead. Carry on with your part.",
          "goal_report": "The lead reports on the goal. Finish your part and say what you made."}
COLOURS = ((0.33, 0.85, 0.55), (1.0, 0.62, 0.25), (0.35, 0.80, 0.95), (0.93, 0.45, 0.85))

# Set by the engine: launch(name, prompt) starts an agent and returns its run (see agent.runner.AgentRun);
# dismiss(name) takes its access away again.
launch: Optional[Callable[[str, str], Any]] = None
dismiss: Optional[Callable[[str], None]] = None
state: Dict[str, Any] = {"workers": [], "cost_usd": 0.0}
_lock = threading.Lock()

BRIEF = """[You are "{name}", one of {count} specialists building parts of the same Blender scene at the same time.]

The whole job, as the user asked for it: {job}

The design the lead chose (build towards this, and add to it in its spirit):
{design}

Your part: {name}
{brief}

What everybody on the team has been told:
{shared}

The others, working right now: {others}

How to work as one of a team:
- Build only your part. Never delete, move, rename or restyle anything you did not make; if another part is in your way, work around it and say so at the end.
- Put everything you make in a collection named "{name}" and name each object for what it is. Address objects by name: the active object and the selection change under you while the others work, so set them inside the same script that needs them.
- Leave the camera, the world, the lights, the render settings and the frame range alone unless your part is exactly that.
- Make your part rich. After its main forms, add the smaller things a real one always has (fittings, fixtures, trim, edges, signs, wear) and the things people leave around one, at their real sizes and in their real places: that is what makes it read as real and lived in instead of as a blockout. Invent fitting ones the design does not list.
- Keep it light and quick: a script per stage, not per object; no object over 50,000 triangles (few subdivisions, few segments); the others are waiting for the slowest of you.
- Look at your own work (`look` with your objects as targets, shading 'rendered') and fix what is wrong or plain before you finish. Do not run scene-wide checks: the lead does that when everyone is done.
- Finish with at most three lines: what you made (object names), where it sits (its extent), and anything the lead has to join up."""


def worker_named(name: str) -> Optional[Dict[str, Any]]:
    return next((w for w in state["workers"] if w["name"] == name), None)


def note(name: str, tool_name: str, label: str) -> None:
    """Called by the engine for every tool call a helper makes: what it is doing right now."""
    worker = worker_named(name)
    if worker is not None:
        worker["steps"] += 1
        worker["doing"] = label or tool_name


def stop(name: str) -> None:
    worker = worker_named(name)
    if worker is not None and worker["status"] == "working":
        worker["status"] = "stopped"
        try:
            worker["run"].stop()
        except Exception:
            pass


def stop_all() -> None:
    state["halt"] = True
    for worker in list(state["workers"]):
        stop(worker["name"])


def take_cost() -> float:
    """What the helpers of this turn cost, handed over once (the engine adds it to the turn)."""
    cost, state["cost_usd"] = state["cost_usd"], 0.0
    return cost


def clear() -> None:
    """A new turn: no helpers on show."""
    state.update(workers=[], halt=False)


def _drain(worker: Dict[str, Any]) -> None:
    while True:
        try:
            event = worker["run"].events.get_nowait()
        except queue.Empty:
            return
        kind = event.get("kind")
        if kind == "text":
            worker["said"] = event["text"]
        elif kind == "done":
            worker["seconds"] = round(time.monotonic() - worker["started"], 1)
            worker["said"] = (event.get("text") or worker["said"] or "").strip()
            state["cost_usd"] += float(event.get("cost_usd") or 0.0)
            if worker["status"] == "working":
                worker["status"] = "failed" if event.get("is_error") else "done"
            worker["doing"] = ""


@tool(
    "work_in_parallel",
    """Split a job into parts and have each built at the same time by an agent of its own, then
    get their reports. Use it for any build with two to four parts that do not depend on each
    other step by step: a building and its site and its lighting; the body, the clothes and the
    props of a character; several separate props; a room's furniture, its shell and its light.
    It is faster (the parts take about as long as the slowest one) and better (each agent gives
    its whole effort to one part and details it). First settle what the parts must agree on, and
    give it in `shared`: overall size, where the origin and the ground are, which way the front
    faces, where each part goes with real numbers, the style and the materials' look. Each `brief`
    must stand on its own: what to build, its size and place, what it must meet (a door opening
    2.1 m high at x = 1.5, the roof's eaves at z = 2.7), and how detailed. Do not split a small
    job, or one where each step needs the last. While they work you wait; afterwards look at the
    whole, join the parts up, run check_scene and report.""",
    {
        "parts": {"type": "array", "description": "Two to four parts, built at the same time.",
                  "items": {"type": "object", "properties": {
                      "name": {"type": "string", "description": "Two or three words for the part: 'House facades'."},
                      "brief": {"type": "string", "description": "What to build, its size and place, what it meets, how detailed."},
                  }, "required": ["name", "brief"]}},
        "shared": {"type": "string", "description": "What every part must agree on: sizes, positions, facing, ground, style."},
        "design": {"type": "string",
                   "description": "The particular version of the thing you decided to make, in six to twelve lines, before anyone "
                                  "builds: its character (who made or uses it, where, when), its overall form, the features that "
                                  "make it this one and not the generic one, its materials and colours, and the lived-in details "
                                  "on and around it. Choose the more interesting version, the one a good designer would put in a "
                                  "portfolio; the plainest thing that fits the words is the wrong choice."},
    },
    ["parts", "design"],
    main_thread=False,
    timeout=TIME_LIMIT_S + 120.0,
    mutates=True,
    label="Worked as a team",
)
def work_in_parallel(parts: List[Dict[str, str]], design: str = "", shared: str = "") -> Dict[str, Any]:
    if launch is None:
        raise ToolError("Team work is not available in this session. Build the parts yourself, one after the other.")
    parts = [p for p in parts if isinstance(p, dict) and str(p.get("name", "")).strip() and str(p.get("brief", "")).strip()]
    names = [" ".join(str(p["name"]).split())[:40] for p in parts]
    if not 2 <= len(parts) <= MAX_PARTS or len(set(n.lower() for n in names)) != len(names):
        raise ToolError(f"Give two to {MAX_PARTS} parts with different names, each with a brief. One part is not a team: build it yourself.")
    with _lock:
        if any(w["status"] == "working" for w in state["workers"]):
            raise ToolError("A team is already working. Wait for it.")
        state.update(workers=[], halt=False)
        job = state.get("job") or "(see your part)"
        for index, (name, part) in enumerate(zip(names, parts)):
            others = ", ".join(f'"{n}"' for n in names if n != name)
            prompt = BRIEF.format(name=name, count=len(names), job=job, brief=str(part["brief"]).strip(),
                                  design=design.strip() or "(none given: decide your part's design yourself, and make it a particular one)",
                                  shared=shared.strip() or "(nothing more)", others=others)
            worker = {"name": name, "status": "working", "started": time.monotonic(), "seconds": 0.0, "steps": 0,
                      "doing": "starting", "said": "", "colour": COLOURS[index % len(COLOURS)], "run": None}
            try:
                worker["run"] = launch(name, prompt)
            except Exception as exc:
                worker.update(status="failed", said=f"could not start: {exc}", doing="")
            state["workers"].append(worker)
    deadline = time.monotonic() + TIME_LIMIT_S
    try:
        while any(w["status"] == "working" for w in state["workers"]):
            for worker in state["workers"]:
                if worker["run"] is not None:
                    _drain(worker)
            busy = sum(1 for w in state["workers"] if w["status"] == "working")
            _util.status["text"] = f"Team: {busy} of {len(names)} working"
            if state.get("halt") or time.monotonic() > deadline:
                for worker in state["workers"]:
                    stop(worker["name"])
                break
            time.sleep(0.15)
        for worker in state["workers"]:
            if worker["run"] is not None:
                _drain(worker)
    finally:
        _util.status["text"] = ""
        if dismiss is not None:
            for name in names:
                dismiss(name)
    report = [{"part": w["name"], "status": w["status"], "seconds": w["seconds"] or round(time.monotonic() - w["started"], 1),
               "tool_calls": w["steps"], "report": (w["said"] or "")[:900]} for w in state["workers"]]
    out: Dict[str, Any] = {"parts": report,
                           "next": "The parts were built side by side and nobody has looked at the whole. Do that now: "
                                   "scene_info, a look at everything, check_scene; join up what does not meet; finish what a "
                                   "part left undone yourself. Their reports are what they say they did, not what you have seen."}
    unfinished = [w["name"] for w in state["workers"] if w["status"] != "done"]
    if unfinished:
        out["not_finished"] = unfinished
    return out
