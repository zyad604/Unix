# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Larpbase
"""Online asset library: Poly Haven (CC0 HDRIs, PBR textures and models).

Real photographed lighting and scanned materials are the cheapest way to make a scene
look good, and they need no generation model. Network work (catalog, downloads) runs on
the calling worker thread; only the bpy part hops to the main thread. Downloads are
restricted to Poly Haven hosts, size-capped, md5-verified and cached on disk.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

import bpy

from . import _turn
from ._util import data_dir, get_object, ui_context
from .materials import set_material
from .registry import ToolError, registry, tool

API = "https://api.polyhaven.com"
ALLOWED_HOSTS = {"api.polyhaven.com", "dl.polyhaven.org", "cdn.polyhaven.com"}
USER_AGENT = "ChinaUnix/0.1 (Blender add-on)"
MAX_FILE_BYTES = 400 * 1024 * 1024
CATALOG_TTL_S = 7 * 24 * 3600
_KINDS = {"hdri": "hdris", "texture": "textures", "model": "models"}
settings = {"enabled": True}


def _require_enabled() -> None:
    if not settings["enabled"]:
        raise ToolError("Online assets are turned off in the add-on preferences. Build it procedurally instead.")


def _open(url: str, timeout: float = 30.0):
    host = urllib.parse.urlparse(url).hostname or ""
    if host not in ALLOWED_HOSTS:
        raise ToolError(f"Download host not allowed: {host}")
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=timeout)


def _get_json(url: str) -> Any:
    try:
        with _open(url) as response:
            return json.loads(response.read().decode("utf-8"))
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Could not reach Poly Haven ({type(exc).__name__}: {exc}). Check the internet connection.")


def _catalog(kind: str) -> Dict[str, Any]:
    path = os.path.join(data_dir("assets"), f"catalog_{kind}.json")
    if os.path.isfile(path) and time.time() - os.path.getmtime(path) < CATALOG_TTL_S:
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    data = _get_json(f"{API}/assets?t={_KINDS[kind]}")
    slim = {key: {"name": a.get("name", key), "categories": a.get("categories", []), "tags": a.get("tags", []),
                  "downloads": a.get("download_count", 0), "dimensions": a.get("dimensions")}
            for key, a in data.items()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(slim, fh)
    return slim


def _download(entry: Dict[str, Any], dest: str) -> str:
    """Fetch one file described by a Poly Haven {url, size, md5} entry into ``dest`` (cached)."""
    size = int(entry.get("size") or 0)
    if size > MAX_FILE_BYTES:
        raise ToolError(f"File is {size / 1e6:.0f} MB, over the {MAX_FILE_BYTES // 2**20} MB limit. Pick a lower resolution.")
    if os.path.isfile(dest) and (not size or os.path.getsize(dest) == size):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    partial = dest + ".part"
    digest = hashlib.md5()  # noqa: S324 - integrity check against the catalog, not security
    try:
        with _open(entry["url"], timeout=60.0) as response, open(partial, "wb") as out:
            total = 0
            while True:
                chunk = response.read(1 << 16)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise ToolError("Download exceeded the size limit.")
                digest.update(chunk)
                out.write(chunk)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"Download failed ({type(exc).__name__}: {exc}).")
    if entry.get("md5") and digest.hexdigest() != entry["md5"]:
        os.remove(partial)
        raise ToolError("Downloaded file failed its integrity check; try again.")
    os.replace(partial, dest)
    return dest


def _pick_resolution(options: Dict[str, Any], wanted: str) -> str:
    """The wanted resolution, else the nearest higher one, else the nearest lower."""
    order = ["1k", "2k", "4k", "8k"]
    start = order.index(wanted) if wanted in order else 1
    for res in order[start:] + order[:start][::-1]:
        if res in options:
            return res
    raise ToolError(f"No downloadable resolution found (available: {', '.join(options) or 'none'}).")


def _files(asset_id: str, kind: str) -> Dict[str, Any]:
    if asset_id not in _catalog(kind):
        raise ToolError(f"No {kind} with id '{asset_id}'. Use asset_search to find ids.")
    return _get_json(f"{API}/files/{urllib.parse.quote(asset_id)}")


@tool(
    "asset_search",
    """Search the free Poly Haven library (CC0, photographed or scanned): 'hdri' for lighting
    environments, 'texture' for PBR surface materials (wood, metal, fabric, ground, walls...),
    'model' for ready-made props and furniture. Returns asset ids to pass to asset_hdri,
    asset_material or asset_model. Prefer a real asset over a flat colour or bare lights
    whenever the user wants something realistic.""",
    {
        "kind": {"type": "string", "enum": ["hdri", "texture", "model"]},
        "query": {"type": "string", "description": "Keywords, e.g. 'oak wood planks', 'studio softbox', 'overcast field', 'armchair'."},
        "limit": {"type": "integer", "description": "Max results (default 10)."},
    },
    ["kind", "query"],
    main_thread=False,
    timeout=90.0,
    label="Searched the asset library",
)
def asset_search(kind: str, query: str, limit: int = 10) -> Dict[str, Any]:
    _require_enabled()
    words = [w for w in query.lower().replace(",", " ").split() if w]
    scored = []
    for asset_id, asset in _catalog(kind).items():
        name = (asset_id + " " + asset["name"]).lower().replace("_", " ")
        tags = " ".join(asset["tags"]).lower()
        cats = " ".join(asset["categories"]).lower()
        score = sum(3 * (w in name) + 2 * (w in tags) + 1 * (w in cats) for w in words)
        if score and all(w in name or w in tags or w in cats for w in words[:3]):
            scored.append((score, asset.get("downloads", 0), asset_id, asset))
    if not scored:  # fall back to any-word match
        for asset_id, asset in _catalog(kind).items():
            hay = (asset_id + " " + asset["name"] + " " + " ".join(asset["tags"] + asset["categories"])).lower()
            score = sum(w in hay for w in words)
            if score:
                scored.append((score, asset.get("downloads", 0), asset_id, asset))
    scored.sort(key=lambda row: (-row[0], -row[1]))
    rows = [{"id": a_id, "name": a["name"], "categories": a["categories"][:5], "tags": a["tags"][:8]}
            for _score, _dl, a_id, a in scored[:max(1, min(int(limit), 25))]]
    return {"kind": kind, "results": rows, "total_matches": len(scored),
            "note": "No matches; try broader or different keywords." if not rows else "CC0 assets from polyhaven.com"}


@tool(
    "asset_hdri",
    """Light the scene with a Poly Haven HDRI: downloads it and builds the world node tree
    (environment texture, mapping for rotation, background strength). Gives realistic light
    and reflections in one call. Follow with look(shading='rendered') to judge it.""",
    {
        "id": {"type": "string", "description": "HDRI id from asset_search."},
        "resolution": {"type": "string", "enum": ["1k", "2k", "4k"], "description": "Default 2k. 1k is enough for lighting only."},
        "strength": {"type": "number", "description": "Background strength (default 1.0)."},
        "rotation_deg": {"type": "number", "description": "Rotate the environment around Z to move the key light."},
    },
    ["id"],
    main_thread=False,
    timeout=600.0,
    mutates=True,
    label="Lit the scene with an HDRI",
)
def asset_hdri(id: str, resolution: str = "2k", strength: float = 1.0, rotation_deg: float = 0.0) -> Dict[str, Any]:
    _require_enabled()
    options = _files(id, "hdri").get("hdri", {})
    res = _pick_resolution(options, resolution)
    path = _download(options[res]["hdr"], os.path.join(data_dir("assets", "hdri"), f"{id}_{res}.hdr"))

    def apply() -> Dict[str, Any]:
        _turn.mark_mutation()
        scene = bpy.context.scene
        world = scene.world or bpy.data.worlds.new("World")
        scene.world = world
        world.use_nodes = True
        nodes, links = world.node_tree.nodes, world.node_tree.links
        nodes.clear()
        coord = nodes.new("ShaderNodeTexCoord")
        mapping = nodes.new("ShaderNodeMapping")
        env = nodes.new("ShaderNodeTexEnvironment")
        background = nodes.new("ShaderNodeBackground")
        output = nodes.new("ShaderNodeOutputWorld")
        for index, node in enumerate((coord, mapping, env, background, output)):
            node.location = (-800 + 220 * index, 300)
        env.image = bpy.data.images.load(path, check_existing=True)
        mapping.inputs["Rotation"].default_value[2] = math.radians(rotation_deg)
        background.inputs["Strength"].default_value = float(strength)
        links.new(coord.outputs["Generated"], mapping.inputs["Vector"])
        links.new(mapping.outputs["Vector"], env.inputs["Vector"])
        links.new(env.outputs["Color"], background.inputs["Color"])
        links.new(background.outputs["Background"], output.inputs["Surface"])
        return {"world": world.name, "hdri": id, "resolution": res, "strength": strength, "file": path}

    return registry.run_on_main(apply, 120.0)


_TEXTURE_MAPS = {"base_color": ("Diffuse", "diff"), "normal": ("nor_gl",), "roughness": ("Rough", "rough"),
                 "metallic": ("Metal", "metal")}


@tool(
    "asset_material",
    """Create a photoreal PBR material from a Poly Haven texture set (base colour, normal,
    roughness, metallic where available) and assign it to objects. The objects need UVs
    (check_uv); `scale` tiles the texture (2.0 = twice as many repeats). One texture tile
    covers roughly 1-2 m in real life, so set scale from the object's size.""",
    {
        "id": {"type": "string", "description": "Texture id from asset_search."},
        "objects": {"type": "array", "items": {"type": "string"}, "description": "Objects to assign it to."},
        "resolution": {"type": "string", "enum": ["1k", "2k", "4k"], "description": "Default 2k."},
        "scale": {"type": "number", "description": "UV tiling multiplier (default 1.0)."},
    },
    ["id"],
    main_thread=False,
    timeout=900.0,
    mutates=True,
    label="Applied a scanned material",
)
def asset_material(id: str, objects: Optional[List[str]] = None, resolution: str = "2k", scale: float = 1.0) -> Dict[str, Any]:
    _require_enabled()
    files = _files(id, "texture")
    folder = data_dir("assets", "textures", id)
    textures: Dict[str, str] = {}
    used_res = resolution
    for slot, keys in _TEXTURE_MAPS.items():
        entry = next((files[k] for k in keys if k in files), None)
        if entry is None:
            continue
        used_res = _pick_resolution(entry, resolution)
        fmt = entry[used_res].get("jpg") or entry[used_res].get("png")
        if fmt is None:
            continue
        ext = ".jpg" if "jpg" in entry[used_res] else ".png"
        textures[slot] = _download(fmt, os.path.join(folder, f"{id}_{slot}_{used_res}{ext}"))
    if "base_color" not in textures:
        raise ToolError(f"Texture '{id}' has no base colour map to download.")

    def apply() -> Dict[str, Any]:
        targets = [get_object(n) for n in (objects or [])]
        out = set_material(name=f"PH_{id}", objects=[o.name for o in targets], textures=textures)
        mat = bpy.data.materials[out["material"]]
        nodes, links = mat.node_tree.nodes, mat.node_tree.links
        coord = nodes.get("unix_texcoord") or nodes.new("ShaderNodeTexCoord")
        mapping = nodes.get("unix_mapping") or nodes.new("ShaderNodeMapping")
        coord.name, mapping.name = "unix_texcoord", "unix_mapping"
        coord.location, mapping.location = (-1260, 0), (-1040, 0)
        mapping.inputs["Scale"].default_value = (scale, scale, scale)
        links.new(coord.outputs["UV"], mapping.inputs["Vector"])
        for node in nodes:
            if node.type == 'TEX_IMAGE' and node.name.startswith("unix_"):
                links.new(mapping.outputs["Vector"], node.inputs["Vector"])
        no_uv = [o.name for o in targets if o.type == 'MESH' and not o.data.uv_layers]
        out.update(resolution=used_res, maps=sorted(textures), tiling=scale)
        if no_uv:
            out["warning"] = f"No UV layer on: {', '.join(no_uv)}. Unwrap them or the texture will not show correctly."
        return out

    return registry.run_on_main(apply, 120.0)


