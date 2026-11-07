# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Stop during the check after a turn frees Unix (5 Nov: it stayed busy for good, Stop did nothing and nothing
could be sent). No model calls. Run: blender -b --factory-startup --python tests/test_stop_check.py"""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bpy  # noqa: F401
from unix.tools import _util
_util._base_dir = tempfile.mkdtemp(prefix="unix_stop_")
from unix.core import Engine

fails = []


def check(label, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f"  -> {str(detail)[:400]}"))
    if not cond:
        fails.append(label)


def endless(*_args):
    _util.status["text"] = "Checking the scene"     # a check on a big scene: many slices
    while True:
        yield


for when in ("during", "before"):
    engine = Engine()
    engine._after_turn_steps = endless
    engine._after = engine.dispatcher.spawn(engine._after_turn({}, {}, [], False), "the check after the turn")
    if when == "during":
        engine.dispatcher.pump(0.01)
        check("the check is running: Unix is busy", engine.busy and _util.status["text"] == "Checking the scene")
    engine.stop_turn()
    engine.dispatcher.pump(0.01)
    check(f"Stop {when} the check frees Unix", not engine.busy, (engine._after, engine.run))
    check(f"...and the status line is cleared ({when})", _util.status["text"] != "Checking the scene", _util.status)
    _util.status["text"] = ""

print("RESULT", "ALL PASS" if not fails else f"{len(fails)} FAILED: {fails}")
