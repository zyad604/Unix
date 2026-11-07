# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Screenshot of another program's window (Windows only, stdlib only).

Used to let the model see what it built in Roblox Studio. The window is asked to paint
itself into a bitmap (``PrintWindow`` with full-content rendering), so it does not have to
be in front; a minimised window cannot be captured.
"""

from __future__ import annotations

import struct
import sys
import zlib
from typing import List, Optional, Tuple


class ShotError(Exception):
    pass


_API: dict = {}


def _api():
    """user32, kernel32 and gdi32 with every function this module calls declared.

    Undeclared, ctypes passes and returns plain 32-bit ints: a returned handle is cut to its low half and
    a pointer argument can arrive with garbage above it. Private ``WinDLL`` objects keep these
    declarations from changing the shared ``ctypes.windll`` functions other code in Blender may use."""
    if _API:
        return _API["user32"], _API["kernel32"], _API["gdi32"], _API["enum_proc"]
    import ctypes
    from ctypes import wintypes as w
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)

    def declare(fn, argtypes, restype):
        fn.argtypes, fn.restype = argtypes, restype

    declare(user32.EnumWindows, [enum_proc, w.LPARAM], w.BOOL)
    declare(user32.IsWindowVisible, [w.HWND], w.BOOL)
    declare(user32.IsIconic, [w.HWND], w.BOOL)
    declare(user32.GetWindowTextLengthW, [w.HWND], ctypes.c_int)
    declare(user32.GetWindowTextW, [w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int)
    declare(user32.GetWindowThreadProcessId, [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD)
    declare(user32.GetClientRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL)
    declare(user32.GetDC, [w.HWND], w.HDC)
    declare(user32.ReleaseDC, [w.HWND, w.HDC], ctypes.c_int)
    declare(user32.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL)
    declare(kernel32.OpenProcess, [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
    declare(kernel32.QueryFullProcessImageNameW, [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL)
    declare(kernel32.CloseHandle, [w.HANDLE], w.BOOL)
    declare(gdi32.CreateCompatibleDC, [w.HDC], w.HDC)
    declare(gdi32.CreateCompatibleBitmap, [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP)
    declare(gdi32.SelectObject, [w.HDC, w.HGDIOBJ], w.HGDIOBJ)
    declare(gdi32.DeleteObject, [w.HGDIOBJ], w.BOOL)
    declare(gdi32.DeleteDC, [w.HDC], w.BOOL)
    declare(gdi32.GetDIBits, [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int)
    _API.update(user32=user32, kernel32=kernel32, gdi32=gdi32, enum_proc=enum_proc)
    return user32, kernel32, gdi32, enum_proc


def _windows(process_name: str) -> List[Tuple[int, str]]:
    """[(window handle, title)] of visible top-level windows that belong to a process name."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32, _gdi32, enum_proc = _api()
    found: List[Tuple[int, str]] = []
    suffix = "\\" + process_name.lower()

    def each(hwnd, _lparam):
        if not hwnd or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return True
        name = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        try:
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size))
        finally:
            kernel32.CloseHandle(handle)
        if ok and name.value.lower().endswith(suffix):
            title = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title, length + 1)
            found.append((hwnd, title.value))
        return True

    callback = enum_proc(each)          # referenced until EnumWindows returns
    user32.EnumWindows(callback, 0)
    return found


def find_window(process_name: str, title_contains: str = "") -> Tuple[int, str]:
    if sys.platform != "win32":
        raise ShotError("Window screenshots are only available on Windows.")
    windows = _windows(process_name)
    if title_contains:
        wanted = title_contains.lower()
        windows = [w for w in windows if wanted in w[1].lower()] or windows
    if not windows:
        raise ShotError(f"No open window of {process_name}.")
    return max(windows, key=lambda w: len(w[1]))


def capture(hwnd: int, max_side: int = 1400) -> Tuple[int, int, bytes]:
    """(width, height, RGB bytes top-down), scaled down to at most ``max_side``."""
    import ctypes
    from ctypes import wintypes
    user32, _kernel32, gdi32, _enum = _api()
    if user32.IsIconic(hwnd):
        raise ShotError("The window is minimised. Restore it (it does not have to be in front) and try again.")
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ShotError("The window is gone.")
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width < 8 or height < 8:
        raise ShotError("The window has no visible area.")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    class BITMAPINFO(ctypes.Structure):     # GetDIBits may write colour masks after the header: room for them
        _fields_ = [("header", BITMAPINFOHEADER), ("colours", wintypes.DWORD * 256)]

    window_dc = user32.GetDC(hwnd)
    if not window_dc:
        raise ShotError("Could not reach the window's picture.")
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height) if memory_dc else None
    previous = gdi32.SelectObject(memory_dc, bitmap) if bitmap else None
    try:
        if not memory_dc or not bitmap:
            raise ShotError("Not enough memory for the window's picture.")
        if not user32.PrintWindow(hwnd, memory_dc, 0x1 | 0x2):   # client area only, rendered content included
            raise ShotError("The window refused to draw itself.")
        gdi32.SelectObject(memory_dc, previous)     # GetDIBits wants the bitmap not selected into a DC
        previous = None
        info = BITMAPINFO()
        info.header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        if gdi32.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0) != height:
            raise ShotError("Could not read the window's picture.")
    finally:
        if previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)

    raw = pixels.raw
    step = max(1, -(-max(width, height) // max_side))
    out_w, out_h = len(range(0, width, step)), len(range(0, height, step))
    rows = bytearray()
    stride = 4 * step
    for y in range(0, height, step):    # slices only: the per-pixel loop held the GIL for seconds (see ui/window_pump.py)
        row = raw[y * width * 4:(y + 1) * width * 4]
        rgb = bytearray(out_w * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::stride], row[1::stride], row[0::stride]      # BGRA -> RGB
        rows += rgb
    return out_w, out_h, bytes(rows)


def save_png(path: str, width: int, height: int, rgb: bytes) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    stride = width * 3
    raw = b"".join(b"\x00" + rgb[y * stride:(y + 1) * stride] for y in range(height))
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def is_blank(rgb: bytes) -> bool:
    sample = rgb[::997]
    return len(set(sample)) <= 2


def shoot(process_name: str, path: str, title_contains: str = "", max_side: int = 1400) -> Optional[str]:
    """Save a PNG of the program's window. Returns the window title."""
    hwnd, title = find_window(process_name, title_contains)
    width, height, rgb = capture(hwnd, max_side)
    if is_blank(rgb):
        raise ShotError("The window came back blank (it may be covered by a full-screen program or still loading).")
    save_png(path, width, height, rgb)
    return title


if __name__ == "__main__":
    print(shoot(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""))
