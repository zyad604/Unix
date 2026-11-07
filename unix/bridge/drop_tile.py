# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Launch the Unix drop tile: a small window whose content you drag into any application.

Blender has no way to start an operating-system drag, so exported files are handed to a
tiny Windows helper (``drop_tile.cs``). It is compiled on first use with the C# compiler
that ships with Windows, so no binary has to be distributed. No ``bpy`` import.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "drop_tile.cs")
_COMPILERS = (r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
              r"C:\Windows\Microsoft.NET\Framework\v4.0.30319\csc.exe")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_current: Optional[subprocess.Popen] = None


class DropTileError(Exception):
    pass


def build(bin_dir: str) -> str:
    """Compile the helper into ``bin_dir`` if it is missing or older than its source."""
    if sys.platform != "win32":
        raise DropTileError("The drag tile is available on Windows only.")
    exe = os.path.join(bin_dir, "UnixDrop.exe")
    if os.path.isfile(exe) and os.path.getmtime(exe) >= os.path.getmtime(SOURCE):
        return exe
    compiler = next((c for c in _COMPILERS if os.path.isfile(c)), None)
    if compiler is None:
        raise DropTileError("The .NET Framework C# compiler was not found, so the drag tile cannot be built.")
    os.makedirs(bin_dir, exist_ok=True)
    icon = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "unix.ico")
    result = subprocess.run([compiler, "/nologo", "/target:winexe", "/optimize+",
                             *(["/win32icon:" + icon] if os.path.isfile(icon) else []),
                             "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
                             "/out:" + exe, SOURCE], capture_output=True, text=True, creationflags=_NO_WINDOW)
    if result.returncode != 0 or not os.path.isfile(exe):
        raise DropTileError("Building the drag tile failed: " + (result.stdout + result.stderr).strip()[:400])
    return exe


def show(files: List[str], bin_dir: str) -> str:
    """Show the tile for ``files`` (replacing a tile that is already up). Returns the helper path."""
    global _current
    files = [f for f in files if os.path.isfile(f)]
    if not files:
        raise DropTileError("There is no exported file to drag yet.")
    exe = build(bin_dir)
    if _current is not None and _current.poll() is None:
        _current.terminate()
    _current = subprocess.Popen([exe] + files)
    return exe
