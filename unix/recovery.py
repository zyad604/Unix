# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""What is left behind when Blender dies, and the way back after it.

Three things, all in the add-on's data folder:

- ``crash_logs/python_<time>_<pid>.log``: Python's ``faulthandler`` writes every thread's
  traceback there if the process dies of a fatal error. Not on Windows: there it hooks every
  access violation, also the ones Blender and the graphics driver raise and handle themselves
  many times a second, and in a three-hour session it wrote 120,000 dumps (240 MB) and slowed the
  app down. On Windows Blender's own crash file (``%TEMP%\<file>.crash.txt``) is what is left.
  A clean exit deletes its empty log; the newest few logs with something in them are kept.
- ``sessions/<pid>.json``: written at start and removed at a clean exit. One left behind by a
  process that is no longer running means that session ended without closing.
- The recovery offer: on the next start, if the last session died (or its goal was cut off while
  running), the island asks in plain words whether to reopen the newest snapshot of the scene (a
  copy, so no file of the user's is written) and carry on with the goal. ``/recover`` does the same.
"""

from __future__ import annotations

import atexit
import faulthandler
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

import bpy

from .tools._util import read_kept

KEEP_LOGS = 5
BEAT_SECONDS = 10.0             # how often the session marker says what Unix is doing and how much memory it holds
KEEP_AUTOSAVES = 2      # the newest, and the one before in case the newest was cut short (a scene can be 2 GB)
_state: Dict[str, Any] = {"log": None, "log_path": "", "marker": "", "offer": None, "handle": None, "asked": False,
                          "action": None, "saved": 0.0, "changed": False, "busy": False}


def _data(*parts: str) -> str:
    from .tools._util import data_dir
    return data_dir(*parts)


# -- crash logs --------------------------------------------------------------------------

def _enable_faulthandler() -> None:
    folder = _data("crash_logs")
    for name in os.listdir(folder):              # empty logs are clean exits that could not delete theirs
        path = os.path.join(folder, name)
        try:
            if name.endswith(".log") and os.path.getsize(path) == 0 and path != _state["log_path"]:
                os.remove(path)
        except OSError:
            pass
    logs = sorted((n for n in os.listdir(folder) if n.endswith(".log") and os.path.join(folder, n) != _state["log_path"]),
                  key=lambda n: os.path.getmtime(os.path.join(folder, n)))
    for old in logs[:-KEEP_LOGS]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass
    if _state["log"] is not None:               # the add-on was registered again in this process
        return
    if sys.platform == "win32":                 # it would log every handled exception too (see the module notes)
        return
    path = os.path.join(folder, time.strftime("python_%Y%m%d_%H%M%S") + f"_{os.getpid()}.log")
    try:
        handle = open(path, "w", encoding="utf-8")
        faulthandler.enable(file=handle, all_threads=True)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[Unix] crash log not enabled: {exc!r}")
        return
    _state.update(log=handle, log_path=path)


def crash_logs() -> List[str]:
    """Logs of sessions that died, newest first."""
    folder = _data("crash_logs")
    paths = [os.path.join(folder, n) for n in os.listdir(folder) if n.endswith(".log")]
    paths = [p for p in paths if p != _state["log_path"] and os.path.getsize(p) > 0]
    return sorted(paths, key=os.path.getmtime, reverse=True)


# -- sessions ----------------------------------------------------------------------------

def _pid_running(pid: int) -> bool:
    if pid == os.getpid():
        return True
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Never os.kill on Windows: any signal there is TerminateProcess.
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259   # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _write_marker(**beat: Any) -> None:
    path = _state["marker"] or os.path.join(_data("sessions"), f"{os.getpid()}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pid": os.getpid(), "started": _state.setdefault("started", time.time()), "log": _state["log_path"],
                       **beat}, fh)
    except OSError:
        return
    _state["marker"] = path


def _memory_gb() -> float:
    """Memory this process has committed, in GB (Windows; 0 elsewhere)."""
    if sys.platform != "win32":
        return 0.0
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                 "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                                                 "PagefileUsage", "PeakPagefileUsage")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.WinDLL("kernel32").GetCurrentProcess      # private copies: the shared ones keep their own types
    process.restype = wintypes.HANDLE
    query = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not query(process(), ctypes.byref(counters), counters.cb):
        return 0.0
    return round(counters.PagefileUsage / 2 ** 30, 2)


def _beat() -> None:
    """The session marker keeps the last moment Unix was seen alive, what it was doing and its memory: a crash
    that leaves no crash file of its own (one did, during a Roblox send) still says where it happened."""
    from .core import engine
    from .tools._util import status
    now = time.monotonic()
    if now - _state.get("beat", 0.0) < BEAT_SECONDS:
        return
    _state["beat"] = now
    _write_marker(beat=time.time(), memory_gb=_memory_gb(), objects=len(bpy.data.objects),
                  doing=(status.get("text") or engine.activity or ("working" if engine.busy else "idle"))[:200])


def _start_watchdog() -> None:
    """A small process outside Blender that notes how this one ends and, if it dies while a goal runs,
    starts Unix again to carry the goal on (``watchdog.py``)."""
    if bpy.app.background or not _state["marker"]:
        return
    here = os.path.dirname(os.path.abspath(__file__))
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen([sys.executable, "-I", os.path.join(here, "watchdog.py"), "--pid", str(os.getpid()),
                          "--marker", _state["marker"], "--blender", bpy.app.binary_path, "--data", _data(),
                          "--resume", os.path.join(here, "resume.py")],
                         close_fds=True, creationflags=flags, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        print(f"[Unix] the watchdog did not start: {exc!r}")


def _clean_exit() -> None:
    """atexit: Blender closed normally. Nothing to recover, and the empty crash log goes."""
    if _state["marker"]:
        try:
            os.remove(_state["marker"])
        except OSError:
            pass
    handle = _state["log"]
    if handle is not None:
        try:
            faulthandler.disable()
            handle.close()
            if os.path.getsize(_state["log_path"]) == 0:
                os.remove(_state["log_path"])
        except (OSError, ValueError):
            pass


def _dead_sessions() -> List[Dict[str, Any]]:
    """Sessions whose process ended without closing (a crash, or killed), oldest first."""
    folder = _data("sessions")
    dead = []
    for name in os.listdir(folder):
        if not (name.endswith(".json") and name[:-5].isdigit()):
            continue        # the watchdog's notes (restarts.json, resume_message.txt) are no session: read as one, the
                            # list in restarts.json raised here and the first automatic restart reopened nothing
        path = os.path.join(folder, name)
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            if not isinstance(record, dict):
                raise TypeError("not a session marker")
            pid = int(record.get("pid") or 0)
        except (OSError, ValueError, TypeError):
            pid, record = 0, {}             # a marker a PC crash zeroed: that session is dead
        if pid == os.getpid() or (pid and _pid_running(pid)):
            continue
        record["marker"] = path
        record["ended"] = record.get("beat") or os.path.getmtime(path)
        log = record.get("log") or ""
        if log and os.path.isfile(log):
            record["ended"] = max(record["ended"], os.path.getmtime(log))
            record["traceback"] = os.path.getsize(log) > 0
        dead.append(record)
    return sorted(dead, key=lambda r: r.get("started", 0.0))


# -- safety saves ------------------------------------------------------------------------
# Blender's own auto-save is skipped while any modal operator runs (measured in Blender 4.5: with one
# running and a one-minute interval, no auto-save file in 150 s; without, one after 65 s). Unix always
# has one running (the island's event handler, the window pump), so an unsaved scene had no copy at all
# but the snapshot taken before a turn's first change, and a crash lost everything after it. Unix makes
# that copy itself, on Blender's auto-save settings: into the snapshots folder, only the newest kept.

@bpy.app.handlers.persistent
def _scene_changed(*_args) -> None:
    # Opening a file sets off a burst of updates: that is not work to keep (a saved copy of an old file
    # would only push out the safety saves of newer work)
    if time.monotonic() - _state.get("loaded", 0.0) > 5.0:
        _state["changed"] = True


@bpy.app.handlers.persistent
def _file_loaded(*_args) -> None:
    _state["loaded"], _state["changed"] = time.monotonic(), False


def _watch_scene() -> None:
    for chain, handler in ((bpy.app.handlers.depsgraph_update_post, _scene_changed),
                           (bpy.app.handlers.load_post, _file_loaded)):
        for old in [h for h in chain if getattr(h, "__name__", "") == handler.__name__ and h is not handler]:
            chain.remove(old)           # left by an earlier load of this module
        if handler not in chain:
            chain.append(handler)
    _state["loaded"] = time.monotonic()   # the file opened at start counts as loaded too


def _autosave_due(engine, now: float) -> bool:
    paths = bpy.context.preferences.filepaths
    if not _state["changed"] or not paths.use_auto_save_temporary_files or not engine.settings.get("snapshots", True):
        return False
    if engine.dispatcher.pending or engine.dispatcher.waiting:
        return False                # a tool is half-way through: its temporary objects must not be saved
    since = now - _state["saved"]
    finished = _state["busy"] and not engine.busy           # a turn just ended: a natural point to keep
    return since >= 60.0 if finished else since >= max(60.0, paths.auto_save_time * 60.0)


def _autosave_steps():
    """Steps for the dispatcher, so the save runs inside the window pump, between two tools."""
    yield
    from .tools import files
    _state["changed"] = False
    path = files.snapshot("autosave")
    if not path:
        return
    # Several are kept, not only the newest: opening an older copy of a scene and saving it once must
    # never take away the safety save of the newer work (it did, and a session's work was lost that way).
    folder = os.path.dirname(path)
    saves = sorted((os.path.join(folder, name) for name in os.listdir(folder) if "_autosave" in name),
                   key=os.path.getmtime)
    for old in saves[:-KEEP_AUTOSAVES]:
        try:
            os.remove(old)
        except OSError:
            pass


def saved_now() -> None:
    """A copy of the scene was just written (a turn's snapshot or a safety save): the next safety save
    waits its full interval from now, and only comes if the scene changes again."""
    _state["saved"] = time.monotonic()
    _state["changed"] = False


def _autosave_tick() -> None:
    from .core import engine
    if bpy.app.background:
        return
    if _state["marker"]:
        _beat()
    now = time.monotonic()
    if _autosave_due(engine, now):
        _state["saved"] = now
        engine.dispatcher.spawn(_autosave_steps(), "safety save")
    _state["busy"] = engine.busy


def start() -> None:
    """Add-on register, before anything else: crash log on, this session marked as running, and what
    the last one left behind looked at (the offer is made by ``tick`` once the window is up)."""
    _enable_faulthandler()
    _state["saved"] = time.monotonic()
    _watch_scene()
    if not _state["asked"]:
        try:
            _state["offer"] = find_offer()
        except Exception as exc:          # recovery must never stop the add-on from starting
            print(f"[Unix] could not look for an unfinished session: {exc!r}")
    if not _state["marker"]:
        _write_marker()
        atexit.register(_clean_exit)
        _start_watchdog()


# -- the offer ---------------------------------------------------------------------------

REOPEN_CONTINUE = "Reopen my scene and carry on"
REOPEN_ONLY = "Reopen the scene only"
CARRY_ON_HERE = "Carry on in this scene"
NOT_NOW = "Not now"
_LIVE_GOAL = ("working", "verifying", "waiting")


def _seen() -> Dict[str, Any]:
    try:
        with open(os.path.join(_data(), "recovery.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _forget_sessions(records: List[Dict[str, Any]]) -> None:
    for record in records:
        try:
            os.remove(record["marker"])
        except (OSError, KeyError):
            pass


def _mark_seen(offer: Dict[str, Any]) -> None:
    """The user has answered: neither that chat nor those sessions are offered again."""
    try:
        with open(os.path.join(_data(), "recovery.json"), "w", encoding="utf-8") as fh:
            json.dump({"chat": offer.get("chat_id"), "updated": offer.get("updated")}, fh)
    except OSError:
        pass
    _forget_sessions(offer.get("sessions", []))


def _chats() -> List[Dict[str, Any]]:
    folder = _data("chats")
    rows = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            record = read_kept(os.path.join(folder, name))       # a chat a crash damaged comes back from its last good copy
            record["updated"] = float(record.get("updated") or os.path.getmtime(os.path.join(folder, name)))
            rows.append(record)
        except (OSError, ValueError, TypeError):
            continue
    return sorted(rows, key=lambda r: -r["updated"])


def whole(path: str) -> bool:
    """Is this .blend complete? A file ends with its ENDB block. On 4 Nov the PC went down while a safety
    save was written: the file had its full size but its last 15 MB were zeros, Blender refused it ("Missing
    DNA block") and the restart opened nothing."""
    try:
        with open(path, "rb") as fh:
            fh.seek(-64, os.SEEK_END)
            return b"ENDB" in fh.read()
    except OSError:
        return False


def _snapshot_for(chat: Dict[str, Any], since: float, until: float) -> str:
    """The newest snapshot of the chat's scene: its turns' own snapshots and the safety saves made while
    it ran (``autosave``). Nothing from after ``until``: that would be another session's scene."""
    known = [p for p in (m.get("snapshot") for m in chat.get("messages", [])) if p and os.path.isfile(p)]
    start = min([os.path.getmtime(p) for p in known] + [since or until])
    folder = _data("snapshots")
    for name in os.listdir(folder):
        if name.endswith(".blend") and "_autosave" in name:
            path = os.path.join(folder, name)
            if start - 60 <= os.path.getmtime(path) <= until + 60:
                known.append(path)
    known = [p for p in known if os.path.getmtime(p) <= until + 60]
    if not known:       # no start known (a PC crash zeroed the session file): the newest copy made before it ended
        known = [os.path.join(folder, name) for name in os.listdir(folder) if name.endswith(".blend")
                 and ("_autosave" in name or "_before_turn" in name) and os.path.getmtime(os.path.join(folder, name)) <= until + 60]
    known = [p for p in known if whole(p)]
    return max(known, key=os.path.getmtime) if known else ""


def find_offer(ignore_seen: bool = False) -> Optional[Dict[str, Any]]:
    """What to offer after an unfinished session: the chat it was in, its goal, and the newest snapshot
    of its scene. None when the last session closed normally and no goal was cut off."""
    dead = _dead_sessions()
    chats = _chats()
    chat = None
    if dead:
        started = min(r.get("started", 0.0) for r in dead)
        chat = next((c for c in chats if c["updated"] >= started - 5), None)
    if chat is None:
        # No marker (a version without them, or Blender was closed while a goal ran): a goal still marked as
        # running was cut off.
        chat = next((c for c in chats if (c.get("goal") or {}).get("state") in _LIVE_GOAL), None)
        if chat is None:
            _forget_sessions(dead)      # it died before any chat was saved: there is nothing to bring back
            return None
    seen = _seen()
    if not ignore_seen and seen.get("chat") == chat.get("id") and seen.get("updated") == chat["updated"]:
        _forget_sessions(dead)
        return None
    ended = max([r.get("ended", 0.0) for r in dead] + [chat["updated"]])
    since = min([r.get("started", 0.0) for r in dead] or [0.0])
    goal = chat.get("goal") or {}
    live = goal.get("state") in _LIVE_GOAL
    return {"chat_id": chat.get("id"), "updated": chat["updated"], "title": (chat.get("title") or "your last chat").strip(),
            "goal": ((goal.get("text") or "").strip().splitlines() or [""])[0][:80] if live else "",
            "round": goal.get("round", 0), "crashed": bool(dead), "ended": ended,
            "snapshot": _snapshot_for(chat, since, ended), "file": chat.get("file") or "", "sessions": dead,
            "log": next((r.get("log") for r in reversed(dead) if r.get("traceback")), "")}


def offer_text(offer: Dict[str, Any]) -> str:
    when = time.strftime("%H:%M", time.localtime(offer["ended"]))
    goal = offer["goal"].lstrip("# ").strip()
    what = f"your goal “{goal}” (round {offer['round']})" if goal else f"“{offer['title']}”"
    if offer["crashed"]:
        text = f"Unix closed unexpectedly at {when} while it was working on {what}."
        seen = next((r for r in reversed(offer.get("sessions") or []) if r.get("doing")), None)
        if seen:
            text += f" Last seen: {seen['doing']} ({seen.get('memory_gb', 0):.1f} GB of memory, {seen.get('objects', 0)} objects)."
        ended = next((r for r in reversed(offer.get("sessions") or []) if r.get("exit_meaning")), None)
        if ended:
            text += f" Windows says it ended by {ended['exit_meaning']} (exit code {ended['exit_code']})."
    else:
        text = f"Unix was still working on {what} when it last closed, at {when}."
    if offer["snapshot"]:
        saved = time.strftime("%H:%M", time.localtime(os.path.getmtime(offer["snapshot"])))
        text += (f" I can reopen your scene as it was at {saved} and carry on" + (" with the goal." if goal else " from there.")
                 + " It opens as a copy: none of your own files are changed.")
    else:
        text += " No snapshot of that scene was saved, so I can only carry on in the scene that is open now."
    return text


def offer_options(offer: Dict[str, Any]) -> List[str]:
    return [REOPEN_CONTINUE, REOPEN_ONLY, NOT_NOW] if offer["snapshot"] else [CARRY_ON_HERE, NOT_NOW]


def command() -> Optional[str]:
    """``/recover``: reopen the newest snapshot of the unfinished session and carry on. Returns a message
    when there is nothing to recover. ``tick`` does the work, from the add-on timer: a file must not be
    loaded from inside an operator's event handler (the island's), which loading it would free."""
    offer = _state["offer"] or find_offer(ignore_seen=True)
    if offer is None:
        return "Nothing to recover: the last session closed normally and no goal was cut off."
    _state.update(offer=offer, action=REOPEN_CONTINUE if offer["snapshot"] else CARRY_ON_HERE)
    return None


def tick() -> bool:
    """Add-on timer (main thread, outside any operator): put the offer to the user once the window is up,
    and carry out the answer. Returns True when the island should redraw."""
    from .core import engine
    from .tools import interact
    _autosave_tick()
    offer = _state["offer"]
    if offer is None or bpy.app.background:
        return False
    action = _state["action"]
    if action is None:
        handle = _state["handle"]
        if handle is None:
            wm = bpy.context.window_manager
            if _state["asked"] or engine.busy or interact.pending_question() is not None or not (wm and wm.windows):
                return False
            _state["handle"] = interact.begin_question(offer_text(offer), offer_options(offer))
            _state["asked"] = _state["handle"] is not None
            return _state["asked"]
        action = interact.collect_answer(handle)
        if action is None:
            return False
        _state["handle"] = None
    _state.update(action=None, offer=None)
    problem = act(offer, action)
    if problem:
        print(f"[Unix] recovery: {problem}")
    return True


def act(offer: Dict[str, Any], answer: str) -> Optional[str]:
    """Carry out the answer to the offer. Main thread, outside any operator. Returns a problem, or None."""
    from .core import engine
    _mark_seen(offer)
    if answer == NOT_NOW or not answer:
        return None
    if answer not in (REOPEN_CONTINUE, REOPEN_ONLY, CARRY_ON_HERE):
        return _forward(answer)     # the user typed a message instead of choosing: it was meant for Unix
    if engine.busy:
        return "Unix is working. Stop it first, then type /recover."
    if offer.get("chat_id") and offer["chat_id"] != engine.chat_id:
        problem = engine.open_chat(offer["chat_id"])
        if problem:
            return problem
    if answer != CARRY_ON_HERE:
        problem = reopen(offer)
        if problem:
            return problem
    if answer == REOPEN_ONLY:
        return None
    goal = engine.goal
    # the goal carries on only if it was what ran when Unix closed (the offer names it then); a goal paused
    # before the last request (a send to Roblox after it) stays paused, and that request is carried on
    if offer.get("goal") and goal and goal.get("state") in ("paused",) + _LIVE_GOAL:
        engine.session_id = None    # the CLI remembers work the reopened scene may not have: start from the notes
        goal["next"] = ("Unix closed unexpectedly and the scene was reopened from a snapshot: check what is really "
                        "there first, work from just before that may be missing. " + (goal.get("next") or ""))[:600]
        return _forward("/goal continue")
    return _forward("Continue from where you stopped. Check the scene first, then finish the task.")


KEEP_RESTORED = 3          # reopened copies kept besides the open one (5 Nov: 20 GB of 2 GB copies piled up)
RESTORED_GB = 8.0


def _prune_restored(folder: str, coming: int) -> None:
    """Make room for a new reopened copy: Unix's own copies (``*_recovered_*``) beyond the newest few, or beyond
    RESTORED_GB with the one coming, go, oldest first. Never the file open now, never a file the user named."""
    open_now = os.path.normcase(os.path.abspath(bpy.data.filepath)) if bpy.data.filepath else ""
    ours = []
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        if "_recovered_" in name and name.endswith((".blend", ".blend1")) and os.path.isfile(path) \
                and os.path.normcase(os.path.abspath(path)) != open_now:
            ours.append(path)
    ours.sort(key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in ours) + coming
    while ours and (len(ours) >= KEEP_RESTORED or total > RESTORED_GB * 1024 ** 3):
        old = ours.pop(0)
        try:
            size = os.path.getsize(old)
            os.remove(old)
            total -= size
        except OSError:
            pass


def reopen(offer: Dict[str, Any]) -> Optional[str]:
    """Open a copy of the offer's snapshot. Neither the snapshot nor any file of the user's is written."""
    from .core import engine
    from .tools._util import ui_context
    snapshot = offer["snapshot"]
    if not os.path.isfile(snapshot):
        return "That snapshot is no longer there."
    stem = os.path.splitext(os.path.basename(offer.get("file") or ""))[0] or "untitled"
    working = os.path.join(_data("restored"), f"{stem}_recovered_{time.strftime('%H%M%S')}.blend")
    try:
        _prune_restored(os.path.dirname(working), os.path.getsize(snapshot))
    except OSError:
        pass
    shutil.copyfile(snapshot, working)
    with ui_context():
        bpy.ops.wm.recover_auto_save(filepath=working)
    engine.restored_from = offer.get("file") or ""
    saved = time.strftime("%H:%M", time.localtime(os.path.getmtime(snapshot)))
    engine.messages.append({"role": "assistant", "meta": "", "parts": [{"type": "note", "text": (
        f"Reopened the scene as it was at {saved}, after Unix closed unexpectedly. Your files on disk are untouched: "
        "use File > Save As to keep this version." + (f" What every thread was doing is in {offer['log']}." if offer.get("log") else ""))}]})
    engine.save_chat()
    return None


def _forward(text: str) -> Optional[str]:
    from .ui import operators
    return operators._send(bpy.context, text, auto=True)
