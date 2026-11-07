# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""What is on the Windows clipboard that can be attached to a message: files copied in Explorer,
or a picture (a screenshot, "Copy image" in a browser). Read-only: the clipboard is never changed.

No ``bpy`` and nothing outside the standard library, so it can be tested without Blender.
"""

from __future__ import annotations

import os
import struct
import sys
import time
from typing import List, Optional, Tuple

try:
    from .window_shot import save_png
except ImportError:              # loaded on its own, outside the package (the bridge tests)
    from window_shot import save_png

CF_DIB, CF_HDROP = 8, 15
MAX_SIDE = 2048
ATTACHABLE = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tga", ".tif", ".tiff", ".exr", ".hdr",
              ".glb", ".gltf", ".fbx", ".obj", ".stl", ".ply", ".usd", ".usdc", ".usda", ".usdz", ".blend")


def dib_to_rgb(data: bytes) -> Optional[Tuple[int, int, bytes]]:
    """(width, height, RGB bytes top-down) from a clipboard bitmap (CF_DIB), or None when it is a
    kind this does not read (palettes, compressed bitmaps)."""
    if len(data) < 40:
        return None
    size, width, height, _planes, bits, compression = struct.unpack_from("<IiiHHI", data, 0)
    colours = struct.unpack_from("<I", data, 32)[0]
    if bits not in (24, 32) or compression not in (0, 3) or width <= 0 or height == 0:
        return None
    start = size + (12 if compression == 3 and size == 40 else 0) + colours * 4
    step = bits // 8
    stride = ((width * bits + 31) // 32) * 4
    rows = abs(height)
    if len(data) < start + stride * rows:
        return None
    skip = max(1, -(-max(width, rows) // MAX_SIDE))          # a huge screenshot is thinned, not refused
    out_width = len(range(0, width, skip))
    picked = range(0, rows, skip)
    out = bytearray(out_width * len(picked) * 3)
    for line, row in enumerate(picked):
        source = (rows - 1 - row) if height > 0 else row    # a positive height means the rows are stored bottom-up
        pixels = data[start + source * stride:start + source * stride + width * step]
        at = line * out_width * 3
        out[at:at + out_width * 3:3] = pixels[2::step][::skip]
        out[at + 1:at + out_width * 3:3] = pixels[1::step][::skip]
        out[at + 2:at + out_width * 3:3] = pixels[0::step][::skip]
    return out_width, len(picked), bytes(out)


def _read(user32, kernel32, fmt: int) -> Optional[bytes]:
    handle = user32.GetClipboardData(fmt)
    if not handle:
        return None
    pointer = kernel32.GlobalLock(handle)
    if not pointer:
        return None
    try:
        import ctypes
        return ctypes.string_at(pointer, kernel32.GlobalSize(handle))
    finally:
        kernel32.GlobalUnlock(handle)


def grab(folder: str) -> List[str]:
    """Files to attach from the clipboard: the copied files themselves, or one PNG written into
    ``folder`` from the copied picture. An empty list when the clipboard holds neither."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    # Private WinDLL objects: declaring these on the shared ctypes.windll functions would change them for
    # every other user in the process. Each function is declared in full (handles are pointer-sized).
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    user32.OpenClipboard.argtypes, user32.OpenClipboard.restype = [wintypes.HWND], wintypes.BOOL
    user32.CloseClipboard.argtypes, user32.CloseClipboard.restype = [], wintypes.BOOL
    user32.GetClipboardData.argtypes, user32.GetClipboardData.restype = [wintypes.UINT], wintypes.HANDLE
    user32.IsClipboardFormatAvailable.argtypes, user32.IsClipboardFormatAvailable.restype = [wintypes.UINT], wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes, user32.RegisterClipboardFormatW.restype = [wintypes.LPCWSTR], wintypes.UINT
    kernel32.GlobalLock.argtypes, kernel32.GlobalLock.restype = [wintypes.HGLOBAL], wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes, kernel32.GlobalUnlock.restype = [wintypes.HGLOBAL], wintypes.BOOL
    kernel32.GlobalSize.argtypes, kernel32.GlobalSize.restype = [wintypes.HGLOBAL], ctypes.c_size_t
    shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    png_format = user32.RegisterClipboardFormatW("PNG")
    for _attempt in range(5):                       # another program may hold the clipboard for a moment
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return []
    files: List[str] = []
    png: Optional[bytes] = None
    bitmap: Optional[bytes] = None
    try:
        if user32.IsClipboardFormatAvailable(CF_HDROP):
            handle = user32.GetClipboardData(CF_HDROP)
            if handle:
                for index in range(shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)):
                    length = shell32.DragQueryFileW(handle, index, None, 0)       # without the terminator
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    if length and shell32.DragQueryFileW(handle, index, buffer, length + 1):
                        files.append(buffer.value)
        if not files:
            if png_format and user32.IsClipboardFormatAvailable(png_format):
                png = _read(user32, kernel32, png_format)
            if png is None and user32.IsClipboardFormatAvailable(CF_DIB):
                bitmap = _read(user32, kernel32, CF_DIB)
    finally:
        user32.CloseClipboard()
    if files:
        return [path for path in files if os.path.isfile(path) and path.lower().endswith(ATTACHABLE)]
    if png is None and bitmap is None:
        return []
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("pasted_%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}.png")
    if png is not None and png[:8] == b"\x89PNG\r\n\x1a\n":
        with open(path, "wb") as handle:
            handle.write(png)
        return [path]
    picture = dib_to_rgb(bitmap) if bitmap is not None else None
    if picture is None:
        return []
    save_png(path, *picture)
    return [path]
