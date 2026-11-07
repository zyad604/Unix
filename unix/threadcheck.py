# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Debug check: report ``bpy`` used from any thread but Blender's main thread.

Blender's data is not thread-safe, and a ``bpy`` call from an HTTP worker thread races the main
thread's own changes. Tools that run off the main thread (``main_thread=False``) must hand every
``bpy`` step to the dispatcher (``registry.run_on_main``). ``install()`` swaps the ``bpy`` name in
every loaded module of the add-on for a stand-in that records each attribute read made off the main
thread (and still forwards it). It is switched on by ``UNIX_THREAD_CHECK=1`` in the environment and
by tests/test_stability.py; it costs one attribute lookup per ``bpy.`` and is off otherwise.

It sees ``bpy.data``, ``bpy.context``, ``bpy.ops``... reached through the module name, not reads
through an object fetched earlier on the main thread; tests/test_stability.py also checks the code
statically for that.
"""

from __future__ import annotations

import sys
import threading
import traceback
from typing import List, Tuple

import bpy as _bpy           # never swapped: the stand-in forwards to it

PACKAGE = __name__.rsplit(".", 1)[0]
violations: List[Tuple[str, str, str]] = []      # (module, attribute, where)
_swapped: List[str] = []


class _MainThreadOnly:
    __slots__ = ("_owner",)

    def __init__(self, owner: str) -> None:
        self._owner = owner

    def __getattr__(self, name: str):
        if threading.current_thread() is not threading.main_thread():
            where = "".join(traceback.format_stack(limit=8)[:-1])
            violations.append((self._owner, name, where))
            print(f"[Unix] bpy.{name} used off the main thread (in {self._owner}, "
                  f"thread {threading.current_thread().name}):\n{where}")
        return getattr(_bpy, name)


def install() -> int:
    """Swap ``bpy`` in the add-on's loaded modules. Returns how many modules were covered."""
    for name, module in list(sys.modules.items()):
        if (name == PACKAGE or name.startswith(PACKAGE + ".")) and getattr(module, "bpy", None) is _bpy:
            module.bpy = _MainThreadOnly(name)
            _swapped.append(name)
    return len(_swapped)


def uninstall() -> None:
    for name in _swapped:
        module = sys.modules.get(name)
        if module is not None and isinstance(getattr(module, "bpy", None), _MainThreadOnly):
            module.bpy = _bpy
    _swapped.clear()
