# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Start Unix, reopen the chat and scene the last crash left behind, and send it one message, as if typed.
The watchdog (``watchdog.py``) starts Unix this way when it died while a goal ran.

Run: "foxa (2).exe" --python <add-on>/resume.py -- <message file>
A message that starts with "/goal continue" carries the chat's goal on, with the rest of it as a note for the round.
"""
import sys

import bpy

MESSAGE = open(sys.argv[sys.argv.index("--") + 1], encoding="utf-8").read().strip()


def resume():
    # A timer that raises is dropped without a word: on 31 Oct the first automatic restart sat on an empty scene
    # for minutes that way. Whatever goes wrong now is written down where the next look will find it.
    try:
        return _resume()
    except Exception:
        import traceback
        from unix.tools._util import data_dir
        import os
        import time
        with open(os.path.join(data_dir("crash_logs"), "resume.log"), "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + traceback.format_exc() + "\n")
        traceback.print_exc()
        return None


def _resume():
    from unix import recovery
    from unix.core import engine
    from unix.ui import operators
    wm = bpy.context.window_manager
    if not (wm and wm.windows) or not engine.dispatcher:
        return 1.0                              # the window and the engine are not up yet
    offer = recovery._state["offer"] or recovery.find_offer(ignore_seen=True)
    if recovery._state.get("handle") is not None:      # the start-up question may already be in the chat: it is answered here
        from unix.tools import interact
        interact.withdraw_question(recovery._state["handle"])
    recovery._state.update(offer=None, action=None, handle=None, asked=True)
    if offer is None:
        print("[resume] nothing to recover: sending the message in a new chat")
    else:
        problem = recovery.act(offer, recovery.REOPEN_ONLY)
        print(f"[resume] reopened chat {offer['chat_id']} and {offer['snapshot']}: {problem or 'ok'}")
    if not MESSAGE:
        print("[resume] reopened only: no message to send")
        return None
    if MESSAGE.startswith("/goal continue") and engine.goal:
        # what to tell the goal goes into its notes; the round itself is the goal carrying on
        note = MESSAGE[len("/goal continue"):].strip()
        engine.goal["next"] = (note + " " + (engine.goal.get("next") or ""))[:1200]
        error = operators._send(bpy.context, "/goal continue")
    else:
        error = operators._send(bpy.context, MESSAGE)
    print(f"[resume] message sent: {error or 'ok'}")
    return None


bpy.app.timers.register(resume, first_interval=8.0)
