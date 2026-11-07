# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Tools that talk to the user through the chat panel: a visible plan and questions.

Neither touches ``bpy`` data, so they run on the worker thread. State lives in plain
Python (never on the Scene) so undo cannot rewind the conversation.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from .registry import ToolError, tool

_lock = threading.Lock()
_plan: List[Dict[str, str]] = []
_question: Dict[str, Any] = {"text": "", "options": [], "answer": None, "event": None}
interactive = {"enabled": True}  # False in headless runs: ask_user fails fast


def get_plan() -> List[Dict[str, str]]:
    with _lock:
        return [dict(step) for step in _plan]


def clear_plan() -> None:
    with _lock:
        _plan.clear()


def pending_question() -> Optional[Dict[str, Any]]:
    with _lock:
        if _question["event"] is None:
            return None
        return {"text": _question["text"], "options": list(_question["options"])}


def answer_question(answer: str) -> None:
    """Called by the UI (or on stop, with an empty answer) to release ask_user."""
    with _lock:
        event = _question["event"]
        _question["answer"] = answer
    if event is not None:
        event.set()


def begin_question(text: str, options: Optional[List[str]] = None) -> Optional[threading.Event]:
    """Put a question to the user without waiting for it (for work that runs on the main thread,
    which must not block). Returns a handle for ``collect_answer``, or None when a question is
    already on screen."""
    event = threading.Event()
    with _lock:
        if _question["event"] is not None:
            return None
        _question.update(text=text.strip()[:600], options=[str(o)[:80] for o in (options or [])[:4]],
                         answer=None, event=event)
    return event


def collect_answer(handle: threading.Event) -> Optional[str]:
    """None while the user has not answered; then their answer ('' if the question was released
    without one, for example by Stop)."""
    if not handle.is_set():
        return None
    with _lock:
        answer = _question["answer"] or ""
        if _question["event"] is handle:
            _question.update(text="", options=[], answer=None, event=None)
    return answer


def withdraw_question(handle: threading.Event) -> None:
    with _lock:
        if _question["event"] is handle:
            _question.update(text="", options=[], answer=None, event=None)


@tool(
    "update_plan",
    """Show or update the step-by-step plan in the user's chat panel. Send the whole list each
    time. Use it for any task with 3 or more steps: write the plan before building, then mark
    steps in_progress and done as you go so the user can follow along.""",
    {"steps": {"type": "array", "description": "Ordered steps.", "items": {
        "type": "object",
        "properties": {"title": {"type": "string"},
                       "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}},
        "required": ["title", "status"]}}},
    ["steps"],
    main_thread=False,
    label="Updated the plan",
)
def update_plan(steps: List[Dict[str, str]]) -> Dict[str, Any]:
    clean = []
    for step in steps[:20]:
        if not isinstance(step, dict) or not str(step.get("title", "")).strip():
            raise ToolError("Each step needs a non-empty 'title' and a 'status'.")
        status = step.get("status", "pending")
        if status not in ("pending", "in_progress", "done"):
            raise ToolError("status must be pending, in_progress or done.")
        clean.append({"title": str(step["title"]).strip()[:120], "status": status})
    with _lock:
        _plan[:] = clean
    done = sum(1 for s in clean if s["status"] == "done")
    return {"steps": len(clean), "done": done}


@tool(
    "ask_user",
    """Ask the user one question and wait for the answer. Use it before a creative build whose
    look is open (what style, what details, what it is for), after a first version (what to
    change), and whenever you are choosing between routes or about to do something destructive.
    One question per call, up to three before a build. Offer 2-4 short options whenever you can:
    the user answers with one click, or types their own. Do not ask for things scene_info can
    tell you or that the message already says.""",
    {
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}, "description": "Optional short choices (max 4)."},
    },
    ["question"],
    main_thread=False,
    timeout=900.0,
    label="Asked you a question",
)
def ask_user(question: str, options: Optional[List[str]] = None) -> Dict[str, Any]:
    if not interactive["enabled"]:
        raise ToolError("No user is available in this session. Make a sensible choice and state the assumption.")
    event = threading.Event()
    with _lock:
        if _question["event"] is not None:
            raise ToolError("A question is already waiting for the user.")
        _question.update(text=question.strip()[:600], options=[str(o)[:80] for o in (options or [])[:4]],
                         answer=None, event=event)
    answered = event.wait(840.0)
    with _lock:
        answer = _question["answer"]
        _question.update(text="", options=[], answer=None, event=None)
    if not answered or not answer:
        raise ToolError("The user did not answer. Proceed with the most reasonable assumption and say what you assumed.")
    return {"answer": answer}