@tool(
    "asset_model",
    """Download a ready-made Poly Haven model (furniture, props, plants, rocks...) and import it
    with its textures. Returns the new objects and their real-world dimensions; then position,
    rotate or scale them with run_python. Faster and better looking than modelling a common prop.""",
    {
        "id": {"type": "string", "description": "Model id from asset_search."},
        "resolution": {"type": "string", "enum": ["1k", "2k", "4k"], "description": "Texture resolution (default 1k)."},
    },
    ["id"],
    main_thread=False,
    timeout=1200.0,
    mutates=True,
    label="Imported a library model",
)
def asset_model(id: str, resolution: str = "1k") -> Dict[str, Any]:
    _require_enabled()
    gltf = _files(id, "model").get("gltf", {})
    res = _pick_resolution(gltf, resolution)
    main = gltf[res]["gltf"]
    folder = data_dir("assets", "models", f"{id}_{res}")
    path = _download(main, os.path.join(folder, os.path.basename(urllib.parse.urlparse(main["url"]).path)))
    for rel, entry in (main.get("include") or {}).items():
        safe = os.path.normpath(os.path.join(folder, rel))
        if not safe.startswith(os.path.normpath(folder)):
            raise ToolError("Asset contains an unsafe file path.")
        _download(entry, safe)

    def apply() -> Dict[str, Any]:
        _turn.mark_mutation()
        before = set(bpy.data.objects.keys())
        with ui_context():
            bpy.ops.import_scene.gltf(filepath=path)
        new = [bpy.data.objects[n] for n in bpy.data.objects.keys() if n not in before]
        return {"model": id, "resolution": res,
                "imported": [{"name": o.name, "type": o.type, "dimensions": [round(d, 3) for d in o.dimensions],
                              "location": [round(v, 3) for v in o.location]} for o in new][:40],
                "note": "Imported at the world origin at real-world scale. Move it into place and check with look."}

    return registry.run_on_main(apply, 300.0)
