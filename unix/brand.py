# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Shows "Unix" in the window title where Blender writes "Blender 4.5.x LTS"."""
import os
import re

import bpy

NAME = "Unix"
_TITLE = re.compile(r"Blender \d+\.\d+(?:\.\d+)?(?: [A-Za-z]+)?")

# -- window title (Blender writes "<file> - Blender 4.5.10 LTS" whenever the file changes state) -------------------

_win = {}


def _api():
    if _win:
        return _win
    import ctypes
    from ctypes import wintypes as wt
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [enum_proc, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
    user32.SetWindowTextW.restype = wt.BOOL
    _win.update(ctypes=ctypes, wt=wt, user32=user32, enum_proc=enum_proc)
    return _win


def _own_windows():
    api = _api()
    ctypes, wt, user32 = api["ctypes"], api["wt"], api["user32"]
    me, found = os.getpid(), []

    def each(hwnd, _):
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == me and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    callback = api["enum_proc"](each)
    user32.EnumWindows(callback, 0)
    return found


def _title_tick():
    try:
        api = _api()
        buf = api["ctypes"].create_unicode_buffer(512)
        for hwnd in _own_windows():
            api["user32"].GetWindowTextW(hwnd, buf, 512)
            title = buf.value
            new = _TITLE.sub(NAME, title) if "Blender" in title else title
            if new != title:
                api["user32"].SetWindowTextW(hwnd, new)
    except Exception:
        return None             # never keep failing every half second
    return 0.5


def start() -> None:
    if os.name == "nt" and not bpy.app.background and not bpy.app.timers.is_registered(_title_tick):
        bpy.app.timers.register(_title_tick, first_interval=1.0, persistent=True)


def stop() -> None:
    if bpy.app.timers.is_registered(_title_tick):
        bpy.app.timers.unregister(_title_tick)
