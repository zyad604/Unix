# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""The engine: owns the bridge, runs agent turns, and holds the chat state.

Threads:
- Blender main thread: ``Engine.tick`` (a persistent ``bpy.app.timers`` callback) pumps
  queued tool calls, drains agent events and asks the UI to redraw.
- MCP HTTP threads: receive tool calls and block on the dispatcher.
- Agent reader thread: parses the Claude CLI's event stream into a queue.

Chat state is plain Python on purpose: anything stored on the Scene would be rewound by
undo, and one Ctrl+Z is supposed to revert the agent's turn, not the conversation. Chats
are archived as JSON sidecar files at every turn end, so they survive a crash.
"""

from __future__ import annotations

import json
import os
import re
import queue
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import bpy

from . import tools
from .agent.runner import AgentRun, CodexRun, find_claude, find_codex
from .bridge.main_thread import MainThreadDispatcher, drain
from .bridge import mcp_server
from .bridge.mcp_server import McpServer
from .tools import _turn, files, goal, interact, knowledge, registry, team
from .tools._util import data_dir, read_kept, ui_context, write_kept

PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
# The models offered: every Sonnet and Opus from 4.6 to 5.5, by the exact id the Claude CLI takes
# (each one checked against the CLI). A family's newest comes first. The Codex runner is still
# there (model "codex"), but it is not offered in the picker.
MODELS = (
    ("claude-sonnet-5-5", "Sonnet 5.5", "The newest Sonnet. Fast and strong"),
    ("claude-sonnet-5", "Sonnet 5", "The Sonnet before it"),
    ("claude-sonnet-4-6", "Sonnet 4.6", "An older Sonnet"),
    ("claude-opus-5-5", "Opus 5.5", "The newest Opus. Most capable; the default"),
    ("claude-opus-5", "Opus 5", "The Opus before it"),
    ("claude-opus-4-8", "Opus 4.8", "An older Opus"),
    ("claude-opus-4-7", "Opus 4.7", "An older Opus"),
    ("claude-opus-4-6", "Opus 4.6", "The oldest Opus offered"),
)
DEFAULT_MODEL = "claude-opus-5-5"     # the user's choice: quality first
EFFORTS = (
    ("low", "Low", "Fastest. Good for small edits"),
    ("medium", "Medium", "Balanced. The default"),
    ("high", "High", "Thinks harder. Better on complex builds"),
    ("max", "Max", "Slowest and most thorough"),
)
MAX_CHATS = 40
KEEP_INPUTS = 60            # messages at the end of a chat whose steps keep their inputs (see save_chat)
CHAT_SAVE_S = 30.0          # while Unix works the chat (and its goal) is written this often: a round of a goal
                            # ran 70 minutes, a crash in it lost the chat, the goal and the offer to carry on
_3D_EXTS = {".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply", ".usd", ".usdc", ".usda", ".usdz", ".abc", ".bvh", ".blend"}


def _step_detail(name: str, args: Dict[str, Any]) -> str:
    """Short human detail for a tool call, e.g. the script's purpose or the object name."""
    for key in ("purpose", "name", "object", "query", "id", "question", "style", "path", "label"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return (os.path.basename(value) if key == "path" else value.strip())[:70]
    for key in ("names", "objects", "targets", "views"):
        value = args.get(key)
        if isinstance(value, list) and value:
            return ", ".join(str(v) for v in value[:4])[:70]
    return ""


def _trim_input(args: Dict[str, Any], limit: int = 20_000) -> Dict[str, Any]:
    """A copy of a tool call's arguments with very long strings cut, safe to store in the chat."""
    out = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > limit:
            value = value[:limit] + f"\n...[{len(value) - limit} more characters]"
        out[key] = value
    return out


def _count(names: Any) -> int:
    return len(names) if isinstance(names, list) else 0


def _friendly_result(name: str, text: str, is_error: bool) -> str:
    """Turn a tool's JSON reply into a few words for the step row."""
    if is_error:
        return "failed"
    try:
        data = json.loads(text.split(" [+", 1)[0]) if text.lstrip().startswith("{") else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return ""
    if name == "run_python":
        if not data.get("ok", True):
            return "script error"
        changes = data.get("changes") or {}
        bits = [f"{_count(changes.get(k))} {k}" for k in ("created", "modified", "deleted") if _count(changes.get(k))]
        return " · ".join(bits) or "no object changes"
    if name in {"check_mesh", "check_uv"}:
        bad = data.get("with_issues", 0)
        return f"{data.get('checked', 0)} checked, " + (f"{bad} with issues" if bad else "all clean")
    if name in {"check_materials", "check_scene", "check_motion", "check_rig"}:
        issues = _count(data.get("issues"))
        return f"{issues} issue{'s' if issues != 1 else ''}" if issues else "clean"
    if name == "look_motion":
        return f"{_count(data.get('frames'))} frames of {data.get('action') or 'the move'}"
    if name == "retarget_motion":
        return f"{data.get('action', '')}: {data.get('seconds', 0)} s" + (", loops" if data.get("loops") else "")
    if name == "motion_search":
        return f"{data.get('found', 0)} found"
    if name == "work_in_parallel":
        parts = data.get("parts") or []
        return f"{sum(1 for p in parts if p.get('status') == 'done')} of {len(parts)} parts done"
    if name == "look":
        return f"{_count(data.get('views'))} view{'s' if _count(data.get('views')) != 1 else ''}"
    if name == "asset_search":
        return f"{_count(data.get('results'))} found"
    if name in {"import_file", "asset_model"}:
        return f"{_count(data.get('imported'))} objects"
    if name == "export_file":
        return f"{data.get('bytes', 0) / 1e6:.1f} MB"
    if name == "update_plan":
        return f"{data.get('done', 0)}/{data.get('steps', 0)} done"
    return ""


REVIEW_ROUNDS = 2

# A goal (typed as "/goal ..."): rounds go on until a fresh reviewer agrees it is met, or for this long.
GOAL_HOURS = 24.0
GOAL_FRESH_EVERY = 12           # rounds on one conversation before the next starts afresh from the goal's notes
GOAL_RETRY_S = (60, 300, 900, 1800)     # after a failed round (a usage limit, the network), wait this long and try again
GOAL_FIRST = (
    "[Goal] The user has set a goal. You will be sent back round after round, for hours if need be, until it is met, "
    "and nobody is watching: do not ask questions, decide, and say what you decided in your report.\n"
    "The goal, exactly as the user wrote it:\n{goal}\n\n"
    "This round: set out the whole job as a plan (update_plan), then build as much of it as you can, properly and at "
    "the quality the goal asks for, checking your own work with look and check_scene as you go. End the round with "
    "goal_report: done only if every part of the goal is met and you have seen it; otherwise exactly what is missing.")
GOAL_NEXT = (
    "[Goal, round {round}, {elapsed} in] Keep going until the goal is met. Nobody is watching: do not ask, decide.\n"
    "The goal, exactly as the user wrote it:\n{goal}\n\n"
    "From the last report. Met: {met}\nStill missing: {remaining}\nNext: {next}\n{note}\n"
    "First see where the scene really stands (scene_info and a look), then work on what is missing. End the round with "
    "goal_report.")
GOAL_VERIFY = (
    "[Goal check] A goal was set and the work on it says it is finished. Check that as a strict reviewer who did not "
    "build it: assume something is missing until you have seen otherwise.\n"
    "The goal, exactly as the user wrote it:\n{goal}\n\n"
    "Go through it phrase by phrase. Find each part in the scene (scene_info; look from several sides and through the "
    "camera; check_scene; look_motion for anything animated) and judge whether it is there and good enough for what "
    "the user asked. Fix small things yourself. Then call goal_report: done only if every part is met; otherwise list "
    "precisely what is missing or not good enough.")
_REALISM = re.compile(r"\b(realis\w*|real[- ]?life|real[- ]world|photo-?real\w*|life-?like|accurate\w*|exact\w*|"
                      r"detailed|high[- ]?quality|true to life|looks? real|same as|identical)\b", re.I)
REVIEW_PROMPT = (
    "[Automatic review] The user asked for realism, and what you have is a first version. Do not report yet: "
    "improve it now. Improve what is there; do not delete good parts to rebuild them.\n"
    "1. If you have not looked at photos of the real thing in this chat (and the user attached none), call "
    "reference_search and study them.\n"
    "2. Call look_around: all six sides in one picture. Then match_reference (view 'front', shading 'rendered') with "
    "the photo you built from, which also measures the outline and the openings.\n"
    "3. Name the biggest differences a stranger would notice, most visible first, checking every side: an end or top "
    "view that is a stack of rectangles (parts need rounded cross-sections and their real, different widths); the "
    "outline and what the measurement says is missing or closed up; whole parts that are missing; any feature on the "
    "wrong side, at the wrong end or facing the wrong way; small parts that are plain blocks or generic shapes (look "
    "up a close-up of each distinctive one with reference_search and build what it shows); round parts that are not "
    "lathe_solid, and tubes or barrels with no bore; detail that exists only in the photo texture but changes the "
    "outline or is a hole; parts of one thickness from end to end; sides the photo did not cover that look "
    "unfinished; surface and light.\n"
    "4. Fix them in that order, with profile_solid for shapes and project_reference for the surface once the form is "
    "right. Keep each object inside the triangle budget check_scene reports.\n"
    "5. Look around once more. If a side still reads as boxes, fix that before anything else.\n"
    "Then reply in at most four short lines: what you changed, and what still differs from the real thing.")
MISNAMED_PROMPT = (
    "Your call to {bare} failed only because the name was written without its `mcp__unix__` prefix. Nothing is "
    "unavailable: the tool is there and works, as {full}. Every Blender tool you have is named that way. Do not tell "
    "the user a tool is missing. Now do everything that was asked, in full, using {full} where you need it."
)

MOTION_REVIEW_PROMPT = (
    "[Automatic review] You made or changed animation this turn and did not watch all of it play ({actions}). A pose "
    "that looks right in one frame can belong to a move that is wrong. Do not report yet.\n"
    "1. For each of those actions: look_motion (from the side and from the front), then check_motion. Check that each "
    "one moves the way its name says (check_motion reports which way, seen from the character).\n"
    "2. Name what a stranger would call wrong, worst first: the body leaning or reaching the wrong way for where it is "
    "going; arms held out from the body, or through it; feet sinking into the floor, hanging over it or sliding; one "
    "limb through another; a torso that never moves; a jump where a loop repeats.\n"
    "3. Fix those. For a move a person makes (walk, run, sidestep, back up, turn, start, stop, jump), replace "
    "typed-in motion with a recording: motion_search, then retarget_motion.\n"
    "4. Watch each action you fixed once more.\n"
    "Then reply in at most four short lines: what you changed, and what is still not right.")


# What the island says while a call to these tools is still being written (a script can take a minute)
_COMING = {"run_python": "Writing a script", "look": "Getting ready to look", "look_around": "Getting ready to look",
           "vfx_column": "Setting up the effect", "vfx_create": "Setting up the effect", "send_to_engine": "Getting ready to send",
           "work_in_parallel": "Planning the team's parts", "retarget_motion": "Choosing the motion",
           "check_scene": "Getting ready to check the scene"}

_BUILD = re.compile(r"\b(make|build|create|model|design|generate|construct|sculpt)\b", re.I)
TEAM_GATE = (
    "Not yet: nothing was changed. Decide how to do this job first.\n"
    "If what was asked for has parts that can be built side by side (a building with its grounds, a room with its "
    "furniture, a character with clothes and gear, a vehicle, a set of props, any scene), call work_in_parallel now. "
    "Write the `design` first: a particular, interesting version of what was asked, not the plainest thing that fits "
    "the words. Put the sizes, positions, facing, ground level and style in `shared`. Give two to four parts with a brief each, "
    "and make sure the parts together cover what makes the result rich and alive, not only the main thing: its "
    "surroundings, its light, and the smaller things around and on it.\n"
    "Only if this is one thing (one object, one visual effect, one animation), or a change to something already in "
    "the scene, call {tool} again and do it yourself.")


NATIVE_GATE = (
    "Not yet: nothing was built. Things are made in Unix and then sent. If the user asked for something to be made "
    "and to end up in Roblox, make it here first (vfx_column or vfx_create for an effect, modelling for an object), look "
    "at it, and call send_to_engine: what arrives is what they watched being made, and an effect arrives as a flipbook "
    "of its own frames. Do not build it in Studio instead, and do not rebuild by hand something that was sent.\n"
    "Call roblox_build again only if the user's own words ask for something made or changed inside Roblox Studio itself "
    "(a Luau script, UI, a control, an effect they want built from Roblox's own emitters), or to add to a sent asset "
    "what cannot travel.")


def _same_todo(now: List[str], before: List[str]) -> bool:
    """Is a goal's to-do list the same as last round's, reworded or not? Same when most of its words are."""
    def words(items):
        return {w for w in re.findall(r"[a-z0-9_]{3,}", " ".join(items).lower())}
    a, b = words(now), words(before)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.6


def _actions_now() -> Dict[str, Tuple[float, float]]:
    """Every action that moves a skeleton and the frames it covers: enough to tell whether a turn made
    or changed character animation (effects and plain object moves are not reviewed this way)."""
    try:
        from .tools import motion
        return motion.body_actions()
    except Exception:
        return {}


class Engine:
    def __init__(self) -> None:
        self.dispatcher = MainThreadDispatcher()
        self.server = McpServer(registry.descriptors, self._call_tool)
        self._launch: Dict[str, Any] = {}       # how this turn's agent was started, so helpers start the same way
        self.run: Optional[AgentRun] = None
        self.messages: List[Dict[str, Any]] = []
        self.chat_id = uuid.uuid4().hex[:12]
        self.session_id: Optional[str] = None
        self.session_provider = "claude"  # which CLI owns session_id
        self.status = "idle"            # idle | working | waiting
        self.activity = ""              # one-line "what is happening now"
        self.activity_since = 0.0       # when it last changed: the island shows how long it has been
        self.started_at = 0.0
        self.last_error = ""
        self.can_continue = False       # last turn was stopped or failed part-way
        self.restored_from = ""         # original .blend path after a snapshot restore
        self._saved_at = 0.0            # when the chat was last written (monotonic)
        self.pending_prompt = ""        # text to put back in the composer after a restore
        # auto_check: after a turn that changed the scene, audit it and send the agent back to fix
        # what is found (switched on by the add-on's preferences; off in headless use)
        # auto_review: when the user asked for realism, a first version is sent back to be compared with
        # photos of the real thing and improved, before it counts as an answer
        # team: a build is held back once until the agent has chosen between a team and working alone
        self.settings = {"snapshots": True, "auto_check": False, "auto_review": False, "team": False}
        self.goal: Optional[Dict[str, Any]] = None     # the goal being worked on (see _goal_*), or None
        self._decide_first = False
        self._made_here_first = False           # the first roblox_build of a message is held back once (NATIVE_GATE)
        self._teamed = False                    # this message's build was split: one review round is enough
        self._review_rounds = 0
        self._wants_realism = False
        self._motion_rounds = 0                 # animation made without being watched is sent back once per message
        self._misnamed: set = set()             # tools this turn called by their bare name (the CLI refuses those)
        self._misnamed_rounds = 0               # ...and the turn sent back once per message to call them properly
        self._actions_before: Dict[str, Tuple[float, float]] = {}
        self._last_send: Dict[str, Any] = {}
        self._check_rounds = 0
        self._problems_shown: set = set()       # scene problems the agent has already been sent back for, in this chat
        self._steps_by_id: Dict[str, Dict[str, Any]] = {}
        self._snapshot_taken = False
        self._tool_events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._scene_after_turn: Optional[Dict[str, Any]] = None
        self._scene_file = ""
        self._after = None                      # the end of the last turn, running in slices (see _finish)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        tools.load_all()
        data_dir()  # resolve the base path on the main thread
        self.dispatcher.bind_main_thread()
        registry.run_on_main = self.dispatcher.submit
        registry.on_event = self._tool_events.put
        interact.interactive["enabled"] = not bpy.app.background
        team.launch, team.dismiss = self._start_helper, self.server.dismiss
        if not self.server.running:
            self.server.start()

    def _call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Every tool call from every agent comes through here. A helper's calls are counted for the
        island, and the few tools that belong to the lead are refused to it."""
        guest = getattr(mcp_server.caller, "guest", "")
        known = registry.get(name)
        if guest:
            if name in team.DENIED:
                return {"content": [{"type": "text", "text": team.DENIED[name]}], "isError": True}
            team.note(guest, name, known.label if known else name)
        elif name == "work_in_parallel":
            self._decide_first = False
        elif name == "ask_user" and self._goal_running():
            return {"content": [{"type": "text", "text": "A goal is running and nobody is watching: do not ask. Decide "
                                                         "yourself and say what you decided in goal_report."}], "isError": True}
        elif name == "roblox_build" and self._made_here_first:
            # Asked to make a thing and get it to Roblox, the agent built it in Studio out of stock particles and
            # never opened Blender. The first native build of a message is held back once with the rule.
            self._made_here_first = False
            return {"content": [{"type": "text", "text": NATIVE_GATE}], "isError": True}
        elif self._decide_first and known is not None and known.mutates:
            # Asked in the prompt to split a job, the agent builds it alone anyway. So the first change of a
            # build is held back once, and it has to choose: a team, or (by calling again) alone.
            self._decide_first = False
            return {"content": [{"type": "text", "text": TEAM_GATE.format(tool=name)}], "isError": True}
        return registry.call(name, arguments)

    def _start_helper(self, name: str, prompt: str) -> AgentRun:
        """Start one more agent on this turn's job (see tools/team.py), with the lead's model and tools."""
        how = self._launch
        if not how:
            raise RuntimeError("no turn is running")
        slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "helper"
        mcp_file = os.path.join(how["workdir"], f"mcp_{slug}.json")
        with open(mcp_file, "w", encoding="utf-8") as fh:
            json.dump(self.server.guest_config(name), fh)
        run = how["runner"](claude_bin=how["binary"], prompt=prompt, system_prompt_file=how["system"],
                            mcp_config_file=mcp_file, workdir=how["workdir"], model=how["model"], resume=None,
                            effort=how["effort"], max_turns=60)
        run.start()
        return run

    def shutdown(self) -> None:
        self.stop_turn()
        self.server.stop()

    @property
    def busy(self) -> bool:
        # a check after a turn that is over (finished, or stopped before it began) keeps nobody waiting
        return self.run is not None or (self._after is not None and not self._after.done.is_set())

    # -- prompt assembly ---------------------------------------------------

    def _write_session_files(self, rules: str) -> Dict[str, str]:
        workdir = data_dir("workspace")
        with open(os.path.join(PACKAGE_DIR, "prompts", "system.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        prompt = prompt.replace("{{BLENDER_VERSION}}", bpy.app.version_string)
        prompt = prompt.replace("{{SKILL_INDEX}}", knowledge.skill_index() or "(no skills installed)")
        prompt = prompt.replace("{{USER_RULES}}", rules.strip() or "(none)")
        system_file = os.path.join(workdir, "system_prompt.md")
        with open(system_file, "w", encoding="utf-8") as fh:
            fh.write(prompt)
        mcp_file = os.path.join(workdir, "mcp.json")
        with open(mcp_file, "w", encoding="utf-8") as fh:
            json.dump(self.server.client_config(), fh)
        return {"workdir": workdir, "system": system_file, "mcp": mcp_file}

    def _context_block(self) -> str:
        """What the user is looking at and what they changed since the last turn."""
        from .tools import script
        lines = []
        try:
            view_layer = bpy.context.view_layer
            active = view_layer.objects.active
            selected = [o.name for o in view_layer.objects if o.select_get()]
            lines.append(f"mode: {active.mode if active else 'OBJECT'}; active: {active.name if active else 'none'}; "
                         f"selected ({len(selected)}): {', '.join(selected[:12]) or 'none'}"
                         + (" ..." if len(selected) > 12 else ""))
            lines.append(f"file: {os.path.basename(bpy.data.filepath) or '(unsaved)'}; "
                         f"frame {bpy.context.scene.frame_current}")
            previous = self._scene_after_turn
            if previous is not None and self._scene_file == bpy.data.filepath:
                diff = script._diff(previous, script._snapshot())
                if "note" not in diff:
                    parts = [f"{key} {', '.join(str(n) for n in names[:10])}" for key, names in diff.items()]
                    lines.append("the user changed since your last turn: " + "; ".join(parts))
        except Exception:
            pass
        return "\n".join(lines)

    def _compose(self, text: str, attachments: List[str], instruction: str = "") -> str:
        blocks = [instruction, text] if instruction else [text]
        context = self._context_block()
        if context:
            blocks.append("[Scene context]\n" + context)
        if attachments:
            rows = []
            for path in attachments:
                ext = os.path.splitext(path)[1].lower()
                if os.path.isdir(path):
                    kind = "folder"
                elif ext in _3D_EXTS:
                    kind = "3D file: bring it in with import_file"
                else:
                    kind = "image: open it with view_image before you start"
                rows.append(f"- {path}  ({kind})")
            blocks.append("[Attached by the user]\n" + "\n".join(rows))
        return "\n\n".join(blocks)

    # -- turns -------------------------------------------------------------

    def send(self, text: str, model: str = DEFAULT_MODEL, claude_bin: str = "",
             attachments: Optional[List[str]] = None, rules: str = "",
             mode: str = "agent", instruction: str = "", effort: str = "medium", shown: str = "") -> Optional[str]:
        """Start a turn. Returns an error string, or None when started. ``shown`` is what the chat
        displays when it differs from what the model is sent (the automatic scene check)."""
        text = text.strip()
        if not text:
            return "Type a message first."
        if self.busy:
            return "Unix is still working. Stop it first, or wait."
        display = ""
        if not shown and text.lower() in ("/clear", "/new"):      # a fresh chat, as in other chat apps
            self.new_chat()
            return None
        if not shown and text.lower() in ("/recover", "/recovery"):
            from . import recovery
            return recovery.command()       # reopen the newest snapshot after a crash and carry on
        if not shown and text[:5].lower() == "/goal" and (len(text) == 5 or text[5].isspace()):
            text, display = self._goal_command(text[5:].strip())
            if not text:
                return display or None
        provider = "codex" if model == "codex" else "claude"
        if provider == "codex":
            binary = find_codex()
            if not binary:
                return "Codex CLI not found. Install it and sign in with `codex login`, or pick a Claude model."
        else:
            binary = claude_bin or find_claude()
            if not binary:
                return ("Claude CLI not found. Install it (npm i -g @anthropic-ai/claude-code) "
                        "or set its path in the add-on preferences.")
        if provider != self.session_provider:  # the two CLIs cannot share a conversation
            self.session_id, self.session_provider = None, provider
        attachments = [p for p in (attachments or []) if os.path.exists(p)]
        self.start()
        paths = self._write_session_files(rules)
        prompt = self._compose(text, attachments, instruction)
        if not shown:        # a message from the user: remember how to continue it, start counting checks afresh
            self._last_send = dict(model=model, claude_bin=claude_bin, rules=rules, mode=mode, instruction=instruction,
                                   effort=effort)
            self._check_rounds = 0
            self._review_rounds = 0
            self._motion_rounds = 0
            self._misnamed_rounds = 0
            self._wants_realism = bool(_REALISM.search(text))
            team.state["job"] = " ".join((self.goal["text"] if display and self.goal else text).split())[:1500]
        self._decide_first = bool(not shown and mode == "agent" and self.settings.get("team") and _BUILD.search(text))
        self._made_here_first = not shown
        if not shown:
            self._teamed = False
        team.clear()
        self._actions_before = _actions_now()
        self.messages.append({"role": "user", "text": shown or display or text, "attachments": attachments, "snapshot": "",
                              "mode": mode, **({"auto": True} if shown else {})})
        self.messages.append({"role": "assistant", "parts": [], "meta": ""})
        self._steps_by_id.clear()
        self._misnamed.clear()
        self._snapshot_taken = False
        self.can_continue = False
        interact.clear_plan()
        _turn.begin(text)
        _turn.before_first_mutation = self._before_first_mutation
        runner = CodexRun if provider == "codex" else AgentRun
        self._launch = dict(runner=runner, binary=binary, system=paths["system"], workdir=paths["workdir"],
                            model="" if provider == "codex" else model,
                            effort=effort if effort in {"low", "medium", "high", "max"} else None)
        self.run = runner(claude_bin=binary, prompt=prompt, system_prompt_file=paths["system"],
                          mcp_config_file=paths["mcp"], workdir=paths["workdir"],
                          model="" if provider == "codex" else model, resume=self.session_id,
                          effort=effort if effort in {"low", "medium", "high", "max"} else None)
        try:
            self.run.start()
        except OSError as exc:
            self.run = None
            self.messages[-1]["parts"].append({"type": "error", "text": f"Could not start the Claude CLI: {exc}"})
            return str(exc)
        self.save_chat()                    # the request (and a new goal) is on disk before any work is done
        self._saved_at = time.monotonic()
        self.status, self.activity, self.started_at, self.last_error = "working", "Thinking…", time.monotonic(), ""
        self.activity_since = self.started_at
        return None

    def _before_first_mutation(self) -> None:
        if self.settings.get("snapshots") and not self._snapshot_taken:
            self._snapshot_taken = True
            path = files.snapshot("before_turn")
            user = next((m for m in reversed(self.messages) if m["role"] == "user"), None)
            if path and user is not None:
                user["snapshot"] = path

    def stop_turn(self) -> None:
        if self._goal_running():
            self._goal_end("stopped", "Goal stopped.")
        team.stop_all()
        registry.abandon()
        if self.run is not None:
            self.run.stop()
        if self._after is not None:
            self._after.cancelled = True        # the check after a turn stops at its next slice
        interact.answer_question("")  # release a waiting ask_user

    # -- chats -------------------------------------------------------------

    def _chat_path(self, chat_id: str) -> str:
        return os.path.join(data_dir("chats"), f"{chat_id}.json")

    def save_chat(self) -> None:
        if not self.messages:
            return
        # A goal runs for hours and every step keeps the script it ran: the chat grew without end, and was written
        # whole at every turn. Steps older than the last turns keep what they did, not their inputs.
        for message in self.messages[:-KEEP_INPUTS]:
            for part in message.get("parts", ()):
                for step in (part.get("steps", ()) if part.get("type") == "steps" else ()):
                    step.pop("input", None)
        first = next((m["text"] for m in self.messages if m["role"] == "user"), "Chat")
        record = {"id": self.chat_id, "title": first.splitlines()[0][:48], "updated": time.time(),
                  "session_id": self.session_id, "provider": self.session_provider,
                  "file": bpy.data.filepath, "messages": self.messages, "goal": self.goal}
        try:
            write_kept(self._chat_path(self.chat_id), record)
        except OSError:
            return
        stored = sorted((f for f in os.listdir(data_dir("chats")) if f.endswith(".json")),
                        key=lambda f: os.path.getmtime(os.path.join(data_dir("chats"), f)))
        for old in stored[:-MAX_CHATS]:
            try:
                os.remove(os.path.join(data_dir("chats"), old))
            except OSError:
                pass

    def list_chats(self) -> List[Dict[str, Any]]:
        rows = []
        folder = data_dir("chats")
        for name in os.listdir(folder):
            if not name.endswith(".json"):
                continue
            try:
                record = read_kept(os.path.join(folder, name))
                rows.append({"id": record["id"], "title": record.get("title", "Chat"),
                             "updated": record.get("updated", 0.0), "turns": sum(1 for m in record["messages"] if m["role"] == "user")})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(rows, key=lambda row: -row["updated"])

    def open_chat(self, chat_id: str) -> Optional[str]:
        if self.busy:
            return "Stop the current run first."
        try:
            record = read_kept(self._chat_path(chat_id))
        except (OSError, ValueError):
            return "That chat could not be read."
        self.save_chat()
        self.chat_id, self.session_id = record["id"], record.get("session_id")
        self.session_provider = record.get("provider", "claude")
        self.messages = record.get("messages", [])
        self.goal = record.get("goal")
        if self.goal and self.goal.get("state") in ("working", "verifying", "waiting"):
            self.goal["state"] = "paused"        # it was cut off (Blender closed): "/goal continue" picks it up
        goal.state["active"] = False
        for message in self.messages:  # stale live state from a crash
            for part in message.get("parts", []):
                for step in part.get("steps", []):
                    if step.get("status") == "running":
                        step["status"] = "error"
        interact.clear_plan()
        self._scene_after_turn, self.can_continue = None, False
        return None

    def transcript(self, scope: str = "chat") -> str:
        """The conversation as plain text for the clipboard.

        scope: 'reply' = Unix's last answer; 'chat' = every message; 'work' = the last turn
        with every step it took, including the scripts it ran and what came back.
        """
        if not self.messages:
            return ""
        if scope == "reply":
            last = next((m for m in reversed(self.messages) if m["role"] == "assistant"), None)
            return "\n\n".join(p["text"] for p in (last or {}).get("parts", []) if p["type"] != "steps").strip()
        messages = self.messages
        if scope == "work":
            start = max((i for i, m in enumerate(self.messages) if m["role"] == "user"), default=0)
            messages = self.messages[start:]
        lines: List[str] = []
        for message in messages:
            if message["role"] == "user":
                lines += ["You:", message["text"], ""]
                continue
            lines.append("Unix:")
            for part in message.get("parts", []):
                if part["type"] != "steps":
                    lines += [part["text"], ""]
                    continue
                for number, step in enumerate(part["steps"], start=1):
                    head = f"  {number}. {step['label']}" + (f": {step['detail']}" if step.get("detail") else "")
                    tail = step.get("result") or ("failed" if step.get("status") == "error" else "")
                    lines.append(head + (f"  ->  {tail}" if tail else ""))
                    if scope != "work":
                        continue
                    args = step.get("input") or {}
                    code = args.get("code")
                    rest = {k: v for k, v in args.items() if k not in ("code", "purpose")}
                    if rest:
                        lines.append("     arguments: " + json.dumps(rest, ensure_ascii=False))
                    if isinstance(code, str):
                        lines += ["     script:"] + ["       " + row for row in code.splitlines()]
                    if step.get("summary"):
                        lines.append("     result: " + step["summary"].replace("\n", " "))
                lines.append("")
            if message.get("meta"):
                lines += [f"({message['meta']})", ""]
        return "\n".join(lines).strip()

    def new_chat(self) -> None:
        self.stop_turn()
        self.save_chat()
        self.goal = None
        goal.state["active"] = False
        self.messages = []
        self.chat_id, self.session_id = uuid.uuid4().hex[:12], None
        self._problems_shown = set()
        interact.clear_plan()
        self._scene_after_turn, self.can_continue = None, False
        self.status, self.activity = ("working" if self.busy else "idle"), ""

    def restore(self, message_index: int) -> Optional[str]:
        """Load the snapshot taken before the turn at ``message_index`` and rewind the chat to it.

        The snapshot is copied first and the copy is opened, so the user's own file is never
        written and the snapshot itself is never the open document.
        """
        if self.busy:
            return "Stop the current run first."
        try:
            message = self.messages[message_index]
        except IndexError:
            return "That message no longer exists."
        snapshot = message.get("snapshot") or ""
        if message["role"] != "user" or not os.path.isfile(snapshot):
            return "No snapshot was saved for that turn."
        original = self.restored_from or bpy.data.filepath
        stem = os.path.splitext(os.path.basename(original))[0] or "untitled"
        working = os.path.join(data_dir("restored"), f"{stem}_restored_{time.strftime('%H%M%S')}.blend")
        shutil.copyfile(snapshot, working)
        with ui_context():
            bpy.ops.wm.recover_auto_save(filepath=working)
        self.restored_from = original
        text = message["text"]
        self.messages = self.messages[:message_index]
        self.session_id = None  # the CLI conversation cannot be rewound; continue in a fresh one
        self.messages.append({"role": "assistant", "meta": "", "parts": [{"type": "text", "text":
                              "Restored the scene to before that message. I no longer remember the turns after "
                              "this point. Your project file on disk is untouched: use File > Save As to keep "
                              "this version" + (f" (it came from {os.path.basename(original)})." if original else ".")}]})
        self._scene_after_turn, self.can_continue = None, False
        interact.clear_plan()
        self.save_chat()
        self.pending_prompt = text
        return None

    # -- main-thread tick --------------------------------------------------

    def tick(self) -> bool:
        """Pump tool calls and drain agent events. Returns True when the UI should redraw."""
        changed = self.dispatcher.pump() > 0
        run = self.run
        if run is None:
            if self.goal and self.goal.get("state") == "waiting" and time.time() >= self.goal.get("retry_at", 0.0):
                self._goal_send("next")         # the wait after a failed round is over
                changed = True
            return changed
        while True:
            try:
                event = run.events.get_nowait()
            except queue.Empty:
                break
            self._apply(event)
            changed = True
        while True:
            try:
                tool_event = self._tool_events.get_nowait()
            except queue.Empty:
                break
            if tool_event.get("kind") == "tool_end" and tool_event.get("images"):
                step = next((s for s in reversed(list(self._steps_by_id.values()))
                             if s["name"] == tool_event["name"] and not s.get("images")), None)
                if step is not None:
                    step["images"] = tool_event["images"][:4]
                    changed = True
        if time.monotonic() - self._saved_at > CHAT_SAVE_S:
            self.save_chat()                # a crash or closing Unix mid-turn keeps the chat and its goal
            self._saved_at = time.monotonic()
        waiting = interact.pending_question() is not None
        status = "waiting" if waiting else "working"
        if self.run is not None and status != self.status:
            self.status, changed = status, True
        return changed

    def _current(self) -> Dict[str, Any]:
        return self.messages[-1]

    def _preparing(self, event: Dict[str, Any]) -> None:
        if event["kind"] == "preparing_more":
            base = self.activity.split("  ·  ")[0] or "Writing the next step"
            self.activity = f"{base}  ·  {event['chars']:,} characters so far"
            return
        if event["what"] == "thinking":
            self.activity = "Thinking"
        elif event["what"] == "text":
            self.activity = "Writing the reply"
        else:
            self.activity = _COMING.get(event["name"], "Writing the next step")
        self.activity_since = time.monotonic()

    def _apply(self, event: Dict[str, Any]) -> None:
        kind = event["kind"]
        msg = self._current()
        parts: List[Dict[str, Any]] = msg["parts"]
        if kind in ("preparing", "preparing_more"):
            self._preparing(event)
            return
        if kind == "session":
            if event.get("bridge") != "connected":
                parts.append({"type": "error", "text": f"The Blender bridge is {event.get('bridge')}; tools are unavailable."})
        elif kind == "text":
            parts.append({"type": "text", "text": event["text"]})
            self.activity = "Writing…"
            self.activity_since = time.monotonic()
        elif kind == "tool_use":
            tool = registry.get(event["name"])
            step = {"name": event["name"], "label": tool.label if tool else event["name"],
                    "input": _trim_input(event.get("input") or {}),
                    "detail": _step_detail(event["name"], event.get("input") or {}),
                    "acted": bool(tool and tool.mutates), "status": "running", "summary": "", "result": "",
                    "images": []}
            self._steps_by_id[event.get("id") or ""] = step
            if event.get("misnamed") and tool is not None:
                self._misnamed.add(event["name"])
            if parts and parts[-1]["type"] == "steps":
                parts[-1]["steps"].append(step)
            else:
                parts.append({"type": "steps", "steps": [step], "open": False})
            self.activity = (step["label"] + (f": {step['detail']}" if step["detail"] else ""))[:90]
            self.activity_since = time.monotonic()
        elif kind == "tool_result":
            step = self._steps_by_id.get(event.get("id") or "")
            if step is not None:
                step["status"] = "error" if event["is_error"] else "ok"
                step["summary"] = event.get("summary", "")
                step["result"] = _friendly_result(step["name"], event.get("text", ""), event["is_error"])
            self.activity = "Thinking…"
            self.activity_since = time.monotonic()
        elif kind == "done":
            self._finish(event, msg, parts)

    def _finish(self, event: Dict[str, Any], msg: Dict[str, Any], parts: List[Dict[str, Any]]) -> None:
        stopped = bool(event.get("stopped"))
        # The agent is gone. Whatever it left running (helpers it stopped waiting for, a bake whose call
        # it gave up on) would otherwise work on for minutes underneath the next turn.
        if any(w["status"] == "working" for w in team.state["workers"]):
            team.stop_all()
        registry.abandon()
        for step in self._steps_by_id.values():
            if step["status"] == "running":
                step["status"] = "error" if (event["is_error"] or stopped) else "ok"
        if event.get("session_id"):
            self.session_id = event["session_id"]
        final = (event.get("text") or "").strip()
        last_text = next((p["text"] for p in reversed(parts) if p["type"] == "text"), None)
        if stopped:
            parts.append({"type": "note", "text": "Stopped."})
        elif event["is_error"]:
            parts.append({"type": "error", "text": final or "The run failed."})
            self.last_error = final
        elif final and final != last_text:
            parts.append({"type": "text", "text": final})
        # What is left (taking shared planes apart, closing the undo step, measuring the scene) takes seconds on a
        # big scene. It runs in slices on the main thread, so the window keeps answering; Unix counts as busy
        # until it is done, and Stop ends it.
        work = self._after_turn(event, msg, parts, stopped)
        if bpy.app.background:
            drain(work)                     # nothing pumps the dispatcher in between: finish now
        else:
            self._after = self.dispatcher.spawn(work, "the check after the turn")

    def _after_turn(self, event: Dict[str, Any], msg: Dict[str, Any], parts: List[Dict[str, Any]], stopped: bool):
        """Steps: the end of a finished turn (see ``_finish``). Stop closes them wherever they are; Unix is
        not busy any more either way. (5 Nov: Stop during the scene check closed it inside its first part, the
        part that frees Unix never ran, and it stayed busy for good: Stop did nothing and nothing could be sent.)"""
        from .tools import _util
        try:
            yield from self._after_turn_steps(event, msg, parts, stopped)
        finally:
            self._after = None
            if _util.status.get("text") == "Checking the scene":
                _util.status["text"] = ""

    def _after_turn_steps(self, event: Dict[str, Any], msg: Dict[str, Any], parts: List[Dict[str, Any]], stopped: bool):
        from .tools import _util, script
        checking = _turn.mutated() and self.settings.get("auto_check")
        changed_scene, cost = False, 0.0
        try:
            if checking:
                _util.status["text"] = "Checking the scene"
                # Surfaces left sharing a plane flicker as the camera moves. That is geometry, not judgement: it is
                # put right here, inside the turn's undo step, instead of sending the agent back to nudge things.
                try:
                    from .tools import quality
                    apart = yield from quality.guarded(quality.separate_steps(None, _turn.touched()))   # stops on an undo
                    if apart:
                        names = ", ".join(sorted({item["moved"] for item in apart})[:6])
                        parts.append({"type": "note", "text": f"Took {len(apart)} surface{'s' if len(apart) != 1 else ''} that "
                                                              f"shared a plane off it so nothing flickers ({names})."})
                except Exception as exc:        # tidying must never break a finished turn
                    print(f"[Unix] could not separate shared planes: {exc!r}")
        finally:                            # also when Stop ends it early: the turn is closed either way
            changed_scene = _turn.end()
            bits = [f"{event.get('seconds', 0):.0f}s"]
            cost = float(event.get("cost_usd") or 0.0) + team.take_cost()      # the helpers' share is part of the turn
            if cost:
                bits.append(f"${cost:.3f}")
            if changed_scene and not bpy.app.background:
                bits.append("Ctrl+Z undoes this turn")
            msg["meta"] = " · ".join(bits)
            interact.answer_question("")
            self.can_continue = (stopped or event["is_error"]) and bool(self._steps_by_id)
            self.run = None
            self.status, self.activity = "idle", ""
            try:
                self._scene_after_turn, self._scene_file = script._snapshot(), bpy.data.filepath
            except Exception:
                self._scene_after_turn = None
            if any(m is msg for m in self.messages):     # not when the chat was replaced meanwhile
                self.save_chat()
        try:
            if not stopped and not event["is_error"] and self._misnamed and self._misnamed_rounds < 1 and self._last_send:
                # 5 Nov: the model called run_python without its prefix, read the CLI's "No such tool available" as
                # the tool being gone, and told the user it could not do the job (twice, through a resumed session)
                self._after = None
                self._misnamed_rounds += 1
                names = sorted(self._misnamed)
                self.send(MISNAMED_PROMPT.format(bare=", ".join(f"`{n}`" for n in names),
                                                 full=", ".join(f"`mcp__unix__{n}`" for n in names)),
                          shown="Automatic retry: a tool name was mistyped. Unix is carrying on with your request.",
                          **self._last_send)
            elif changed_scene and not stopped and not event["is_error"]:
                job, self._after = self._after, None        # the review may start a turn: not busy for that
                if not self._review_after_turn():
                    self._after = job
                    yield from self._check_after_turn_steps()
            self._after = None
            if self.goal:
                self.goal["cost"] = round(self.goal.get("cost", 0.0) + cost, 4)
                if self.run is None and self._goal_running():
                    self._goal_after(event, changed_scene)
        finally:
            self._after = None
            if checking:
                _util.status["text"] = ""

    # -- goals -------------------------------------------------------------

    def _goal_running(self) -> bool:
        return bool(self.goal) and self.goal.get("state") in ("working", "verifying", "waiting")

    def _goal_command(self, argument: str) -> Tuple[str, str]:
        """What "/goal ..." means. Returns (prompt to send, what the chat shows); with no prompt, the
        second is a message for the user (or empty when the chat already says it)."""
        word = argument.lower()
        if word in ("", "status"):
            return "", self.goal_line() or "No goal is set. Start one with: /goal and then what you want, in full."
        if word in ("stop", "off", "end", "cancel"):
            if not self._goal_running():
                return "", "No goal is running."
            self._goal_end("stopped", "Goal stopped.")
            return "", ""
        if word in ("continue", "resume"):
            if not self.goal or self.goal.get("state") == "done":
                return "", "There is no unfinished goal in this chat. Start one with: /goal and what you want."
            self.goal.update(state="working", errors=0, resumed=time.time())
            goal.state.update(active=True, report=None)
            return self._goal_prompt("next"), "/goal continue"
        now = time.time()
        self.goal = {"text": argument, "state": "working", "round": 1, "started": now, "resumed": now, "spent": 0.0,
                     "cost": 0.0, "met": [], "remaining": [], "next": "", "errors": 0, "stalled": 0, "checks": 0}
        goal.state.update(active=True, report=None)
        return self._goal_prompt("first"), "/goal " + argument

    def _goal_elapsed(self) -> float:
        g = self.goal or {}
        return g.get("spent", 0.0) + (time.time() - g.get("resumed", time.time()) if self._goal_running() else 0.0)

    def _goal_prompt(self, kind: str) -> str:
        g = self.goal
        if kind == "first":
            return GOAL_FIRST.format(goal=g["text"])
        if kind == "verify":
            return GOAL_VERIFY.format(goal=g["text"])
        hours, minutes = divmod(int(self._goal_elapsed() // 60), 60)
        note = []
        if g.get("no_report"):
            note.append("Last round ended without goal_report: end this one with it.")
        if g.get("stalled", 0) >= 3:
            note.append(f"The same things have been missing for {g['stalled']} rounds: what you are doing is not working. "
                        "Try a different way (another tool, a rebuild of that part, a skill you have not loaded).")
        if g.get("checks"):
            note.append("A strict check of the finished work found the missing items above: fix them all.")
        return GOAL_NEXT.format(round=g["round"], elapsed=f"{hours}h {minutes:02d}m", goal=g["text"],
                                met="; ".join(g["met"]) or "(nothing reported yet)",
                                remaining="; ".join(g["remaining"]) or "(not reported: look and find out)",
                                next=g["next"] or "(decide)", note=" ".join(note))

    def _goal_send(self, kind: str) -> None:
        """Start the next round. If it cannot start (no CLI, a run still going), wait and try again."""
        g = self.goal
        label = {"verify": "Goal check: a fresh reviewer is checking the finished work against your goal.",
                 "next": f"Goal, round {g['round']}: carrying on."}[kind]
        goal.state.update(active=True, report=None)
        g["state"] = "verifying" if kind == "verify" else "working"
        error = self.send(self._goal_prompt(kind), shown=label, **self._last_send) if self._last_send else "nothing to continue"
        if error:
            self._goal_wait(f"the round could not start ({error})")

    def _goal_wait(self, why: str) -> None:
        g = self.goal
        g["errors"] = g.get("errors", 0) + 1
        delay = GOAL_RETRY_S[min(g["errors"], len(GOAL_RETRY_S)) - 1]
        g.update(state="waiting", retry_at=time.time() + delay)
        self.session_id = None              # the conversation may be what failed: the next round starts from the notes
        self.messages.append({"role": "assistant", "meta": "", "parts": [{"type": "note", "text": (
            f"Goal: {why}. Trying again at {time.strftime('%H:%M', time.localtime(g['retry_at']))}.")}]})
        self.save_chat()

    def _goal_end(self, state: str, text: str) -> None:
        g = self.goal
        g["spent"] = self._goal_elapsed()
        g["state"] = state
        goal.state.update(active=False, report=None)
        hours, minutes = divmod(int(g["spent"] // 60), 60)
        self.messages.append({"role": "assistant", "meta": "", "parts": [{"type": "note", "text": (
            f"{text}  {g['round']} round{'s' if g['round'] != 1 else ''}, {hours}h {minutes:02d}m, ${g.get('cost', 0.0):.2f}.")}]})
        self.save_chat()

    def _goal_after(self, event: Dict[str, Any], changed: bool = True) -> None:
        """A round is over: believe a "done" only after a fresh reviewer agrees, otherwise carry on."""
        g = self.goal
        report, goal.state["report"] = goal.state["report"], None
        if event.get("stopped"):
            return                              # the user pressed Stop: stop_turn has ended the goal
        if self._goal_elapsed() > GOAL_HOURS * 3600:
            self._goal_end("stopped", f"Goal stopped: it has been worked on for {GOAL_HOURS:g} hours.")
            return
        if event.get("is_error"):
            self._goal_wait("the round failed (" + str(event.get("text") or "no reason given").strip()[:160] + ")")
            return
        g["errors"] = 0
        g["no_report"] = report is None
        # Rounds that change nothing are waiting for something only the user can give (a cut-off spec, a file, a
        # decision): going on would only spend money. Three in a row pause the goal and say why.
        g["idle"] = 0 if changed or (report is not None and report["done"]) else g.get("idle", 0) + 1
        if report is not None:
            # So do rounds whose to-do list does not move, however it is worded and whatever they touched: on 5 Nov
            # twelve broken source files were "re-tested" by importing them every round (a change, of nothing), and
            # nine rounds went by at about $10 each before the goal stopped.
            g["stalled"] = g.get("stalled", 0) + 1 if report["remaining"] and not report["done"] \
                and _same_todo(report["remaining"], g.get("remaining") or []) else 0
            g.update(met=report["met"] or g["met"], remaining=report["remaining"], next=report["next"])
        if g["idle"] >= 3 or g.get("stalled", 0) >= 2:
            said = next((p["text"] for p in reversed(self.messages[-1]["parts"]) if p["type"] == "text"), "")
            why = "changed nothing" if g["idle"] >= 3 else "left the same things to do"
            self._goal_end("paused", f"Goal paused: three rounds in a row {why}, so it is waiting for something "
                                     "from you. Its last words: " + " ".join(said.split())[:400]
                           + "  Give it what it needs, then type /goal continue.")
            return
        if g["state"] == "verifying":
            self.session_id = g.pop("worker_session", None)     # back to the conversation that did the work
            if report is not None and report["done"]:
                self._goal_end("done", "Goal reached: a fresh check agrees it is all there.")
                return
            g["checks"] = g.get("checks", 0) + 1
        elif report is not None and report["done"]:
            # The work says it is finished. A new agent that did not build it, with no memory of how it went, checks it.
            g["worker_session"], self.session_id = self.session_id, None
            self._goal_send("verify")
            return
        g["round"] += 1
        if g["round"] % GOAL_FRESH_EVERY == 0:
            self.session_id = None              # a long conversation dulls: start afresh from the goal and its notes
        self._goal_send("next")

    def goal_line(self) -> str:
        """One line for the island: the goal, where it stands, for how long, for how much."""
        g = self.goal
        if not g:
            return ""
        hours, minutes = divmod(int(self._goal_elapsed() // 60), 60)
        state = {"working": f"round {g['round']}", "verifying": "checking it is really done",
                 "waiting": f"waiting to retry at {time.strftime('%H:%M', time.localtime(g.get('retry_at', 0)))}",
                 "done": "reached", "stopped": "stopped", "paused": "paused: type /goal continue"}.get(g["state"], g["state"])
        return f"Goal · {state} · {hours}h {minutes:02d}m · ${g.get('cost', 0.0):.2f} — {g['text']}"

    def _review_after_turn(self) -> bool:
        """The user asked for realism: a first version never is. Send the agent back to put its work
        beside photos of the real thing and close the differences, at most twice per user message.
        Returns True when it was sent back.

        Animation is held to the same standard whatever the user's words were: a turn that made or
        changed an action without once watching it with look_motion is sent back, once."""
        if not self.settings.get("auto_review") or not self._last_send:
            return False
        steps = list(self._steps_by_id.values())
        after = _actions_now()
        changed = {name for name in after if after[name] != self._actions_before.get(name)}
        changed |= {(s.get("input") or {}).get("name") for s in steps if s["name"] == "retarget_motion" and s["status"] == "ok"}
        changed.discard(None)
        changed &= set(after)
        if changed:                 # an animation turn: judged by watching it, not by photos of a real thing
            watched = {(s.get("input") or {}).get("action") for s in steps if s["name"] == "look_motion" and s["status"] == "ok"}
            if any(s["name"] == "look_motion" and not (s.get("input") or {}).get("action") for s in steps):
                watched |= {o.animation_data.action.name for o in bpy.context.scene.objects
                            if o.type == 'ARMATURE' and o.animation_data and o.animation_data.action}
            missed = sorted(changed - watched)
            if missed and self._motion_rounds < 1:
                self._motion_rounds += 1
                self.send(MOTION_REVIEW_PROMPT.format(actions=", ".join(missed[:20])),
                          shown="Automatic review: Unix is watching the animation it made and fixing what looks wrong.",
                          **self._last_send)
                return True
            return False
        teamed = any(s["name"] == "work_in_parallel" for s in steps)     # each part was already detailed by its own agent
        if not self._wants_realism or self._review_rounds >= (1 if teamed or self._teamed else REVIEW_ROUNDS):
            return False
        if self._goal_running():
            # once for the whole goal, not after every round: on 5 Nov it ran after each round on imported game art
            # (nothing to compare with photos) at about $13 each time
            if self.goal.get("reviewed"):
                return False
            self.goal["reviewed"] = True
        self._teamed = self._teamed or teamed
        self._review_rounds += 1
        self.send(REVIEW_PROMPT, shown="Automatic review: comparing the result with the real thing. Unix is improving it.",
                  **self._last_send)
        return True

    def _check_after_turn(self) -> None:
        """``_check_after_turn_steps`` run to the end at once."""
        drain(self._check_after_turn_steps())

    def _check_after_turn_steps(self):
        """Steps: measure the scene the agent just left. If it has problems the agent has not been shown yet
        (surfaces that flicker, an object hidden inside another, different things standing in each
        other, something in the air), send it back with those, at most twice per user message.

        A problem is shown once. If the agent saw it and left it, that was its answer: asking again
        only makes it move things back and forth, and a later turn that changes nothing (sending
        the scene to an engine) must not start the fixing all over."""
        if not self.settings.get("auto_check") or self._check_rounds >= 2 or not self._last_send:
            return
        from .tools import quality
        try:
            found = yield from quality.guarded(quality.audit_steps())     # stops on an undo meanwhile
        except Exception as exc:        # the check must never break a finished turn (nor an undo meanwhile)
            print(f"[Unix] scene check failed: {exc!r}")
            return
        self._after = None              # measured: a turn may start again (the one sent back below)
        fresh = [line for line, key in zip(found["problems"], found["keys"]) if key not in self._problems_shown]
        self._problems_shown.update(found["keys"])
        if not fresh:
            return
        self._check_rounds += 1
        listed = "\n".join(f"- {line}" for line in fresh[:14])
        prompt = ("[Automatic scene check] Your turn is over, and the scene was measured on its triangles (not on "
                  "bounding boxes). Parts of one thing that are pushed into each other are not listed. These are new:\n"
                  f"{listed}\n\nFix each one with the smallest change that removes it, and do not undo an earlier fix. If an "
                  "item is intended (something meant to float, or to stay hidden), leave it and say so in one line; you will "
                  "not be asked about it again. Do not send anything to a game engine again unless the user asked for that. "
                  "Keep the reply short: what you fixed and what, if anything, is left.")
        self.send(prompt, shown=f"Automatic check: {len(fresh)} problem{'s' if len(fresh) != 1 else ''} found in the "
                                f"scene. Unix is fixing {'them' if len(fresh) != 1 else 'it'}.", **self._last_send)

engine = Engine()
