# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""The watchdog: how a Unix process ended goes into its session marker, and a goal that was running is
carried on in a new Unix (so is a request a crash cut short), at most three times an hour. Stand-in processes, nothing is restarted for real.
Run: blender -b --factory-startup --python tests/test_watchdog.py"""
import json, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy  # noqa: F401  (run inside Blender like the other tests; the watchdog itself needs only Python)

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WATCHDOG = os.path.join(HERE, "unix", "watchdog.py")
PYTHON = sys.executable
fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:600]}"))
    if not cond:
        fails.append(label)


def folder(goal_state):
    data = tempfile.mkdtemp(prefix="unix_watch_")
    for sub in ("chats", "sessions", "crash_logs"):
        os.makedirs(os.path.join(data, sub))
    with open(os.path.join(data, "chats", "c1.json"), "w", encoding="utf-8") as fh:
        json.dump({"id": "c1", "messages": [], "goal": {"state": goal_state, "text": "a ranch"}}, fh)
    return data


def watch(data, ending, doing="Exported a file"):
    """A stand-in Unix that writes its marker (named by its pid, as Unix's are) and ends as ``ending`` says;
    the watchdog's output."""
    if ending in ("crash", "killed"):   # ends the way an access violation, or Task Manager, ends a process
        code = ("import ctypes, time; time.sleep(1.5); ctypes.windll.kernel32.ExitProcess(%s)"
                % ("0xC0000005" if ending == "crash" else "0xFFFFFFFF"))
    else:                   # closes normally: its marker is gone first, as atexit does in Unix
        code = ("import os, sys, time; time.sleep(1.5); "
                "os.remove(os.path.join(sys.argv[1], 'sessions', str(os.getpid()) + '.json'))")
    stand_in = subprocess.Popen([PYTHON, "-I", "-c", code, data])
    marker = os.path.join(data, "sessions", f"{stand_in.pid}.json")
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump({"pid": stand_in.pid, "doing": doing, "memory_gb": 3.2}, fh)
    out = subprocess.run([PYTHON, "-I", WATCHDOG, "--pid", str(stand_in.pid), "--marker", marker, "--blender", "unix.exe",
                          "--data", data, "--resume", "resume.py", "--dry-run"], capture_output=True, text=True, timeout=60)
    return marker, out.stdout + out.stderr


data = folder("working")
marker, out = watch(data, "crash")
with open(marker, encoding="utf-8") as fh:
    ended = json.load(fh)
check("a crash is written into the session marker with Windows' exit code and what it means",
      ended.get("exit_code") == "0xc0000005" and "access violation" in ended.get("exit_meaning", ""), ended)
check("...and into the endings log", "0xc0000005" in open(os.path.join(data, "crash_logs", "endings.log"), encoding="utf-8").read())
check("a goal that was running is carried on in a new Unix", "WOULD RESTART" in out and "resume.py" in out, out)
message = open(os.path.join(data, "sessions", "resume_message.txt"), encoding="utf-8").read()
check("with /goal continue and why it stopped", message.startswith("/goal continue") and "access violation" in message, message)

for _ in range(3):
    marker, out = watch(data, "crash")
check("at most three restarts an hour: a crash that comes back does not loop", "WOULD RESTART" not in out, out)

from unix.tools import _util       # noqa: E402
from unix import recovery           # noqa: E402
data = folder("working")
marker, out = watch(data, "crash")          # leaves the watchdog's restarts.json and resume_message.txt in sessions/
_util._base_dir = data
try:
    offer, problem = recovery.find_offer(ignore_seen=True), ""
except Exception as exc:                    # 31 Oct: the list in restarts.json raised here, and nothing was reopened
    offer, problem = None, repr(exc)
check("after a restart by the watchdog the scene and goal are still offered (its own notes are no sessions)",
      offer is not None and offer.get("chat_id") == "c1" and offer.get("goal") == "a ranch"
      and os.path.isfile(os.path.join(data, "sessions", "restarts.json")), problem or offer)

data = folder("paused")
marker, out = watch(data, "crash", doing="idle")
check("no goal and no request running: the ending is noted, nothing is restarted", "WOULD RESTART" not in out
      and json.load(open(marker, encoding="utf-8")).get("exit_code") == "0xc0000005", out)

data = folder("paused")
marker, out = watch(data, "crash", doing="Looked at the scene")
message = open(os.path.join(data, "sessions", "resume_message.txt"), encoding="utf-8").read()     if os.path.exists(os.path.join(data, "sessions", "resume_message.txt")) else ""
check("a crash half way through a request carries the request on (2 Nov), not a goal", "WOULD RESTART" in out
      and not message.startswith("/goal") and "last request" in message, out + message)

data = folder("paused")
marker, out = watch(data, "killed", doing="Looked at the scene")
check("ended from outside half way through a request: not restarted", "WOULD RESTART" not in out, out)

data = folder("working")
other = subprocess.Popen([PYTHON, "-I", "-c", "import time; time.sleep(30)"])      # another Unix, open meanwhile
with open(os.path.join(data, "sessions", f"{other.pid}.json"), "w", encoding="utf-8") as fh:
    json.dump({"pid": other.pid, "doing": "idle"}, fh)
marker, out = watch(data, "crash")
other.kill()
check("another Unix already open: a goal is not started a second time beside it (5 Nov)", "WOULD RESTART" not in out, out)
os.remove(os.path.join(data, "sessions", f"{other.pid}.json"))
other.wait()
marker, out = watch(data, "crash")
check("...a marker left by one that is gone does not count", "WOULD RESTART" in out, out)

data = folder("working")
marker, out = watch(data, "clean")
check("a normal close does nothing at all", "WOULD RESTART" not in out and not os.path.exists(marker)
      and not os.path.exists(os.path.join(data, "crash_logs", "endings.log")), out)

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
