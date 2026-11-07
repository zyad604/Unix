# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Watches one Unix process from outside it (started by ``recovery.start`` with Blender's own Python).

When Unix ends without closing (its session marker is still there) the watchdog writes down how it
ended, the exit code Windows gives the process, into the marker: Unix died several times leaving no
crash file of its own and nothing in the event logs, and the exit code is what tells a crash (access
violation, stack overflow, heap corruption) from being shut from outside. If a goal (or, after a crash,
a request) was running it then starts Unix again with ``resume.py``, which reopens the newest safety save and carries the goal or
the request on, at most RESTARTS_PER_HOUR times an hour so that a crash that comes back cannot loop for ever.

Standard library only: it runs with ``python -I`` outside Blender.
"""

import argparse
import json
import os
import subprocess
import sys
import time

RESTARTS_PER_HOUR = 3
LIVE_GOAL = ("working", "verifying", "waiting")
EXIT_NAMES = {
    0xC0000005: "a crash: access violation",
    0xC00000FD: "a crash: stack overflow",
    0xC0000409: "a crash: fail-fast (stack buffer overrun or a runtime that stopped itself)",
    0xC0000374: "a crash: heap corruption",
    0xC000001D: "a crash: illegal instruction",
    0xC0000017: "out of memory",
    0xC000013A: "closed with Ctrl+C or the console window",
    0xB: "a crash inside Blender (its crash handler ended it; its report is %TEMP%\\<file name>.crash.txt)",
    0xFFFFFFFF: "being ended by another program (Task Manager or a script)",
    1: "ended from outside (Task Manager, another program) or by its own exit(1)",
}


def wait_for_exit(pid: int) -> int:
    """Block until the process ends; its exit code (-1 when it cannot be known)."""
    if sys.platform != "win32":
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                return -1
            time.sleep(2.0)
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, pid)        # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return -1
    try:
        kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)
        code = wintypes.DWORD()
        return int(code.value) if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) else -1
    finally:
        kernel32.CloseHandle(handle)


def read_kept(path: str):
    """A chat file, or its last good copy (``_util.write_kept``)."""
    for candidate in (path, path + ".bak"):
        try:
            with open(candidate, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            continue
    return None


def newest_chat(data: str):
    folder = os.path.join(data, "chats")
    try:
        names = sorted((n for n in os.listdir(folder) if n.endswith(".json")),
                       key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)
    except OSError:
        return None
    return read_kept(os.path.join(folder, names[0])) if names else None


def live_goal(data: str):
    """The newest chat when its goal was running, else None."""
    record = newest_chat(data)
    goal = (record or {}).get("goal") or {}
    return record if goal.get("state") in LIVE_GOAL else None


def crashed_mid_turn(code: int, marker: dict) -> bool:
    """Did Unix crash (not get closed) while it was working on a message? On 2 Nov it died half way through
    a plain request and sat on Blender's crash box: only goals were carried on by themselves then."""
    doing = (marker.get("doing") or "idle").strip().lower()
    return (code == 0xB or 0xC0000000 <= code < 0xD0000000) and code != 0xC000013A and doing != "idle"


def alive(pid: int) -> bool:
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)          # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259     # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def another_running(data: str, me: int) -> bool:
    """Is another Unix open (a session marker of a live process)? Then it is not started again: on 5 Nov a second
    one was opened beside it, both carried the same chat on, and together they took twice the memory."""
    folder = os.path.join(data, "sessions")
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        stem = name[:-5] if name.endswith(".json") else ""
        if stem.isdigit() and int(stem) != me and alive(int(stem)):
            return True
    return False


def allowed_restart(data: str) -> bool:
    path = os.path.join(data, "sessions", "restarts.json")
    now = time.time()
    try:
        with open(path, encoding="utf-8") as fh:
            recent = [t for t in json.load(fh) if now - t < 3600.0]
    except (OSError, ValueError):
        recent = []
    if len(recent) >= RESTARTS_PER_HOUR:
        return False
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(recent + [now], fh)
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    for name in ("pid", "marker", "blender", "data", "resume"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    code = wait_for_exit(int(args.pid))
    if not os.path.isfile(args.marker):
        return                                  # it closed normally: nothing to do
    unsigned = code & 0xFFFFFFFF if code >= 0 else code
    how = EXIT_NAMES.get(unsigned, "an unknown ending")
    try:
        with open(args.marker, encoding="utf-8") as fh:
            marker = json.load(fh)
    except (OSError, ValueError):
        marker = {"pid": int(args.pid)}
    marker.update(exit_code=hex(unsigned) if unsigned >= 0 else "unknown", exit_meaning=how, exited=time.time())
    with open(args.marker, "w", encoding="utf-8") as fh:
        json.dump(marker, fh)
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} pid {args.pid} ended: exit code {marker['exit_code']} ({how}); "
            f"last seen: {marker.get('doing', '?')}, {marker.get('memory_gb', '?')} GB\n")
    with open(os.path.join(args.data, "crash_logs", "endings.log"), "a", encoding="utf-8") as fh:
        fh.write(line)
    goal = live_goal(args.data) is not None
    if not (goal or crashed_mid_turn(unsigned, marker)) or another_running(args.data, int(args.pid)) \
            or not allowed_restart(args.data):
        return
    message = os.path.join(args.data, "sessions", "resume_message.txt")
    with open(message, "w", encoding="utf-8") as fh:
        fh.write(("/goal continue\n" if goal else "") +
                 f"Unix closed unexpectedly at {time.strftime('%H:%M')} ({how}, exit code "
                 f"{marker['exit_code']}) while {marker.get('doing', 'working')}, and started again by itself; the scene is "
                 f"reopened from the newest safety save. First check what is really there, then carry on where "
                 + ("the goal was." if goal else "you were with my last request."))
    command = [args.blender, "--python", args.resume, "--", message]
    if args.dry_run:
        print("WOULD RESTART", command)
        return
    time.sleep(3.0)
    if another_running(args.data, int(args.pid)):        # opened by hand meanwhile
        return
    subprocess.Popen(command, cwd=os.path.dirname(args.blender), close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))


if __name__ == "__main__":
    main()
