# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""A goal: something the user wants done in full, worked on round after round until it is.

The user starts one by typing ``/goal`` and what they want. The engine (``Engine._goal_*`` in
``core.py``) then sends the agent back after every turn, for as many hours as it takes, and
decides from the agent's own report whether to carry on. That report is this tool: every round
ends with ``goal_report``, which says what of the goal is met, what is still missing and what
it was checked with. A round that says "done" is not believed on its word: the engine starts a
fresh agent, which knows nothing of how the work went, to check the scene against the goal as a
strict reviewer, and only its "done" ends the goal.

No ``bpy`` here: the report is data for the engine.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .registry import ToolError, tool

# ``active``: a goal is being worked on (set by the engine). ``report``: the last report of this round.
state: Dict[str, Any] = {"active": False, "report": None}


def _lines(items: Optional[List[Any]]) -> List[str]:
    return [" ".join(str(item).split())[:300] for item in (items or []) if str(item).strip()][:30]


@tool(
    "goal_report",
    """Only while a goal is set (the message starts with [Goal] or [Goal check]). End every round
    by calling this once, as your last step: whether the whole goal is met, what of it is met,
    what is still missing, and how you checked. `done` is true only when every part of the goal
    as the user wrote it is in the scene, works and looks right, and you have seen that yourself
    in this round (look, check_scene, look_motion for animation, a send's report for an engine):
    "mostly", "a first version" or "should work" is not done. When it is not done, `remaining`
    lists exactly what is missing or wrong, one item per line, so the next round can start on it
    without looking for it. After this call, reply in at most three lines.""",
    {
        "done": {"type": "boolean", "description": "True only if every part of the goal is met and seen."},
        "met": {"type": "array", "items": {"type": "string"}, "description": "The parts of the goal that are met, briefly."},
        "remaining": {"type": "array", "items": {"type": "string"},
                      "description": "What is still missing or wrong, precisely. Empty only when done."},
        "evidence": {"type": "string", "description": "How you checked this round: what you looked at and what it showed."},
        "next": {"type": "string", "description": "What the next round should do first."},
    },
    ["done", "remaining", "evidence"],
    main_thread=False,
    label="Reported on the goal",
)
def goal_report(done: bool, remaining: List[str], evidence: str, met: Optional[List[str]] = None,
                next: str = "") -> Dict[str, Any]:
    if not state["active"]:
        raise ToolError("No goal is set. Use this only when the user has started a goal with /goal.")
    remaining = _lines(remaining)
    if done and remaining:
        raise ToolError("done is true but remaining is not empty. If anything is still missing, the goal is not done: "
                        "call again with done false, or remove what is in fact finished from remaining.")
    state["report"] = {"done": bool(done), "met": _lines(met), "remaining": remaining,
                       "evidence": " ".join(str(evidence).split())[:1200], "next": " ".join(str(next).split())[:600]}
    return {"recorded": True, "note": "Recorded. Stop here: reply in at most three lines."}
