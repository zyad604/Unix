# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Builds the installer you give to people: dist/<Name>-Setup-<version>.exe. Windows only.

    python scripts/build_release.py

It starts from the app in app/<folder> but takes only the engine and the add-on: none of your settings, chats,
snapshots, exports or projects go in. The result is one file. Double-clicking it installs the app for that user
(no admin), with Start menu and desktop shortcuts and an Uninstall entry in Settings > Apps.

The app's name is NAME below: change it there and build again.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import build_app  # noqa: E402  (shares the app folder, the C# compiler and the icon writer)

NAME = "Unix"
ENGINE = NAME + "-engine.exe"
LOGO_PNG = os.path.join(ROOT, "branding", "unix_logo.png")
LOGO_ICO = os.path.join(ROOT, "branding", "unix.ico")
MAGIC = b"UNIXPAK1"

APP = build_app.APP
DIST = os.path.join(ROOT, "dist")
STAGE = os.path.join(DIST, "stage", NAME)
ADDON_SRC = os.path.join(ROOT, "unix")
ADDON_DST = os.path.join(STAGE, build_app.VERSION, "scripts", "addons_core", "unix")
CSC = build_app.CSC


def version():
    text = open(os.path.join(ADDON_SRC, "blender_manifest.toml"), encoding="utf-8").read()
    return re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1)


def engine_in(folder):
    """The app's engine exe: the one exe in the app folder that is not the small launcher."""
    exes = [f for f in os.listdir(folder) if f.lower().endswith(".exe")]
    big = [f for f in exes if os.path.getsize(os.path.join(folder, f)) > 20 * 1024 ** 2]
    if len(big) != 1:
        raise SystemExit(f"Expected one engine exe in {folder}, found {big}")
    return big[0]


