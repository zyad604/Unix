# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Builds the standalone Unix app from an installed Blender. Windows only.

    python scripts/build_app.py                 full build (copies Blender on the first run)
    python scripts/build_app.py --sync          only re-copy the add-on and template into the app

Result: app/Unix/Unix.exe - a portable, self-contained application:
  - the Blender 4.5 engine, renamed unix.exe, with the Unix icon;
  - the unix add-on bundled and enabled;
  - the Unix application template (splash, Zen and Studio modes, top bar);
  - its own settings in app/Unix/portable, separate from any installed Blender.

Blender is GPL software; the engine files are unmodified apart from the icon resource.
"""
import argparse
import ctypes
import io
import os
import shutil
import struct
import subprocess
import sys
from ctypes import wintypes as wt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSION = "4.5"


def _app_folder():
    """Where the app is. It is built as app/Unix, but the folder may have been renamed since:
    then it is whichever folder under app/ holds the add-on, and a sync goes there instead of
    quietly making a second, stale copy under the old name."""
    base = os.path.join(ROOT, "app")
    built = os.path.join(base, "Unix")
    if os.path.isdir(base) and not os.path.isdir(built):
        for name in sorted(os.listdir(base)):
            if os.path.isdir(os.path.join(base, name, VERSION, "scripts", "addons_core", "unix")):
                return os.path.join(base, name)
    return built


APP = _app_folder()
SOURCE_BLENDER = rf"C:\Program Files\Blender Foundation\Blender {VERSION}"
TEMPLATE_DST = os.path.join(APP, VERSION, "scripts", "startup", "bl_app_templates_system", "Unix")
ADDON_DST = os.path.join(APP, VERSION, "scripts", "addons_core", "unix")
ENGINE = os.path.join(APP, "unix.exe")
LAUNCHER = os.path.join(APP, "Unix.exe")
CSC = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"


def run(cmd, **kw):
    print("  >", " ".join(os.path.basename(c) if os.path.isabs(c) else c for c in cmd[:4]), "...")
    return subprocess.run(cmd, **kw)


def copy_engine():
    if os.path.isfile(ENGINE) or os.path.isfile(os.path.join(APP, "blender.exe")):
        return
    print("Copying Blender", VERSION, "(about 900 MB)")
    os.makedirs(os.path.dirname(APP), exist_ok=True)
    code = run(["robocopy", SOURCE_BLENDER, APP, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/MT:16",
                "/XF", "blender.pdb"]).returncode
    if code >= 8:
        raise SystemExit("robocopy failed")


def sync_sources():
    print("Syncing the add-on and the application template")
    for src, dst in ((os.path.join(ROOT, "unix"), ADDON_DST),
                     (os.path.join(ROOT, "app_template", "Unix"), TEMPLATE_DST)):
        os.makedirs(dst, exist_ok=True)
        code = run(["robocopy", src, dst, "/MIR", "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
                    "/XD", "__pycache__", "/XF", "startup.blend", "userpref.blend"]).returncode
        if code >= 8:
            raise SystemExit("robocopy failed")
    shutil.copyfile(os.path.join(ROOT, "branding", "unix_logo_256.png"), os.path.join(TEMPLATE_DST, "logo.png"))
    os.makedirs(os.path.join(APP, "portable"), exist_ok=True)


# -- icon resource -------------------------------------------------------------

def _icon_entries(logo_path=None):
    """[(size, bytes, is_png)] from branding/unix_logo.png (or ``logo_path``): PNG for 256, 32-bit DIB below."""
    from PIL import Image
    logo = Image.open(logo_path or os.path.join(ROOT, "branding", "unix_logo.png")).convert("RGBA")
    out = []
    for size in (256, 128, 64, 48, 32, 24, 16):
        img = logo.resize((size, size), Image.LANCZOS)
        if size == 256:
            buf = io.BytesIO()
            img.save(buf, "PNG")
            out.append((size, buf.getvalue(), True))
        else:
            pixels = img.tobytes("raw", "BGRA")
            rows = [pixels[y * size * 4:(y + 1) * size * 4] for y in range(size)][::-1]
            mask = b"\x00" * (((size + 31) // 32) * 4) * size
            header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
            out.append((size, header + b"".join(rows) + mask, False))
    return out


def set_icon(exe, logo_path=None):
    """Replace every icon group in ``exe`` with the Unix icon (or the picture at ``logo_path``)."""
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.LoadLibraryExW.restype = wt.HMODULE
    k.LoadLibraryExW.argtypes = [wt.LPCWSTR, wt.HANDLE, wt.DWORD]
    k.FreeLibrary.argtypes = [wt.HMODULE]
    k.BeginUpdateResourceW.restype = wt.HANDLE
    k.BeginUpdateResourceW.argtypes = [wt.LPCWSTR, wt.BOOL]
    k.UpdateResourceW.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wt.WORD, ctypes.c_void_p, wt.DWORD]
    k.EndUpdateResourceW.argtypes = [wt.HANDLE, wt.BOOL]
    NAME = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wt.LPARAM)
    LANG = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMODULE, ctypes.c_void_p, ctypes.c_void_p, wt.WORD, wt.LPARAM)
    k.EnumResourceNamesW.argtypes = [wt.HMODULE, ctypes.c_void_p, NAME, wt.LPARAM]
    k.EnumResourceLanguagesW.argtypes = [wt.HMODULE, ctypes.c_void_p, ctypes.c_void_p, LANG, wt.LPARAM]
    RT_ICON, RT_GROUP_ICON = 3, 14

    module = k.LoadLibraryExW(exe, None, 0x2)
    if not module:
        raise OSError(ctypes.get_last_error())
    found = {RT_ICON: [], RT_GROUP_ICON: []}
    for rtype in found:
        names = []
        k.EnumResourceNamesW(module, rtype, NAME(lambda h, t, n, p: names.append(int(n) if n < 0x10000 else ctypes.wstring_at(n)) or True), 0)
        for name in names:
            ref = name if isinstance(name, int) else ctypes.c_wchar_p(name)
            langs = []
            k.EnumResourceLanguagesW(module, rtype, ref, LANG(lambda h, t, n, lang, p: langs.append(lang) or True), 0)
            found[rtype] += [(name, lang) for lang in langs]
    k.FreeLibrary(module)

    entries = _icon_entries(logo_path)
    handle = k.BeginUpdateResourceW(exe, False)
    if not handle:
        raise OSError(ctypes.get_last_error())

    def put(rtype, name, lang, data):
        ref = name if isinstance(name, int) else ctypes.c_wchar_p(name)
        buf = ctypes.create_string_buffer(data, len(data)) if data is not None else None
        if not k.UpdateResourceW(handle, rtype, ref, lang, buf, len(data) if data is not None else 0):
            raise OSError(ctypes.get_last_error())

    for name, lang in found[RT_ICON]:
        put(RT_ICON, name, lang, None)                       # delete the old images
    groups = found[RT_GROUP_ICON] or [(1, 1033)]
    lang = groups[0][1]
    directory = struct.pack("<HHH", 0, 1, len(entries))
    for index, (size, data, _png) in enumerate(entries, start=1):
        put(RT_ICON, index, lang, data)
        directory += struct.pack("<BBBBHHIH", size % 256, size % 256, 0, 0, 1, 32, len(data), index)
    for name, group_lang in groups:
        put(RT_GROUP_ICON, name, group_lang, directory)
    if not k.EndUpdateResourceW(handle, False):
        raise OSError(ctypes.get_last_error())


def brand_engine():
    if not os.path.isfile(ENGINE):
        os.rename(os.path.join(APP, "blender.exe"), ENGINE)
        print("Renamed the engine to unix.exe")
    marker = os.path.join(APP, "portable", ".icon_set")
    if not os.path.isfile(marker):
        print("Setting the engine icon")
        set_icon(ENGINE)
        open(marker, "w").close()
    old_launcher = os.path.join(APP, "blender-launcher.exe")
    if os.path.isfile(old_launcher):
        os.remove(old_launcher)  # it looks for blender.exe, which no longer exists here


def build_launcher():
    print("Compiling Unix.exe")
    result = run([CSC, "/nologo", "/target:winexe", "/optimize+", "/platform:x64",
                  "/win32icon:" + os.path.join(ROOT, "branding", "unix.ico"),
                  "/reference:System.Windows.Forms.dll", "/out:" + LAUNCHER,
                  os.path.join(ROOT, "app_template", "launcher.cs")], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stdout + result.stderr)


def setup_inside_app():
    print("Building the start-up file and preferences inside the app (a window opens briefly)")
    result = run([ENGINE, "--factory-startup", "--app-template", "Unix", "--window-geometry", "80", "80",
                  "1500", "900", "--python", os.path.join(ROOT, "scripts", "app_setup.py"), "--", TEMPLATE_DST],
                 capture_output=True, text=True, timeout=300)
    lines = [l for l in result.stdout.splitlines() if l.startswith("SETUP") or "Error" in l or "Traceback" in l]
    print("\n".join("  " + l for l in lines))
    if not os.path.isfile(os.path.join(TEMPLATE_DST, "startup.blend")) or any("FAILED" in l for l in lines):
        raise SystemExit("The in-app setup did not finish. Output above.")


def shortcut():
    ps = (f'$s=(New-Object -ComObject WScript.Shell).CreateShortcut("{os.path.join(ROOT, "Unix.lnk")}");'
          f'$s.TargetPath="{LAUNCHER}";$s.WorkingDirectory="{APP}";$s.IconLocation="{LAUNCHER},0";$s.Save()')
    run(["powershell", "-NoProfile", "-Command", ps])


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync", action="store_true", help="only re-copy the add-on and template")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("This build script is for Windows.")
    if args.sync:
        if not os.path.isdir(ADDON_DST):
            raise SystemExit(f"No app to sync into under {os.path.dirname(APP)}. Run without --sync to build it.")
        sync_sources()
        if os.path.isfile(ENGINE):
            build_launcher()
        else:       # the engine was renamed by hand: the launcher looks for it by name, so it is left as it is
            print(f"The engine in {APP} is not named {os.path.basename(ENGINE)}: the launcher was not rebuilt.")
        print("Synced:", APP)
        return
    copy_engine()
    sync_sources()
    brand_engine()
    build_launcher()
    setup_inside_app()
    shortcut()
    print("Done:", LAUNCHER)


if __name__ == "__main__":
    main()
