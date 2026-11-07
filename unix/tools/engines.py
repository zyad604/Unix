# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Game-engine bridge: find the user's engine projects and send assets straight into them.

For Unity, Unreal and Godot "attach" means discovery, not a plugin: the bridge reads which
editors are running (and their open project from the process command line), falls back to
each engine's own recent-project list, and writes the asset into the folder that engine
watches; the engine imports a file that appears there on its own. Roblox Studio has no
project folder, so it gets a real link instead: a plugin in every open Studio window asks
Unix for work and builds what it is sent (see ``roblox.py`` and ``bridge/roblox_link.py``).
Without the plugin its file goes to an exports folder and a drag tile. An Unreal editor whose
project has remote execution on is linked too: the scene is built in its open level (see
``unreal.py`` and ``bridge/unreal_link.py``); without it the FBX goes into Content/Unix as before
and the link is switched on in the project's files for the editor's next start.

The file name is stable (the asset's name), so sending again overwrites the file and
the engine re-imports it in place: that is the live-update path.

Windows only for discovery; exports work anywhere.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import bpy
from mathutils import Vector

from ..bridge import roblox_link, unreal_link
from ._util import data_dir, ensure_object_mode, get_object, ui_context
from .files import _glb_summary
from .layout import _bounds
from .registry import Pending, ToolError, registry, tool

ENGINES = ("unity", "unreal", "godot", "roblox")
LABELS = {"unity": "Unity", "unreal": "Unreal", "godot": "Godot", "roblox": "Roblox Studio", "file": "a file"}
FOLDERLESS = {"roblox", "file"}  # no project folder: the file goes to Unix's exports and the drag tile
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_EXPORTABLE = {'MESH', 'ARMATURE', 'EMPTY', 'CURVE', 'SURFACE', 'FONT', 'META'}
ROBLOX_TRIANGLE_LIMIT = 20_000
WAIT_POLL = 0.1                 # seconds between looks while a send waits for the engine
_cache: Dict[str, Any] = {"time": 0.0, "targets": []}


# -- discovery ---------------------------------------------------------------------

def _running_editors() -> List[Tuple[str, str]]:
    """[(process name, command line)] for running engine editors."""
    if sys.platform != "win32":
        return []
    # single-quoted for PowerShell (inner quotes doubled) so no double quotes need escaping
    query = ("Get-CimInstance Win32_Process -Filter 'name=''Unity.exe'' or name=''UnrealEditor.exe'' or "
             "name=''RobloxStudioBeta.exe'' or name like ''Godot%''' | ForEach-Object { $_.Name + '|' + $_.CommandLine }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True, timeout=20,
                             creationflags=_NO_WINDOW).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [tuple(line.split("|", 1)) for line in out.splitlines() if "|" in line]  # type: ignore[misc]


def _project_root(engine: str, path: str) -> Optional[str]:
    """Normalise a path to the engine's project folder, or None when it is not a project."""
    path = os.path.normpath(os.path.expandvars(path.strip().strip('"')))
    if engine == "unreal" and path.lower().endswith(".uproject"):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        return None
    if engine == "unity" and os.path.isdir(os.path.join(path, "Assets")):
        return path
    if engine == "unreal" and any(f.lower().endswith(".uproject") for f in os.listdir(path)):
        return path
    if engine == "godot" and os.path.isfile(os.path.join(path, "project.godot")):
        return path
    return None


def _recent_projects() -> List[Tuple[str, str]]:
    """[(engine, project folder)] from each engine's own records, newest first where known."""
    found: List[Tuple[str, str]] = []
    appdata, local = os.environ.get("APPDATA", ""), os.environ.get("LOCALAPPDATA", "")
    # Godot: sections of projects.cfg are project folders
    try:
        with open(os.path.join(appdata, "Godot", "projects.cfg"), encoding="utf-8") as fh:
            found += [("godot", m) for m in re.findall(r"^\[(.+)\]\s*$", fh.read(), re.M)]
    except OSError:
        pass
    # Unreal: RecentlyOpenedProjectFiles in each version's EditorSettings.ini
    unreal: List[Tuple[str, str]] = []
    base = os.path.join(local, "UnrealEngine")
    for version in sorted(os.listdir(base), reverse=True) if os.path.isdir(base) else []:
        ini = os.path.join(base, version, "Saved", "Config", "WindowsEditor", "EditorSettings.ini")
        try:
            with open(ini, encoding="utf-8", errors="replace") as fh:
                for path, stamp in re.findall(r'RecentlyOpenedProjectFiles=\(ProjectName="([^"]+)",LastOpenTime=([^)]+)\)', fh.read()):
                    if "\\temp\\" not in os.path.normpath(path).lower():
                        unreal.append((stamp, path))
        except OSError:
            continue
    found += [("unreal", path) for _stamp, path in sorted(unreal, reverse=True)]
    # Unity: the Hub's project list, then the editor's registry entries
    for name in ("projects-v1.json", "projectDir.json"):
        try:
            with open(os.path.join(appdata, "UnityHub", name), encoding="utf-8") as fh:
                data = json.load(fh)
            entries = data.get("data", data) if isinstance(data, dict) else {}
            found += [("unity", p) for p in (entries if isinstance(entries, dict) else {})]
        except (OSError, ValueError):
            pass
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Unity Technologies\Unity Editor 5.x") as key:
            for index in range(winreg.QueryInfoKey(key)[1]):
                value_name, value, _kind = winreg.EnumValue(key, index)
                if value_name.startswith("RecentlyUsedProjectPaths") and isinstance(value, bytes):
                    found.append(("unity", value.decode("utf-8", errors="replace").rstrip("\x00")))
    except (ImportError, OSError):
        pass
    return found


def _saved() -> Dict[str, str]:
    try:
        with open(os.path.join(data_dir(), "engines.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def remember(engine: str, project: str) -> None:
    saved = _saved()
    saved[engine] = project
    with open(os.path.join(data_dir(), "engines.json"), "w", encoding="utf-8") as fh:
        json.dump(saved, fh)


def find_targets(refresh: bool = False) -> List[Dict[str, Any]]:
    """Engine targets, best first per engine: running editors, then recent, then remembered."""
    if not refresh and _cache.get("time") and time.monotonic() - _cache["time"] < 20.0:     # an empty list is an answer too
        return _cache["targets"]
    targets: List[Dict[str, Any]] = []
    seen = set()

    def add(engine: str, project: Optional[str], running: bool) -> None:
        key = (engine, (project or "").lower())
        if key in seen:
            return
        seen.add(key)
        name = os.path.basename(project) if project else "open place"
        targets.append({"engine": engine, "label": LABELS[engine], "project": project or "", "name": name, "running": running})

    linked = roblox_link.link.sessions()
    for session in linked:      # every Studio window whose plugin is talking to us is its own target
        targets.append({"engine": "roblox", "label": LABELS["roblox"], "project": session["session"],
                        "name": session["place"], "running": True, "linked": True})
    for process, command in _running_editors():
        lower = process.lower()
        if lower == "unity.exe":
            match = re.search(r'-projectpath\s+("([^"]+)"|(\S+))', command, re.I)
            root = _project_root("unity", match.group(2) or match.group(3)) if match else None
            if root:
                add("unity", root, True)
        elif lower == "unrealeditor.exe":
            match = re.search(r'([A-Za-z]:[^"]*?\.uproject)', command)
            root = _project_root("unreal", match.group(1)) if match else None
            if root:
                add("unreal", root, True)
        elif lower.startswith("godot"):
            match = re.search(r'--path\s+("([^"]+)"|(\S+))', command)
            root = _project_root("godot", match.group(2) or match.group(3)) if match else None
            if root:
                add("godot", root, True)
        elif lower == "robloxstudiobeta.exe" and not linked:
            add("roblox", None, True)
    for engine, path in _recent_projects():
        root = _project_root(engine, path)
        if root:
            add(engine, root, False)
    for engine, path in _saved().items():
        root = _project_root(engine, path) if engine in ENGINES else None
        if root:
            add(engine, root, False)
    if not any(t["engine"] == "roblox" for t in targets):
        add("roblox", None, False)
    targets.sort(key=lambda t: (ENGINES.index(t["engine"]), not t["running"]))
    _cache.update(time=time.monotonic(), targets=targets)
    return targets


# -- export ------------------------------------------------------------------------

def paced(sender: Any) -> Pending:
    """A send's steps, back to back while Blender has work (the gaps between steps were most of a
    send of many small meshes), and every WAIT_POLL seconds while it only waits for the engine."""
    def poll() -> Any:
        out = sender.step()
        if out is not None:
            return out
        return Pending(poll, every=WAIT_POLL if sender.waiting else 0.0, cancel=sender.close)
    return Pending(poll, every=0.0, cancel=sender.close)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or "Asset"


def _family(names: Optional[List[str]]) -> List[bpy.types.Object]:
    view_layer = bpy.context.view_layer
    roots = [get_object(n) for n in names] if names else [o for o in view_layer.objects if o.select_get()]
    if not roots:
        raise ToolError("Nothing to send. Select the objects in the viewport, or pass their names in `objects`.")
    family: List[bpy.types.Object] = []
    for obj in roots:
        family.extend([obj] + list(obj.children_recursive))
    family = [o for o in dict.fromkeys(family) if o.type in _EXPORTABLE and o.name in view_layer.objects]
    if not any(o.type == 'MESH' for o in family):
        raise ToolError("The selection has no mesh to export.")
    return family


def _asset_name(family: List[bpy.types.Object], name: str) -> str:
    roots = [o for o in family if o.parent is None or o.parent not in family]
    active = bpy.context.view_layer.objects.active
    return _safe_name(name or (roots[0].name if len(roots) == 1 else active.name if active in family else roots[0].name))


def link_help() -> str:
    """Why Roblox Studio is not linked, and the one thing to do about it."""
    try:
        state = roblox_link.install_plugin()
    except OSError as exc:
        return f"The Unix Bridge plugin could not be installed into Roblox Studio ({exc})."
    if state == "no roblox":
        return "Roblox Studio does not seem to be installed for this Windows user."
    if state == "off":
        return ("The Roblox Studio link is switched off (add-on preferences > Roblox Studio link), so the asset was "
                "exported as a file to import by hand.")
    if state in ("installed", "updated"):
        return ("The Unix Bridge plugin was just put into Roblox Studio's plugin folder. Restart Roblox Studio once, "
                "and click Allow when it asks whether the plugin may talk to 127.0.0.1. After that sends are automatic.")
    running = any(process.lower() == "robloxstudiobeta.exe" for process, _command in _running_editors())
    if running:
        return ("Roblox Studio is open but its Unix Bridge plugin has not connected. Restart Studio once (plugins load at "
                "start), allow its HTTP requests if Studio asks, and stop any play test: the link works in Edit mode.")
    return "Roblox Studio is not open. Open your place in Studio and send again: the link connects by itself."


def send_roblox(objects: Optional[List[str]] = None, name: str = "", project: str = "", at_origin: bool = True,
                upload: bool = False):
    """Prepare objects for a linked Roblox Studio window. Main thread. Raises ToolError('no_link') when no
    Studio window is connected. Returns a ``roblox.Sender``: call its ``step`` until it returns the report.
    ``upload`` means the user has already said yes to uploading meshes; otherwise their preference decides,
    and by default they are asked when the send turns out to need it."""
    from . import roblox
    ensure_object_mode()
    session = roblox.pick_session(project)
    view_layer = bpy.context.view_layer
    roots = [get_object(n) for n in objects] if objects else [o for o in view_layer.objects if o.select_get()]
    if not roots:
        raise ToolError("Nothing to send. Select the objects in the viewport, or pass their names in `objects`.")
    family: List[bpy.types.Object] = []
    for obj in roots:
        family.extend([obj] + list(obj.children_recursive))
    family = [o for o in dict.fromkeys(family) if o.name in view_layer.objects]
    keep = "upload" if upload else _mesh_policy()
    return roblox.Sender(family, _asset_name(family, name), session, at_origin, keep, _remember_upload)


def _preferences():
    addon = bpy.context.preferences.addons.get(__package__.rpartition(".")[0])
    return addon.preferences if addon else None


def _mesh_policy() -> str:
    """What to do with meshes Roblox would only keep as uploaded assets: 'ask', 'upload' or 'preview'."""
    prefs = _preferences()
    if prefs is None:
        return "preview"         # no add-on preferences (tests, scripts): never upload on our own
    return {"UPLOAD": "upload", "PREVIEW": "preview"}.get(getattr(prefs, "roblox_meshes", "ASK"), "ask")


def _remember_upload() -> None:
    """The user answered 'always': keep it, so they are not asked again."""
    prefs = _preferences()
    if prefs is None:
        return
    prefs.roblox_meshes = 'UPLOAD'
    try:
        bpy.ops.wm.save_userpref()
    except Exception:
        pass                     # still set for this session


def _destination(engine: str, project: str, name: str) -> Tuple[str, str]:
    """(absolute file path, path as the engine shows it)."""
    if engine == "unity":
        return os.path.join(project, "Assets", "Unix", f"{name}.fbx"), f"Assets/Unix/{name}.fbx"
    if engine == "unreal":
        return os.path.join(project, "Content", "Unix", f"{name}.fbx"), f"/Game/Unix/{name}"
    if engine == "godot":
        return os.path.join(project, "unix", f"{name}.glb"), f"res://unix/{name}.glb"
    if engine == "file":
        return os.path.join(data_dir("exports", "drag"), f"{name}.glb"), f"{name}.glb"
    return os.path.join(data_dir("exports", "roblox"), f"{name}.fbx"), f"{name}.fbx"


def _export(engine: str, path: str) -> Dict[str, Any]:
    """Run the exporter on the current selection with the engine's preset. Returns the preset used."""
    if engine in {"godot", "file"}:
        preset = dict(export_format='GLB', use_selection=True, export_apply=True, export_yup=True,
                      export_cameras=False, export_lights=False)
        bpy.ops.export_scene.gltf(filepath=path, **preset)
        return preset
    preset = dict(use_selection=True, object_types={'MESH', 'ARMATURE', 'EMPTY'}, use_mesh_modifiers=True,
                  axis_forward='-Z', axis_up='Y', apply_unit_scale=True, add_leaf_bones=False,
                  use_armature_deform_only=True, path_mode='COPY', embed_textures=True, bake_anim=True)
    if engine == "unity":
        # FBX_SCALE_UNITS + baked space transform: 1 Blender meter = 1 Unity unit, no stray -90 X rotation
        preset.update(apply_scale_options='FBX_SCALE_UNITS', bake_space_transform=True, mesh_smooth_type='OFF', use_tspace=True)
    elif engine == "unreal":
        # FBX_SCALE_NONE: Unreal reads the file's meter unit and converts to centimeters itself
        preset.update(apply_scale_options='FBX_SCALE_NONE', mesh_smooth_type='FACE', use_tspace=True)
    else:  # roblox
        preset.update(apply_scale_options='FBX_SCALE_ALL', mesh_smooth_type='FACE', use_triangles=True, bake_anim=False)
    bpy.ops.export_scene.fbx(filepath=path, **preset)
    return {k: (sorted(v) if isinstance(v, set) else v) for k, v in preset.items()}


def send(engine: str, objects: Optional[List[str]] = None, name: str = "", project: str = "",
         at_origin: bool = True) -> Dict[str, Any]:
    """Export objects into an engine project. Main thread. Shared by the tool and the UI button."""
    if engine not in ENGINES and engine != "file":
        raise ToolError(f"Unknown engine '{engine}'. Use one of: {', '.join(ENGINES)}.")
    ensure_object_mode()
    family = _family(objects)
    targets = find_targets()
    running = False
    if engine not in FOLDERLESS:
        if project:
            root = _project_root(engine, project)
            if root is None:
                marker = {"unity": "an Assets folder", "unreal": "a .uproject file", "godot": "a project.godot file"}[engine]
                raise ToolError(f"'{project}' is not a {LABELS[engine]} project (it needs {marker}).")
            project = root
            remember(engine, project)
        else:
            match = next((t for t in targets if t["engine"] == engine and t["project"]), None)
            if match is None:
                raise ToolError(f"No {LABELS[engine]} project found. Open the project in {LABELS[engine]} and try again, "
                                f"or pass `project` with the project folder.")
            project, running = match["project"], match["running"]
    else:
        running = any(t["engine"] == "roblox" and t["running"] for t in targets)

    roots = [o for o in family if o.parent is None or o.parent not in family]
    asset = _safe_name(name or (roots[0].name if len(roots) == 1 else bpy.context.view_layer.objects.active.name
                                if bpy.context.view_layer.objects.active in family else roots[0].name))
    path, engine_path = _destination(engine, project, asset)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    existed = os.path.isfile(path)

    view_layer = bpy.context.view_layer
    saved_selection = [o for o in view_layer.objects if o.select_get()]
    saved_active = view_layer.objects.active
    offset = Vector((0.0, 0.0, 0.0))
    box = _bounds(family)
    if at_origin and box is not None:  # base-centre pivot at the world origin, for the export only
        lo, hi = box
        offset = Vector((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z))
    try:
        for root in roots:
            root.matrix_world.translation = root.matrix_world.translation + offset
        for o in view_layer.objects:
            o.select_set(False)
        for o in family:
            o.select_set(True)
        view_layer.objects.active = next(o for o in family if o.type == 'MESH')
        view_layer.update()
        with ui_context():
            preset = _export(engine, path)
    finally:
        for root in roots:
            root.matrix_world.translation = root.matrix_world.translation - offset
        for o in view_layer.objects:
            o.select_set(False)
        for o in saved_selection:
            try:
                o.select_set(True)
            except Exception:
                pass
        view_layer.objects.active = saved_active
        view_layer.update()
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ToolError("The exporter ran but wrote no file.")

    meshes = [o for o in family if o.type == 'MESH']
    triangles = {o.name: sum(len(p.vertices) - 2 for p in o.data.polygons) for o in meshes}
    warnings: List[str] = []
    unscaled = [o.name for o in meshes if any(abs(s - 1.0) > 1e-4 for s in o.scale)]
    if unscaled:
        warnings.append(f"unapplied scale on {', '.join(unscaled[:5])}: sizes may be off in the engine")
    no_uv = [o.name for o in meshes if not o.data.uv_layers]
    if no_uv:
        warnings.append(f"no UVs on {', '.join(no_uv[:5])}: textures will not map")
    if engine == "roblox":
        heavy = [n for n, t in triangles.items() if t > ROBLOX_TRIANGLE_LIMIT]
        if heavy:
            warnings.append(f"over Roblox's per-mesh limit of about {ROBLOX_TRIANGLE_LIMIT:,} triangles: {', '.join(heavy[:5])}")
    next_step = {
        "unity": "Unity imports it when its window gets focus. Find it under Assets/Unix and drag it into the scene.",
        "unreal": "Unreal detects the new file in Content/Unix and offers to import it (Auto Reimport). "
                  "If nothing appears, right-click the Unix folder in the Content Browser and choose Import.",
        "godot": "Godot imports it when its window gets focus. Find it under res://unix and drag it into the scene.",
        "roblox": "In Roblox Studio use File > Import 3D and pick this file (or drag the Unix tile into Studio). "
                  "In the importer set the scale unit to Meter.",
        "file": "Drag the Unix tile into any application, or use the file directly.",
    }[engine]
    out: Dict[str, Any] = {
        "engine": LABELS[engine], "project": project or "(no project folder)", "editor_running": running,
        "file": path, "in_engine": engine_path, "updated_existing": existed, "bytes": os.path.getsize(path),
        "objects": [o.name for o in family][:40], "triangles": sum(triangles.values()),
        "pivot": "base centre at the origin" if at_origin else "as placed in the scene",
        "preset": preset, "next": next_step,
    }
    if path.endswith(".glb"):
        out["file_contains"] = _glb_summary(path)
    if warnings:
        out["warnings"] = warnings
    return out


@tool(
    "engine_targets",
    """List the game-engine projects Unix can send assets to: editors that are running now
    (Unity, Unreal, Godot, Roblox Studio) with their open project, then each engine's recent
    projects. Call it when the user mentions a game engine and you need to know where to send.""",
    main_thread=False,
    timeout=40.0,
    label="Looked for game engines",
)
def engine_targets() -> Dict[str, Any]:
    targets = find_targets(refresh=True)
    editors = unreal_link.find_editors()        # Unreal editors with remote execution on answer this

    def linked(t: Dict[str, Any]) -> bool:
        if t["engine"] == "unreal":
            return any(unreal_link.same_folder(e.get("project_root", ""), t["project"]) for e in editors)
        return bool(t.get("linked"))
    out: Dict[str, Any] = {
        "targets": [{"engine": t["engine"], "project": t["project"] or None, "name": t["name"], "running": t["running"],
                     **({"live_link": True} if linked(t) else {})} for t in targets],
        "note": "send_to_engine picks the running project of an engine by default."}
    places = [t["name"] for t in targets if t.get("linked")]
    if len(places) > 1:
        out["roblox"] = (f"{len(places)} Roblox Studio windows are linked: {', '.join(places)}. Ask the user which place before "
                         f"sending, and pass its name as `project`.")
    elif not places:
        out["roblox"] = link_help()
    return out


@tool(
    "send_to_engine",
    """Send objects from Blender straight into a game engine project with that engine's
    correct format, scale and axes: Unity (FBX into Assets/Unix), Godot (GLB into res://unix),
    Unreal through its live link: a whole map goes (with nothing selected or named, the whole
    scene), and each mesh with its materials and textures, the lights and the camera are built
    in the level open in the editor, where they stood in Blender, lit as Blender shows them (with
    a whole map, Blender's world comes along as its sky light unless the level has its own);
    sending again replaces them and imports only what changed, so send again freely after a fix.
    If no editor is linked, the tool switches the link on in the Unreal project's files by
    itself (the user asked for that: never ask first), exports the FBX into Content/Unix
    meanwhile, and its `live_link` says the editor must be restarted once: relay that. Or
    Roblox Studio through its live link: the asset is built inside the open place, in front of
    the Studio camera. Sizes follow the scene's units: units None means 1 Blender unit = 1 stud,
    metric means metres (a stud is 0.28 m); never rescale or parent the scene to change the size. Boxes, cylinders,
    balls and wedge-shaped prisms become native Parts and every other shape a MeshPart. An
    effect made here that plays on the spot (fire, smoke, any vfx_column) arrives as itself: the
    tool films its frames and Roblox plays them as a looping flipbook, so it looks there as it
    looks here; bursts, weather and orbs are rebuilt from Roblox's own particle emitters. For
    Roblox send the effect's VFX_ root, a whole scene or anything else as it is: do not refuse
    because something is a simulation or has no clean mesh, do not make a flipbook yourself
    first, and do not build it in Studio with roblox_build instead. Never
    decimate or simplify to fit Roblox: heavy meshes are cut into pieces of 10,000 triangles so
    a realistic model keeps all its detail, small things travel together, and a shape used many
    times is sent once. Objects that move in Blender's timeline (keyframes, constraints) keep
    that motion in Roblox, looping, in Edit mode and in Play. A rigged character travels rigged:
    meshes an armature deforms arrive skinned to its bones, and every action of the rig arrives
    as an animation that loops there (the model's Animation attribute picks one by name, Enabled
    stops and starts it). Send the meshes, or the armature with them, as they are: do not apply
    the Armature modifier or bake a pose first.
    When the user asks to send something, send it at once: do not run check_mesh or
    check_materials first and do not fix normals yourself. For Roblox the tool turns meshes that
    were built inside out, draws open ones from both sides, carries each surface whole (its
    colour, normal, roughness and metal pictures up to 1024 px, cut-outs and glow, with tint and
    tiling, as a SurfaceAppearance), and bakes any look made by shader nodes into such pictures,
    all without changing the scene. In both engines a picture projected from the object's own
    coordinates (Object or Generated, flat or Box) travels as itself on UVs made to match, sharp
    and repeating, instead of a bake. Nothing has to be prepared first. Native Parts keep a stock
    Roblox material; `approximated` in the result lists what Roblox can only come close to.
    Roblox keeps a mesh, and the pictures of surfaces and effects, only as assets on the user's
    Roblox account.
    The tool asks the user about that upload itself when a send needs it: do not ask it yourself
    first. If the result has `preview_only`, those meshes turn into checkered boxes in Play; if
    it has `effects_not_as_made`, the effect in Roblox is a stock stand-in, not the one made
    here: say either plainly.
    If several Roblox Studio windows are open the call tells you: ask the user which place,
    then pass it as `project`. The project is otherwise found automatically (the open editor
    first). Sending the same asset again updates it in place. By default the asset's pivot is
    put at its base centre. Relay the `next` line and any warnings to the user.""",
    {
        "engine": {"type": "string", "enum": list(ENGINES)},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Objects to send (children included). Omit to send the current selection (Unreal: the whole scene when nothing is selected)."},
        "name": {"type": "string", "description": "Asset name in the engine (default: the root object's name)."},
        "project": {"type": "string", "description": "Project folder, if it cannot be found automatically. For Roblox: the "
                                                     "name of the open place, when more than one Studio window is open. For "
                                                     "Unreal: the project folder, when more than one editor is linked."},
        "upload": {"type": "boolean", "description": "Roblox only. Leave it out: the tool asks the user when a send needs "
                                                     "meshes or an effect's pictures uploaded. Pass true only when the user "
                                                     "has just told you in the chat to upload to their Roblox account."},
        "at_origin": {"type": "boolean", "description": "Export with the pivot at the base centre (default true). False keeps scene placement."},
        "drag_tile": {"type": "boolean", "description": "Also show the drag tile so the user can drop the file into any window (always shown for Roblox)."},
    },
    ["engine"],
    main_thread=False,
    timeout=7200.0,                 # the export, then up to an hour of the editor building (unreal.LINK_SECONDS)
    label="Sent to the game engine",
)
def send_to_engine(engine: str, objects: Optional[List[str]] = None, name: str = "", project: str = "",
                   at_origin: bool = True, drag_tile: bool = False, upload: bool = False) -> Any:
    # Finding the engines asks Windows for the running editors (PowerShell, a second or more). That is done
    # here, on the worker thread; the main thread then reads the fresh list and does the export.
    find_targets()
    editors = unreal_link.find_editors() if engine == "unreal" else []     # a second of listening: here, not on the main thread
    return registry.run_on_main(lambda: _send_to_engine(engine, objects, name, project, at_origin, drag_tile, upload,
                                                        editors), 3600.0)


def _send_to_engine(engine: str, objects: Optional[List[str]], name: str, project: str, at_origin: bool,
                    drag_tile: bool, upload: bool, editors: Optional[List[Dict[str, Any]]] = None) -> Any:
    not_linked = ""
    if engine == "unreal":
        return _send_unreal(objects, name, project, editors or [], drag_tile)
    if engine == "roblox":
        try:
            sender = send_roblox(objects, name, project, at_origin, upload)
            return paced(sender)
        except ToolError as exc:
            if str(exc) != "no_link":
                raise
            not_linked = link_help()      # fall back to a file the user imports by hand
    out = send(engine, objects, name, project if engine != "roblox" else "", at_origin)
    if not_linked:
        out["live_link"] = not_linked
    if drag_tile or engine in FOLDERLESS:
        out["drag_tile"] = show_tile(out["file"])
    return out


def _send_unreal(objects: Optional[List[str]], name: str, project: str, editors: List[Dict[str, Any]],
                 drag_tile: bool) -> Any:
    """Build in a linked Unreal editor; without one, export the FBX as before and switch the link on
    in the project's files so that the next start of the editor is linked."""
    from . import unreal
    ensure_object_mode()
    root: Optional[str] = None
    if project:
        root = _project_root("unreal", project)
        if root is None:
            raise ToolError(f"'{project}' is not a Unreal project (it needs a .uproject file).")
        remember("unreal", root)
    elif not editors:
        match = next((t for t in find_targets() if t["engine"] == "unreal" and t["project"]), None)
        root = match["project"] if match else None
    view_layer = bpy.context.view_layer
    roots = [get_object(n) for n in objects] if objects else [o for o in view_layer.objects if o.select_get()]
    editor = unreal.pick_editor(editors, root)
    if editor is not None:
        family = None                           # nothing named or selected: the whole scene
        if roots:
            family = list(dict.fromkeys(o for obj in roots for o in [obj] + list(obj.children_recursive)))
        asset = _asset_name(family, name) if family else _safe_name(name or bpy.context.scene.name)
        staging = os.path.join(data_dir("exports", "unreal"), asset)
        sender = unreal.Sender(family, asset, editor, staging)
        return paced(sender)
    if not roots:                               # the file export takes a selection: the whole scene's top objects
        objects = [o.name for o in view_layer.objects if o.parent is None and o.visible_get()]
        name = name or bpy.context.scene.name   # named after the scene, not after whatever happened to be active
    help_text = unreal.link_help(root) if root else ""
    out = send("unreal", objects, name, project)
    out["live_link"] = help_text or unreal.link_help(out["project"])
    if drag_tile:
        out["drag_tile"] = show_tile(out["file"])
    return out


def show_tile(path: str) -> str:
    """Put the drag tile on screen for ``path``. Returns a short status for the reply."""
    from ..bridge import drop_tile
    if bpy.app.background:
        return "not shown (no desktop in background mode)"
    try:
        drop_tile.show([path], data_dir("bin"))
        return "shown at the bottom-right of the screen: drag it into the engine"
    except drop_tile.DropTileError as exc:
        return f"not available: {exc}"