def stage_files():
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)                                     # our own previous build output
    os.makedirs(STAGE)
    src_engine = engine_in(APP)
    print("Copying the engine from", APP)
    leave_out_dirs = [os.path.join(APP, d) for d in os.listdir(APP)
                      if os.path.isdir(os.path.join(APP, d))
                      and d not in (build_app.VERSION, "blender.crt", "blender.shared", "license")]
    leave_out_dirs += [os.path.join(APP, build_app.VERSION, "scripts", "addons_core", "unix"),
                       os.path.join(APP, build_app.VERSION, "scripts", "startup", "bl_app_templates_system", "Unix"),
                       "__pycache__"]
    # the engine is copied under its new name; the old launcher and Blender's debug .cmd files point at blender.exe
    root_files = [os.path.join(APP, f) for f in os.listdir(APP)
                  if os.path.isfile(os.path.join(APP, f)) and f.lower().endswith((".exe", ".cmd", ".lib"))]
    print("  left out:", ", ".join(os.path.relpath(d, APP) if os.path.isabs(d) else d for d in leave_out_dirs))
    code = subprocess.run(["robocopy", APP, STAGE, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/MT:16",
                           "/XD", *leave_out_dirs,
                           "/XF", *root_files, "*.pdb", "*.bak*", "*.pyc"]).returncode
    if code >= 8:
        raise SystemExit("robocopy failed")
    shutil.copyfile(os.path.join(APP, src_engine), os.path.join(STAGE, ENGINE))
    shutil.copytree(ADDON_SRC, ADDON_DST, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.bak*", "*.orig"))
    for name, dest in (("LICENSE", "LICENSE.txt"), ("NOTICE.txt", "NOTICE.txt")):
        shutil.copyfile(os.path.join(ROOT, name), os.path.join(STAGE, dest))
    print("Setting the engine icon")
    build_app.set_icon(os.path.join(STAGE, ENGINE), LOGO_PNG)


def compile_cs(source, out, *extra):
    result = subprocess.run([CSC, "/nologo", "/target:winexe", "/optimize+", "/platform:x64",
                             "/win32icon:" + LOGO_ICO, "/reference:System.Windows.Forms.dll",
                             "/reference:System.Drawing.dll", "/reference:System.IO.Compression.dll",
                             *extra, "/out:" + out, source], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(result.stdout + result.stderr)


def build_launcher(tmp):
    print("Compiling", NAME + ".exe")
    text = open(os.path.join(ROOT, "app_template", "launcher.cs"), encoding="utf-8").read()
    text = text.replace('const string Engine = "unix.exe";', f'const string Engine = "{ENGINE}";')
    text = text.replace('const string Template = "Unix";', 'const string Template = "";')
    assert f'"{ENGINE}"' in text and 'Template = ""' in text, "launcher.cs changed: update build_release.py"
    text = text.replace("static class Launcher", ASSEMBLY.replace("@NAME@", NAME).replace("@VERSION@", version())
                        + "static class Launcher", 1)
    src = os.path.join(tmp, "launcher.cs")
    open(src, "w", encoding="utf-8").write(text)
    compile_cs(src, os.path.join(STAGE, NAME + ".exe"))


def installer_source(tmp, ver):
    text = open(os.path.join(ROOT, "installer", "setup.cs"), encoding="utf-8").read()
    text = text.replace("@NAME@", NAME).replace("@VERSION@", ver).replace("@ENGINE@", ENGINE)
    src = os.path.join(tmp, "setup.cs")
    open(src, "w", encoding="utf-8").write(text)
    return src


ASSEMBLY = """[assembly: System.Reflection.AssemblyTitle("@NAME@")]
[assembly: System.Reflection.AssemblyProduct("@NAME@")]
[assembly: System.Reflection.AssemblyCompany("Larpbase")]
[assembly: System.Reflection.AssemblyCopyright("Copyright (C) 2026 Larpbase. GPL-3.0-or-later.")]
[assembly: System.Reflection.AssemblyVersion("@VERSION@.0")]
[assembly: System.Reflection.AssemblyFileVersion("@VERSION@.0")]
"""

SETUP_PREFS = r'''
import bpy, addon_utils, os
addon_utils.enable("unix", default_set=True, persistent=True)
import unix
theme = os.path.join(os.path.dirname(unix.__file__), "theme", "Unix.xml")
bpy.ops.script.execute_preset(filepath=theme, menu_idname="USERPREF_MT_interface_theme_presets")
prefs = bpy.context.preferences
prefs.view.show_splash = False          # Blender's own splash: the app has its own look
prefs.view.show_developer_ui = False
# nothing from the build machine: the theme's source path, and Blender's per-user asset library (it pointed at this
# PC's Documents folder; Blender makes each user their own when it is missing)
try:
    prefs.themes[0].filepath = ""
except (AttributeError, TypeError) as exc:
    print("PREFS theme path kept:", exc)
while len(prefs.filepaths.asset_libraries):
    bpy.ops.preferences.asset_library_remove(index=0)
bpy.ops.wm.save_userpref()
print("PREFS saved", "unix" in prefs.addons)
'''


def make_prefs(tmp):
    """Fresh preferences for a new user: the add-on on, the purple theme, nothing else of yours."""
    print("Writing fresh preferences")
    os.makedirs(os.path.join(STAGE, "portable"), exist_ok=True)       # makes the engine portable: settings live here
    script = os.path.join(tmp, "prefs.py")
    open(script, "w", encoding="utf-8").write(SETUP_PREFS)
    out = subprocess.run([os.path.join(STAGE, ENGINE), "-b", "--factory-startup", "--python", script],
                         capture_output=True, text=True, timeout=300)
    lines = [l for l in out.stdout.splitlines() if l.startswith("PREFS") or "Error" in l or "Traceback" in l]
    print("  " + "\n  ".join(lines))
    saved = os.path.join(STAGE, "portable", "config", "userpref.blend")
    if not os.path.isfile(saved) or not any(l.startswith("PREFS saved True") for l in lines):
        raise SystemExit("Preferences were not written:\n" + out.stdout[-3000:] + out.stderr[-3000:])
    keep = open(saved, "rb").read()
    # Blender clears a path by ending it at its first byte: the old text stays behind in the fixed-size field.
    # Zero every leftover Windows path (same length, so the file's layout is untouched).
    keep = re.sub(rb"[A-Za-z]?:\\[^\x00]{3,}", lambda m: b"\x00" * len(m.group()), keep)
    home = os.path.expanduser("~").encode()
    for leak in (ROOT.encode(), home, home.replace(b"\\", b"/")):
        if leak.lower() in keep.lower() or leak[2:].lower() in keep.lower():
            raise SystemExit(f"The fresh preferences still name this PC's folder {leak!r}")
    shutil.rmtree(os.path.join(STAGE, "portable"))                    # whatever the add-on made while it ran
    os.makedirs(os.path.join(STAGE, "portable", "config", "unix"))
    open(saved, "wb").write(keep)
    # a new user starts in Beginner (the add-on alone leaves a plain Blender as it is until asked)
    with open(os.path.join(STAGE, "portable", "config", "unix", "island.json"), "w", encoding="utf-8") as fh:
        json.dump({"level": "beginner"}, fh)
    with open(os.path.join(STAGE, "brand.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": NAME}, fh)


ABOUT = """{name} {ver}
An AI 3D artist. Type what you want; it builds it in 3D, looks at its work and checks it.

Before the first use
  {name} runs on Claude Code, signed in with your own Claude account (no API key).
  Install Claude Code: https://docs.anthropic.com/en/docs/claude-code/setup
  then open a terminal, run  claude  once and sign in.

Your files
  Settings, chats and the copies {name} saves while it works are in the "portable" folder here.
  Uninstall (Settings > Apps, or uninstall.exe here) asks whether to keep them.

Licence
  {name} is Copyright (C) 2026 Larpbase, free software under the GNU GPL v3 or later (LICENSE.txt, NOTICE.txt).
  {name} is built on Blender {blender} (https://www.blender.org), free software under the GNU GPL v3 or later.
  The engine ({engine}) is Blender's own program, unmodified apart from its name and icon; its source code is at
  https://download.blender.org/source/ (blender-{blender}). The licence texts are in the "license" folder.
  {name}'s own code (4.5/scripts/addons_core/unix) is also GPL-3.0-or-later and is included as source.
  {name} is not made or endorsed by the Blender Foundation.
"""


def blender_version():
    out = subprocess.run([os.path.join(STAGE, ENGINE), "-b", "--factory-startup", "--version"],
                         capture_output=True, text=True, timeout=120).stdout
    m = re.search(r"Blender (\d+\.\d+\.\d+)", out)
    return m.group(1) if m else build_app.VERSION


def pack(setup_exe, ver):
    payload = os.path.join(DIST, "payload.zip")
    print("Packing (a minute or two)")
    total = 0
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for folder, dirs, files in os.walk(STAGE):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if f.endswith(".pyc"):
                    continue
                full = os.path.join(folder, f)
                z.write(full, os.path.relpath(full, STAGE).replace("\\", "/"))
                total += os.path.getsize(full)
    size = os.path.getsize(payload)
    out = os.path.join(DIST, f"{NAME}-Setup-{ver}.exe")
    with open(out, "wb") as dst:
        for part in (setup_exe, payload):
            with open(part, "rb") as src:
                shutil.copyfileobj(src, dst, 16 * 1024 ** 2)
        dst.write(struct.pack("<q", size) + MAGIC)
    os.remove(payload)
    os.remove(setup_exe)
    print(f"  app {total / 1024 ** 2:.0f} MB -> installer {os.path.getsize(out) / 1024 ** 2:.0f} MB")
    return out


def main():
    if sys.platform != "win32":
        raise SystemExit("This build script is for Windows.")
    ver = version()
    tmp = os.path.join(DIST, "tmp")
    os.makedirs(tmp, exist_ok=True)
    stage_files()
    build_launcher(tmp)
    make_prefs(tmp)
    src = installer_source(tmp, ver)
    compile_cs(src, os.path.join(STAGE, "uninstall.exe"), "/define:UNINSTALL")
    open(os.path.join(STAGE, "ABOUT.txt"), "w", encoding="utf-8").write(
        ABOUT.format(name=NAME, ver=ver, blender=blender_version(), engine=ENGINE))
    stub = os.path.join(tmp, "setup_stub.exe")
    compile_cs(src, stub)
    out = pack(stub, ver)
    shutil.rmtree(tmp)
    print("Done:", out)


if __name__ == "__main__":
    main()
